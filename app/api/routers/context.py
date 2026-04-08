"""
Router: /api/context
Zarządzanie 3-warstwowym kontekstem firmy.
"""
import logging
import uuid
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc
from pydantic import BaseModel

from app.api.deps import get_current_user
from app.auth import verify_token
from app.database import get_db, AsyncSessionLocal
from app.schemas import UserResponse
from app.models import CompanyContextModel, ContextSnapshotModel
from app.services.context_canonical import (
    CanonicalContext,
    get_or_create_canonical,
    update_canonical,
    process_and_upsert_canonical,
)
from app.services.context_operational import (
    OperationalContext,
    get_or_create_operational,
    update_operational_from_metrics,
)
from app.services.context_snapshots import (
    get_snapshots,
    create_weekly_snapshot,
    create_monthly_snapshot,
)

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(verify_token)])


# ==============================================================================
# SCHEMATY ŻĄDAŃ
# ==============================================================================

class CanonicalUpdateRequest(BaseModel):
    updates: dict


class OperationalUpdateRequest(BaseModel):
    updates: dict


class MetricsSyncRequest(BaseModel):
    """Payload bezpośredniej synchronizacji metryk (z ERP/KSeF)."""
    cash_on_hand: Optional[float] = None
    credit_line: Optional[float] = None
    mrr: Optional[float] = None
    net_burn_rate: Optional[float] = None
    receivables_total: Optional[float] = None
    payables_total: Optional[float] = None
    receivables_overdue_30d: Optional[float] = None
    vat_position: Optional[float] = None
    budget_utilization_pct: Optional[float] = None
    vat_to_pay: Optional[float] = None
    cit_estimated: Optional[float] = None
    next_tax_deadline: Optional[str] = None
    jpk_status: Optional[str] = None


class CanonicalExtractRequest(BaseModel):
    text: str


class SnapshotTriggerRequest(BaseModel):
    period_type: str  # 'weekly' | 'monthly'


class SnapshotResponse(BaseModel):
    id: str
    company_id: str
    period_type: str
    period_start: str
    period_end: str
    key_metrics: Optional[dict] = None
    ai_summary: Optional[str] = None
    created_at: str

    @classmethod
    def from_model(cls, s: ContextSnapshotModel) -> "SnapshotResponse":
        return cls(
            id=str(s.id),
            company_id=str(s.company_id),
            period_type=s.period_type,
            period_start=s.period_start.isoformat(),
            period_end=s.period_end.isoformat(),
            key_metrics=s.key_metrics,
            ai_summary=s.ai_summary,
            created_at=s.created_at.isoformat(),
        )


class FullContextResponse(BaseModel):
    canonical: dict
    operational: dict
    recent_snapshots: List[SnapshotResponse]


def _get_company_id(current_user: UserResponse) -> uuid.UUID:
    company_id = getattr(current_user, "company_id", None)
    if not company_id:
        raise HTTPException(status_code=400, detail="Użytkownik nie jest przypisany do firmy.")
    return uuid.UUID(str(company_id))


# ==============================================================================
# KONTEKST KANONICZNY (L1)
# ==============================================================================

@router.get("/canonical", response_model=dict)
async def get_canonical(
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(get_current_user),
):
    """Zwraca kontekst kanoniczny (tożsamość, model biznesu, strategia)."""
    company_id = _get_company_id(current_user)
    ctx = await get_or_create_canonical(db, company_id)
    return ctx.model_dump()


@router.patch("/canonical", response_model=dict)
async def patch_canonical(
    request: CanonicalUpdateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(get_current_user),
):
    """Manualna aktualizacja L1 bez LLM (z formularzy UI)."""
    company_id = _get_company_id(current_user)
    updated = await update_canonical(db, company_id, request.updates)
    return updated.model_dump()


@router.post("/canonical/extract", response_model=dict)
async def extract_canonical(
    request: CanonicalExtractRequest,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(get_current_user),
):
    """Wyciągnie faktów L1 z dostarczonego tekstu przez Gemini."""
    company_id = _get_company_id(current_user)
    try:
        updated = await process_and_upsert_canonical(db, company_id, request.text)
        return updated.model_dump()
    except Exception as e:
        logger.error(f"Canonical extraction failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Błąd ekstrakcji kontekstu kanonicznego.")


# ==============================================================================
# KONTEKST OPERACYJNY (L2)
# ==============================================================================

@router.get("/operational", response_model=dict)
async def get_operational(
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(get_current_user),
):
    """Zwraca bieżący kontekst operacyjny."""
    company_id = _get_company_id(current_user)
    ctx = await get_or_create_operational(db, company_id)
    return ctx.model_dump(mode="json")


@router.patch("/operational", response_model=dict)
async def patch_operational(
    request: OperationalUpdateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(get_current_user),
):
    """Manualna aktualizacja L2 (np. ręczne wpisanie gotówki, MRR)."""
    company_id = _get_company_id(current_user)
    updated = await update_operational_from_metrics(db, company_id, request.updates)
    return updated.model_dump(mode="json")


@router.post("/operational/sync-metrics", response_model=dict)
async def sync_metrics(
    request: MetricsSyncRequest,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(get_current_user),
):
    """
    Bezpośrednia synchronizacja metryk bez LLM.
    Używane przez webhooks ERP/KSeF i ręczne aktualizacje z UI.
    """
    company_id = _get_company_id(current_user)
    payload = {k: v for k, v in request.model_dump().items() if v is not None}
    updated = await update_operational_from_metrics(db, company_id, payload)
    return updated.model_dump(mode="json")


# ==============================================================================
# SNAPSHOTY (L3)
# ==============================================================================

@router.get("/snapshots", response_model=List[SnapshotResponse])
async def list_snapshots(
    period_type: Optional[str] = Query(None, description="'weekly' lub 'monthly'"),
    limit: int = Query(12, ge=1, le=52),
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(get_current_user),
):
    """Lista snapshotów historycznych (najnowsze pierwsze)."""
    company_id = _get_company_id(current_user)
    snapshots = await get_snapshots(db, company_id, period_type=period_type, limit=limit)
    return [SnapshotResponse.from_model(s) for s in snapshots]


@router.post("/snapshots/trigger", response_model=SnapshotResponse)
async def trigger_snapshot(
    request: SnapshotTriggerRequest,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(get_current_user),
):
    """Ręczne wyzwolenie snapshotu (weekly lub monthly)."""
    company_id = _get_company_id(current_user)
    if request.period_type not in ("weekly", "monthly"):
        raise HTTPException(status_code=400, detail="period_type musi być 'weekly' lub 'monthly'")
    try:
        if request.period_type == "weekly":
            snapshot = await create_weekly_snapshot(db, company_id)
        else:
            snapshot = await create_monthly_snapshot(db, company_id)
        return SnapshotResponse.from_model(snapshot)
    except Exception as e:
        logger.error(f"Snapshot trigger failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Błąd podczas tworzenia snapshotu.")


# ==============================================================================
# PEŁNY KONTEKST (wszystkie warstwy naraz — dla CfoDossier)
# ==============================================================================

@router.get("/full", response_model=FullContextResponse)
async def get_full_context(
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(get_current_user),
):
    """
    Zwraca wszystkie 3 warstwy kontekstu w jednym wywołaniu.
    Używany przez komponent CfoDossier na frontendzie.
    """
    company_id = _get_company_id(current_user)

    canonical = await get_or_create_canonical(db, company_id)
    operational = await get_or_create_operational(db, company_id)
    snapshots = await get_snapshots(db, company_id, limit=12)

    return FullContextResponse(
        canonical=canonical.model_dump(),
        operational=operational.model_dump(mode="json"),
        recent_snapshots=[SnapshotResponse.from_model(s) for s in snapshots],
    )
