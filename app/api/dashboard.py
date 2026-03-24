"""
Dashboard Router — AI Audit endpoints for the frontend dashboard.
"""
from datetime import datetime, timedelta
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, case, and_

from app.database import get_db
from app.models import Invoice, User
from app.schemas import InvoiceResponse, AuditSummaryResponse, BatchAuditResponse
from app.api.deps import get_current_user
from app.services.audit_service import AuditService

router = APIRouter(tags=["Dashboard AI"])


# ── 1. Single Invoice Audit ──────────────────────────────────
@router.post("/invoices/{invoice_id}/audit", response_model=InvoiceResponse)
async def audit_single_invoice(
    invoice_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Trigger an on-demand AI audit for a specific invoice.
    Verifies the invoice belongs to the current user's organization.
    """
    if not current_user.company_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User has no company assigned.",
        )

    # Fetch invoice scoped to user's organization
    result = await db.execute(
        select(Invoice).where(
            Invoice.id == invoice_id,
            Invoice.company_id == current_user.company_id,
        )
    )
    invoice = result.scalar_one_or_none()

    if not invoice:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Invoice not found or does not belong to your organization.",
        )

    try:
        updated_invoice = await AuditService.audit_single_invoice(invoice, db)
        return updated_invoice
    except RuntimeError as e:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Audit failed: {str(e)}")


# ── 2. Batch Audit (Pending Invoices) ────────────────────────
@router.post("/invoices/audit-pending", response_model=BatchAuditResponse)
async def audit_pending_invoices(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Find all invoices for the logged-in organization that have not been audited
    (risk_score == 0 AND status in ['new', 'pending']) and process them through
    the AI audit service.
    """
    if not current_user.company_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User has no company assigned.",
        )

    try:
        audited_invoices = await AuditService.audit_pending_invoices(
            current_user.company_id, db
        )
        return BatchAuditResponse(
            processed_count=len(audited_invoices),
            invoices=[InvoiceResponse.model_validate(inv) for inv in audited_invoices],
        )
    except RuntimeError as e:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e))


# ── 3. Audit Summary (Aggregates for widgets) ────────────────
@router.get("/audit-summary", response_model=AuditSummaryResponse)
async def get_audit_summary(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Returns aggregated audit statistics for the 'Tax Risk' widget on the frontend.
    """
    if not current_user.company_id:
        return AuditSummaryResponse()

    company_filter = Invoice.company_id == current_user.company_id

    # Current month boundaries
    now = datetime.utcnow()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    # Single aggregation query
    result = await db.execute(
        select(
            func.count(Invoice.id).label("total_invoices"),
            func.count(
                case((Invoice.status == "audited", Invoice.id))
            ).label("total_audited"),
            func.count(
                case(
                    (
                        and_(
                            Invoice.status == "audited",
                            Invoice.updated_at >= month_start,
                        ),
                        Invoice.id,
                    )
                )
            ).label("total_audited_this_month"),
            func.count(
                case(
                    (Invoice.status.in_(["new", "pending"]), Invoice.id)
                )
            ).label("pending_audit"),
            func.count(
                case(
                    (
                        and_(Invoice.status == "audited", Invoice.risk_score >= 70),
                        Invoice.id,
                    )
                )
            ).label("high_risk_count"),
            func.count(
                case(
                    (
                        and_(
                            Invoice.status == "audited",
                            Invoice.risk_score >= 30,
                            Invoice.risk_score < 70,
                        ),
                        Invoice.id,
                    )
                )
            ).label("medium_risk_count"),
            func.count(
                case(
                    (
                        and_(
                            Invoice.status == "audited",
                            Invoice.risk_score >= 1,
                            Invoice.risk_score < 30,
                        ),
                        Invoice.id,
                    )
                )
            ).label("low_risk_count"),
            func.count(
                case(
                    (
                        and_(Invoice.status == "audited", Invoice.risk_score == 0),
                        Invoice.id,
                    )
                )
            ).label("secure_count"),
            func.coalesce(
                func.avg(
                    case((Invoice.status == "audited", Invoice.risk_score))
                ),
                0,
            ).label("avg_risk_score"),
        ).where(company_filter)
    )

    row = result.one()

    return AuditSummaryResponse(
        total_invoices=row.total_invoices,
        total_audited=row.total_audited,
        total_audited_this_month=row.total_audited_this_month,
        pending_audit=row.pending_audit,
        high_risk_count=row.high_risk_count,
        medium_risk_count=row.medium_risk_count,
        low_risk_count=row.low_risk_count,
        secure_count=row.secure_count,
        avg_risk_score=round(float(row.avg_risk_score), 1),
    )
