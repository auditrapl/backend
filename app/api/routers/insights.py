"""
/api/insights — Proactive Insights (Tax Eye + CFO Radar + Morning)
"""
import logging
import uuid
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import func, and_
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import get_current_user, require_feature
from app.auth import verify_token
from app.database import get_db
from app.schemas import UserResponse, ProactiveInsightResponse, InsightsSummaryResponse
from app.models import ProactiveInsight

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(verify_token)])


@router.get("/", response_model=list[ProactiveInsightResponse])
async def list_insights(
    insight_type: str | None = None,
    include_dismissed: bool = False,
    limit: int = 20,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(get_current_user),
):
    """Lista insightow firmy — filtrowanie po type i dismissed."""
    company_id = getattr(current_user, "company_id", None)
    if not company_id:
        raise HTTPException(status_code=400, detail="Brak przypisanej firmy.")

    stmt = (
        select(ProactiveInsight)
        .where(ProactiveInsight.company_id == company_id)
    )
    if insight_type:
        stmt = stmt.where(ProactiveInsight.insight_type == insight_type)
    if not include_dismissed:
        stmt = stmt.where(ProactiveInsight.is_dismissed == False)

    stmt = stmt.order_by(ProactiveInsight.created_at.desc()).limit(limit)
    result = await db.execute(stmt)
    return result.scalars().all()


@router.get("/summary", response_model=InsightsSummaryResponse)
async def insights_summary(
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(get_current_user),
):
    """Counter: ile insightow, ile PLN oszczednosci, top insight."""
    company_id = getattr(current_user, "company_id", None)
    if not company_id:
        raise HTTPException(status_code=400, detail="Brak przypisanej firmy.")

    # Counts per type
    counts = await db.execute(
        select(
            ProactiveInsight.insight_type,
            func.count(ProactiveInsight.id),
        )
        .where(
            ProactiveInsight.company_id == company_id,
            ProactiveInsight.is_dismissed == False,
        )
        .group_by(ProactiveInsight.insight_type)
    )
    count_map = {row[0]: row[1] for row in counts.fetchall()}

    # Total savings
    savings = await db.execute(
        select(func.sum(ProactiveInsight.estimated_savings))
        .where(
            ProactiveInsight.company_id == company_id,
            ProactiveInsight.is_dismissed == False,
            ProactiveInsight.estimated_savings.isnot(None),
        )
    )
    total_savings = savings.scalar() or 0.0

    # Top insight (highest savings, most recent)
    top_result = await db.execute(
        select(ProactiveInsight)
        .where(
            ProactiveInsight.company_id == company_id,
            ProactiveInsight.is_dismissed == False,
        )
        .order_by(
            ProactiveInsight.estimated_savings.desc().nullslast(),
            ProactiveInsight.created_at.desc(),
        )
        .limit(1)
    )
    top = top_result.scalar_one_or_none()

    return InsightsSummaryResponse(
        total_insights=sum(count_map.values()),
        total_savings_pln=float(total_savings),
        tax_eye_count=count_map.get("tax_eye", 0),
        cfo_radar_count=count_map.get("cfo_radar", 0),
        top_insight=top,
    )


@router.patch("/{insight_id}/dismiss")
async def dismiss_insight(
    insight_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(get_current_user),
):
    """Odrzuc insight (is_dismissed = true)."""
    company_id = getattr(current_user, "company_id", None)
    result = await db.execute(
        select(ProactiveInsight).where(
            ProactiveInsight.id == insight_id,
            ProactiveInsight.company_id == company_id,
        )
    )
    insight = result.scalar_one_or_none()
    if not insight:
        raise HTTPException(status_code=404, detail="Insight nie znaleziony.")

    insight.is_dismissed = True
    await db.commit()
    return {"status": "dismissed"}
