from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from typing import List
import uuid

from app.database import get_db
from app.models import Department, User, Company
from app.schemas import DepartmentCreate, DepartmentUpdate, DepartmentResponse, DepartmentBudgetUpdate
from app.api.deps import get_current_user

router = APIRouter()

@router.post("", response_model=DepartmentResponse, status_code=status.HTTP_201_CREATED)
async def create_department(
    department_in: DepartmentCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """
    Create a new department for the user's organization.
    """
    if not current_user.company_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User is not associated with any organization."
        )

    # Note: organization_id mapped as company_id based on Multi-Tenancy model
    department = Department(
        company_id=current_user.company_id,
        name=department_in.name,
        monthly_limit=department_in.monthly_limit
    )
    db.add(department)
    await db.commit()
    await db.refresh(department)
    return department

@router.get("", response_model=List[DepartmentResponse])
async def get_departments(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """
    Retrieve all departments within the user's current organization.
    """
    if not current_user.company_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User is not associated with any organization."
        )

    result = await db.execute(
        select(Department)
        .where(Department.company_id == current_user.company_id)
        .order_by(Department.name)
    )
    return result.scalars().all()

from datetime import datetime
from sqlalchemy import func
from app.models import Invoice
from decimal import Decimal
from app.schemas import DepartmentBudgetResponse

@router.get("/budgets", response_model=List[DepartmentBudgetResponse])
async def get_departments_budgets(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    if not current_user.company_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User is not associated with any organization."
        )

    # All departments for the company
    dept_result = await db.execute(
        select(Department).where(Department.company_id == current_user.company_id)
    )
    departments = dept_result.scalars().all()

    now = datetime.utcnow()
    start_of_month = datetime(now.year, now.month, 1)

    budgets = []
    for dept in departments:
        # Sum spent
        inv_result = await db.execute(
            select(func.sum(Invoice.net_amount)).where(
                Invoice.department_id == dept.id,
                Invoice.company_id == current_user.company_id,
                Invoice.issue_date >= start_of_month
            )
        )
        total_spent = inv_result.scalar_one_or_none() or Decimal(0)
        
        budgets.append({
            "id": dept.id,
            "name": dept.name,
            "monthly_limit": dept.monthly_limit,
            "current_spent": total_spent
        })

    return budgets



from app.schemas import CompanyBudgetResponse, CompanyBudgetUpdate

@router.get("/company-budget", response_model=CompanyBudgetResponse)
async def get_company_budget(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """
    Retrieve the total global budget for the user's company.
    """
    if not current_user.company_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User is not associated with any organization."
        )

    result = await db.execute(
        select(Company).where(Company.id == current_user.company_id)
    )
    company = result.scalars().first()
    
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    return {"total_budget": company.total_budget}

@router.patch("/company-budget", response_model=CompanyBudgetResponse)
async def update_company_budget(
    budget_update: CompanyBudgetUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """
    Update the total global budget for the user's company.
    """
    if not current_user.company_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User is not associated with any organization."
        )

    result = await db.execute(
        select(Company).where(Company.id == current_user.company_id)
    )
    company = result.scalars().first()
    
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    company.total_budget = budget_update.total_budget
    await db.commit()
    await db.refresh(company)

    return {"total_budget": company.total_budget}


from app.schemas import AiBlueprintRequest

@router.post("/generate-blueprint", status_code=status.HTTP_200_OK)
async def generate_budget_blueprint(
    request: AiBlueprintRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """
    Inteligently maps a total budget to existing organizational structures
    using AI-like keywords matching.
    """
    if not current_user.company_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User is not associated with any organization."
        )

    # Fetch existing departments for this company
    result = await db.execute(
        select(Department)
        .where(Department.company_id == current_user.company_id)
    )
    existing_departments = result.scalars().all()

    if not existing_departments:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Musisz najpierw utworzyć działy/oddziały firmy"
        )

    # Keyword mapping weights
    keyword_weights = [
        (["marketing", "sprzedaż", "sprzedaz", "handlowy"], Decimal("0.20")),
        (["it", "infrastruktura", "technologia"], Decimal("0.15")),
        (["kadry", "płace", "place", "hr"], Decimal("0.40")),
        (["administracja", "prawny", "biuro"], Decimal("0.10")),
    ]

    matched_depts = []
    unmatched_depts = []
    total_assigned_weight = Decimal("0.0")

    for dept in existing_departments:
        name_lower = dept.name.lower()
        assigned_w = None
        for keywords, weight in keyword_weights:
            if any(kw in name_lower for kw in keywords):
                assigned_w = weight
                break
        
        if assigned_w is not None:
            matched_depts.append((dept, assigned_w))
            total_assigned_weight += assigned_w
        else:
            unmatched_depts.append(dept)

    # Normalize if exceeding 1.0 (100%)
    if total_assigned_weight > Decimal("1.0"):
        for i in range(len(matched_depts)):
            dept, w = matched_depts[i]
            matched_depts[i] = (dept, w / total_assigned_weight)
        total_assigned_weight = Decimal("1.0")

    remaining_weight = Decimal("1.0") - total_assigned_weight

    if unmatched_depts and remaining_weight > Decimal("0.0"):
        weight_per_unmatched = remaining_weight / Decimal(len(unmatched_depts))
        for dept in unmatched_depts:
            matched_depts.append((dept, weight_per_unmatched))
    elif unmatched_depts and remaining_weight <= Decimal("0.0"):
        for dept in unmatched_depts:
            matched_depts.append((dept, Decimal("0.0")))
    else:
        if remaining_weight > Decimal("0.0") and matched_depts:
            # Sort by weight descending to add remaining weight to the largest department
            matched_depts.sort(key=lambda x: x[1], reverse=True)
            dept, w = matched_depts[0]
            matched_depts[0] = (dept, w + remaining_weight)
            
    # Calculate limits from total_budget
    total_budget = Decimal(str(request.total_budget))
    allocated_total = Decimal("0.0")

    # Sort matching to make largest weight first for rounding difference
    matched_depts.sort(key=lambda x: x[1], reverse=True)

    for dept, w in matched_depts:
        limit = (total_budget * w).quantize(Decimal("0.01"))
        allocated_total += limit
        dept.monthly_limit = limit

    # Fix rounding difference (can be positive or negative)
    diff = total_budget - allocated_total
    if diff != Decimal("0.0") and matched_depts:
        matched_depts[0][0].monthly_limit += diff

    await db.commit()
    return {"message": "Blueprint successfully generated and applied to existing departments."}

@router.patch("/{department_id}/budget", response_model=DepartmentResponse)
async def update_department_budget(
    department_id: uuid.UUID,
    budget_update: DepartmentBudgetUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    if not current_user.company_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="User is not associated with any organization.")

    result = await db.execute(
        select(Department)
        .where(
            Department.id == department_id,
            Department.company_id == current_user.company_id
        )
    )
    department = result.scalars().first()

    if not department:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Department not found.")

    department.monthly_limit = budget_update.monthly_limit
    await db.commit()
    await db.refresh(department)
    return department

from app.schemas import DepartmentInvoiceResponse
@router.get("/{department_id}/invoices", response_model=List[DepartmentInvoiceResponse])
async def get_department_invoices(
    department_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    if not current_user.company_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="User is not associated with any organization.")

    now = datetime.utcnow()
    start_of_month = datetime(now.year, now.month, 1)

    result = await db.execute(
        select(Invoice)
        .where(
            Invoice.department_id == department_id,
            Invoice.company_id == current_user.company_id,
            Invoice.status.in_(["approved", "audited", "paid"]),
            Invoice.issue_date >= start_of_month
        )
        .order_by(Invoice.issue_date.desc())
    )
    
    return result.scalars().all()

@router.patch("/{department_id}", response_model=DepartmentResponse)
async def update_department(
    department_id: uuid.UUID,
    department_update: DepartmentUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """
    Update a specific department's name or monthly limit.
    Enforces organization multi-tenancy rules.
    """
    if not current_user.company_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User is not associated with any organization."
        )

    result = await db.execute(
        select(Department)
        .where(
            Department.id == department_id,
            Department.company_id == current_user.company_id
        )
    )
    department = result.scalars().first()

    if not department:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Department not found.")

    update_data = department_update.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(department, field, value)

    await db.commit()
    await db.refresh(department)
    return department

@router.delete("/{department_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_department(
    department_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """
    Delete a specific department ensuring it belongs to current user's organization.
    """
    if not current_user.company_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User is not associated with any organization."
        )

    result = await db.execute(
        select(Department)
        .where(
            Department.id == department_id,
            Department.company_id == current_user.company_id
        )
    )
    department = result.scalars().first()

    if not department:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Department not found.")

    await db.delete(department)
    await db.commit()
    return None
