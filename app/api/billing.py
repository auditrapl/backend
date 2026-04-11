import logging
import os
import uuid
import json
from datetime import UTC, datetime

import stripe
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.auth import verify_token
from app.database import get_db
from app.models import Company, User
from app.schemas import (
    BillingCheckoutRequest,
    BillingCheckoutResponse,
    BillingInvoiceItem,
    BillingPortalRequest,
    BillingPortalResponse,
    BillingStatusResponse,
)
from app.services.stripe_billing import (
    apply_subscription_to_company,
    configure_stripe,
    create_checkout_session,
    create_portal_session,
    default_checkout_urls,
    default_price_id,
    ensure_stripe_customer,
    from_unix,
    parse_invoice_summary,
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
    configure_stripe()

    company = await get_user_company(db, current_user)

    chosen_price_id = payload.price_id or default_price_id()
    if not chosen_price_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing Stripe price id",
        )

    default_success, default_cancel = default_checkout_urls()
    success_url = payload.success_url or default_success
    cancel_url = payload.cancel_url or default_cancel

    customer_id = ensure_stripe_customer(company, current_user)
    if not company.stripe_customer_id:
        company.stripe_customer_id = customer_id

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
    configure_stripe()

    company = await get_user_company(db, current_user)
    if not company.stripe_customer_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Stripe customer not found. Start subscription first.",
        )

    return_url = payload.return_url or default_checkout_urls()[0]
    portal = create_portal_session(company.stripe_customer_id, return_url)
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

    configure_stripe()

    invoices: list[BillingInvoiceItem] = []
    next_charge_amount = None
    next_charge_date = None

    if company.stripe_customer_id:
        stripe_invoices = stripe.Invoice.list(customer=company.stripe_customer_id, limit=6)
        invoices = [BillingInvoiceItem(**parse_invoice_summary(inv)) for inv in stripe_invoices.data]

    if company.stripe_subscription_id:
        subscription = stripe.Subscription.retrieve(company.stripe_subscription_id)
        apply_subscription_to_company(company, subscription)

        try:
            upcoming = stripe.Invoice.upcoming(customer=company.stripe_customer_id)
            next_charge_amount = to_decimal_from_cents(upcoming.get("amount_due"))
            next_charge_date = from_unix(upcoming.get("next_payment_attempt"))
        except Exception:
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


@router.post("/webhook")
async def stripe_webhook(request: Request, db: AsyncSession = Depends(get_db)):
    stripe_available = stripe_is_configured()
    if stripe_available:
        configure_stripe()

    payload = await request.body()
    signature = request.headers.get("Stripe-Signature")
    secret = os.getenv("STRIPE_WEBHOOK_SECRET")

    if not signature or not secret:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing Stripe webhook signature or secret",
        )

    try:
        event = stripe.Webhook.construct_event(payload=payload, sig_header=signature, secret=secret)
    except Exception:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid webhook signature")

    event_payload = json.loads(payload.decode("utf-8"))
    event_type = event_payload.get("type")
    data_object = event_payload.get("data", {}).get("object", {})

    supported_events = {
        "checkout.session.completed",
        "customer.subscription.created",
        "customer.subscription.updated",
        "customer.subscription.deleted",
        "invoice.paid",
        "invoice.payment_failed",
    }

    if event_type not in supported_events:
        logger.info("Stripe webhook ignored (unsupported event): %s", event_type)
        return {"received": True, "event_type": event_type, "ignored": True}

    company: Company | None = None
    customer_id = data_object.get("customer") if isinstance(data_object, dict) else None

    if customer_id:
        result = await db.execute(select(Company).where(Company.stripe_customer_id == customer_id))
        company = result.scalar_one_or_none()

    if event_type == "checkout.session.completed":
        customer_id = data_object.get("customer") if isinstance(data_object, dict) else None
        subscription_id = data_object.get("subscription") if isinstance(data_object, dict) else None
        if customer_id and not company:
            metadata = data_object.get("metadata", {}) if isinstance(data_object, dict) else {}
            metadata_company_id = metadata.get("company_id") if isinstance(metadata, dict) else None
            if metadata_company_id:
                try:
                    parsed_company_id = uuid.UUID(metadata_company_id)
                    result = await db.execute(select(Company).where(Company.id == parsed_company_id))
                    company = result.scalar_one_or_none()
                    if company:
                        company.stripe_customer_id = customer_id
                except ValueError:
                    logger.warning("Invalid company_id in Stripe metadata: %s", metadata_company_id)

        if company and subscription_id and stripe_available:
            subscription = stripe.Subscription.retrieve(subscription_id)
            apply_subscription_to_company(company, subscription)
        elif company and subscription_id and not stripe_available:
            company.stripe_subscription_id = subscription_id
            company.billing_last_event_at = datetime.now(UTC)

    elif event_type in {"customer.subscription.created", "customer.subscription.updated", "customer.subscription.deleted"}:
        if company:
            apply_subscription_to_company(company, data_object)

    elif event_type == "invoice.paid":
        if company:
            company.billing_status = "active"
            company.billing_last_event_at = datetime.now(UTC)

    elif event_type == "invoice.payment_failed":
        if company:
            company.billing_status = "past_due"
            company.billing_last_event_at = datetime.now(UTC)

    if company:
        db.add(company)
        await db.commit()

    logger.info("Stripe webhook handled: %s", event_type)
    return {"received": True, "event_type": event_type}
