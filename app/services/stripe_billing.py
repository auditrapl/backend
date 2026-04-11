import os
from datetime import UTC, datetime
from decimal import Decimal
from functools import wraps
from typing import Any, Callable, TypeVar, cast

import stripe
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import BillingPlan, Company, User

T = TypeVar("T")


class StripeServiceError(Exception):
    """Controlled Stripe SDK exception for API and service layers."""


def stripe_error_handler(action: str) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """Wrap Stripe calls and normalize SDK exceptions to StripeServiceError."""

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> T:
            try:
                return func(*args, **kwargs)
            except stripe.error.StripeError as exc:
                message = str(getattr(exc, "user_message", None) or str(exc) or "Stripe API error")
                raise StripeServiceError(f"{action}: {message}") from exc

        return wrapper

    return decorator


def stripe_is_configured() -> bool:
    return bool(os.getenv("STRIPE_SECRET_KEY"))


def configure_stripe() -> None:
    api_key = os.getenv("STRIPE_SECRET_KEY")
    if not api_key:
        raise ValueError("STRIPE_SECRET_KEY is not configured")
    stripe.api_key = api_key


def initialize_stripe_client() -> stripe:
    """Initialize Stripe SDK and return configured client module."""
    configure_stripe()
    return cast(stripe, stripe)


def default_checkout_urls() -> tuple[str, str]:
    frontend = os.getenv("FRONTEND_APP_URL", "http://localhost:3000")
    success_url = f"{frontend}/dashboard/platnosci?stripe=success"
    cancel_url = f"{frontend}/dashboard/platnosci?stripe=cancel"
    return success_url, cancel_url


def default_price_id() -> str | None:
    return os.getenv("STRIPE_DEFAULT_PRICE_ID")


def to_decimal_from_cents(amount: int | None) -> Decimal | None:
    if amount is None:
        return None
    return (Decimal(amount) / Decimal("100")).quantize(Decimal("0.01"))


def from_unix(ts: int | None) -> datetime | None:
    if not ts:
        return None
    return datetime.fromtimestamp(ts, tz=UTC)


def company_display_name(company: Company) -> str:
    return company.name or f"Company-{company.id}"


def company_metadata(company: Company) -> dict[str, str]:
    return {
        "company_id": str(company.id),
        "company_nip": company.nip,
    }


@stripe_error_handler("create_customer")
def create_customer(company: Company, user: User) -> stripe.Customer:
    initialize_stripe_client()
    return stripe.Customer.create(
        email=user.email,
        name=company_display_name(company),
        metadata={
            **company_metadata(company),
            "auditra_user_id": str(user.id),
            "auditra_user_email": user.email,
        },
    )


@stripe_error_handler("retrieve_customer")
def retrieve_customer(customer_id: str) -> stripe.Customer:
    initialize_stripe_client()
    return stripe.Customer.retrieve(customer_id)


def map_auditra_user_to_stripe_customer(company: Company, user: User) -> stripe.Customer:
    """Map Auditra user/company to a Stripe customer object."""
    if company.stripe_customer_id:
        try:
            return retrieve_customer(company.stripe_customer_id)
        except StripeServiceError:
            # Existing Stripe customer id is stale; fallback to create.
            pass

    return create_customer(company, user)


def ensure_stripe_customer(company: Company, user: User) -> str:
    customer = map_auditra_user_to_stripe_customer(company, user)
    return customer.id


@stripe_error_handler("create_subscription")
def create_subscription(*, customer_id: str, price_id: str, company: Company) -> stripe.Subscription:
    initialize_stripe_client()
    return stripe.Subscription.create(
        customer=customer_id,
        items=[{"price": price_id}],
        metadata={
            **company_metadata(company),
            "price_id": price_id,
        },
    )


@stripe_error_handler("cancel_subscription")
def cancel_subscription(subscription_id: str, cancel_at_period_end: bool = True) -> stripe.Subscription:
    initialize_stripe_client()
    if cancel_at_period_end:
        return stripe.Subscription.modify(
            subscription_id,
            cancel_at_period_end=True,
        )
    return stripe.Subscription.cancel(subscription_id)


@stripe_error_handler("retrieve_subscription")
def retrieve_subscription(subscription_id: str) -> stripe.Subscription:
    initialize_stripe_client()
    return stripe.Subscription.retrieve(subscription_id)


@stripe_error_handler("list_customer_invoices")
def list_customer_invoices(customer_id: str, limit: int = 6) -> stripe.ListObject:
    initialize_stripe_client()
    return stripe.Invoice.list(customer=customer_id, limit=limit)


@stripe_error_handler("retrieve_upcoming_invoice")
def retrieve_upcoming_invoice(customer_id: str) -> stripe.Invoice:
    initialize_stripe_client()
    return stripe.Invoice.upcoming(customer=customer_id)


@stripe_error_handler("construct_webhook_event")
def construct_webhook_event(payload: bytes, signature: str, secret: str) -> stripe.Event:
    initialize_stripe_client()
    return stripe.Webhook.construct_event(payload=payload, sig_header=signature, secret=secret)


# CamelCase aliases requested by task scope.
createCustomer = create_customer
createSubscription = create_subscription
cancelSubscription = cancel_subscription


def create_checkout_session(
    *,
    company: Company,
    user: User,
    customer_id: str,
    price_id: str,
    success_url: str,
    cancel_url: str,
) -> stripe.checkout.Session:
    initialize_stripe_client()
    return stripe.checkout.Session.create(
        mode="subscription",
        customer=customer_id,
        line_items=[{"price": price_id, "quantity": 1}],
        allow_promotion_codes=True,
        success_url=success_url,
        cancel_url=cancel_url,
        billing_address_collection="required",
        customer_update={"name": "auto", "address": "auto"},
        metadata={
            **company_metadata(company),
            "user_id": str(user.id),
        },
        subscription_data={
            "metadata": {
                **company_metadata(company),
                "price_id": price_id,
            }
        },
    )


def create_portal_session(customer_id: str, return_url: str) -> stripe.billing_portal.Session:
    initialize_stripe_client()
    return stripe.billing_portal.Session.create(
        customer=customer_id,
        return_url=return_url,
    )


def apply_subscription_to_company(company: Company, subscription: stripe.Subscription) -> None:
    item = None
    if subscription.get("items") and subscription["items"].get("data"):
        item = subscription["items"]["data"][0]

    price = item.get("price") if item else None
    recurring = price.get("recurring") if price else None

    company.stripe_subscription_id = subscription.get("id")
    company.stripe_price_id = price.get("id") if price else company.stripe_price_id
    company.billing_status = subscription.get("status") or company.billing_status
    company.billing_currency = (price.get("currency") if price else None) or company.billing_currency
    company.billing_interval = (recurring.get("interval") if recurring else None) or company.billing_interval
    company.billing_amount = to_decimal_from_cents(
        price.get("unit_amount") if price else None
    ) or company.billing_amount
    company.billing_current_period_end = from_unix(subscription.get("current_period_end"))
    company.billing_cancel_at_period_end = bool(subscription.get("cancel_at_period_end"))
    company.billing_last_event_at = datetime.now(UTC)


def parse_invoice_summary(invoice: stripe.Invoice) -> dict[str, object]:
    return {
        "id": invoice.get("id"),
        "number": invoice.get("number"),
        "status": invoice.get("status"),
        "amount_paid": to_decimal_from_cents(invoice.get("amount_paid")) or Decimal("0.00"),
        "currency": (invoice.get("currency") or "pln").upper(),
        "created_at": from_unix(invoice.get("created")) or datetime.now(UTC),
        "invoice_pdf_url": invoice.get("invoice_pdf"),
        "hosted_invoice_url": invoice.get("hosted_invoice_url"),
    }


@stripe_error_handler("list_prices")
def list_prices(*, only_active: bool = True, limit: int = 100) -> stripe.ListObject:
    initialize_stripe_client()
    return stripe.Price.list(active=only_active, limit=limit, expand=["data.product"])


async def sync_stripe_products_and_prices(
    db: AsyncSession,
    *,
    only_active: bool = True,
    limit: int = 100,
) -> int:
    """Upsert Stripe products/prices into local BillingPlan table."""
    prices = list_prices(only_active=only_active, limit=limit)
    synced = 0

    for price in prices.data:
        price_id = price.get("id")
        if not price_id:
            continue

        product = price.get("product")
        product_id = product.get("id") if isinstance(product, dict) else str(product)

        existing = await db.execute(select(BillingPlan).where(BillingPlan.stripe_price_id == price_id))
        plan = existing.scalar_one_or_none()
        if not plan:
            plan = BillingPlan(
                stripe_price_id=price_id,
                stripe_product_id=product_id,
                name=(product.get("name") if isinstance(product, dict) else None) or price.get("nickname") or price_id,
            )

        recurring = price.get("recurring") or {}
        plan.stripe_product_id = product_id
        plan.name = (product.get("name") if isinstance(product, dict) else None) or price.get("nickname") or plan.name
        plan.description = product.get("description") if isinstance(product, dict) else plan.description
        plan.currency = (price.get("currency") or "usd").upper()
        plan.interval = recurring.get("interval")
        plan.interval_count = recurring.get("interval_count")
        plan.amount = to_decimal_from_cents(price.get("unit_amount"))
        plan.metadata_json = {
            "price_metadata": price.get("metadata") or {},
            "product_metadata": product.get("metadata") if isinstance(product, dict) else {},
        }
        plan.is_active = bool(price.get("active", True))

        db.add(plan)
        synced += 1

    await db.commit()
    return synced


syncStripeProductsPrices = sync_stripe_products_and_prices
