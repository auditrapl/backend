"""
Warstwa 2 — Kontekst Operacyjny (L2)
Przechowuje szybko-zmieniający się bieżący stan firmy:
puls finansowy, pozycja podatkowa, cele okresu, aktywne ryzyka.
"""
import os
import uuid
from copy import deepcopy
from datetime import datetime, timezone, date, timedelta
from typing import Optional, List, Literal

from pydantic import BaseModel, Field
from google import genai
from google.genai import types
import logging
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, case, and_

from app.models import CompanyContextModel, Invoice
from app.services.cashflow_engine import CashflowForecast

logger = logging.getLogger(__name__)


# ==============================================================================
# MODELE PYDANTIC — L2 (Operacyjny)
# ==============================================================================

class LiveFinancialPulse(BaseModel):
    cash_on_hand: Optional[float] = Field(None, description="Dostępna gotówka (PLN) — wypełniane przez SafeCash lub fallback ręczny")
    credit_line: Optional[float] = Field(None, description="Dostępna, niewykorzystana linia kredytowa (PLN)")
    mrr: Optional[float] = Field(None, description="MRR — Monthly Recurring Revenue (PLN)")
    net_burn_rate: Optional[float] = Field(None, description="Miesięczny burn rate netto (PLN/msc)")
    receivables_total: Optional[float] = Field(None, description="Należności ogółem (PLN) — auto z nieopłaconych faktur income")
    payables_total: Optional[float] = Field(None, description="Zobowiązania ogółem (PLN) — auto z nieopłaconych faktur expense")
    receivables_overdue_30d: Optional[float] = Field(None, description="Należności przeterminowane powyżej 30 dni (PLN)")
    vat_position: Optional[float] = Field(None, description="Pozycja VAT: dodatnia = nadwyżka do zwrotu, ujemna = zaległość")
    budget_utilization_pct: Optional[float] = Field(None, description="Stopień wykonania budżetu w % (np. 72.5)")
    last_ksef_sync: Optional[datetime] = Field(None, description="Data ostatniej synchronizacji z KSeF")
    last_erp_sync: Optional[datetime] = Field(None, description="Data ostatniej synchronizacji z ERP")
    # SafeCash — gotówka wyliczana z faktur
    safecash: Optional[float] = Field(None, description="SafeCash — gotówka wyliczona z opłaconych faktur (PLN). Źródło prawdy.")
    safecash_updated_at: Optional[datetime] = Field(None, description="Kiedy SafeCash był ostatnio przeliczony")
    simulation_cash: Optional[float] = Field(None, description="Gotówka deklarowana przez użytkownika w rozmowie — parametr symulacyjny, NIE realna pozycja")


class TaxPosition(BaseModel):
    vat_to_pay: Optional[float] = Field(None, description="VAT do zapłaty w bieżącym okresie (PLN)")
    cit_estimated: Optional[float] = Field(None, description="Szacunkowy CIT za bieżący rok (PLN)")
    next_tax_deadline: Optional[str] = Field(None, description="Najbliższy termin podatkowy (YYYY-MM-DD)")
    jpk_status: Optional[str] = Field(None, description="Status JPK: 'złożony', 'oczekuje', 'zaległy'")


class PeriodGoal(BaseModel):
    description: str = Field(description="Opis celu okresu, np. 'Zamknięcie 3 kontraktów enterprise'")
    target_date: Optional[str] = Field(None, description="Docelowa data realizacji (YYYY-MM-DD)")
    metric: Optional[str] = Field(None, description="Mierzalna metryka sukcesu, np. 'MRR > 200 000 PLN'")


class RiskFlag(BaseModel):
    description: str = Field(description="Zwięzły opis ryzyka")
    severity: Literal['high', 'medium', 'low'] = Field(description="Krytyczność ryzyka")


class InvoiceMetrics(BaseModel):
    """Auto-wyliczane metryki z faktur — odświeżane z każdą fakturą."""
    monthly_revenue: Optional[float] = Field(None, description="Przychody brutto w bieżącym miesiącu (PLN)")
    monthly_expenses: Optional[float] = Field(None, description="Koszty brutto w bieżącym miesiącu (PLN)")
    monthly_net_cashflow: Optional[float] = Field(None, description="Netto cashflow bieżącego miesiąca (PLN)")
    prev_month_revenue: Optional[float] = Field(None, description="Przychody brutto w poprzednim miesiącu (PLN)")
    revenue_mom_growth_pct: Optional[float] = Field(None, description="Wzrost przychodów MoM w %")
    top_contractor_name: Optional[str] = Field(None, description="Kontrahent z najwyższymi przychodami")
    top_contractor_pct: Optional[float] = Field(None, description="% przychodów od top kontrahenta")
    top3_concentration_pct: Optional[float] = Field(None, description="% przychodów od top 3 kontrahentów")
    auto_burn_rate: Optional[float] = Field(None, description="Śr. miesięczne koszty z ostatnich 3 msc (PLN)")
    auto_mrr: Optional[float] = Field(None, description="Śr. miesięczne przychody z ostatnich 3 msc (PLN)")
    invoice_count: Optional[int] = Field(None, description="Łączna liczba faktur w systemie")
    metrics_updated_at: Optional[datetime] = Field(None, description="Kiedy metryki były ostatnio przeliczone")


class OperationalContext(BaseModel):
    """
    Kontekst operacyjny — szybko-zmieniający się stan GDZIE firma JEST TERAZ.
    Aktualizowany po każdym zapytaniu CFO, synchronizacji ERP/KSeF lub ręcznie.
    """
    live_financial_pulse: LiveFinancialPulse = Field(default_factory=LiveFinancialPulse)
    invoice_metrics: InvoiceMetrics = Field(default_factory=InvoiceMetrics)
    tax_position: TaxPosition = Field(default_factory=TaxPosition)
    current_period_goals: List[PeriodGoal] = Field(default_factory=list)
    active_risk_flags: List[RiskFlag] = Field(default_factory=list)
    cashflow_forecast: "CashflowForecast" = Field(default_factory=lambda: CashflowForecast())
    operational_version: int = Field(default=1)
    last_ai_extraction: Optional[datetime] = None


# ==============================================================================
# LOGIKA MERGE
# ==============================================================================

def merge_operational(old: OperationalContext, new: OperationalContext) -> OperationalContext:
    """
    Scalanie L2: najnowsze niezerowe dane finansowe wygrywają.
    SafeCash i receivables/payables NIE są nadpisywane przez merge (obliczane z faktur).
    cash_on_hand z AI extraction → redirect do simulation_cash.
    Ryzyka prepend + dedupe. Cele append jeśli nowe.
    """
    merged = deepcopy(old)

    # live_financial_pulse — najświeższe dane wygrywają (z wyjątkiem SafeCash)
    pulse = new.live_financial_pulse
    m = merged.live_financial_pulse

    # cash_on_hand z AI extraction → redirect do simulation_cash
    # SafeCash jest obliczany wyłącznie przez refresh_safecash() z faktur
    if pulse.cash_on_hand is not None:
        m.simulation_cash = pulse.cash_on_hand
    if pulse.simulation_cash is not None:
        m.simulation_cash = pulse.simulation_cash
    # safecash, safecash_updated_at — NIGDY nie nadpisywane przez merge
    # receivables_total, payables_total — NIGDY nie nadpisywane (auto z faktur)

    if pulse.credit_line is not None:
        m.credit_line = pulse.credit_line
    if pulse.mrr is not None:
        m.mrr = pulse.mrr
    if pulse.net_burn_rate is not None:
        m.net_burn_rate = pulse.net_burn_rate
    if pulse.receivables_overdue_30d is not None:
        m.receivables_overdue_30d = pulse.receivables_overdue_30d
    if pulse.vat_position is not None:
        m.vat_position = pulse.vat_position
    if pulse.budget_utilization_pct is not None:
        m.budget_utilization_pct = pulse.budget_utilization_pct
    if pulse.last_ksef_sync is not None:
        m.last_ksef_sync = pulse.last_ksef_sync
    if pulse.last_erp_sync is not None:
        m.last_erp_sync = pulse.last_erp_sync

    # tax_position
    tx = new.tax_position
    mt = merged.tax_position
    if tx.vat_to_pay is not None:
        mt.vat_to_pay = tx.vat_to_pay
    if tx.cit_estimated is not None:
        mt.cit_estimated = tx.cit_estimated
    if tx.next_tax_deadline:
        mt.next_tax_deadline = tx.next_tax_deadline
    if tx.jpk_status:
        mt.jpk_status = tx.jpk_status

    # active_risk_flags — nowe ryzyka na przód, dedupe
    if new.active_risk_flags:
        existing_descs = {r.description.lower() for r in merged.active_risk_flags}
        novel = [r for r in new.active_risk_flags if r.description.lower() not in existing_descs]
        merged.active_risk_flags = novel + merged.active_risk_flags

    # current_period_goals — append jeśli nie ma jeszcze
    if new.current_period_goals:
        existing_goals = {g.description.lower() for g in merged.current_period_goals}
        for goal in new.current_period_goals:
            if goal.description.lower() not in existing_goals:
                merged.current_period_goals.append(goal)

    merged.operational_version = old.operational_version + 1
    merged.last_ai_extraction = datetime.now(timezone.utc)
    return merged


# ==============================================================================
# AI EKSTRAKCJA
# ==============================================================================

async def extract_operational_from_conversation(text: str) -> OperationalContext:
    """Gemini extractor skupiony na L2 — bieżącym stanie operacyjnym."""
    client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))

    system_instruction = """Jesteś analitykiem finansowym wyciągającym TYLKO bieżące, dynamiczne dane operacyjne firmy.

ZASADY:
1. Nie wymyślaj. Brak informacji = null.
2. Skupiaj się na: MRR, burn rate, VAT, ryzykach operacyjnych, celach bieżącego okresu.
3. NIE wyciągaj strukturalnych danych (forma prawna, NIP, model biznesu) — to należy do kontekstu kanonicznego.
4. Ryzyka: opisuj zwięźle, oceń severity (high/medium/low).
5. Cele okresu: tylko te wyraźnie wspomniane jako aktualne.
6. GOTÓWKA: Jeśli użytkownik wspomina kwotę gotówki (np. "mamy 840k gotówki", "stan konta 500k"),
   zapisz ją w polu simulation_cash (NIE cash_on_hand). cash_on_hand jest obliczane
   automatycznie z faktur (SafeCash) i nie powinno być wyciągane z rozmowy.
7. Pola receivables_total i payables_total ustaw na null — są obliczane automatycznie z faktur."""

    response = client.models.generate_content(
        model='gemini-2.5-flash',
        contents=text,
        config=types.GenerateContentConfig(
            system_instruction=system_instruction,
            response_mime_type="application/json",
            response_schema=OperationalContext,
            temperature=0.0,
        ),
    )
    return OperationalContext.model_validate_json(response.text)


# ==============================================================================
# SAFECASH — GOTÓWKA Z FAKTUR
# ==============================================================================

async def calculate_safecash(db: AsyncSession, company_id: uuid.UUID) -> dict:
    """
    Wylicza SafeCash + należności + zobowiązania z tabeli invoices.
    SafeCash = SUM(gross paid income) - SUM(gross paid expense).
    Returns {"safecash": float|None, "receivables": float, "payables": float}.
    safecash = None jeśli brak opłaconych faktur (brak danych do wyliczenia).
    """
    result = await db.execute(
        select(
            func.coalesce(func.sum(case(
                (and_(Invoice.invoice_type == "income", Invoice.is_paid == True), Invoice.gross_amount),
                else_=0,
            )), 0).label("income_paid"),
            func.coalesce(func.sum(case(
                (and_(Invoice.invoice_type == "expense", Invoice.is_paid == True), Invoice.gross_amount),
                else_=0,
            )), 0).label("expense_paid"),
            func.coalesce(func.sum(case(
                (and_(Invoice.invoice_type == "income", Invoice.is_paid == False), Invoice.gross_amount),
                else_=0,
            )), 0).label("receivables"),
            func.coalesce(func.sum(case(
                (and_(Invoice.invoice_type == "expense", Invoice.is_paid == False), Invoice.gross_amount),
                else_=0,
            )), 0).label("payables"),
            func.count(case(
                (Invoice.is_paid == True, Invoice.id),
                else_=None,
            )).label("paid_count"),
        ).where(
            Invoice.company_id == company_id,
            Invoice.gross_amount.isnot(None),
        )
    )
    row = result.one()

    safecash = None
    if row.paid_count > 0:
        safecash = float(row.income_paid) - float(row.expense_paid)

    return {
        "safecash": safecash,
        "receivables": float(row.receivables),
        "payables": float(row.payables),
    }


async def calculate_invoice_metrics(db: AsyncSession, company_id: uuid.UUID) -> InvoiceMetrics:
    """
    Auto-metryki z faktur: trendy miesięczne, koncentracja klientów, auto burn/MRR.
    Wywoływane razem z calculate_safecash() w background task.
    """
    now = datetime.utcnow()
    current_month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if now.month == 1:
        prev_month_start = current_month_start.replace(year=now.year - 1, month=12)
    else:
        prev_month_start = current_month_start.replace(month=now.month - 1)

    metrics = InvoiceMetrics()

    # ── Query 1: Monthly revenue/expenses (bieżący + poprzedni miesiąc) ──
    result = await db.execute(
        select(
            func.coalesce(func.sum(case(
                (and_(Invoice.invoice_type == "income", Invoice.issue_date >= current_month_start), Invoice.gross_amount),
                else_=0,
            )), 0).label("cur_revenue"),
            func.coalesce(func.sum(case(
                (and_(Invoice.invoice_type == "expense", Invoice.issue_date >= current_month_start), Invoice.gross_amount),
                else_=0,
            )), 0).label("cur_expenses"),
            func.coalesce(func.sum(case(
                (and_(
                    Invoice.invoice_type == "income",
                    Invoice.issue_date >= prev_month_start,
                    Invoice.issue_date < current_month_start,
                ), Invoice.gross_amount),
                else_=0,
            )), 0).label("prev_revenue"),
            func.count(Invoice.id).label("total_count"),
        ).where(
            Invoice.company_id == company_id,
            Invoice.gross_amount.isnot(None),
        )
    )
    row = result.one()
    cur_rev = float(row.cur_revenue)
    cur_exp = float(row.cur_expenses)
    prev_rev = float(row.prev_revenue)

    metrics.monthly_revenue = cur_rev if cur_rev > 0 else None
    metrics.monthly_expenses = cur_exp if cur_exp > 0 else None
    if cur_rev > 0 or cur_exp > 0:
        metrics.monthly_net_cashflow = cur_rev - cur_exp
    metrics.prev_month_revenue = prev_rev if prev_rev > 0 else None
    if prev_rev > 0 and cur_rev > 0:
        metrics.revenue_mom_growth_pct = round((cur_rev - prev_rev) / prev_rev * 100, 1)
    metrics.invoice_count = row.total_count

    # ── Query 2: Top contractors (koncentracja klientów) ──
    result = await db.execute(
        select(
            Invoice.contractor_name,
            func.sum(Invoice.gross_amount).label("total"),
        ).where(
            Invoice.company_id == company_id,
            Invoice.invoice_type == "income",
            Invoice.gross_amount.isnot(None),
            Invoice.contractor_name.isnot(None),
        ).group_by(Invoice.contractor_name)
        .order_by(func.sum(Invoice.gross_amount).desc())
        .limit(3)
    )
    top_rows = result.all()

    if top_rows:
        # Total revenue for concentration calc
        total_result = await db.execute(
            select(func.sum(Invoice.gross_amount)).where(
                Invoice.company_id == company_id,
                Invoice.invoice_type == "income",
                Invoice.gross_amount.isnot(None),
            )
        )
        total_income = float(total_result.scalar() or 0)

        if total_income > 0:
            top1 = float(top_rows[0].total)
            metrics.top_contractor_name = top_rows[0].contractor_name
            metrics.top_contractor_pct = round(top1 / total_income * 100, 1)

            top3_sum = sum(float(r.total) for r in top_rows)
            metrics.top3_concentration_pct = round(top3_sum / total_income * 100, 1)

    # ── Query 3: Auto burn rate / MRR (średnia z ostatnich 3 miesięcy) ──
    three_months_ago = current_month_start.replace(
        month=current_month_start.month - 3 if current_month_start.month > 3
        else current_month_start.month + 9,
        year=current_month_start.year if current_month_start.month > 3
        else current_month_start.year - 1,
    )
    result = await db.execute(
        select(
            func.coalesce(func.sum(case(
                (Invoice.invoice_type == "income", Invoice.gross_amount),
                else_=0,
            )), 0).label("income_3m"),
            func.coalesce(func.sum(case(
                (Invoice.invoice_type == "expense", Invoice.gross_amount),
                else_=0,
            )), 0).label("expense_3m"),
            # Count distinct months with invoices for proper averaging
            func.count(func.distinct(func.date_trunc("month", Invoice.issue_date))).label("months_with_data"),
        ).where(
            Invoice.company_id == company_id,
            Invoice.gross_amount.isnot(None),
            Invoice.issue_date >= three_months_ago,
            Invoice.issue_date < current_month_start,
        )
    )
    row = result.one()
    months = max(row.months_with_data, 1)  # unikamy dzielenia przez 0
    income_3m = float(row.income_3m)
    expense_3m = float(row.expense_3m)

    if income_3m > 0:
        metrics.auto_mrr = round(income_3m / months, 2)
    if expense_3m > 0:
        metrics.auto_burn_rate = round(expense_3m / months, 2)

    # ── Query 4: Przeterminowane należności >30 dni ──
    overdue_cutoff = (now - timedelta(days=30)).isoformat()[:10]
    try:
        result = await db.execute(
            select(
                func.coalesce(func.sum(Invoice.gross_amount), 0)
            ).where(
                Invoice.company_id == company_id,
                Invoice.invoice_type == "income",
                Invoice.is_paid == False,
                Invoice.gross_amount.isnot(None),
                Invoice.issue_date <= now - timedelta(days=30),
            )
        )
        overdue = float(result.scalar() or 0)
    except Exception:
        overdue = 0.0

    metrics.metrics_updated_at = datetime.now(timezone.utc)

    return metrics, overdue


async def refresh_financial_metrics(db: AsyncSession, company_id: uuid.UUID) -> dict:
    """
    Przelicza SafeCash + InvoiceMetrics z faktur i zapisuje do L2.
    Wywoływane jako background task po każdej zmianie faktury.
    """
    safecash_data = await calculate_safecash(db, company_id)
    inv_metrics, overdue_30d = await calculate_invoice_metrics(db, company_id)

    result = await db.execute(
        select(CompanyContextModel).where(CompanyContextModel.company_id == company_id)
    )
    record = result.scalar_one_or_none()

    if not record:
        logger.info(f"[Metrics] Brak rekordu kontekstu dla company_id={company_id}, pomijam")
        return safecash_data

    try:
        current = OperationalContext.model_validate(record.operational_data or {})
    except Exception:
        current = OperationalContext()

    # SafeCash
    p = current.live_financial_pulse
    if safecash_data["safecash"] is not None:
        p.safecash = safecash_data["safecash"]
        p.cash_on_hand = safecash_data["safecash"]
    p.safecash_updated_at = datetime.now(timezone.utc)
    p.receivables_total = safecash_data["receivables"]
    p.payables_total = safecash_data["payables"]
    if overdue_30d > 0:
        p.receivables_overdue_30d = overdue_30d

    # Invoice Metrics
    current.invoice_metrics = inv_metrics

    # Cashflow Forecast — prognoza + kalendarz podatkowy + DSO
    try:
        from app.services.cashflow_engine import calculate_cashflow_forecast
        existing_goal = current.cashflow_forecast.ceo_monthly_goal
        forecast = await calculate_cashflow_forecast(
            db, company_id,
            safecash=safecash_data["safecash"],
            auto_mrr=inv_metrics.auto_mrr,
            auto_burn_rate=inv_metrics.auto_burn_rate,
            existing_ceo_goal=existing_goal,
        )
        current.cashflow_forecast = forecast
    except Exception as e:
        logger.warning(f"[Metrics] Cashflow forecast failed for {company_id}: {e}")

    current.operational_version += 1
    record.operational_data = current.model_dump(mode="json")
    record.operational_updated_at = datetime.utcnow()
    await db.commit()
    await db.refresh(record)

    logger.info(
        f"[Metrics] company_id={company_id} → "
        f"safecash={safecash_data['safecash']}, "
        f"rev={inv_metrics.monthly_revenue}, exp={inv_metrics.monthly_expenses}, "
        f"direction={current.cashflow_forecast.direction_score}, "
        f"runway={current.cashflow_forecast.runway_weeks}w"
    )
    return safecash_data


# Backward compat alias
refresh_safecash = refresh_financial_metrics


async def refresh_safecash_background(company_id_str: str):
    """Background task wrapper — otwiera własną sesję DB."""
    from app.database import AsyncSessionLocal
    try:
        async with AsyncSessionLocal() as db:
            await refresh_financial_metrics(db, uuid.UUID(company_id_str))
    except Exception as e:
        logger.warning(f"[Metrics] Refresh failed for {company_id_str}: {e}")


# ==============================================================================
# OPERACJE DB
# ==============================================================================

async def get_or_create_operational(db: AsyncSession, company_id: uuid.UUID) -> OperationalContext:
    result = await db.execute(
        select(CompanyContextModel).where(CompanyContextModel.company_id == company_id)
    )
    record = result.scalar_one_or_none()

    if not record:
        empty = OperationalContext()
        record = CompanyContextModel(
            company_id=company_id,
            context_data={},
            operational_data=empty.model_dump(mode="json"),
        )
        db.add(record)
        await db.commit()
        await db.refresh(record)

    if record.operational_data is None:
        return OperationalContext()

    try:
        return OperationalContext.model_validate(record.operational_data)
    except Exception:
        return OperationalContext()


async def update_operational_from_metrics(
    db: AsyncSession,
    company_id: uuid.UUID,
    metrics_payload: dict,
) -> OperationalContext:
    """
    Bezpośrednia aktualizacja metryk bez LLM — dla webhooków ERP/KSeF.
    cash_on_hand z payloadu → redirect do simulation_cash (SafeCash pochodzi z faktur).
    Akceptuje safecash jako explicit override (np. z ERP).
    """
    result = await db.execute(
        select(CompanyContextModel).where(CompanyContextModel.company_id == company_id)
    )
    record = result.scalar_one_or_none()
    if not record:
        record = CompanyContextModel(
            company_id=company_id,
            context_data={},
            operational_data=OperationalContext().model_dump(mode="json"),
        )
        db.add(record)

    try:
        current = OperationalContext.model_validate(record.operational_data or {})
    except Exception:
        current = OperationalContext()

    # cash_on_hand → redirect do simulation_cash (SafeCash pochodzi z faktur)
    if "cash_on_hand" in metrics_payload and "safecash" not in metrics_payload:
        metrics_payload["simulation_cash"] = metrics_payload.pop("cash_on_hand")

    pulse_fields = {
        "safecash", "simulation_cash",
        "credit_line", "mrr", "net_burn_rate",
        "receivables_overdue_30d",
        "vat_position", "budget_utilization_pct",
    }
    tax_fields = {"vat_to_pay", "cit_estimated", "next_tax_deadline", "jpk_status"}

    for field in pulse_fields:
        if field in metrics_payload:
            setattr(current.live_financial_pulse, field, metrics_payload[field])
    for field in tax_fields:
        if field in metrics_payload:
            setattr(current.tax_position, field, metrics_payload[field])

    current.operational_version += 1
    record.operational_data = current.model_dump(mode="json")
    record.operational_updated_at = datetime.utcnow()
    await db.commit()
    await db.refresh(record)
    return current


async def process_and_upsert_operational(
    db: AsyncSession,
    company_id: uuid.UUID,
    text_source: str,
) -> OperationalContext:
    """Ekstrakcja AI + merge + zapis L2. Zastępuje process_and_upsert_context z context_manager."""
    old = await get_or_create_operational(db, company_id)
    new = await extract_operational_from_conversation(text_source)
    merged = merge_operational(old, new)

    result = await db.execute(
        select(CompanyContextModel).where(CompanyContextModel.company_id == company_id)
    )
    record = result.scalar_one_or_none()
    if record:
        record.operational_data = merged.model_dump(mode="json")
        record.operational_updated_at = datetime.utcnow()
        await db.commit()
        await db.refresh(record)

    return merged
