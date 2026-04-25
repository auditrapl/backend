"""
Silnik Cashflow — prognoza, kalendarz podatkowy, DSO kontrahentów.
compute_snapshot() = SINGLE SOURCE OF TRUTH dla metryk finansowych.
Dashboard, AI, Briefing, Insights — wszystko z tego.
"""
import uuid
import time as _time
from datetime import date, datetime, timezone, timedelta
from decimal import Decimal
from typing import Optional, List

from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, case, and_, text
import logging

from app.models import Invoice, CompanyContextModel, Department
from app.services.context_canonical import CanonicalContext, get_or_create_canonical

logger = logging.getLogger(__name__)


# ==============================================================================
# MODELE PYDANTIC
# ==============================================================================

class TaxCalendarEntry(BaseModel):
    """Pojedyncze zobowiązanie podatkowe w kalendarzu."""
    date: date
    tax_type: str                       # "VAT-7" | "CIT-advance" | "ZUS" | "PIT-advance" | ...
    label: str                          # "VAT-7 za luty 2026"
    estimated_amount: Optional[float] = None
    is_paid: bool = False
    source: str = "calendar"            # "calendar" | "manual" | "ai_estimated"
    category: str = "standard"          # "VAT" | "CIT" | "PIT" | "ZUS" | "Zdrowotna" | "JPK" | "Special"


class WeeklyProjection(BaseModel):
    """Prognoza cashflow na jeden tydzień."""
    week_start: date
    week_end: date
    opening_balance: float = 0.0
    expected_income: float = 0.0
    expected_expenses: float = 0.0
    tax_obligations: float = 0.0
    closing_balance: float = 0.0
    confidence: float = 1.0             # 0.0-1.0 (maleje z czasem)


class ContractorReliability(BaseModel):
    """DSO i wiarygodność płatnicza kontrahenta."""
    contractor_name: str
    contractor_nip: Optional[str] = None
    avg_payment_days: Optional[float] = None
    invoices_paid: int = 0
    invoices_pending: int = 0
    total_outstanding: float = 0.0
    reliability_score: str = "unknown"  # "excellent" | "good" | "warning" | "poor" | "unknown"


class CashflowForecast(BaseModel):
    """Kompletna prognoza cashflow firmy."""
    forecast_date: Optional[datetime] = None
    weekly_projections: List[WeeklyProjection] = Field(default_factory=list)
    tax_calendar: List[TaxCalendarEntry] = Field(default_factory=list)
    contractor_reliability: List[ContractorReliability] = Field(default_factory=list)
    safe_burn_limit: Optional[float] = None
    runway_weeks: Optional[int] = None
    direction_score: Optional[float] = None     # -100..+100
    ceo_monthly_goal: Optional[float] = None


# ==============================================================================
# POLSKI KALENDARZ PODATKOWY
# ==============================================================================

_MONTH_NAMES_PL = [
    "", "styczeń", "luty", "marzec", "kwiecień", "maj", "czerwiec",
    "lipiec", "sierpień", "wrzesień", "październik", "listopad", "grudzień"
]


def _add_months(d: date, months: int) -> date:
    """Dodaje miesiące do daty, zwraca 1. dzień nowego miesiąca."""
    month = d.month + months
    year = d.year + (month - 1) // 12
    month = ((month - 1) % 12) + 1
    return date(year, month, 1)


def generate_tax_calendar(
    canonical: CanonicalContext,
    months_ahead: int = 3,
    vat_monthly_estimate: Optional[float] = None,
    cit_annual_estimate: Optional[float] = None,
    zus_monthly: Optional[float] = None,
) -> List[TaxCalendarEntry]:
    """
    Generuje kalendarz podatkowy na N miesięcy do przodu na podstawie L1 Canonical.
    Registry-driven — iteruje po ALL_OBLIGATIONS i generuje wpisy wg applies_to.
    Obsługuje: VAT, CIT, PIT (JDG), ZUS (warianty), składka zdrowotna, ~20 opłat specjalnych.
    """
    from app.services.tax_obligation_registry import (
        ALL_OBLIGATIONS, ZUS_AMOUNTS_2026, ZDROWOTNA_DEFAULTS,
        _is_jdg, _has_employees,
    )

    entries: List[TaxCalendarEntry] = []
    today = date.today()
    tax = canonical.tax_regime
    end_date = _add_months(today, months_ahead)

    # Pre-compute amounts
    cit_monthly = (cit_annual_estimate / 12) if cit_annual_estimate else None

    is_jdg = _is_jdg(canonical)
    has_employees = _has_employees(canonical)

    # ZUS: 15. dla JDG solo, 20. dla pracodawców
    zus_day = 15 if is_jdg and not has_employees else 20

    # ZUS amount from type
    zus_type = tax.zus_type or ("pelny" if is_jdg else "pelny")
    zus_amount = zus_monthly or ZUS_AMOUNTS_2026.get(zus_type)

    # Składka zdrowotna amount
    zdrowotna_amount = ZDROWOTNA_DEFAULTS.get(tax.pit_form or tax.taxation_form or "", None)

    # Amount lookup
    def _get_amount(amount_key: str, ob_id: str) -> Optional[float]:
        if amount_key == "vat":
            return vat_monthly_estimate
        elif amount_key == "vat_quarterly":
            return (vat_monthly_estimate or 0) * 3 if vat_monthly_estimate else None
        elif amount_key == "cit_monthly":
            return cit_monthly
        elif amount_key == "cit_annual":
            return cit_annual_estimate
        elif amount_key == "zus":
            return zus_amount
        elif amount_key == "zdrowotna":
            return zdrowotna_amount
        return None

    # Format label
    def _format_label(template: str, y: int, m: int) -> str:
        prev_m = m - 1 if m > 1 else 12
        prev_month = _MONTH_NAMES_PL[prev_m]
        month = _MONTH_NAMES_PL[m]
        # Quarter label
        q_map = {1: "Q4", 2: "Q1", 3: "Q1", 4: "Q1", 5: "Q2", 6: "Q2",
                 7: "Q2", 8: "Q3", 9: "Q3", 10: "Q3", 11: "Q4", 12: "Q4"}
        quarter = q_map.get(m, "")
        prev_year = str(y - 1)

        return (template
                .replace("{prev_month}", prev_month)
                .replace("{month}", month)
                .replace("{year}", str(y))
                .replace("{quarter}", quarter)
                .replace("{prev_year}", prev_year))

    # Iterate over all obligations
    for obligation, applies_to in ALL_OBLIGATIONS:
        if not applies_to(canonical):
            continue

        amount = _get_amount(obligation.amount_key, obligation.id)
        source = "calendar" if amount is not None else "ai_estimated"
        if obligation.amount_key == "none":
            source = "calendar"

        if obligation.frequency == "monthly":
            for m_offset in range(months_ahead):
                target = _add_months(today, m_offset)
                y, m = target.year, target.month

                # Dynamic ZUS day
                day = obligation.day_of_month
                if obligation.id == "zus":
                    day = zus_day

                entry_date = date(y, m, min(day, 28))
                if entry_date < today:
                    continue

                entries.append(TaxCalendarEntry(
                    date=entry_date,
                    tax_type=obligation.id,
                    label=_format_label(obligation.label_template, y, m),
                    estimated_amount=amount,
                    is_paid=False,
                    source=source,
                    category=obligation.category,
                ))

        elif obligation.frequency == "quarterly":
            for m_offset in range(months_ahead):
                target = _add_months(today, m_offset)
                y, m = target.year, target.month
                if m not in obligation.quarter_months:
                    continue

                entry_date = date(y, m, min(obligation.day_of_month, 28))
                if entry_date < today:
                    continue

                entries.append(TaxCalendarEntry(
                    date=entry_date,
                    tax_type=obligation.id,
                    label=_format_label(obligation.label_template, y, m),
                    estimated_amount=amount,
                    is_paid=False,
                    source=source,
                    category=obligation.category,
                ))

        elif obligation.frequency == "semi_annual":
            for m_offset in range(months_ahead):
                target = _add_months(today, m_offset)
                y, m = target.year, target.month
                if m not in obligation.quarter_months:
                    continue

                entry_date = date(y, m, min(obligation.day_of_month, 28))
                if entry_date < today:
                    continue

                entries.append(TaxCalendarEntry(
                    date=entry_date,
                    tax_type=obligation.id,
                    label=_format_label(obligation.label_template, y, m),
                    estimated_amount=amount,
                    is_paid=False,
                    source=source,
                    category=obligation.category,
                ))

        elif obligation.frequency == "annual":
            for year_offset in range(2):
                a_year = today.year + year_offset
                a_date = date(a_year, obligation.annual_month, obligation.annual_day)
                if today <= a_date <= end_date:
                    entries.append(TaxCalendarEntry(
                        date=a_date,
                        tax_type=obligation.id,
                        label=_format_label(obligation.label_template, a_year, obligation.annual_month),
                        estimated_amount=amount,
                        is_paid=False,
                        source=source,
                        category=obligation.category,
                    ))

    # Sortuj po dacie
    entries.sort(key=lambda e: e.date)
    return entries


# ==============================================================================
# DSO KONTRAHENTÓW
# ==============================================================================

async def calculate_contractor_reliability(
    db: AsyncSession,
    company_id: uuid.UUID,
) -> List[ContractorReliability]:
    """
    Oblicza DSO (Days Sales Outstanding) per kontrahent z faktur income.
    Wymaga payment_days na fakturach (auto-wyliczany przy is_paid=True).
    """
    stmt = (
        select(
            Invoice.contractor_name,
            Invoice.contractor_nip,
            func.avg(Invoice.payment_days).filter(Invoice.is_paid == True).label("avg_dso"),
            func.count().filter(Invoice.is_paid == True).label("paid_count"),
            func.count().filter(Invoice.is_paid == False).label("pending_count"),
            func.coalesce(
                func.sum(Invoice.gross_amount).filter(Invoice.is_paid == False), 0
            ).label("outstanding"),
        )
        .where(
            Invoice.company_id == company_id,
            Invoice.invoice_type == "income",
            Invoice.contractor_name.isnot(None),
        )
        .group_by(Invoice.contractor_name, Invoice.contractor_nip)
        .order_by(func.coalesce(func.sum(Invoice.gross_amount).filter(Invoice.is_paid == False), 0).desc())
        .limit(20)
    )

    result = await db.execute(stmt)
    rows = result.all()

    contractors = []
    for row in rows:
        avg_dso = float(row.avg_dso) if row.avg_dso is not None else None

        if avg_dso is None:
            score = "unknown"
        elif avg_dso < 14:
            score = "excellent"
        elif avg_dso < 30:
            score = "good"
        elif avg_dso < 60:
            score = "warning"
        else:
            score = "poor"

        contractors.append(ContractorReliability(
            contractor_name=row.contractor_name,
            contractor_nip=row.contractor_nip,
            avg_payment_days=round(avg_dso, 1) if avg_dso is not None else None,
            invoices_paid=row.paid_count or 0,
            invoices_pending=row.pending_count or 0,
            total_outstanding=float(row.outstanding or 0),
            reliability_score=score,
        ))

    return contractors


# ==============================================================================
# PROGNOZA CASHFLOW — 12 TYGODNI
# ==============================================================================

async def _get_unpaid_invoices_by_week(
    db: AsyncSession,
    company_id: uuid.UUID,
    week_start: date,
    week_end: date,
    invoice_type: str,
) -> float:
    """Sumuje nieopłacone faktury z due_date w danym tygodniu. Fallback: issue_date + 14 dni."""
    # Faktury z jawnym due_date w tym tygodniu
    stmt_due = (
        select(func.coalesce(func.sum(Invoice.gross_amount), 0))
        .where(
            Invoice.company_id == company_id,
            Invoice.invoice_type == invoice_type,
            Invoice.is_paid == False,
            Invoice.due_date.isnot(None),
            func.cast(Invoice.due_date, func.DATE if hasattr(func, 'DATE') else Invoice.due_date.type) >= week_start,
            Invoice.due_date < datetime(week_end.year, week_end.month, week_end.day) + timedelta(days=1),
        )
    )

    # Faktury bez due_date — fallback: issue_date + 14 dni
    fallback_start = week_start - timedelta(days=14)
    fallback_end = week_end - timedelta(days=14)
    stmt_fallback = (
        select(func.coalesce(func.sum(Invoice.gross_amount), 0))
        .where(
            Invoice.company_id == company_id,
            Invoice.invoice_type == invoice_type,
            Invoice.is_paid == False,
            Invoice.due_date.is_(None),
            Invoice.issue_date >= datetime(fallback_start.year, fallback_start.month, fallback_start.day),
            Invoice.issue_date < datetime(fallback_end.year, fallback_end.month, fallback_end.day) + timedelta(days=1),
        )
    )

    r1 = await db.execute(stmt_due)
    r2 = await db.execute(stmt_fallback)
    return float(r1.scalar() or 0) + float(r2.scalar() or 0)


def _calculate_direction_score(projections: List[WeeklyProjection]) -> float:
    """
    Oblicza direction_score (-100..+100) na podstawie trendu closing_balance.
    Pozytywny = firma rośnie, negatywny = firma traci.
    """
    if len(projections) < 2:
        return 0.0

    balances = [p.closing_balance for p in projections]
    first_half = balances[:len(balances)//2]
    second_half = balances[len(balances)//2:]

    avg_first = sum(first_half) / len(first_half) if first_half else 0
    avg_second = sum(second_half) / len(second_half) if second_half else 0

    if avg_first == 0:
        return 50.0 if avg_second > 0 else -50.0

    change_pct = ((avg_second - avg_first) / abs(avg_first)) * 100
    # Clamp to -100..+100
    return max(-100.0, min(100.0, change_pct))


async def calculate_cashflow_forecast(
    db: AsyncSession,
    company_id: uuid.UUID,
    safecash: Optional[float] = None,
    auto_mrr: Optional[float] = None,
    auto_burn_rate: Optional[float] = None,
    existing_ceo_goal: Optional[float] = None,
) -> CashflowForecast:
    """
    Główna funkcja — generuje 12-tygodniową prognozę cashflow.

    Łączy:
    - SafeCash (aktualna gotówka)
    - Nieopłacone faktury z due_date (known obligations)
    - Auto MRR/burn rate (ekstrapolacja)
    - Kalendarz podatkowy (tax obligations)
    - DSO kontrahentów
    """
    # 1. Pobierz canonical context dla kalendarza podatkowego
    canonical = await get_or_create_canonical(db, company_id)

    # 2. Pobierz TaxPosition dla szacunków VAT/CIT
    ctx_result = await db.execute(
        select(CompanyContextModel).where(CompanyContextModel.company_id == company_id)
    )
    ctx_record = ctx_result.scalar_one_or_none()
    vat_estimate = None
    cit_estimate = None
    if ctx_record and ctx_record.operational_data:
        op_data = ctx_record.operational_data
        tp = op_data.get("tax_position", {})
        vat_estimate = tp.get("vat_to_pay")
        cit_estimate = tp.get("cit_estimated")

    # 3. Generuj kalendarz podatkowy
    tax_calendar = generate_tax_calendar(
        canonical,
        months_ahead=3,
        vat_monthly_estimate=vat_estimate,
        cit_annual_estimate=cit_estimate,
    )

    # 4. DSO kontrahentów
    contractors = await calculate_contractor_reliability(db, company_id)

    # 5. Generuj 12 tygodniowych projekcji
    today = date.today()
    # Zacznij od poniedziałku bieżącego tygodnia
    week_start = today - timedelta(days=today.weekday())
    opening = safecash or 0.0
    weekly_mrr = (auto_mrr or 0.0) / 4.0  # tygodniowe MRR
    weekly_burn = (auto_burn_rate or 0.0) / 4.0

    projections: List[WeeklyProjection] = []

    for i in range(12):
        w_start = week_start + timedelta(weeks=i)
        w_end = w_start + timedelta(days=6)

        # Confidence maleje z czasem
        if i < 4:
            confidence = 0.9
        elif i < 8:
            confidence = 0.7
        else:
            confidence = 0.5

        # Known income: nieopłacone faktury income z due_date w tym tygodniu
        known_income = await _get_unpaid_invoices_by_week(db, company_id, w_start, w_end, "income")
        # Known expenses: nieopłacone faktury expense z due_date w tym tygodniu
        known_expenses = await _get_unpaid_invoices_by_week(db, company_id, w_start, w_end, "expense")

        # Ekstrapolacja: dodaj tygodniowe MRR/burn skalowane confidence
        # Tylko jeśli known jest mało (< 50% oczekiwanego) — nie podwajaj
        extrapolated_income = max(0, weekly_mrr * confidence - known_income) if weekly_mrr > 0 else 0
        extrapolated_expenses = max(0, weekly_burn * confidence - known_expenses) if weekly_burn > 0 else 0

        total_income = known_income + extrapolated_income
        total_expenses = known_expenses + extrapolated_expenses

        # Tax obligations: suma podatków z kalendarza w tym tygodniu
        tax_this_week = sum(
            t.estimated_amount or 0
            for t in tax_calendar
            if w_start <= t.date <= w_end and not t.is_paid
        )

        closing = opening + total_income - total_expenses - tax_this_week

        projections.append(WeeklyProjection(
            week_start=w_start,
            week_end=w_end,
            opening_balance=round(opening, 2),
            expected_income=round(total_income, 2),
            expected_expenses=round(total_expenses, 2),
            tax_obligations=round(tax_this_week, 2),
            closing_balance=round(closing, 2),
            confidence=confidence,
        ))

        opening = closing

    # 6. Safe burn limit
    tax_8w = sum(
        t.estimated_amount or 0
        for t in tax_calendar
        if today <= t.date <= today + timedelta(weeks=8) and not t.is_paid
    )
    ceo_goal = existing_ceo_goal or 0.0
    available = (safecash or 0.0) - tax_8w - (ceo_goal * 2)
    safe_burn = max(0, available / 2) if available > 0 else 0.0

    # 7. Runway weeks
    runway = None
    for i, p in enumerate(projections):
        if p.closing_balance <= 0:
            runway = i
            break
    if runway is None and projections:
        runway = len(projections)  # >12 weeks

    # 8. Direction score
    direction = _calculate_direction_score(projections)

    return CashflowForecast(
        forecast_date=datetime.now(timezone.utc),
        weekly_projections=projections,
        tax_calendar=tax_calendar,
        contractor_reliability=contractors,
        safe_burn_limit=round(safe_burn, 2),
        runway_weeks=runway,
        direction_score=round(direction, 1),
        ceo_monthly_goal=existing_ceo_goal,
    )


# ==============================================================================
# SNAPSHOT CACHE — in-memory, 60s TTL
# ==============================================================================

_snapshot_cache: dict[str, tuple[float, "FinancialSnapshot"]] = {}
_SNAPSHOT_TTL = 60.0


def invalidate_snapshot_cache(company_id: str | uuid.UUID):
    """Explicit cache invalidation — po zmianie faktury, declared_balance, etc."""
    key = str(company_id)
    _snapshot_cache.pop(key, None)
    logger.info(f"[Snapshot] Cache invalidated for {key}")


# ==============================================================================
# COMPUTE SNAPSHOT — Single Source of Truth
# ==============================================================================

async def get_snapshot(company_id: str | uuid.UUID, db: AsyncSession | None = None) -> "FinancialSnapshot":
    """
    Lazy compute + cache. Jedyny punkt wejścia dla metryk finansowych.
    Dashboard, AI, Briefing, Insights — wszystko woła TO.
    """
    key = str(company_id)
    now = _time.monotonic()

    # Cache hit?
    if key in _snapshot_cache:
        ts, cached = _snapshot_cache[key]
        if now - ts < _SNAPSHOT_TTL:
            return cached

    # Cache miss — compute
    if db is None:
        from app.database import AsyncSessionLocal
        async with AsyncSessionLocal() as db:
            snapshot = await compute_snapshot(db, uuid.UUID(key))
    else:
        snapshot = await compute_snapshot(db, uuid.UUID(key))

    _snapshot_cache[key] = (now, snapshot)
    return snapshot


async def compute_snapshot(db: AsyncSession, company_id: uuid.UUID) -> "FinancialSnapshot":
    """
    Liczy WSZYSTKIE metryki finansowe w jednym przebiegu.
    Jedna funkcja = jedna prawda. Zero różnic między modułami.
    """
    from app.schemas_financial import (
        FinancialSnapshot, CashMetrics, RevenueMetrics, ExpenseMetrics,
        ForecastMetrics, ObligationsMetrics, TimeboxedMetric,
        CashDiscrepancy, Contractor, DepartmentExpense, CategoryExpense, UpcomingPayment,
    )
    from app.services.context_operational import calculate_safecash, calculate_invoice_metrics

    now = datetime.utcnow()
    cid = company_id

    # ── 1. SafeCash + receivables/payables ──
    sc_data = await calculate_safecash(db, cid)
    safecash = sc_data["safecash"]
    receivables = sc_data["receivables"]
    payables = sc_data["payables"]

    # ── 2. Declared balance ──
    declared = None
    declared_at = None
    try:
        dr = await db.execute(text(
            "SELECT balance, as_of FROM declared_balances WHERE company_id = :c ORDER BY created_at DESC LIMIT 1"
        ), {"c": str(cid)})
        row = dr.fetchone()
        if row:
            declared = float(row[0])
            declared_at = row[1] if isinstance(row[1], datetime) else datetime.combine(row[1], datetime.min.time())
    except Exception:
        await db.rollback()  # Rollback failed transaction so next queries work

    # ── 3. Primary value — hierarchia bank > declared > safecash ──
    # Bank sync = Q3, for now always None
    bank_balance = None
    if bank_balance is not None:
        primary_value = bank_balance
        primary_source = "bank"
        primary_confidence = "high"
    elif declared is not None and declared_at and (now - declared_at).days < 30:
        primary_value = declared
        primary_source = "declared"
        primary_confidence = "medium"
    elif safecash is not None:
        primary_value = safecash
        primary_source = "safecash"
        primary_confidence = "low"
    else:
        primary_value = 0.0
        primary_source = "none"
        primary_confidence = "low"

    # ── 4. Discrepancies ──
    discrepancies = []
    if safecash and declared and abs(safecash - declared) > 0.2 * abs(safecash):
        diff = declared - safecash
        discrepancies.append(CashDiscrepancy(
            metric_a="safecash",
            metric_b="declared_balance",
            value_a=safecash,
            value_b=declared,
            difference_pln=round(diff, 2),
            difference_pct=round(diff / abs(safecash) * 100, 1) if safecash else 0,
            severity="warning" if abs(diff) < abs(safecash) * 0.5 else "critical",
            likely_cause="Prawdopodobnie: pensje, podatki, kredyty lub transakcje pozafakturowe" if diff > 0
                else "Nieopłacone faktury w systemie lub błędne dane",
        ))

    cash = CashMetrics(
        safecash=safecash,
        safecash_as_of=now,
        declared_balance=declared,
        declared_at=declared_at,
        bank_balance=bank_balance,
        primary_value=primary_value,
        primary_source=primary_source,
        primary_confidence=primary_confidence,
        receivables_total=receivables,
        payables_total=payables,
        receivables_overdue_30d=0.0,
        discrepancies=discrepancies,
    )

    # ── 5. Invoice metrics (revenue + expenses) ──
    inv_metrics, overdue_30d = await calculate_invoice_metrics(db, cid)
    cash.receivables_overdue_30d = overdue_30d

    revenue = RevenueMetrics(
        current_month=TimeboxedMetric(value=inv_metrics.monthly_revenue or 0, timeframe="monthly", as_of=now, source="invoices"),
        prev_month=TimeboxedMetric(value=inv_metrics.prev_month_revenue or 0, timeframe="monthly", source="invoices"),
        mom_change_pct=inv_metrics.revenue_mom_growth_pct,
        mrr=TimeboxedMetric(value=inv_metrics.auto_mrr or 0, timeframe="3m_avg", as_of=now, source="invoices"),
        mrr_mom_change_pct=inv_metrics.revenue_mom_growth_pct,
        concentration_top3_pct=inv_metrics.top3_concentration_pct or 0,
        invoice_count=inv_metrics.invoice_count or 0,
        last_invoice_date=inv_metrics.metrics_updated_at,
    )

    # Top contractors
    try:
        tc_result = await db.execute(text("""
            SELECT contractor_name, contractor_nip, SUM(gross_amount) as total
            FROM invoices WHERE company_id = :c AND invoice_type = 'income'
            GROUP BY contractor_name, contractor_nip
            ORDER BY total DESC LIMIT 5
        """), {"c": str(cid)})
        total_rev = revenue.current_month.value * 4 or 1  # avoid /0
        revenue.top_contractors = [
            Contractor(name=r[0] or "?", nip=r[1], revenue_pln=float(r[2] or 0),
                       share_pct=round(float(r[2] or 0) / total_rev * 100, 1) if total_rev else 0)
            for r in tc_result.fetchall()
        ]
    except Exception:
        pass

    # ── 6. Expenses + departments ──
    expenses = ExpenseMetrics(
        current_month=TimeboxedMetric(value=inv_metrics.monthly_expenses or 0, timeframe="monthly", as_of=now, source="invoices"),
        burn_rate_3m=TimeboxedMetric(value=inv_metrics.auto_burn_rate or 0, timeframe="3m_avg", as_of=now, source="invoices"),
        payables_total=payables,
        payables_overdue=overdue_30d,
    )

    # Department breakdown — only if >50% assigned
    try:
        dept_result = await db.execute(text("""
            SELECT d.name, SUM(i.net_amount) as total, COUNT(i.id) as cnt
            FROM invoices i JOIN departments d ON i.department_id = d.id
            WHERE i.company_id = :c AND i.invoice_type = 'expense'
            GROUP BY d.name ORDER BY total DESC
        """), {"c": str(cid)})
        dept_rows = dept_result.fetchall()

        total_count_result = await db.execute(text(
            "SELECT COUNT(*) FROM invoices WHERE company_id = :c AND invoice_type = 'expense'"
        ), {"c": str(cid)})
        total_expense_count = total_count_result.scalar() or 1
        assigned_count = sum(r[2] for r in dept_rows)

        coverage = assigned_count / total_expense_count if total_expense_count else 0
        expenses.department_coverage_pct = round(coverage * 100, 1)

        if coverage >= 0.5 and dept_rows:
            total_dept = sum(float(r[1] or 0) for r in dept_rows)
            expenses.by_department = [
                DepartmentExpense(
                    department_name=r[0], total_pln=float(r[1] or 0), invoice_count=r[2],
                    share_pct=round(float(r[1] or 0) / total_dept * 100, 1) if total_dept else 0
                ) for r in dept_rows
            ]
    except Exception as e:
        logger.warning(f"[Snapshot] Department breakdown failed: {e}")

    # Category breakdown (AI-inferred)
    try:
        cat_result = await db.execute(text("""
            SELECT category_name, category_source, SUM(net_amount) as total, COUNT(*) as cnt
            FROM invoices WHERE company_id = :c AND invoice_type = 'expense' AND category_name IS NOT NULL
            GROUP BY category_name, category_source ORDER BY total DESC
        """), {"c": str(cid)})
        cat_rows = cat_result.fetchall()
        if cat_rows:
            expenses.by_category = [
                CategoryExpense(category_name=r[0], source=r[1] or "ai_inferred", total_pln=float(r[2] or 0), invoice_count=r[3])
                for r in cat_rows
            ]
    except Exception:
        pass

    # ── 7. Forecast ──
    burn = inv_metrics.auto_burn_rate or 0
    mrr = inv_metrics.auto_mrr or 0
    net_monthly = mrr - burn if mrr and burn else None

    forecast = ForecastMetrics(
        net_cashflow_monthly=round(net_monthly, 2) if net_monthly else None,
        forecast_30d=round(primary_value + (net_monthly or 0), 2) if net_monthly else None,
        forecast_60d=round(primary_value + (net_monthly or 0) * 2, 2) if net_monthly else None,
        forecast_90d=round(primary_value + (net_monthly or 0) * 3, 2) if net_monthly else None,
    )

    # Runway
    if burn and burn > mrr and primary_value > 0:
        net_burn = burn - mrr
        forecast.runway_weeks = int((primary_value / net_burn) * 4.33)
        forecast.runway_confidence = "medium" if forecast.runway_weeks > 12 else "low"
    elif mrr >= burn:
        forecast.runway_weeks = 999  # profitable
        forecast.runway_confidence = "high"

    # Safe burn limit from existing forecast
    try:
        cf = await calculate_cashflow_forecast(db, cid, safecash=safecash, auto_mrr=mrr, auto_burn_rate=burn)
        forecast.safe_burn_limit = cf.safe_burn_limit
    except Exception:
        pass

    # ── 8. Obligations ──
    obligations = ObligationsMetrics()
    try:
        canonical = await get_or_create_canonical(db, cid)
        tax_cal = generate_tax_calendar(canonical, months_ahead=2, vat_monthly_estimate=None, cit_annual_estimate=None)
        upcoming = [t for t in tax_cal if not t.is_paid and t.date >= date.today()]
        obligations.upcoming_payments = [
            UpcomingPayment(date=t.date, label=t.label, amount=t.estimated_amount, type="tax")
            for t in upcoming[:10]
        ]
        if upcoming:
            obligations.next_deadline = upcoming[0].date
            obligations.total_next_30d = sum(t.estimated_amount or 0 for t in upcoming if t.date <= date.today() + timedelta(days=30))
        # VAT/CIT from tax calendar
        vat_entries = [t for t in upcoming if "VAT" in t.tax_type]
        cit_entries = [t for t in upcoming if "CIT" in t.tax_type]
        if vat_entries and vat_entries[0].estimated_amount:
            obligations.vat_to_pay = vat_entries[0].estimated_amount
        if cit_entries and cit_entries[0].estimated_amount:
            obligations.cit_estimated = cit_entries[0].estimated_amount
    except Exception as e:
        logger.warning(f"[Snapshot] Obligations failed: {e}")

    # ── BUILD SNAPSHOT ──
    snapshot = FinancialSnapshot(
        company_id=cid,
        computed_at=now,
        cash=cash,
        revenue=revenue,
        expenses=expenses,
        forecast=forecast,
        obligations=obligations,
    )

    logger.info(
        f"[Snapshot] {cid} → cash={primary_value:.0f} ({primary_source}), "
        f"mrr={mrr:.0f}, burn={burn:.0f}, runway={forecast.runway_weeks}w"
    )

    return snapshot
