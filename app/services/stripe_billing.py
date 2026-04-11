import os
from datetime import UTC, datetime
from decimal import Decimal

import stripe
from app.models import Company, User


def stripe_is_configured() -> bool:
    return bool(os.getenv("STRIPE_SECRET_KEY"))


def configure_stripe() -> None:
    api_key = os.getenv("STRIPE_SECRET_KEY")
    if not api_key:
        raise ValueError("STRIPE_SECRET_KEY is not configured")
    stripe.api_key = api_key


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


def ensure_stripe_customer(company: Company, user: User) -> str:
    if company.stripe_customer_id:
        return company.stripe_customer_id

    customer = stripe.Customer.create(
        email=user.email,
        name=company_display_name(company),
        metadata=company_metadata(company),
    )
    return customer.id


def create_checkout_session(
    *,
    company: Company,
    user: User,
    customer_id: str,
    price_id: str,
    success_url: str,
    cancel_url: str,
) -> stripe.checkout.Session:
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
