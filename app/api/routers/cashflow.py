"""
Router: /api/cashflow
Prognoza cashflow, kalendarz podatkowy, DSO kontrahentów, cel CEO.
"""
import logging
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.auth import verify_token
from app.database import get_db
from app.models import User, Company, CompanyContextModel
from app.services.context_operational import (
    OperationalContext,
    get_or_create_operational,
    refresh_financial_metrics,
)
from app.services.cashflow_engine import (
    CashflowForecast,
    TaxCalendarEntry,
    ContractorReliability,
    calculate_contractor_reliability,
    calculate_cashflow_forecast,
)
from app.services.context_canonical import get_or_create_canonical
from sqlalchemy import select

logger = logging.getLogger(__name__)

router = APIRouter(tags=["cashflow"], dependencies=[Depends(verify_token)])


async def _ensure_canonical_from_company(
    db: AsyncSession, company_id, canonical
) -> "CanonicalContext":
    """
    Jeśli L1 Canonical nie ma tax_regime (firma zarejestrowana przed upgrade),
    uzupełnia domyślne wartości z tabeli Company lub sensowne defaults.
    """
    from app.services.context_canonical import CanonicalContext, CompanyIdentity, TaxRegime

    result = await db.execute(select(Company).where(Company.id == company_id))
    company = result.scalar_one_or_none()
    if not company:
        return canonical

    tax = canonical.tax_regime
    identity = canonical.company_identity

    # Uzupełnij z Company jeśli ma dane z nowej rejestracji
    if company.legal_form and not identity.legal_form:
        identity.legal_form = company.legal_form
    if company.taxation_form and not tax.taxation_form:
        tax.taxation_form = company.taxation_form
    if company.vat_status and not tax.vat_rate:
        tax.vat_rate = "23%" if company.vat_status == "czynny" else "ZW"
        tax.jpk_obligation = company.vat_status == "czynny"
    if company.employee_count is not None and tax.employee_count is None:
        tax.employee_count = company.employee_count
    if company.zus_type and not tax.zus_type:
        tax.zus_type = company.zus_type

    # Fallback defaults: jeśli nadal pusty → domyślna Sp. z o.o. + VAT czynny
    if not identity.legal_form:
        identity.legal_form = "Sp. z o.o."
    if not tax.vat_rate:
        tax.vat_rate = "23%"
        tax.jpk_obligation = True
    if not tax.taxation_form:
        is_jdg = "JDG" in (identity.legal_form or "").upper()
        tax.taxation_form = "Skala podatkowa" if is_jdg else "CIT 19%"
    if tax.cit_rate is None and "JDG" not in (identity.legal_form or "").upper():
        if "9%" in (tax.taxation_form or ""):
            tax.cit_rate = 9.0
        elif "estoński" not in (tax.taxation_form or "").lower():
            tax.cit_rate = 19.0
    if not tax.vat_frequency:
        tax.vat_frequency = "monthly"
    if tax.employee_count is None:
        tax.employee_count = 0

    return canonical


@router.get("/forecast")
async def get_cashflow_forecast(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Zwraca prognozę cashflow. Kalendarz podatkowy zawsze generowany live z L1."""
    if not current_user.company_id:
        raise HTTPException(status_code=400, detail="User has no company assigned.")

    from app.services.cashflow_engine import generate_tax_calendar

    operational = await get_or_create_operational(db, current_user.company_id)
    cf = operational.cashflow_forecast

    # Kalendarz podatkowy — zawsze live z L1 Canonical (tanie, zero DB queries)
    canonical = await get_or_create_canonical(db, current_user.company_id)

    # Jeśli L1 nie ma tax_regime (stara firma), spróbuj uzupełnić z Company
    if not canonical.tax_regime.taxation_form and not canonical.company_identity.legal_form:
        canonical = await _ensure_canonical_from_company(db, current_user.company_id, canonical)

    # Pobierz szacunki VAT/CIT z operational
    vat_estimate = None
    cit_estimate = None
    if operational.tax_position:
        vat_estimate = getattr(operational.tax_position, 'vat_to_pay', None)
        cit_estimate = getattr(operational.tax_position, 'cit_estimated', None)

    live_tax_calendar = generate_tax_calendar(
        canonical,
        months_ahead=3,
        vat_monthly_estimate=vat_estimate,
        cit_annual_estimate=cit_estimate,
    )

    return {
        "forecast_date": cf.forecast_date,
        "direction_score": cf.direction_score,
        "safe_burn_limit": cf.safe_burn_limit,
        "runway_weeks": cf.runway_weeks,
        "ceo_monthly_goal": cf.ceo_monthly_goal,
        "weekly_projections": [wp.model_dump() for wp in cf.weekly_projections],
        "tax_calendar": [t.model_dump() for t in live_tax_calendar],
        "contractor_reliability": [c.model_dump() for c in cf.contractor_reliability],
    }


@router.get("/tax-calendar")
async def get_tax_calendar(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Zwraca kalendarz podatkowy (live z L1 Canonical)."""
    if not current_user.company_id:
        raise HTTPException(status_code=400, detail="User has no company assigned.")

    from app.services.cashflow_engine import generate_tax_calendar

    canonical = await get_or_create_canonical(db, current_user.company_id)

    if not canonical.tax_regime.taxation_form and not canonical.company_identity.legal_form:
        canonical = await _ensure_canonical_from_company(db, current_user.company_id, canonical)

    operational = await get_or_create_operational(db, current_user.company_id)
    vat_estimate = None
    cit_estimate = None
    if operational.tax_position:
        vat_estimate = getattr(operational.tax_position, 'vat_to_pay', None)
        cit_estimate = getattr(operational.tax_position, 'cit_estimated', None)

    live_calendar = generate_tax_calendar(
        canonical,
        months_ahead=3,
        vat_monthly_estimate=vat_estimate,
        cit_annual_estimate=cit_estimate,
    )

    return {"tax_calendar": [t.model_dump() for t in live_calendar]}


@router.get("/contractors")
async def get_contractor_reliability(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Zwraca DSO i wiarygodność kontrahentów (live query, nie cached)."""
    if not current_user.company_id:
        raise HTTPException(status_code=400, detail="User has no company assigned.")

    contractors = await calculate_contractor_reliability(db, current_user.company_id)
    return {"contractors": [c.model_dump() for c in contractors]}


class CeoGoalUpdate(BaseModel):
    monthly_goal: float


@router.patch("/goal")
async def update_ceo_goal(
    body: CeoGoalUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Ustawia cel CEO (kwota miesięczna zarezerwowana na rozwój)."""
    if not current_user.company_id:
        raise HTTPException(status_code=400, detail="User has no company assigned.")

    result = await db.execute(
        select(CompanyContextModel).where(
            CompanyContextModel.company_id == current_user.company_id
        )
    )
    record = result.scalar_one_or_none()
    if not record:
        raise HTTPException(status_code=404, detail="Company context not found.")

    try:
        operational = OperationalContext.model_validate(record.operational_data or {})
    except Exception:
        operational = OperationalContext()

    operational.cashflow_forecast.ceo_monthly_goal = body.monthly_goal

    record.operational_data = operational.model_dump(mode="json")
    await db.commit()

    return {"ceo_monthly_goal": body.monthly_goal, "status": "updated"}


@router.post("/refresh")
async def force_refresh_cashflow(
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Force refresh prognozy cashflow (przelicza SafeCash + metryki + forecast)."""
    if not current_user.company_id:
        raise HTTPException(status_code=400, detail="User has no company assigned.")

    from app.services.context_operational import refresh_safecash_background
    background_tasks.add_task(refresh_safecash_background, str(current_user.company_id))

    return {"status": "refresh_scheduled"}
