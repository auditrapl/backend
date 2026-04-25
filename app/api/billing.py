import logging
import os
import uuid
import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import stripe
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.auth import verify_token
from app.database import get_db
from app.models import (
    BillingInvoice,
    BillingPayment,
    BillingPlan,
    BillingSubscription,
    Company,
    StripeWebhookEventLog,
    User,
)
from app.schemas import (
    BillingCheckoutRequest,
    BillingCheckoutResponse,
    BillingInvoiceItem,
    BillingPlanItemResponse,
    BillingPlansResponse,
    BillingPlanSyncResponse,
    BillingPortalRequest,
    BillingPortalResponse,
    BillingStatusResponse,
    PublicCheckoutRequest,
    PublicCheckoutResponse,
)
from app.services.stripe_billing import (
    StripeServiceError,
    apply_subscription_to_company,
    configure_stripe,
    construct_webhook_event,
    create_checkout_session,
    create_portal_session,
    default_checkout_urls,
    default_price_id,
    ensure_stripe_customer,
    from_unix,
    list_customer_invoices,
    parse_invoice_summary,
    retrieve_subscription,
    retrieve_upcoming_invoice,
    sync_stripe_products_and_prices,
    stripe_is_configured,
    to_decimal_from_cents,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["billing"])


def ensure_billing_ready() -> None:
    if not stripe_is_configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Stripe billing is not configured",
        )


def raise_stripe_http_error(exc: StripeServiceError) -> None:
    raise HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail=str(exc),
    )


def ensure_company_or_400(current_user: User, company: Company | None) -> Company:
    if not current_user.company_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User is not assigned to a company",
        )
    if not company:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Company not found",
        )
    return company


async def get_user_company(db: AsyncSession, current_user: User) -> Company:
    result = await db.execute(select(Company).where(Company.id == current_user.company_id))
    company = result.scalar_one_or_none()
    return ensure_company_or_400(current_user, company)


def extract_subscription_price_id(subscription: Any) -> str | None:
    items = subscription.get("items", {}) if hasattr(subscription, "get") else {}
    item_data = items.get("data", []) if hasattr(items, "get") else []
    if not item_data:
        return None
    first_item = item_data[0]
    price = first_item.get("price", {}) if hasattr(first_item, "get") else {}
    return price.get("id") if hasattr(price, "get") else None


async def upsert_billing_subscription(
    db: AsyncSession,
    *,
    company: Company,
    user: User | None,
    subscription: Any,
) -> BillingSubscription | None:
    stripe_subscription_id = subscription.get("id") if hasattr(subscription, "get") else None
    if not stripe_subscription_id:
        return None

    existing = await db.execute(
        select(BillingSubscription).where(BillingSubscription.stripe_subscription_id == stripe_subscription_id)
    )
    subscription_record = existing.scalar_one_or_none()
    if not subscription_record:
        subscription_record = BillingSubscription(
            company_id=company.id,
            user_id=user.id if user else None,
            stripe_subscription_id=stripe_subscription_id,
            stripe_customer_id=(subscription.get("customer") or company.stripe_customer_id or ""),
        )

    price_id = extract_subscription_price_id(subscription)
    plan_id = None
    if price_id:
        plan_result = await db.execute(select(BillingPlan).where(BillingPlan.stripe_price_id == price_id))
        plan = plan_result.scalar_one_or_none()
        plan_id = plan.id if plan else None

    subscription_record.company_id = company.id
    subscription_record.user_id = user.id if user else subscription_record.user_id
    subscription_record.plan_id = plan_id
    subscription_record.stripe_customer_id = subscription.get("customer") or subscription_record.stripe_customer_id
    subscription_record.status = subscription.get("status") or subscription_record.status
    subscription_record.current_period_start = from_unix(subscription.get("current_period_start"))
    subscription_record.current_period_end = from_unix(subscription.get("current_period_end"))
    subscription_record.cancel_at_period_end = bool(subscription.get("cancel_at_period_end"))
    subscription_record.canceled_at = from_unix(subscription.get("canceled_at"))
    subscription_record.trial_end = from_unix(subscription.get("trial_end"))

    db.add(subscription_record)
    return subscription_record


async def upsert_billing_invoice_and_payment(
    db: AsyncSession,
    *,
    company: Company,
    user: User | None,
    invoice_payload: Any,
) -> BillingInvoice | None:
    stripe_invoice_id = invoice_payload.get("id") if hasattr(invoice_payload, "get") else None
    if not stripe_invoice_id:
        return None

    existing_invoice = await db.execute(
        select(BillingInvoice).where(BillingInvoice.stripe_invoice_id == stripe_invoice_id)
    )
    invoice_record = existing_invoice.scalar_one_or_none()
    if not invoice_record:
        invoice_record = BillingInvoice(
            company_id=company.id,
            user_id=user.id if user else None,
            stripe_invoice_id=stripe_invoice_id,
        )

    subscription_ref = invoice_payload.get("subscription")
    subscription_id = None
    if subscription_ref:
        subscription_result = await db.execute(
            select(BillingSubscription).where(BillingSubscription.stripe_subscription_id == subscription_ref)
        )
        subscription_record = subscription_result.scalar_one_or_none()
        subscription_id = subscription_record.id if subscription_record else None

    status_transitions = invoice_payload.get("status_transitions", {}) if hasattr(invoice_payload, "get") else {}

    invoice_record.company_id = company.id
    invoice_record.user_id = user.id if user else invoice_record.user_id
    invoice_record.subscription_id = subscription_id
    invoice_record.number = invoice_payload.get("number")
    invoice_record.status = invoice_payload.get("status") or invoice_record.status
    invoice_record.amount_due = to_decimal_from_cents(invoice_payload.get("amount_due"))
    invoice_record.amount_paid = to_decimal_from_cents(invoice_payload.get("amount_paid"))
    invoice_record.currency = (invoice_payload.get("currency") or "USD").upper()
    invoice_record.invoice_pdf_url = invoice_payload.get("invoice_pdf")
    invoice_record.hosted_invoice_url = invoice_payload.get("hosted_invoice_url")
    invoice_record.period_start = from_unix(invoice_payload.get("period_start"))
    invoice_record.period_end = from_unix(invoice_payload.get("period_end"))
    invoice_record.due_date = from_unix(invoice_payload.get("due_date"))
    invoice_record.paid_at = from_unix(status_transitions.get("paid_at"))

    db.add(invoice_record)

    payment_intent_id = invoice_payload.get("payment_intent")
    payment_record = None
    if payment_intent_id:
        payment_result = await db.execute(
            select(BillingPayment).where(BillingPayment.stripe_payment_intent_id == payment_intent_id)
        )
        payment_record = payment_result.scalar_one_or_none()
    else:
        payment_result = await db.execute(
            select(BillingPayment).where(BillingPayment.stripe_invoice_id == stripe_invoice_id)
        )
        payment_record = payment_result.scalar_one_or_none()

    if not payment_record:
        payment_record = BillingPayment(
            company_id=company.id,
            user_id=user.id if user else None,
            subscription_id=subscription_id,
            amount=Decimal("0.00"),
        )

    amount_paid = to_decimal_from_cents(invoice_payload.get("amount_paid"))
    amount_due = to_decimal_from_cents(invoice_payload.get("amount_due"))

    payment_record.company_id = company.id
    payment_record.user_id = user.id if user else payment_record.user_id
    payment_record.subscription_id = subscription_id
    payment_record.stripe_invoice_id = stripe_invoice_id
    payment_record.stripe_payment_intent_id = payment_intent_id
    payment_record.amount = amount_paid or amount_due or Decimal("0.00")
    payment_record.currency = (invoice_payload.get("currency") or "USD").upper()
    payment_record.status = invoice_payload.get("status") or payment_record.status
    payment_record.paid_at = from_unix(status_transitions.get("paid_at"))
    payment_record.failure_message = (
        invoice_payload.get("last_finalization_error", {}) or {}
    ).get("message")

    db.add(payment_record)
    return invoice_record


async def find_company_user_by_customer_id(db: AsyncSession, company: Company, stripe_customer_id: str | None) -> User | None:
    if not stripe_customer_id:
        return None
    user_result = await db.execute(
        select(User).where(
            User.company_id == company.id,
            User.stripe_customer_id == stripe_customer_id,
        )
    )
    return user_result.scalar_one_or_none()


@router.post(
    "/checkout-session",
    response_model=BillingCheckoutResponse,
    dependencies=[Depends(verify_token)],
)
async def create_billing_checkout_session(
    payload: BillingCheckoutRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    ensure_billing_ready()

    company = await get_user_company(db, current_user)

    chosen_price_id = payload.price_id or default_price_id()
    if not chosen_price_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing Stripe price id",
        )

    plan_result = await db.execute(
        select(BillingPlan).where(
            BillingPlan.stripe_price_id == chosen_price_id,
            BillingPlan.is_active.is_(True),
        )
    )
    chosen_plan = plan_result.scalar_one_or_none()
    if not chosen_plan:
        try:
            await sync_stripe_products_and_prices(db)
        except StripeServiceError as exc:
            raise_stripe_http_error(exc)
        plan_result = await db.execute(
            select(BillingPlan).where(
                BillingPlan.stripe_price_id == chosen_price_id,
                BillingPlan.is_active.is_(True),
            )
        )
        chosen_plan = plan_result.scalar_one_or_none()
    if not chosen_plan:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Selected Stripe price is not available",
        )

    default_success, default_cancel = default_checkout_urls()
    success_url = payload.success_url or default_success
    cancel_url = payload.cancel_url or default_cancel

    try:
        customer_id = ensure_stripe_customer(company, current_user)
    except StripeServiceError as exc:
        raise_stripe_http_error(exc)
    if not company.stripe_customer_id:
        company.stripe_customer_id = customer_id
    current_user.stripe_customer_id = customer_id
    db.add(current_user)

    session = create_checkout_session(
        company=company,
        user=current_user,
        customer_id=customer_id,
        price_id=chosen_price_id,
        success_url=success_url,
        cancel_url=cancel_url,
    )

    company.billing_status = "checkout_pending"
    company.stripe_price_id = chosen_price_id
    company.billing_last_event_at = datetime.now(UTC)
    db.add(company)
    await db.commit()

    return BillingCheckoutResponse(session_id=session.id, checkout_url=session.url)


@router.post(
    "/portal-session",
    response_model=BillingPortalResponse,
    dependencies=[Depends(verify_token)],
)
async def create_billing_portal(
    payload: BillingPortalRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    ensure_billing_ready()

    company = await get_user_company(db, current_user)
    if not company.stripe_customer_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Stripe customer not found. Start subscription first.",
        )

    return_url = payload.return_url or default_checkout_urls()[0]
    try:
        portal = create_portal_session(company.stripe_customer_id, return_url)
    except StripeServiceError as exc:
        raise_stripe_http_error(exc)
    return BillingPortalResponse(portal_url=portal.url)


@router.get(
    "/status",
    response_model=BillingStatusResponse,
    dependencies=[Depends(verify_token)],
)
async def get_billing_status(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    company = await get_user_company(db, current_user)

    if not stripe_is_configured():
        return BillingStatusResponse(
            stripe_customer_id=company.stripe_customer_id,
            stripe_subscription_id=company.stripe_subscription_id,
            price_id=company.stripe_price_id,
            status=company.billing_status,
            currency=(company.billing_currency or "pln").upper() if company.billing_currency else None,
            interval=company.billing_interval,
            amount=company.billing_amount,
            cancel_at_period_end=company.billing_cancel_at_period_end,
            current_period_end=company.billing_current_period_end,
            invoices=[],
        )

    invoices: list[BillingInvoiceItem] = []
    next_charge_amount = None
    next_charge_date = None

    if company.stripe_customer_id:
        try:
            stripe_invoices = list_customer_invoices(company.stripe_customer_id, limit=6)
        except StripeServiceError as exc:
            raise_stripe_http_error(exc)
        invoices = [BillingInvoiceItem(**parse_invoice_summary(inv)) for inv in stripe_invoices.data]

    if company.stripe_subscription_id:
        try:
            subscription = retrieve_subscription(company.stripe_subscription_id)
        except StripeServiceError as exc:
            raise_stripe_http_error(exc)
        apply_subscription_to_company(company, subscription)
        await upsert_billing_subscription(db, company=company, user=current_user, subscription=subscription)
        current_user.stripe_subscription_id = company.stripe_subscription_id
        db.add(current_user)

        try:
            upcoming = retrieve_upcoming_invoice(company.stripe_customer_id)
            next_charge_amount = to_decimal_from_cents(upcoming.get("amount_due"))
            next_charge_date = from_unix(upcoming.get("next_payment_attempt"))
        except StripeServiceError:
            # No upcoming invoice exists in some statuses, e.g. canceled trials.
            next_charge_amount = None
            next_charge_date = None

        db.add(company)
        await db.commit()

    return BillingStatusResponse(
        stripe_customer_id=company.stripe_customer_id,
        stripe_subscription_id=company.stripe_subscription_id,
        price_id=company.stripe_price_id,
        status=company.billing_status,
        currency=(company.billing_currency or "pln").upper() if company.billing_currency else None,
        interval=company.billing_interval,
        amount=company.billing_amount,
        cancel_at_period_end=company.billing_cancel_at_period_end,
        current_period_end=company.billing_current_period_end,
        next_charge_amount=next_charge_amount,
        next_charge_date=next_charge_date,
        invoices=invoices,
    )


@router.get(
    "/plans",
    response_model=BillingPlansResponse,
    dependencies=[Depends(verify_token)],
)
async def get_billing_plans(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await get_user_company(db, current_user)
    ensure_billing_ready()

    result = await db.execute(select(BillingPlan).where(BillingPlan.is_active.is_(True)).order_by(BillingPlan.amount.asc()))
    plans = result.scalars().all()

    if not plans:
        try:
            await sync_stripe_products_and_prices(db)
        except StripeServiceError as exc:
            raise_stripe_http_error(exc)
        result = await db.execute(select(BillingPlan).where(BillingPlan.is_active.is_(True)).order_by(BillingPlan.amount.asc()))
        plans = result.scalars().all()

    response_items = [
        BillingPlanItemResponse(
            stripe_product_id=plan.stripe_product_id,
            stripe_price_id=plan.stripe_price_id,
            name=plan.name,
            description=plan.description,
            currency=(plan.currency or "USD").upper(),
            interval=plan.interval,
            interval_count=plan.interval_count,
            amount=plan.amount,
            is_active=plan.is_active,
        )
        for plan in plans
    ]

    return BillingPlansResponse(plans=response_items)


@router.post(
    "/sync-plans",
    response_model=BillingPlanSyncResponse,
    dependencies=[Depends(verify_token)],
)
async def sync_billing_plans(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # Enforce company membership to keep this endpoint scoped to authenticated tenants.
    await get_user_company(db, current_user)
    ensure_billing_ready()

    try:
        synced_count = await sync_stripe_products_and_prices(db)
    except StripeServiceError as exc:
        raise_stripe_http_error(exc)

    return BillingPlanSyncResponse(synced_count=synced_count)


@router.post("/public-checkout", response_model=PublicCheckoutResponse)
async def public_checkout(
    payload: PublicCheckoutRequest,
    db: AsyncSession = Depends(get_db),
):
    """Public checkout — no auth required. Creates Stripe Checkout with email."""
    ensure_billing_ready()

    chosen_price_id = payload.price_id or default_price_id()
    if not chosen_price_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing price_id",
        )

    # Anti-duplicate-subscription: jesli user z tym emailem juz ma aktywne konto
    # z aktywna subskrypcja, nie pozwalamy na drugie checkout.
    # Zamiast tego dajemy im sygnal zeby sie zalogowali lub uzyli 'forgot password'.
    existing_user_result = await db.execute(select(User).where(User.email == payload.email))
    existing_user = existing_user_result.scalar_one_or_none()
    if existing_user and existing_user.company_id:
        company_result = await db.execute(select(Company).where(Company.id == existing_user.company_id))
        existing_company = company_result.scalar_one_or_none()
        if existing_company and (existing_company.billing_status or "").lower() in {"active", "trialing"}:
            frontend_url_redirect = os.getenv("FRONTEND_APP_URL", "http://localhost:3000")
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "message": "Konto z tym emailem już istnieje i ma aktywną subskrypcję.",
                    "reason": "account_exists",
                    "login_url": f"{frontend_url_redirect}/login",
                    "reset_url": f"{frontend_url_redirect}/login?forgot=true",
                },
            )

    frontend = os.getenv("FRONTEND_APP_URL", "http://localhost:3000")
    success_url = f"{frontend}/checkout/success?session_id={{CHECKOUT_SESSION_ID}}"
    cancel_url = f"{frontend}/checkout?canceled=true"

    try:
        configure_stripe()
        from app.services.stripe_billing import trial_days_from_env

        subscription_data: dict = {"metadata": {"source": "public_checkout", "email": payload.email}}
        trial_days = trial_days_from_env()
        if trial_days:
            subscription_data["trial_period_days"] = trial_days

        session = stripe.checkout.Session.create(
            customer_email=payload.email,
            mode="subscription",
            line_items=[{"price": chosen_price_id, "quantity": 1}],
            success_url=success_url,
            cancel_url=cancel_url,
            metadata={"source": "public_checkout", "email": payload.email},
            allow_promotion_codes=True,
            subscription_data=subscription_data,
        )
    except stripe.error.StripeError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        )

    return PublicCheckoutResponse(checkout_url=session.url)


@router.post("/webhook")
async def stripe_webhook(request: Request, db: AsyncSession = Depends(get_db)):
    stripe_available = stripe_is_configured()

    payload = await request.body()
    signature = request.headers.get("Stripe-Signature")
    secret = os.getenv("STRIPE_WEBHOOK_SECRET")

    if not signature or not secret:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing Stripe webhook signature or secret",
        )

    try:
        construct_webhook_event(payload=payload, signature=signature, secret=secret)
    except StripeServiceError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid webhook signature")

    event_payload = json.loads(payload.decode("utf-8"))
    event_id = event_payload.get("id")
    event_type = event_payload.get("type")
    data_object = event_payload.get("data", {}).get("object", {})
    customer_id = data_object.get("customer") if isinstance(data_object, dict) else None

    if not event_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing Stripe event id")

    existing_log_result = await db.execute(
        select(StripeWebhookEventLog).where(StripeWebhookEventLog.event_id == event_id)
    )
    existing_log = existing_log_result.scalar_one_or_none()
    if existing_log:
        logger.info("Stripe webhook duplicate ignored: id=%s type=%s", event_id, event_type)
        return {"received": True, "event_type": event_type, "duplicate": True}

    webhook_log = StripeWebhookEventLog(
        event_id=event_id,
        event_type=event_type or "unknown",
        customer_id=customer_id,
        status="received",
        payload={
            "id": event_id,
            "type": event_type,
            "created": event_payload.get("created"),
            "livemode": event_payload.get("livemode"),
        },
    )
    db.add(webhook_log)

    supported_events = {
        "checkout.session.completed",
        "customer.subscription.created",
        "customer.subscription.updated",
        "customer.subscription.deleted",
        "invoice.paid",
        "invoice.payment_failed",
    }

    if event_type not in supported_events:
        webhook_log.status = "ignored"
        webhook_log.processed_at = datetime.now(UTC)
        await db.commit()
        logger.info("Stripe webhook ignored (unsupported event): %s", event_type)
        return {"received": True, "event_type": event_type, "ignored": True}

    company: Company | None = None
    event_user: User | None = None

    if customer_id:
        result = await db.execute(select(Company).where(Company.stripe_customer_id == customer_id))
        company = result.scalar_one_or_none()
        if company:
            webhook_log.company_id = company.id

    if event_type == "checkout.session.completed":
        customer_id = data_object.get("customer") if isinstance(data_object, dict) else None
        subscription_id = data_object.get("subscription") if isinstance(data_object, dict) else None
        metadata = data_object.get("metadata", {}) if isinstance(data_object, dict) else {}
        metadata_user_id = metadata.get("user_id") if isinstance(metadata, dict) else None

        if metadata_user_id:
            try:
                parsed_user_id = uuid.UUID(metadata_user_id)
                user_result = await db.execute(select(User).where(User.id == parsed_user_id))
                event_user = user_result.scalar_one_or_none()
            except ValueError:
                logger.warning("Invalid user_id in Stripe metadata: %s", metadata_user_id)

        if customer_id and not company:
            metadata_company_id = metadata.get("company_id") if isinstance(metadata, dict) else None
            if metadata_company_id:
                try:
                    parsed_company_id = uuid.UUID(metadata_company_id)
                    result = await db.execute(select(Company).where(Company.id == parsed_company_id))
                    company = result.scalar_one_or_none()
                    if company:
                        company.stripe_customer_id = customer_id
                        webhook_log.company_id = company.id
                except ValueError:
                    logger.warning("Invalid company_id in Stripe metadata: %s", metadata_company_id)

        if company and subscription_id and stripe_available:
            try:
                subscription = retrieve_subscription(subscription_id)
            except StripeServiceError as exc:
                raise_stripe_http_error(exc)
            apply_subscription_to_company(company, subscription)
            if not event_user:
                event_user = await find_company_user_by_customer_id(db, company, customer_id)
            await upsert_billing_subscription(db, company=company, user=event_user, subscription=subscription)
            if event_user:
                event_user.stripe_customer_id = customer_id
                event_user.stripe_subscription_id = subscription_id
                db.add(event_user)
        elif company and subscription_id and not stripe_available:
            company.stripe_subscription_id = subscription_id
            company.billing_last_event_at = datetime.now(UTC)
            if not event_user:
                event_user = await find_company_user_by_customer_id(db, company, customer_id)
            if event_user:
                event_user.stripe_customer_id = customer_id
                event_user.stripe_subscription_id = subscription_id
                db.add(event_user)

        # If no company found (public checkout), auto-provision
        if not company and customer_id:
            checkout_email = data_object.get("customer_email") or data_object.get("customer_details", {}).get("email")
            subscription_id_for_provision = data_object.get("subscription")
            checkout_metadata = data_object.get("metadata", {})

            if checkout_email and isinstance(checkout_metadata, dict) and checkout_metadata.get("source") == "public_checkout":
                from app.services.account_provisioning import provision_account_after_payment

                try:
                    event_user, company = await provision_account_after_payment(
                        db=db,
                        email=checkout_email,
                        stripe_customer_id=customer_id,
                        stripe_subscription_id=subscription_id_for_provision,
                    )
                    webhook_log.company_id = company.id
                    logger.info("[Webhook] Auto-provisioned account for %s", checkout_email)
                except Exception as e:
                    logger.error("[Webhook] Auto-provision failed for %s: %s", checkout_email, e)

    elif event_type in {"customer.subscription.created", "customer.subscription.updated", "customer.subscription.deleted"}:
        if company:
            event_user = await find_company_user_by_customer_id(db, company, customer_id)
            apply_subscription_to_company(company, data_object)
            await upsert_billing_subscription(db, company=company, user=event_user, subscription=data_object)
            if event_user and company.stripe_subscription_id:
                event_user.stripe_subscription_id = company.stripe_subscription_id
                db.add(event_user)

    elif event_type == "invoice.paid":
        if company:
            company.billing_status = "active"
            company.billing_last_event_at = datetime.now(UTC)
            event_user = await find_company_user_by_customer_id(db, company, customer_id)
            await upsert_billing_invoice_and_payment(db, company=company, user=event_user, invoice_payload=data_object)

    elif event_type == "invoice.payment_failed":
        if company:
            company.billing_status = "past_due"
            company.billing_last_event_at = datetime.now(UTC)
            event_user = await find_company_user_by_customer_id(db, company, customer_id)
            await upsert_billing_invoice_and_payment(db, company=company, user=event_user, invoice_payload=data_object)

    webhook_log.status = "processed"
    webhook_log.processed_at = datetime.now(UTC)

    if company:
        db.add(company)
    db.add(webhook_log)
    await db.commit()

    logger.info(
        "Stripe webhook handled: id=%s type=%s company_id=%s",
        event_id,
        event_type,
        str(company.id) if company else None,
    )
    return {"received": True, "event_id": event_id, "event_type": event_type}
