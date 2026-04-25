from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from typing import List
import uuid

from app.database import get_db
from app.models import Department, User, Company
from app.schemas import DepartmentCreate, DepartmentUpdate, DepartmentResponse, DepartmentBudgetUpdate
from app.api.deps import get_current_user
from app.auth import verify_token

router = APIRouter(dependencies=[Depends(verify_token)])

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
    period: str = "month",  # "month" | "quarter" | "year" | "all"
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """
    period:
      month   = biezacy miesiac kalendarzowy (default — semantyka budzetow miesiecznych)
      quarter = ostatnie 3 miesiace
      year    = biezacy rok kalendarzowy
      all     = wszystkie faktury
    Niezaleznie od period, `department_type` zawsze klasyfikuje sie na bazie 90 dni
    (zeby jednoryzowe faktury nie zmienily typu).
    """
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

    from datetime import timedelta

    now = datetime.utcnow()
    window_90 = now - timedelta(days=90)

    # Wybor okna dla current_spent/current_earned
    period_start: datetime | None
    if period == "quarter":
        # ostatnie 3 miesiace
        month = now.month - 2 if now.month > 2 else 1
        year = now.year if now.month > 2 else now.year - 1
        period_start = datetime(year, month, 1)
    elif period == "year":
        period_start = datetime(now.year, 1, 1)
    elif period == "all":
        period_start = None
    else:  # "month" (default)
        period_start = datetime(now.year, now.month, 1)

    # Okno wybrane — splitted per typ
    period_filters = [
        Invoice.company_id == current_user.company_id,
        Invoice.department_id.isnot(None),
    ]
    if period_start is not None:
        period_filters.append(Invoice.issue_date >= period_start)

    period_rows = await db.execute(
        select(
            Invoice.department_id,
            Invoice.invoice_type,
            func.coalesce(func.sum(Invoice.net_amount), 0).label("total"),
        )
        .where(*period_filters)
        .group_by(Invoice.department_id, Invoice.invoice_type)
    )
    month_map: dict[tuple[str, str], Decimal] = {}
    for r in period_rows.all():
        month_map[(str(r.department_id), r.invoice_type or "expense")] = Decimal(str(r.total or 0))

    # 90 dni — do klasyfikacji typu oddzialu
    window_rows = await db.execute(
        select(
            Invoice.department_id,
            Invoice.invoice_type,
            func.coalesce(func.sum(Invoice.net_amount), 0).label("total"),
        )
        .where(
            Invoice.company_id == current_user.company_id,
            Invoice.issue_date >= window_90,
            Invoice.department_id.isnot(None),
        )
        .group_by(Invoice.department_id, Invoice.invoice_type)
    )
    window_map: dict[str, dict[str, Decimal]] = {}
    for r in window_rows.all():
        d_id = str(r.department_id)
        window_map.setdefault(d_id, {"expense": Decimal(0), "income": Decimal(0)})
        window_map[d_id][r.invoice_type or "expense"] = Decimal(str(r.total or 0))

    def classify(exp: Decimal, inc: Decimal) -> str:
        total = exp + inc
        if total <= 0:
            return "empty"
        exp_share = float(exp) / float(total) if total else 0.0
        inc_share = float(inc) / float(total) if total else 0.0
        if inc_share >= 0.70:
            return "revenue"
        if exp_share >= 0.70:
            return "cost"
        return "mixed"

    budgets = []
    for dept in departments:
        d_id = str(dept.id)
        spent_expense = month_map.get((d_id, "expense"), Decimal(0))
        earned_income = month_map.get((d_id, "income"), Decimal(0))
        w = window_map.get(d_id, {"expense": Decimal(0), "income": Decimal(0)})
        dept_type = classify(w["expense"], w["income"])

        budgets.append({
            "id": dept.id,
            "name": dept.name,
            "monthly_limit": dept.monthly_limit,
            "current_spent": spent_expense,
            "current_earned": earned_income,
            "department_type": dept_type,
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

from app.schemas import DepartmentInvoiceResponse, InvoiceResponse
@router.get("/{department_id}/invoices", response_model=List[InvoiceResponse])
async def get_department_invoices(
    department_id: uuid.UUID,
    period: str = "month",  # "month" | "quarter" | "year" | "all"
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """
    Wszystkie faktury przypisane do działu.
    period: 'month' (default) | 'quarter' (ostatnie 3 msc) | 'year' (bieżący rok) | 'all' (wszystko)
    """
    if not current_user.company_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="User is not associated with any organization.")

    now = datetime.utcnow()
    filters = [
        Invoice.department_id == department_id,
        Invoice.company_id == current_user.company_id,
    ]

    if period == "month":
        filters.append(Invoice.issue_date >= datetime(now.year, now.month, 1))
    elif period == "quarter":
        month = now.month - 2 if now.month > 2 else 1
        year = now.year if now.month > 2 else now.year - 1 if now.month <= 2 else now.year
        filters.append(Invoice.issue_date >= datetime(year, month, 1))
    elif period == "year":
        filters.append(Invoice.issue_date >= datetime(now.year, 1, 1))
    # "all" — brak filtra dat

    result = await db.execute(
        select(Invoice)
        .where(*filters)
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


# ── BULK AI ASSIGN: nieprzypisane faktury → istniejące oddziały ──────────
import os
import json
import logging

logger_bulk = logging.getLogger("auditra.departments.bulk_ai")


@router.post("/ai-assign-unassigned")
async def ai_assign_unassigned_invoices(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Masowe przypisanie przez AI wszystkich nieprzypisanych faktur (department_id IS NULL)
    do JEDNEGO z ISTNIEJĄCYCH oddziałów. AI dostaje Opis + Nazwa kontrahenta i listę
    oddziałów, które REALNIE istnieją w firmie klienta. NIE tworzy nowych działów.
    """
    if not current_user.company_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User is not associated with any organization.",
        )

    # 1) Istniejące oddziały — źródło prawdy dla AI
    dept_result = await db.execute(
        select(Department).where(Department.company_id == current_user.company_id)
    )
    departments = dept_result.scalars().all()

    if not departments:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Brak zdefiniowanych oddziałów. Utwórz najpierw oddziały, do których AI ma przypisać faktury.",
        )

    dept_by_name = {d.name.strip().lower(): d for d in departments}
    dept_names_real = [d.name for d in departments]

    # 2) Nieprzypisane faktury
    inv_result = await db.execute(
        select(Invoice).where(
            Invoice.company_id == current_user.company_id,
            Invoice.department_id.is_(None),
        )
    )
    unassigned = inv_result.scalars().all()

    if not unassigned:
        return {
            "assigned_count": 0,
            "skipped_count": 0,
            "total_candidates": 0,
            "assignments": [],
            "message": "Brak nieprzypisanych faktur.",
        }

    # 3) Payload dla AI — Opis + Nazwa kontrahenta (zgodnie z wymaganiem)
    invoice_payload = []
    for inv in unassigned:
        description_parts = []
        if inv.category_name:
            description_parts.append(inv.category_name)
        if isinstance(inv.parsed_data, dict):
            items = inv.parsed_data.get("items") or []
            if isinstance(items, list):
                for it in items[:5]:
                    if isinstance(it, dict):
                        name = it.get("name") or it.get("description") or ""
                        if name:
                            description_parts.append(str(name))
            desc = inv.parsed_data.get("description")
            if desc:
                description_parts.append(str(desc))
        opis = " | ".join(p for p in description_parts if p) or "Brak opisu"

        invoice_type_pl = "PRZYCHÓD (sprzedaż)" if inv.invoice_type == "income" else "KOSZT (zakup)"
        invoice_payload.append({
            "id": str(inv.id),
            "type": invoice_type_pl,
            "contractor_name": inv.contractor_name or "Nieznany kontrahent",
            "description": opis[:500],
            "net_amount": float(inv.net_amount) if inv.net_amount else 0.0,
        })

    # 4) Gemini
    from google import genai
    from google.genai import types as genai_types

    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise HTTPException(
            status_code=500,
            detail="GOOGLE_API_KEY nie skonfigurowany — nie można uruchomić przypisania AI.",
        )
    client = genai.Client(api_key=api_key)

    prompt = f"""Jesteś Auditra AI. Twoim zadaniem jest przypisać każdą z poniższych faktur do JEDNEGO z oddziałów, które REALNIE istnieją w firmie klienta.

### ODDZIAŁY KTÓRE REALNIE ISTNIEJĄ (WYBIERZ WYŁĄCZNIE Z TEJ LISTY, DOKŁADNA NAZWA):
{json.dumps(dept_names_real, ensure_ascii=False, indent=2)}

### ZASADY (ŚCISŁE):
1. `department_name` MUSI być dokładną nazwą z listy powyżej (case-sensitive, bez modyfikacji).
2. NIE TWÓRZ nowych nazw oddziałów, NIE sugeruj nowych. Wybierasz wyłącznie z listy.
3. Jeśli faktura kompletnie nie pasuje do żadnego istniejącego oddziału → zwróć `department_name: null`.
4. Decyzję podejmuj na podstawie pól `type`, `contractor_name` i `description`.
5. Krótkie uzasadnienie (1 zdanie) w polu `reason`.

### ZASADY WG TYPU FAKTURY:
- **KOSZT (zakup)** → przypisz do oddziału, który KORZYSTA z zakupu (np. laptopy → IT, szkolenie sprzedażowe → Sprzedaż, paliwo → Flota, surowce → Produkcja).
- **PRZYCHÓD (sprzedaż)** → przypisz do oddziału, który WYGENEROWAŁ tę sprzedaż (zwykle Sprzedaż B2B / B2C, lub konkretny oddział produktowy / usługowy, jeśli taki widnieje na liście).
- Jeśli typu nie da się jednoznacznie powiązać z żadnym oddziałem (np. ogólne koszty korporacyjne, zwroty, prowizje bankowe) → `null`.

### FAKTURY DO PRZYPISANIA:
{json.dumps(invoice_payload, ensure_ascii=False, indent=2)}

### WYMAGANY FORMAT ODPOWIEDZI (STRICT JSON, bez markdown):
{{
  "assignments": [
    {{"invoice_id": "<uuid faktury>", "department_name": "<DOKŁADNA nazwa z listy LUB null>", "reason": "<krótkie uzasadnienie>"}}
  ]
}}
"""

    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=genai_types.GenerateContentConfig(
                temperature=0.0,
                response_mime_type="application/json",
            ),
        )
        raw = response.text or ""
        if "```json" in raw:
            raw = raw.split("```json", 1)[1].split("```", 1)[0]
        elif "```" in raw:
            raw = raw.split("```", 1)[1].split("```", 1)[0]
        parsed = json.loads(raw.strip())
    except Exception as e:
        logger_bulk.error(f"Gemini bulk-assign failed: {e}")
        raise HTTPException(status_code=502, detail=f"Błąd modelu AI: {e}")

    assignments_raw = parsed.get("assignments", []) if isinstance(parsed, dict) else []
    inv_by_id = {str(inv.id): inv for inv in unassigned}

    applied = []
    skipped = []

    for item in assignments_raw:
        if not isinstance(item, dict):
            continue
        inv_id = str(item.get("invoice_id") or "").strip()
        dept_name_raw = item.get("department_name")
        reason = (item.get("reason") or "")[:300]

        inv = inv_by_id.get(inv_id)
        if not inv:
            continue

        if not dept_name_raw:
            skipped.append({"invoice_id": inv_id, "reason": reason or "AI: brak dopasowania"})
            continue

        dept = dept_by_name.get(str(dept_name_raw).strip().lower())
        if not dept:
            skipped.append({
                "invoice_id": inv_id,
                "reason": f"AI zwróciło nieistniejący oddział '{dept_name_raw}'",
            })
            continue

        inv.department_id = dept.id
        inv.category_source = "ai_inferred"
        applied.append({
            "invoice_id": inv_id,
            "department_id": str(dept.id),
            "department_name": dept.name,
            "reason": reason,
        })

    if applied:
        await db.commit()

    return {
        "assigned_count": len(applied),
        "skipped_count": len(skipped),
        "total_candidates": len(unassigned),
        "assignments": applied,
        "skipped": skipped,
    }


# ── AI PROPOSE STRUCTURE (read-only) + CONFIRM ───────────────────────────
# Owner otrzymuje liste proponowanych oddzialow na podstawie wszystkich
# faktur w firmie. Owner decyduje co stworzyc. Dedup w 2 warstwach:
#   (1) w promcie AI (nazwy kanoniczne + zakaz duplikatow)
#   (2) w backendzie (case-insensitive match vs istniejace + normalizacja).

from app.schemas import DepartmentCreate as _DepartmentCreate  # re-use existing schema

# Slownik kanonicznych nazw (spojne z audit_service.py)
_CANONICAL_DEPT_TEMPLATES = [
    "Marketing", "Sprzedaż B2B", "Sprzedaż B2C", "IT / Infrastruktura",
    "Obsługa Klienta", "Kadry i Płace", "Rekrutacja", "Zarząd i Administracja",
    "Księgowość i Finanse", "Prawne i Compliance", "Flota i Logistyka",
    "Badania i Rozwój (R&D)", "Zaopatrzenie i Zakupy", "PR i Komunikacja",
    "Utrzymanie Ruchu", "Szkolenia Wewnętrzne", "Bezpieczeństwo IT",
    "Obsługa Posprzedażowa", "Zarządzanie Projektami", "Inwestycje",
    "Magazyn", "Ubezpieczenia", "Infrastruktura Biurowa",
    "Zarządzanie Jakością (QA)", "Import / Eksport", "Content Marketing",
    "Audyt Wewnętrzny", "Employer Branding", "Dział Windykacji",
    "Rozwój Biznesu (Business Dev)", "Produkcja",
]


def _normalize_dept_name(name: str) -> str:
    """Normalizacja do porownywania duplikatow: lowercase, trim, usun spacje podwojne."""
    import re as _re
    s = (name or "").strip().lower()
    # ujednolic separatory " / " "-" "i"
    s = _re.sub(r"\s+", " ", s)
    s = s.replace(" / ", " i ").replace("/", " i ").replace(" - ", " i ")
    # usun nawiasy z dopiskami
    s = _re.sub(r"\s*\([^)]*\)", "", s).strip()
    return s


@router.post("/ai-propose-structure")
async def ai_propose_structure(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    AI analizuje wszystkie faktury firmy i proponuje oddzialy do utworzenia.
    READ-ONLY — nic nie zapisuje. Owner dostaje liste do zatwierdzenia.
    Gwarancje anty-duplikat:
      - istniejace oddzialy sa w promcie i wykluczone z propozycji
      - AI dostaje kanoniczny slownik nazw (zapobiega "Księgowość" + "Finanse")
      - normalizacja po stronie backendu laczy synonimy przed zwrotem
    """
    if not current_user.company_id:
        raise HTTPException(status_code=400, detail="Brak powiazania z firma.")

    # Istniejace oddzialy — wykluczamy je z propozycji
    dept_result = await db.execute(
        select(Department).where(Department.company_id == current_user.company_id)
    )
    existing_departments = dept_result.scalars().all()
    existing_names = [d.name for d in existing_departments]
    existing_norm = {_normalize_dept_name(d.name) for d in existing_departments}

    # Faktury — wszystkie, bo AI potrzebuje pelnego obrazu
    inv_result = await db.execute(
        select(Invoice).where(Invoice.company_id == current_user.company_id)
    )
    invoices = inv_result.scalars().all()

    if not invoices:
        return {
            "proposed": [],
            "existing": existing_names,
            "total_invoices": 0,
            "message": "Brak faktur — nie mogę zaproponować struktury.",
        }

    # Payload — contractor + description + type + net (AI potrzebuje skali)
    invoice_payload = []
    for inv in invoices:
        desc_parts = []
        if inv.category_name:
            desc_parts.append(inv.category_name)
        if isinstance(inv.parsed_data, dict):
            items = inv.parsed_data.get("items") or []
            if isinstance(items, list):
                for it in items[:3]:
                    if isinstance(it, dict):
                        nm = it.get("name") or it.get("description") or ""
                        if nm:
                            desc_parts.append(str(nm))
        opis = " | ".join(p for p in desc_parts if p) or "Brak opisu"
        invoice_payload.append({
            "contractor_name": inv.contractor_name or "Nieznany",
            "description": opis[:250],
            "type": "PRZYCHÓD" if inv.invoice_type == "income" else "KOSZT",
            "net": float(inv.net_amount) if inv.net_amount else 0.0,
        })

    # Gemini
    from google import genai
    from google.genai import types as genai_types

    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise HTTPException(status_code=500, detail="GOOGLE_API_KEY nie skonfigurowany.")
    client = genai.Client(api_key=api_key)

    prompt = f"""Jesteś Auditra AI — architektem struktury organizacyjnej MSP w Polsce.

## KONTEKST
Firma ma {len(invoices)} faktur i potrzebuje sensownej struktury oddziałów.
Owner zatwierdzi Twoją propozycję.

## ODDZIAŁY, KTÓRE JUŻ ISTNIEJĄ (NIE DUPLIKUJ):
{json.dumps(existing_names, ensure_ascii=False, indent=2)}

## KANONICZNY SŁOWNIK NAZW (PREFERUJ TE NAZWY, zapobiegaj synonimom):
{json.dumps(_CANONICAL_DEPT_TEMPLATES, ensure_ascii=False, indent=2)}

## FAKTURY (PEŁNY OBRAZ DZIAŁALNOŚCI):
{json.dumps(invoice_payload, ensure_ascii=False, indent=2)}

## ZASADY (ŚCISŁE, ŁAM = BŁĄD KRYTYCZNY):

### 1. ANTY-DUPLIKAT (NAJWAŻNIEJSZE)
- Każdy obszar biznesowy = DOKŁADNIE JEDNA nazwa oddziału.
- ZABRONIONE PARY (wybierz JEDNĄ nazwę z pary, nie obie):
  * "Księgowość" + "Finanse" + "Księgowość i Finanse" → tylko **"Księgowość i Finanse"**
  * "Marketing" + "Marketing Online" + "Content Marketing" → tylko **"Marketing"** (chyba że faktur marketingu online jest >10 i uzasadniają osobny oddział)
  * "IT" + "Infrastruktura IT" + "Dev" → tylko **"IT / Infrastruktura"**
  * "Kadry" + "HR" + "Płace" → tylko **"Kadry i Płace"**
  * "Sprzedaż" + "Handel" + "Dział Handlowy" → tylko **"Sprzedaż B2B"** lub **"Sprzedaż B2C"**
- NIE PROPONUJ oddziału, który już istnieje (patrz lista wyżej).

### 2. SKALA I ZDROWY ROZSĄDEK
- Minimum {max(3, min(5, len(invoices)//10 or 3))} oddziałów, maksimum 10.
- Każdy proponowany oddział musi mieć ≥2 faktury, które do niego pasują (inaczej nie twórz).
- Preferuj nazwy z KANONICZNEGO SŁOWNIKA. Własne nazwy tylko gdy słownik kompletnie nie pasuje.

### 3. TYPY FAKTUR
- KOSZT → oddział, który korzysta z zakupu.
- PRZYCHÓD → oddział, który wygenerował sprzedaż (zwykle "Sprzedaż B2B"/"Sprzedaż B2C").

### 4. WYJAŚNIENIE (dla ownera)
Dla każdego proponowanego oddziału:
- `reason`: po polsku, 1 zdanie, dlaczego firma go potrzebuje
- `invoice_count`: ile faktur do niego trafi (oszacuj na podstawie listy)
- `example_contractors`: 3 przykładowi kontrahenci z listy (skrócone nazwy)
- `estimated_monthly_spend`: szacunkowa kwota miesięczna w PLN (netto, suma faktur kosztowych / liczba miesięcy, 0 dla sprzedażowych)

## FORMAT ODPOWIEDZI (STRICT JSON):
{{
  "proposed": [
    {{
      "name": "Marketing",
      "reason": "Firma wydaje regularnie na reklamę Google i Facebook — potrzebuje osobnej kontroli budżetu marketingowego.",
      "invoice_count": 9,
      "example_contractors": ["Google Ireland", "Meta Platforms", "Canva"],
      "estimated_monthly_spend": 4500
    }}
  ]
}}
"""

    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=genai_types.GenerateContentConfig(
                temperature=0.0,
                response_mime_type="application/json",
            ),
        )
        raw = response.text or ""
        if "```json" in raw:
            raw = raw.split("```json", 1)[1].split("```", 1)[0]
        elif "```" in raw:
            raw = raw.split("```", 1)[1].split("```", 1)[0]
        parsed = json.loads(raw.strip())
    except Exception as e:
        logger_bulk.error(f"ai-propose-structure: Gemini failed: {e}")
        raise HTTPException(status_code=502, detail=f"Błąd modelu AI: {e}")

    raw_proposed = parsed.get("proposed", []) if isinstance(parsed, dict) else []

    # Warstwa 2: backend-side dedup
    seen_norm: set[str] = set()
    cleaned = []
    for p in raw_proposed:
        if not isinstance(p, dict):
            continue
        name = str(p.get("name") or "").strip()
        if not name:
            continue
        norm = _normalize_dept_name(name)
        if not norm:
            continue
        if norm in existing_norm:
            continue  # juz istnieje
        if norm in seen_norm:
            continue  # duplikat w propozycji
        seen_norm.add(norm)
        try:
            inv_cnt = int(p.get("invoice_count") or 0)
        except Exception:
            inv_cnt = 0
        try:
            est_spend = float(p.get("estimated_monthly_spend") or 0)
        except Exception:
            est_spend = 0.0
        examples = p.get("example_contractors") or []
        if not isinstance(examples, list):
            examples = []
        examples = [str(e)[:80] for e in examples[:3]]
        cleaned.append({
            "name": name,
            "reason": str(p.get("reason") or "")[:400],
            "invoice_count": inv_cnt,
            "example_contractors": examples,
            "estimated_monthly_spend": est_spend,
        })

    return {
        "proposed": cleaned,
        "existing": existing_names,
        "total_invoices": len(invoices),
    }


from pydantic import BaseModel as _BaseModel


class _ConfirmStructureRequest(_BaseModel):
    approved_names: List[str]


@router.post("/ai-propose-structure/confirm")
async def ai_propose_structure_confirm(
    payload: _ConfirmStructureRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Owner zatwierdza wybrane oddzialy z propozycji AI.
    Tworzy tylko te, ktorych jeszcze nie ma (idempotentny, anty-duplikat).
    """
    if not current_user.company_id:
        raise HTTPException(status_code=400, detail="Brak powiazania z firma.")

    if not payload.approved_names:
        raise HTTPException(status_code=400, detail="Pusta lista zatwierdzonych oddzialow.")

    # Pobierz istniejace raz, do dedup
    dept_result = await db.execute(
        select(Department).where(Department.company_id == current_user.company_id)
    )
    existing = {_normalize_dept_name(d.name): d for d in dept_result.scalars().all()}

    created = []
    skipped_duplicates = []
    seen_batch: set[str] = set()

    for raw_name in payload.approved_names:
        name = (raw_name or "").strip()
        if not name:
            continue
        norm = _normalize_dept_name(name)
        if not norm:
            continue
        if norm in existing:
            skipped_duplicates.append({"name": name, "reason": "juz istnieje"})
            continue
        if norm in seen_batch:
            skipped_duplicates.append({"name": name, "reason": "duplikat w zatwierdzonej liscie"})
            continue
        seen_batch.add(norm)

        dept = Department(
            company_id=current_user.company_id,
            name=name,
        )
        db.add(dept)
        await db.flush()
        existing[norm] = dept  # chroni przed kolejnym duplikatem w tej samej petli
        created.append({"id": str(dept.id), "name": dept.name})

    if created:
        await db.commit()

    return {
        "created_count": len(created),
        "skipped_duplicate_count": len(skipped_duplicates),
        "created": created,
        "skipped": skipped_duplicates,
    }


# ── AI SUGGEST BUDGETS (per department) + APPLY ──────────────────────────
# AI analizuje historie wydatkow kazdego oddzialu i proponuje sensowny
# limit miesieczny. Owner moze edytowac przed zatwierdzeniem.

from pydantic import Field as _Field
from datetime import timedelta as _timedelta


class _SuggestBudgetsRequest(_BaseModel):
    total_budget: float | None = None  # opcjonalnie: docelowa suma do rozdzielenia


@router.post("/ai-suggest-budgets")
async def ai_suggest_budgets(
    payload: _SuggestBudgetsRequest | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    AI sugeruje limit miesieczny DLA KAZDEGO oddzialu osobno.
    - Bazuje na historii wydatkow (ostatnie 3 miesiace) i charakterystyce dzialu.
    - Jesli podano `total_budget`, AI rozdziela go tak, zeby suma limitow byla <= total_budget.
    - Jesli nie podano, AI sugeruje sensowne limity niezaleznie (na podstawie zuzycia + buforu).
    READ-ONLY.
    """
    if not current_user.company_id:
        raise HTTPException(status_code=400, detail="Brak powiazania z firma.")

    dept_result = await db.execute(
        select(Department).where(Department.company_id == current_user.company_id)
    )
    departments = dept_result.scalars().all()
    if not departments:
        raise HTTPException(
            status_code=400,
            detail="Brak oddzialow. Utworz je najpierw (np. przez 'Zaproponuj oddzialy z AI').",
        )

    # Historia 90 dni — KOSZTY i PRZYCHODY osobno
    now_dt = datetime.utcnow()
    window_start = now_dt - _timedelta(days=90)

    history_rows = await db.execute(
        select(
            Invoice.department_id,
            Invoice.invoice_type,
            func.coalesce(func.sum(Invoice.net_amount), 0).label("total_net"),
            func.count(Invoice.id).label("invoice_count"),
        )
        .where(
            Invoice.company_id == current_user.company_id,
            Invoice.issue_date >= window_start,
            Invoice.department_id.isnot(None),
        )
        .group_by(Invoice.department_id, Invoice.invoice_type)
    )
    history: dict[str, dict[str, dict]] = {}
    for row in history_rows.all():
        d_id = str(row.department_id)
        t = row.invoice_type or "expense"
        history.setdefault(d_id, {"expense": {}, "income": {}})
        history[d_id][t] = {
            "total": float(row.total_net or 0),
            "count": int(row.invoice_count or 0),
        }

    def _classify(exp_total: float, inc_total: float) -> str:
        total = exp_total + inc_total
        if total <= 0:
            return "empty"
        if inc_total / total >= 0.70:
            return "revenue"
        if exp_total / total >= 0.70:
            return "cost"
        return "mixed"

    dept_payload = []
    for d in departments:
        h = history.get(str(d.id), {"expense": {}, "income": {}})
        exp_total = h.get("expense", {}).get("total", 0.0)
        inc_total = h.get("income", {}).get("total", 0.0)
        exp_count = h.get("expense", {}).get("count", 0)
        inc_count = h.get("income", {}).get("count", 0)
        dept_payload.append({
            "id": str(d.id),
            "name": d.name,
            "current_limit": float(d.monthly_limit) if d.monthly_limit else 0.0,
            "type": _classify(exp_total, inc_total),
            "avg_monthly_expense_3m": round(exp_total / 3.0, 2) if exp_total else 0.0,
            "avg_monthly_income_3m": round(inc_total / 3.0, 2) if inc_total else 0.0,
            "expense_invoice_count_3m": exp_count,
            "income_invoice_count_3m": inc_count,
        })

    total_budget = payload.total_budget if payload and payload.total_budget and payload.total_budget > 0 else None
    # Budzet dotyczy TYLKO kosztow — wiec sumujemy tylko koszty z oddzialow kosztowych/mixed
    total_current_avg = sum(
        d["avg_monthly_expense_3m"] for d in dept_payload if d["type"] in ("cost", "mixed", "empty")
    )

    budget_section = (
        f"## DOCELOWA KWOTA KOSZTOWA DO ROZDZIELENIA (MIESIĘCZNIE): {total_budget:.2f} PLN\nSuma `suggested_monthly_limit` dla oddziałów KOSZTOWYCH i MIXED musi się zmieścić w tej kwocie (±2%). Oddziały PRZYCHODOWE (type='revenue') otrzymują limit 0 — nie wchodzą do budżetu kosztów."
        if total_budget
        else "## BRAK DOCELOWEJ KWOTY\nSugeruj limit niezależnie dla każdego oddziału kosztowego — kieruj się historią kosztów + rozsądnym buforem (15–25%% nad średnią)."
    )

    from google import genai
    from google.genai import types as genai_types

    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise HTTPException(status_code=500, detail="GOOGLE_API_KEY nie skonfigurowany.")
    client = genai.Client(api_key=api_key)

    prompt = f"""Jesteś Auditra AI — CFO firmy MSP w Polsce. Zaproponuj miesięczne limity KOSZTOWE dla każdego oddziału.

{budget_section}

## WAŻNE — LIMIT = LIMIT KOSZTÓW, NIE PRZYCHODÓW
W Auditra `monthly_limit` kontroluje **wydatki** oddziału. Oddziały, które generują głównie przychody
(np. Sprzedaż B2B/B2C), NIE powinny mieć wysokiego limitu kosztowego — ich główna rola to zarabianie,
a nie wydawanie. Dla takich oddziałów:
- `suggested_monthly_limit` = 0 (lub minimalna kwota na drobne koszty operacyjne jeśli mają jakieś wydatki)
- `is_revenue_department`: true
- W `reason` napisz, że to oddział przychodowy, a nie kosztowy.

## ODDZIAŁY (z historią 90 dni — KOSZTY i PRZYCHODY osobno + pole `type`):
{json.dumps(dept_payload, ensure_ascii=False, indent=2)}

Pole `type`:
- "cost"    = dominuja koszty (>=70%% wartosci) → ustaw limit KOSZTOWY na bazie avg_monthly_expense_3m
- "revenue" = dominuja przychody (>=70%%)       → ustaw `suggested_monthly_limit: 0` i `is_revenue_department: true`
- "mixed"   = oba istotne                       → ustaw limit KOSZTOWY tylko dla części kosztowej (avg_monthly_expense_3m + bufor)
- "empty"   = brak historii                     → oszacuj z NAZWY (patrz reguły)

Suma średnich miesięcznych KOSZTÓW (oddziały kosztowe + mixed + puste): **{total_current_avg:.2f} PLN**.

## ZASADY USTALANIA LIMITU (KOSZTOWEGO):
1. **Baza = `avg_monthly_expense_3m`** (średnia koszty/3 mies.). NIGDY nie używaj `avg_monthly_income_3m` do obliczania limitu.
2. **Bufor** nad średnią kosztową:
   - Marketing: +25–35%% (zmienność kampanii)
   - IT / Infrastruktura: +15–20%%
   - Produkcja / Zaopatrzenie: +20–25%%
   - Kadry i Płace, ZUS: +10–15%%
   - Flota: +15–20%%
   - Administracja / Biuro: +10–15%%
   - Prawne, Jednorazowe: +50–100%%
3. **Sprzedaż B2B/B2C (type='revenue')** → `suggested_monthly_limit: 0`, `is_revenue_department: true`.
   Nie szacuj limitu z przychodów. Jeśli oddział sprzedażowy ma drobne koszty (np. CRM, leady), uwzględnij TYLKO avg_monthly_expense_3m + bufor.
4. **Brak historii** (type='empty') → oszacuj z NAZWY:
   - Marketing 3000–8000, IT 2000–6000, Kadry 2000–4000, Księgowość 1500–3000, Flota 3000–7000
   - Sprzedaż* → 0 (revenue) lub 500–1500 jeśli tylko drobne koszty narzędziowe
5. **Zaokrąglaj** do 50 PLN w górę.

## DLA KAŻDEGO oddziału zwróć:
- `department_id`: id (dokładnie z listy)
- `suggested_monthly_limit`: kwota w PLN (liczba)
- `is_revenue_department`: bool (true dla oddziałów przychodowych)
- `reason`: 1 zdanie PL wyjaśniające czemu (powołaj się na avg koszty, bufor, lub fakt że to revenue department)
- `confidence`: "high" (>=2 msc historii kosztów), "medium" (1 msc), "low" (brak/szacunek z nazwy)

## FORMAT (STRICT JSON):
{{
  "suggestions": [
    {{"department_id": "uuid", "suggested_monthly_limit": 4500, "is_revenue_department": false, "reason": "Śr. koszty 3600 PLN/mies + 25%% bufor.", "confidence": "high"}},
    {{"department_id": "uuid2", "suggested_monthly_limit": 0, "is_revenue_department": true, "reason": "Oddział przychodowy (228k przychodów/mies) — limit kosztowy nie ma zastosowania.", "confidence": "high"}}
  ]
}}
"""

    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=genai_types.GenerateContentConfig(
                temperature=0.0,
                response_mime_type="application/json",
            ),
        )
        raw = response.text or ""
        if "```json" in raw:
            raw = raw.split("```json", 1)[1].split("```", 1)[0]
        elif "```" in raw:
            raw = raw.split("```", 1)[1].split("```", 1)[0]
        parsed = json.loads(raw.strip())
    except Exception as e:
        logger_bulk.error(f"ai-suggest-budgets: Gemini failed: {e}")
        raise HTTPException(status_code=502, detail=f"Błąd modelu AI: {e}")

    raw_suggestions = parsed.get("suggestions", []) if isinstance(parsed, dict) else []
    by_id = {p["id"]: p for p in dept_payload}
    valid_ids = set(by_id.keys())

    suggestions_clean = []
    for s in raw_suggestions:
        if not isinstance(s, dict):
            continue
        dept_id = str(s.get("department_id") or "").strip()
        if dept_id not in valid_ids:
            continue
        try:
            limit = float(s.get("suggested_monthly_limit") or 0)
        except Exception:
            limit = 0.0
        limit = max(0.0, round(limit, 2))
        dp = by_id[dept_id]
        # Wymus limit=0 dla oddzialow przychodowych, jesli AI sie pomyli
        is_revenue = bool(s.get("is_revenue_department")) or dp["type"] == "revenue"
        if is_revenue and limit > dp["avg_monthly_expense_3m"] * 1.3:
            # AI dalo za duzo dla oddzialu przychodowego → tnij do realnych kosztow + 20%
            limit = max(0.0, round(dp["avg_monthly_expense_3m"] * 1.20 / 50) * 50) if dp["avg_monthly_expense_3m"] else 0.0
        suggestions_clean.append({
            "department_id": dept_id,
            "department_name": dp["name"],
            "current_limit": dp["current_limit"],
            "department_type": dp["type"],
            "is_revenue_department": is_revenue,
            "avg_monthly_expense_3m": dp["avg_monthly_expense_3m"],
            "avg_monthly_income_3m": dp["avg_monthly_income_3m"],
            "expense_invoice_count_3m": dp["expense_invoice_count_3m"],
            "income_invoice_count_3m": dp["income_invoice_count_3m"],
            "suggested_monthly_limit": limit,
            "reason": str(s.get("reason") or "")[:300],
            "confidence": str(s.get("confidence") or "medium").lower(),
        })

    # Fallback: oddzialy bez odpowiedzi od AI -> heurystyka oparta na typie
    suggested_ids = {s["department_id"] for s in suggestions_clean}
    for dp in dept_payload:
        if dp["id"] not in suggested_ids:
            is_rev = dp["type"] == "revenue"
            if is_rev:
                fallback_limit = 0.0
                fb_reason = "Oddział przychodowy — limit kosztowy nie ma zastosowania (heurystyka)."
            else:
                exp_avg = dp["avg_monthly_expense_3m"]
                fallback_limit = max(500.0, round(exp_avg * 1.20 / 50) * 50) if exp_avg else 2000.0
                fb_reason = "Szacunek heurystyczny (AI nie zwróciło odpowiedzi)."
            suggestions_clean.append({
                "department_id": dp["id"],
                "department_name": dp["name"],
                "current_limit": dp["current_limit"],
                "department_type": dp["type"],
                "is_revenue_department": is_rev,
                "avg_monthly_expense_3m": dp["avg_monthly_expense_3m"],
                "avg_monthly_income_3m": dp["avg_monthly_income_3m"],
                "expense_invoice_count_3m": dp["expense_invoice_count_3m"],
                "income_invoice_count_3m": dp["income_invoice_count_3m"],
                "suggested_monthly_limit": fallback_limit,
                "reason": fb_reason,
                "confidence": "low",
            })

    total_suggested = sum(s["suggested_monthly_limit"] for s in suggestions_clean)

    return {
        "suggestions": suggestions_clean,
        "total_suggested": round(total_suggested, 2),
        "target_budget": total_budget,
        "total_current_avg_spend": round(total_current_avg, 2),
    }


class _BudgetAssignment(_BaseModel):
    department_id: str
    monthly_limit: float = _Field(ge=0)


class _ApplyBudgetsRequest(_BaseModel):
    assignments: List[_BudgetAssignment]


@router.post("/ai-suggest-budgets/apply")
async def ai_suggest_budgets_apply(
    payload: _ApplyBudgetsRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Zastosuj zatwierdzone (i ewentualnie edytowane przez ownera) limity do oddzialow.
    """
    if not current_user.company_id:
        raise HTTPException(status_code=400, detail="Brak powiazania z firma.")

    if not payload.assignments:
        raise HTTPException(status_code=400, detail="Pusta lista przypisan.")

    dept_result = await db.execute(
        select(Department).where(Department.company_id == current_user.company_id)
    )
    by_id = {str(d.id): d for d in dept_result.scalars().all()}

    updated = []
    skipped = []
    for a in payload.assignments:
        dept = by_id.get(str(a.department_id))
        if not dept:
            skipped.append({"department_id": str(a.department_id), "reason": "nie znaleziono oddzialu"})
            continue
        dept.monthly_limit = round(float(a.monthly_limit), 2)
        updated.append({
            "department_id": str(dept.id),
            "name": dept.name,
            "monthly_limit": float(dept.monthly_limit),
        })

    if updated:
        await db.commit()

    return {
        "updated_count": len(updated),
        "skipped_count": len(skipped),
        "updated": updated,
        "skipped": skipped,
    }
