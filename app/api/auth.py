import logging

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from app.database import get_db
from app.models import User, Company, CompanyContextModel
from app.schemas import CompanyCreate, CompanyResponse, UserResponse
from app.api.deps import get_current_user
from app.auth import verify_token, verify_supabase_token, set_auth_cookie, clear_auth_cookie
from app.services.context_canonical import (
    CanonicalContext, CompanyIdentity, TaxRegime, BusinessModelDetail,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["auth"])


class SessionCookieRequest(BaseModel):
    access_token: str


@router.post("/session")
async def set_session_cookie(payload: SessionCookieRequest, response: Response):
    verify_supabase_token(payload.access_token)
    set_auth_cookie(response, payload.access_token)
    return {"message": "Session cookie set"}


@router.post("/logout")
async def logout(response: Response):
    clear_auth_cookie(response)
    return {"message": "Session cookie cleared"}


def _infer_cit_rate(company_data: CompanyCreate) -> float | None:
    """Inferuje stawkę CIT z formy prawnej i opodatkowania."""
    lf = (company_data.legal_form or "").upper()
    tf = (company_data.taxation_form or "").lower()

    if "JDG" in lf:
        return None  # JDG płaci PIT, nie CIT

    if "estoński" in tf:
        return None  # CIT estoński = podatek od dystrybucji

    if "9%" in tf:
        return 9.0

    # Default dla spółek
    if company_data.legal_form:
        return 19.0

    return None


def _infer_pit_form(company_data: CompanyCreate) -> str | None:
    """Inferuje formę PIT — tylko dla JDG."""
    lf = (company_data.legal_form or "").upper()
    if "JDG" in lf and company_data.taxation_form:
        return company_data.taxation_form
    return None


@router.post("/register-company", response_model=CompanyResponse)
async def register_company(
    company_data: CompanyCreate,
    _: dict = Depends(verify_token),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    if current_user.company_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User already belongs to a company"
        )

    # Check nip uniqueness
    existing_company = await db.execute(select(Company).where(Company.nip == company_data.nip))
    if existing_company.scalars().first():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Company directly with this NIP already exists"
        )

    # Create Company
    new_company = Company(
        name=company_data.name,
        nip=company_data.nip,
        industry=company_data.industry,
        description=company_data.description,
        legal_form=company_data.legal_form,
        taxation_form=company_data.taxation_form,
        vat_status=company_data.vat_status,
        employee_count=company_data.employee_count,
        zus_type=company_data.zus_type,
    )
    db.add(new_company)
    await db.commit()
    await db.refresh(new_company)

    # Link User & Update Info
    current_user.company_id = new_company.id
    current_user.first_name = company_data.first_name
    current_user.last_name = company_data.last_name
    db.add(current_user)
    await db.commit()

    # Auto-initialize L1 Canonical Context from registration data
    try:
        vat_status_label = None
        if company_data.vat_status == "czynny":
            vat_status_label = "Czynny podatnik VAT"
        elif company_data.vat_status == "zwolniony":
            vat_status_label = "Zwolniony z VAT"

        initial_canonical = CanonicalContext(
            company_identity=CompanyIdentity(
                name=company_data.name,
                nip=company_data.nip,
                legal_form=company_data.legal_form,
                vat_status=vat_status_label,
            ),
            tax_regime=TaxRegime(
                taxation_form=company_data.taxation_form,
                vat_rate="23%" if company_data.vat_status == "czynny" else ("ZW" if company_data.vat_status == "zwolniony" else None),
                jpk_obligation=company_data.vat_status == "czynny" if company_data.vat_status else None,
                cit_rate=_infer_cit_rate(company_data),
                pit_form=_infer_pit_form(company_data),
                zus_type=company_data.zus_type,
                employee_count=company_data.employee_count or 0,
                vat_frequency="monthly",
            ),
            business_model=BusinessModelDetail(industry=company_data.industry),
        )

        context_record = CompanyContextModel(
            company_id=new_company.id,
            context_data={},
            canonical_data=initial_canonical.model_dump(),
        )
        db.add(context_record)
        await db.commit()
        logger.info(f"L1 Canonical auto-initialized for company {new_company.id}")
    except Exception as e:
        logger.error(f"Failed to auto-init L1 for company {new_company.id}: {e}")
        # Non-blocking — company is created, context will be lazy-initialized later

    return new_company

@router.get("/me", response_model=UserResponse)
async def read_users_me(
    _: dict = Depends(verify_token),
    current_user: User = Depends(get_current_user)
):
    return current_user
