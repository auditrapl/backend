from fastapi import Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from app.database import get_db
from app.models import BillingPlan, Company, User
from app.auth import verify_token, VerifiedToken
import uuid


PLAN_FEATURES = {
    "free": {
        "reports.read",
        "billing.manage",
        "cfo.morning_briefing",
    },
    "pro": {
        "reports.read",
        "reports.generate",
        "billing.manage",
        "cfo.morning_briefing",
        "cfo.simulate",
    },
    "enterprise": {
        "reports.read",
        "reports.generate",
        "billing.manage",
        "cfo.morning_briefing",
        "cfo.simulate",
        "cfo.share",
    },
}


GRACE_BILLING_STATUSES = {"past_due", "unpaid"}


def infer_plan_tier(company: Company, plan_name: str | None = None) -> str:
    """Infer feature tier from current billing context."""
    status_value = (company.billing_status or "inactive").lower()
    if status_value not in {"active", "trialing", *GRACE_BILLING_STATUSES}:
        return "free"

    probe_values = [
        (company.stripe_price_id or "").lower(),
        (plan_name or "").lower(),
    ]
    if any("enterprise" in value for value in probe_values):
        return "enterprise"
    return "pro"


def require_feature(feature_key: str):
    async def dependency(
        current_user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db),
    ) -> dict:
        company = None
        if current_user.company_id:
            company_result = await db.execute(select(Company).where(Company.id == current_user.company_id))
            company = company_result.scalars().first()

        if not company:
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail={
                    "message": "Feature requires active subscription.",
                    "feature": feature_key,
                    "plan": "free",
                    "upgrade_url": "/dashboard/platnosci",
                    "reason": "no_company",
                },
            )

        plan_name = None
        if company.stripe_price_id:
            plan_result = await db.execute(select(BillingPlan).where(BillingPlan.stripe_price_id == company.stripe_price_id))
            plan = plan_result.scalars().first()
            plan_name = plan.name if plan else None

        tier = infer_plan_tier(company, plan_name)
        allowed_features = PLAN_FEATURES.get(tier, PLAN_FEATURES["free"])
        billing_status_value = (company.billing_status or "inactive").lower()

        if feature_key not in allowed_features:
            reason = "tier_restriction"
            if billing_status_value in GRACE_BILLING_STATUSES:
                reason = "subscription_grace_period"
            elif billing_status_value not in {"active", "trialing"}:
                reason = "inactive_subscription"

            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail={
                    "message": "Feature blocked by subscription plan.",
                    "feature": feature_key,
                    "plan": tier,
                    "billing_status": billing_status_value,
                    "upgrade_url": "/dashboard/platnosci",
                    "reason": reason,
                },
            )

        return {
            "feature": feature_key,
            "plan": tier,
            "billing_status": billing_status_value,
        }

    return dependency

async def get_current_user(
    token_payload: VerifiedToken = Depends(verify_token),
    db: AsyncSession = Depends(get_db)
) -> User:
    user_id_str = token_payload.get("sub")
    email = token_payload.get("email")
    
    if not user_id_str:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token payload",
        )

    try:
        user_uuid = uuid.UUID(user_id_str)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token payload",
        )
    
    result = await db.execute(select(User).where(User.id == user_uuid))
    user = result.scalars().first()
    
    if not user:
        user = User(id=user_uuid, email=email)
        db.add(user)
        await db.commit()
        await db.refresh(user)
        
    return user
