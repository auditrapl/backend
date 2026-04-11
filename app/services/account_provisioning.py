"""
Account Provisioning — Auto-create Supabase user + company after Stripe payment.
Uses Supabase Admin API (service_role_key) to invite user by email.
"""
import os
import logging
import uuid

from supabase import create_client
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.models import User, Company

logger = logging.getLogger(__name__)


def get_supabase_admin():
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        raise ValueError("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set")
    return create_client(url, key)


async def provision_account_after_payment(
    db: AsyncSession,
    email: str,
    stripe_customer_id: str,
    stripe_subscription_id: str | None = None,
    plan_name: str = "Pro",
) -> tuple[User, Company]:
    """
    Called from Stripe webhook after checkout.session.completed.
    1. Check if user already exists (by email) -> if yes, just link Stripe
    2. Create Supabase Auth user via admin invite (user sets own password)
    3. Create Company + User records in our DB
    Returns (user, company)
    """
    # Check if user already exists
    existing_user_result = await db.execute(select(User).where(User.email == email))
    existing_user = existing_user_result.scalar_one_or_none()

    if existing_user:
        # User exists — just update Stripe mapping
        existing_user.stripe_customer_id = stripe_customer_id
        if stripe_subscription_id:
            existing_user.stripe_subscription_id = stripe_subscription_id
        db.add(existing_user)

        if existing_user.company_id:
            company_result = await db.execute(
                select(Company).where(Company.id == existing_user.company_id)
            )
            company = company_result.scalar_one_or_none()
            if company:
                company.stripe_customer_id = stripe_customer_id
                if stripe_subscription_id:
                    company.stripe_subscription_id = stripe_subscription_id
                company.billing_status = "active"
                db.add(company)
                return existing_user, company

    # Create Supabase Auth user via invite
    supabase = get_supabase_admin()

    supabase_user_id = None
    if not existing_user:
        try:
            # invite_user_by_email sends an email with a link to set password
            # The redirect URL should point to our app
            frontend_url = os.getenv("FRONTEND_APP_URL", "http://localhost:3000")
            response = supabase.auth.admin.invite_user_by_email(
                email,
                options={"redirect_to": f"{frontend_url}/login?onboarding=true"},
            )
            supabase_user_id = str(response.user.id) if response.user else None
            logger.info(
                "[Provisioning] Supabase user invited: %s -> %s",
                email,
                supabase_user_id,
            )
        except Exception as e:
            logger.error("[Provisioning] Failed to invite user %s: %s", email, e)
            # If invite fails (e.g. user already exists in Supabase), try to get existing
            try:
                users_response = supabase.auth.admin.list_users()
                for u in users_response:
                    if hasattr(u, "email") and u.email == email:
                        supabase_user_id = str(u.id)
                        break
            except Exception:
                pass

    # Create Company
    # nip is NOT NULL + unique — use a placeholder keyed to email for provisioning;
    # the user will fill the real NIP during onboarding.
    email_prefix = email.split("@")[0]
    placeholder_nip = f"PENDING-{email_prefix[:20]}-{uuid.uuid4().hex[:6]}"

    company = Company(
        name=f"Firma ({email_prefix})",
        nip=placeholder_nip,
        stripe_customer_id=stripe_customer_id,
        stripe_subscription_id=stripe_subscription_id,
        billing_status="active",
    )
    db.add(company)
    await db.flush()  # Get company.id

    # Create or update User
    if existing_user:
        user = existing_user
        user.company_id = company.id
        user.stripe_customer_id = stripe_customer_id
        if stripe_subscription_id:
            user.stripe_subscription_id = stripe_subscription_id
    else:
        user = User(
            id=uuid.UUID(supabase_user_id) if supabase_user_id else uuid.uuid4(),
            email=email,
            company_id=company.id,
            stripe_customer_id=stripe_customer_id,
            stripe_subscription_id=stripe_subscription_id,
        )
    db.add(user)

    # Send welcome email (fire-and-forget, don't break provisioning)
    try:
        from app.services.email_service import send_welcome_email

        frontend_url = os.getenv("FRONTEND_APP_URL", "https://app.auditra.pl")
        send_welcome_email(
            to_email=email,
            dashboard_url=f"{frontend_url}/dashboard",
        )
    except Exception as e:
        logger.warning("[Provisioning] Welcome email failed for %s: %s", email, e)

    return user, company
