"""
FinancialSnapshot — Single Source of Truth dla metryk finansowych.
Jeden obiekt, jedno źródło, jeden endpoint. Dashboard, AI, Briefing — wszystko z tego.
"""
from datetime import datetime, date
from decimal import Decimal
from typing import Optional, Literal, List
from uuid import UUID

from pydantic import BaseModel, Field, ConfigDict


# ==============================================================================
# BUILDING BLOCKS
# ==============================================================================

class TimeboxedMetric(BaseModel):
    """Każda metryka z jednostką, timeframe i źródłem."""
    value: float
    unit: Literal["PLN", "EUR", "%", "days", "weeks", "count"] = "PLN"
    timeframe: Literal["current", "monthly", "mom", "3m_avg", "forecast_30d", "forecast_90d"] = "current"
    as_of: Optional[datetime] = None
    source: str = "invoices"  # invoices | declared | bank | ai_estimated | calculated
    confidence: float = 1.0   # 0.0-1.0


class CashDiscrepancy(BaseModel):
    """Rozbieżność między dwoma źródłami gotówki."""
    metric_a: str       # "safecash"
    metric_b: str       # "declared_balance"
    value_a: float
    value_b: float
    difference_pln: float
    difference_pct: float
    severity: Literal["info", "warning", "critical"]
    likely_cause: str


class Contractor(BaseModel):
    name: str
    nip: Optional[str] = None
    revenue_pln: float = 0.0
    share_pct: float = 0.0


class DepartmentExpense(BaseModel):
    department_name: str
    total_pln: float = 0.0
    invoice_count: int = 0
    share_pct: float = 0.0


class CategoryExpense(BaseModel):
    category_name: str
    source: Literal["manual", "ai_inferred", "ksef_extracted"] = "ai_inferred"
    total_pln: float = 0.0
    invoice_count: int = 0


class UpcomingPayment(BaseModel):
    date: date
    label: str
    amount: Optional[float] = None
    type: Literal["tax", "salary", "payment", "deadline"] = "payment"


# ==============================================================================
# SNAPSHOT SECTIONS
# ==============================================================================

class CashMetrics(BaseModel):
    """Gotówka — 3 źródła + primary + rozbieżności."""
    # SafeCash — z opłaconych faktur
    safecash: Optional[float] = None
    safecash_as_of: Optional[datetime] = None

    # Declared — user ręcznie podał
    declared_balance: Optional[float] = None
    declared_at: Optional[datetime] = None

    # Bank — z sync (Q3, na razie None)
    bank_balance: Optional[float] = None
    bank_balance_at: Optional[datetime] = None

    # Primary — co pokazujemy userowi
    primary_value: float = 0.0
    primary_source: Literal["bank", "declared", "safecash", "none"] = "none"
    primary_confidence: Literal["high", "medium", "low"] = "low"

    # Receivables / Payables
    receivables_total: float = 0.0
    payables_total: float = 0.0
    receivables_overdue_30d: float = 0.0

    # Discrepancies
    discrepancies: List[CashDiscrepancy] = Field(default_factory=list)


class RevenueMetrics(BaseModel):
    """Przychody — bieżący miesiąc, MRR, kontrahenci."""
    current_month: TimeboxedMetric = Field(default_factory=lambda: TimeboxedMetric(value=0, timeframe="monthly"))
    prev_month: TimeboxedMetric = Field(default_factory=lambda: TimeboxedMetric(value=0, timeframe="monthly"))
    mom_change_pct: Optional[float] = None

    mrr: TimeboxedMetric = Field(default_factory=lambda: TimeboxedMetric(value=0, timeframe="3m_avg"))
    mrr_mom_change_pct: Optional[float] = None

    top_contractors: List[Contractor] = Field(default_factory=list)
    concentration_top3_pct: float = 0.0

    last_invoice_date: Optional[datetime] = None
    invoice_count: int = 0


class ExpenseMetrics(BaseModel):
    """Koszty — bieżący miesiąc, burn rate, oddziały."""
    current_month: TimeboxedMetric = Field(default_factory=lambda: TimeboxedMetric(value=0, timeframe="monthly"))
    burn_rate_3m: TimeboxedMetric = Field(default_factory=lambda: TimeboxedMetric(value=0, timeframe="3m_avg"))

    # Kategoryzacja — None jeśli <50% faktur przypisanych
    by_department: Optional[List[DepartmentExpense]] = None
    by_category: Optional[List[CategoryExpense]] = None
    department_coverage_pct: float = 0.0

    payables_total: float = 0.0
    payables_overdue: float = 0.0


class ForecastMetrics(BaseModel):
    """Prognoza — runway, forecast 30/60/90d."""
    runway_weeks: Optional[int] = None
    runway_confidence: Literal["high", "medium", "low"] = "low"

    net_cashflow_monthly: Optional[float] = None  # przychody - koszty avg

    forecast_30d: Optional[float] = None  # expected cash in 30d
    forecast_60d: Optional[float] = None
    forecast_90d: Optional[float] = None

    safe_burn_limit: Optional[float] = None  # ile można wydać bez zagrożenia


class ObligationsMetrics(BaseModel):
    """Obowiązki podatkowe + nadchodzące płatności."""
    vat_to_pay: Optional[float] = None
    cit_estimated: Optional[float] = None
    next_deadline: Optional[date] = None
    total_next_30d: float = 0.0

    upcoming_payments: List[UpcomingPayment] = Field(default_factory=list)


# ==============================================================================
# FINANCIAL SNAPSHOT — MAIN OBJECT
# ==============================================================================

class FinancialSnapshot(BaseModel):
    """
    Single Source of Truth. Jeden obiekt = cały stan finansowy firmy.
    Dashboard, AI, Briefing, Proactive Insights — czytają TO.
    """
    company_id: UUID
    computed_at: datetime
    snapshot_version: int = 1

    cash: CashMetrics = Field(default_factory=CashMetrics)
    revenue: RevenueMetrics = Field(default_factory=RevenueMetrics)
    expenses: ExpenseMetrics = Field(default_factory=ExpenseMetrics)
    forecast: ForecastMetrics = Field(default_factory=ForecastMetrics)
    obligations: ObligationsMetrics = Field(default_factory=ObligationsMetrics)

    model_config = ConfigDict(from_attributes=True)


# ==============================================================================
# RESPONSE MODELS
# ==============================================================================

class FinancialSnapshotResponse(FinancialSnapshot):
    """API response wrapper."""
    pass


class DeclaredBalanceRequest(BaseModel):
    balance: float = Field(ge=0, description="Saldo bankowe w PLN")
    as_of: date = Field(description="Na jaki dzień")


class DeclaredBalanceResponse(BaseModel):
    company_id: UUID
    balance: float
    as_of: date
    created_at: datetime
