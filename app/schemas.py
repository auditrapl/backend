from pydantic import BaseModel, ConfigDict, EmailStr, Field
from uuid import UUID
from datetime import datetime
from typing import Optional, Dict, Any, Literal, Union, Annotated
from decimal import Decimal

# Company Schemas
class CompanyCreate(BaseModel):
    first_name: str
    last_name: str
    name: str
    nip: str
    industry: Optional[str] = None
    description: Optional[str] = None
    legal_form: Optional[str] = None        # "JDG", "Sp. z o.o.", "S.A.", "S.C.", "S.K."
    taxation_form: Optional[str] = None     # "CIT 19%", "CIT 9%", "CIT estoński", "Liniowy 19%", "Skala podatkowa", "Ryczałt"
    vat_status: Optional[str] = None        # "czynny", "zwolniony"
    employee_count: Optional[int] = None
    zus_type: Optional[str] = None          # "pelny", "preferencyjny", "maly_zus_plus", "ulga_na_start"

class CompanyResponse(BaseModel):
    id: UUID
    name: str
    nip: str
    industry: Optional[str]
    description: Optional[str]
    ksef_token: Optional[str]
    legal_form: Optional[str] = None
    taxation_form: Optional[str] = None
    vat_status: Optional[str] = None
    employee_count: Optional[int] = None
    zus_type: Optional[str] = None
    stripe_customer_id: Optional[str] = None
    stripe_subscription_id: Optional[str] = None
    stripe_price_id: Optional[str] = None
    billing_status: str = "inactive"
    billing_currency: Optional[str] = None
    billing_interval: Optional[str] = None
    billing_amount: Optional[Decimal] = None
    billing_current_period_end: Optional[datetime] = None
    billing_cancel_at_period_end: bool = False
    billing_last_event_at: Optional[datetime] = None
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)

# Department Schemas
class DepartmentCreate(BaseModel):
    name: str
    description: Optional[str] = None
    monthly_limit: Optional[Decimal] = None

class DepartmentUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    monthly_limit: Optional[Decimal] = None

class DepartmentBudgetUpdate(BaseModel):
    monthly_limit: Decimal

class AiBlueprintRequest(BaseModel):
    industry: str
    total_budget: Decimal

class CompanyBudgetUpdate(BaseModel):
    total_budget: Decimal

class CompanyBudgetResponse(BaseModel):
    total_budget: Decimal | None

class DepartmentResponse(BaseModel):
    id: UUID
    company_id: UUID
    name: str
    description: Optional[str]
    monthly_limit: Optional[Decimal]
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)

# User Schemas
class UserCreate(BaseModel):
    email: EmailStr

class UserResponse(BaseModel):
    id: UUID
    email: str
    company_id: Optional[UUID]
    stripe_customer_id: Optional[str] = None
    stripe_subscription_id: Optional[str] = None
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)

# Invoice Schemas
class InvoiceCreate(BaseModel):
    filename: str
    # Removed company_id and user_id as they are auto-assigned
    
    # Optional fields allowed during creation
    file_url: Optional[str] = None
    ksef_xml: Optional[str] = None
    ksef_number: Optional[str] = None
    internal_number: Optional[str] = None
    contractor_name: Optional[str] = None
    contractor_nip: Optional[str] = None
    net_amount: Optional[Decimal] = None
    vat_amount: Optional[Decimal] = None
    gross_amount: Optional[Decimal] = None
    currency: Optional[str] = "PLN"
    issue_date: Optional[datetime] = None
    status: Optional[str] = "pending"
    invoice_type: Optional[str] = "expense"
    is_vat_exempt: bool = False
    department_id: Optional[UUID] = None
    category_name: Optional[str] = None
    is_paid: bool = False
    due_date: Optional[datetime] = None
    payment_date: Optional[datetime] = None
    audit_id: Optional[UUID] = None
    risk_score: int = 0
    ai_analysis_json: Optional[Dict[str, Any]] = None
    parsed_data: Optional[Dict[str, Any]] = None

class InvoiceUpdate(BaseModel):
    status: Optional[str] = None
    department_id: Optional[UUID] = None
    is_paid: Optional[bool] = None
    contractor_name: Optional[str] = None
    contractor_nip: Optional[str] = None
    gross_amount: Optional[Decimal] = None
    issue_date: Optional[datetime] = None
    invoice_type: Optional[str] = None
    is_vat_exempt: Optional[bool] = None
    due_date: Optional[datetime] = None
    payment_date: Optional[datetime] = None

class InvoiceResponse(BaseModel):
    id: UUID
    company_id: UUID
    user_id: Optional[UUID]
    filename: str
    file_url: Optional[str]
    ksef_xml: Optional[str]
    ksef_number: Optional[str]
    internal_number: Optional[str]
    contractor_name: Optional[str]
    contractor_nip: Optional[str]
    net_amount: Optional[Decimal]
    vat_amount: Optional[Decimal]
    gross_amount: Optional[Decimal]
    currency: str
    issue_date: Optional[datetime]
    status: str
    invoice_type: str
    is_vat_exempt: bool
    department_id: Optional[UUID]
    category_name: Optional[str]
    category_source: str = "ai_inferred"
    is_paid: bool
    due_date: Optional[datetime]
    payment_date: Optional[datetime]
    payment_days: Optional[int]
    audit_id: Optional[UUID]
    risk_score: int
    ai_analysis_json: Optional[Dict[str, Any]]
    parsed_data: Optional[Dict[str, Any]]
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)

# Audit Schemas
class AuditSummaryResponse(BaseModel):
    total_invoices: int = 0
    total_audited: int = 0
    total_audited_this_month: int = 0
    pending_audit: int = 0
    high_risk_count: int = 0    # risk_score >= 70
    medium_risk_count: int = 0  # risk_score 30-69
    low_risk_count: int = 0     # risk_score 1-29
    secure_count: int = 0       # risk_score == 0
    avg_risk_score: float = 0.0

class DepartmentInvoiceResponse(BaseModel):
    id: UUID
    internal_number: Optional[str]
    contractor_name: Optional[str]
    issue_date: Optional[datetime]
    net_amount: Optional[Decimal]
    gross_amount: Optional[Decimal] = None
    status: Optional[str] = None
    is_paid: Optional[bool] = None
    invoice_type: Optional[str] = None
    model_config = ConfigDict(from_attributes=True)

class BatchAuditResponse(BaseModel):
    processed_count: int
    invoices: list[InvoiceResponse]

# Report Schemas
class ReportCreate(BaseModel):
    template_name: str
    parameters: Optional[Dict[str, Any]] = None

class ReportResponse(BaseModel):
    id: UUID
    company_id: UUID
    user_id: UUID
    template_name: str
    parameters: Optional[Dict[str, Any]]
    generated_sql: str
    data_records: Optional[list] = None
    narrative_text: str
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)

# Strategy Schemas
class StrategyRequest(BaseModel):
    prompt: str
    date_from: Optional[str] = None
    date_to: Optional[str] = None

class StrategyResponse(BaseModel):
    status: str
    message: Optional[str] = None
    sql_executed: Optional[str] = None
    data_count: Optional[int] = None
    result: Optional[Dict[str, Any]] = None

class StrategyHistoryCreate(BaseModel):
    prompt: str
    date_from: Optional[str] = None
    date_to: Optional[str] = None
    response_data: Dict[str, Any]

class StrategyHistoryResponse(BaseModel):
    id: UUID
    company_id: UUID
    user_id: UUID
    prompt: str
    date_from: Optional[str]
    date_to: Optional[str]
    response_data: Dict[str, Any]
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)

# Notification Schemas
class NotificationResponse(BaseModel):
    id: int
    company_id: UUID
    department_id: Optional[UUID]
    type: str
    title: str
    message: str
    is_read: bool
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)

class DepartmentBudgetResponse(BaseModel):
    id: UUID
    name: str
    monthly_limit: Optional[Decimal]
    # Koszty bieżącego miesiąca (invoice_type == "expense")
    current_spent: Decimal
    # Przychody bieżącego miesiąca (invoice_type == "income")
    current_earned: Decimal = Decimal(0)
    # Charakter oddziału wg historycznego rozkładu faktur (ostatnie 90 dni):
    # "cost" | "revenue" | "mixed" | "empty"
    department_type: str = "empty"
    model_config = ConfigDict(from_attributes=True)

# Anomaly Schemas
class AnomalyItem(BaseModel):
    title: str = Field(description="Krótki, chwytliwy tytuł, np. 'Podejrzany duplikat faktury'")
    risk_level: str = Field(description="Ścisły wybór: 'Wysoki', 'Średni' lub 'Niski'")
    description: str = Field(description="Uzasadnienie z kontekstem, np. 'Dwie faktury na kwotę 12 500 PLN od TechFlow w ciągu 24h. Zwykle płacimy im raz w miesiącu.'")
    contractor_name: str
    amount: float
    time_context: str = Field(description="Format przyjazny dla UI, np. 'Wczoraj', '3 dni temu'")
    template_category: str = Field(description="Jedna z 20 kategorii zdefiniowanych w systemie, np. 'Duplikaty (Kwota/Data)'")

class AnomalyReport(BaseModel):
    summary: str = Field(description="Zwięzłe podsumowanie audytu dla Prezesa (np. 'Wykryto 3 anomalie o łącznej wartości 57 850 PLN. Wymagana pilna weryfikacja TechFlow S.A.')")
    total_anomalies_count: int
    anomalies: list[AnomalyItem]

class AnomalyRequest(BaseModel):
    queryKey: str
    parameters: Optional[Dict[str, str]] = None

# Taxes Schemas
class TaxCitation(BaseModel):
    label: str = Field(description="Sygnatura, artykuł lub wyrok, np. 'Art. 86a VAT'")
    code: str = Field(description="Sygnatura sprawy lub ID wewnętrzne, używane jako klucz (np. 'art-86a')")

class TaxSection(BaseModel):
    heading: str = Field(description="Poważny nagłówek, musi być to 'Stan Prawny' lub 'Interpretacje DKIS / Orzecznictwo'")
    content: str = Field(description="Zwięzła, konkretna treść wypunktowana w Markdownie")
    citations: Optional[list[TaxCitation]] = None

class TaxAnalysisReport(BaseModel):
    title: str = Field(description="Tytuł analizy w kontekście pytania, np. 'Odliczenie VAT od paliwa - Auto Mieszane'")
    sections: Optional[list[TaxSection]] = None

class TaxRequest(BaseModel):
    queryKey: str
    parameters: Optional[Dict[str, str]] = None

# CFO Terminal Schemas
class CfoChartPoint(BaseModel):
    month: str = Field(description="Miesiąc, np. 'Styczeń'")
    baseline_cashflow: float
    simulated_cashflow: float
    simulated_income: float
    simulated_expenses: float
    math_reasoning: str = Field(description="Wyliczenia pokazujace skąd wzięły się wartości w tym punkcie")
    event_trigger: str = Field(description="Wydarzenie zagrażające lub szansa w danym miesiącu")
    confidence_score: float = Field(description="Pewność systemu co do odczytu np 0.95")

class CfoScenarioAnalysis(BaseModel):
    name: str = Field(description="Zawsze jedna z trzech nazw powiązanych ze scenariuszem A, B lub C")
    processed_raw_context: str = Field(description="Przetworzony Surowy Kontekst: Lista zidentyfikowanych w danych wejściowych podmiotów, ram czasowych i metryk od A do Z, bez własnych wniosków.")
    internal_analysis: str = Field(description="Wewnętrzna analiza i tok myślenia agenta, wyjaśniający dlaczego przyjął takie założenia.")
    impact_description: str = Field(description="Krótki opisu skutków")
    risk_level: str = Field(description="Ścisły wybór: 'Wysokie', 'Krytyczne', 'Niskie'")
    health_score: int = Field(description="Ocena od 0 do 100 określająca kondycję i rentowność spółki w tym scenariuszu.")
    chart_data: list[CfoChartPoint] = Field(description="Tabela z projekcją na min 6 miesięcy specjalnie dla Tego Konkretnego Scenariusza")

class StrategyStep(BaseModel):
    timeframe: str = Field(description="np. 'Tydzień 1-2 (Kwiecień)'")
    action_title: str = Field(description="np. 'Renegocjacja kontraktu AWS'")
    description: str = Field(description="Konkretne polecenie, np. 'Skontaktuj się z supportem AWS i przejdź na plan roczny, aby uwolnić 15k PLN gotówki.'")
    owner: str = Field(description="Kto ma to zrobić? np. 'Dział IT', 'Zarząd', 'Marketing'")

class CfoActionPlan(BaseModel):
    opportunities: list[str] = Field(description="Lista zidentyfikowanych szans (np. na optymalizację)")
    risks: list[str] = Field(description="Zagrożenia i słabe punkty akcji")
    efficiency_metrics: list[str] = Field(description="Twarde metryki, np. Przewidywany ROI na poziomie Y% w X miesiącu.")
    execution_playbook: list[StrategyStep] = Field(description="Krok po kroku, jak wdrożyć Złoty Środek w życie.")

class CfoDataSource(BaseModel):
    id: str
    type: str = Field(description="Typ źródła: 'invoice', 'summary', 'db_row'")
    label: str = Field(description="Główna etykieta kafelka (np. kontrahent)")
    value: str = Field(description="Wartość kafelka (np. kwota)")
    date: Optional[str] = None
    details: Optional[str] = None

class CfoReport(BaseModel):
    internal_thought_process: str = Field(description="Niewidoczne dla prezesa miejsce do Chain of Thought (policzenie wariantów na sucho)")
    executive_summary: str = Field(description="Twarde, ustrukturyzowane podsumowanie dla Prezesa (decyzja biznesowa)")
    hero_recommendation: str = Field(description="Główna, potężna rekomendacja 'Droga Majora', jasne wskazanie optymalnej drogi. (np. 1-2 zdania)")
    scenarios_analysis: list[CfoScenarioAnalysis] = Field(description="Dokładnie 3 scenariusze wg wytycznych (w tym Droga Majora jako jedna z nich)")
    major_action_plan: CfoActionPlan = Field(description="Kompletny plan akcji dla rekomendowanego rozwiazania.")
    data_sources: list[CfoDataSource] = Field(default_factory=list, description="Lista faktur/danych wpływających na kontekst")
    layout: Optional[list[Dict[str, Any]]] = Field(default=None, description="Generative UI: Tablica klocków UI wygenerowana przez Major CFO")

class CfoRequest(BaseModel):
    prompt: str
    document_ids: list[str] = Field(default_factory=list, description="IDs dokumentów (z tabeli documents) załączonych przez użytkownika")
    insight_id: Optional[str] = Field(None, description="ID ProactiveInsight — wstrzykuje kontekst insightu do agenta")

class SharedCfoReportCreate(BaseModel):
    prompt: Optional[str] = None
    report_data: Dict[str, Any]

class SharedCfoReportResponse(BaseModel):
    id: UUID
    prompt: Optional[str] = None
    report_data: Dict[str, Any]
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)

# ─────────────────────────────────────────────────────────────────────────────
# GENERATIVE UI — UIBlock Models
# ─────────────────────────────────────────────────────────────────────────────

class ChartDataPoint(BaseModel):
    label: str
    value: float
    value2: Optional[float] = None  # for multi-series
    category: Optional[str] = None  # for stacked/grouped

class MetricItem(BaseModel):
    label: str
    value: str
    trend: Optional[str] = None          # "up" | "down" | "neutral"
    trend_label: Optional[str] = None
    color: Optional[str] = None          # "purple" | "green" | "red" | "yellow"

class TimelineStep(BaseModel):
    timeframe: str
    action_title: str
    description: str
    owner: Optional[str] = None

class TableRow(BaseModel):
    cells: list[str]

class CarouselItem(BaseModel):
    title: str
    body: str

class DecisionPath(BaseModel):
    condition: str = Field(description="Warunek (przypadek if)")
    action: str = Field(description="Akcja / decyzja do podjęcia")
    outcome: str = Field(description="Opis rezultatu")
    impact_score: Optional[int] = None

class PaymentEvent(BaseModel):
    date: str = Field(description="Data płatności np. '2026-04-10'")
    amount: Decimal
    contractor: str
    direction: str = Field(description="Kierunek: 'in' (wpływ) lub 'out' (wydatek)")

# ── Single UIBlock schema (Flattened to avoid Gemini Union/Discriminator Schema errors) ──

class UIBlock(BaseModel):
    block_type: str = Field(description="Typ klocka: 'alert_banner', 'text_card', 'chart', 'metric_grid', 'timeline', 'data_table', 'parsed_data_list', 'scoring', 'carousel', 'swot_analysis', 'decision_tree', 'payment_calendar', 'date_range_request'")
    title: Optional[str] = Field(None, description="Tytuł klocka")
    subtitle: Optional[str] = None
    body: Optional[str] = Field(None, description="Główna treść tekstowa")
    message: Optional[str] = Field(None, description="Treść wiadomości (alert_banner)")
    urgency: Optional[str] = Field(None, description="Wybór: 'high', 'medium', 'low', 'success'")
    theme: Optional[str] = Field(None, description="Wybór: 'neutral', 'success', 'warning', 'danger'")
    chart_type: Optional[str] = Field(None, description="Wybór: 'line', 'area', 'prediction_band', 'bar_vertical', 'bar_horizontal', 'waterfall', 'donut', 'pie', 'stacked_bar', 'combo'")
    data: Optional[list[ChartDataPoint]] = Field(None, description="Dane wykresu")
    series_labels: Optional[list[str]] = None
    metrics: Optional[list[MetricItem]] = None
    steps: Optional[list[TimelineStep]] = None
    columns: Optional[list[str]] = Field(None, description="Kolumny tabeli z danymi")
    rows: Optional[list[TableRow]] = Field(None, description="Wiersze ze stringami")
    items: Optional[list[str]] = Field(None, description="Lista faktów / tekstu dla parsed_data_list")
    carousel_items: Optional[list[CarouselItem]] = Field(None, description="Lista kart karuzeli (CarouselBlock)")
    score: Optional[int] = Field(None, description="Wynik 0-100")
    label: Optional[str] = None
    risk_level: Optional[str] = None
    details: Optional[str] = None
    strengths: Optional[list[str]] = None
    weaknesses: Optional[list[str]] = None
    opportunities: Optional[list[str]] = None
    threats: Optional[list[str]] = None
    paths: Optional[list[DecisionPath]] = None
    payments: Optional[list[PaymentEvent]] = None
    # date_range_request — interactive: AI asks user to pick dates
    question: Optional[str] = Field(None, description="Pytanie do usera (date_range_request)")
    picker_type: Optional[str] = Field(None, description="'range' | 'single' | 'month'")

# ── MajorCfoOutput (Generative UI output) i Agentic Workflow ─────────────────

class TriageDecision(BaseModel):
    route: Literal["instant", "analysis", "strategic_debate"] = Field(description="Typ zapytania: instant (proste fakty), analysis (analiza jednego zagadnienia), strategic_debate (złożone dylematy)")
    category: Literal["analiza", "raportowanie", "strategia", "anomalie", "podatki"] = Field(description="WYMAGANE. Kategoria tematyczna zapytania: analiza | raportowanie | strategia | anomalie | podatki")
    mode: Literal["INVESTMENT", "DIAGNOSTIC"] = Field(default="DIAGNOSTIC", description="INVESTMENT jeśli użytkownik proponuje wydatek/inwestycję/zatrudnienie. DIAGNOSTIC jeśli pyta o stan/problem/analizę.")
    question_type: Literal["single_fact", "comparison", "trend", "forecast", "diagnostic", "recommendation"] = Field(default="single_fact", description="Typ pytania: single_fact (ile/jaki), comparison (porównaj X vs Y), trend (jak się zmienia), forecast (co będzie jeśli), diagnostic (dlaczego), recommendation (co powinienem)")
    output_complexity: Literal["simple", "multi_part", "comparison"] = Field(default="simple", description="simple=1 odpowiedź, multi_part=pytanie ma X części, comparison=porównanie N opcji")
    reasoning: str = Field(description="Krótkie uzasadnienie wyboru")

class AgentTimelineStep(BaseModel):
    agent_name: str = Field(description="Osoba/Agent wykonujący krok (np. 'System Triage', 'Agent Optymista', 'Major CFO')")
    timestamp: str = Field(description="Znacznik czasu lub Faza (np. 'Faza Zbierania Danych' lub 'T+1s')")
    step_type: Literal["reasoning", "tool_call", "tool_result", "system"]
    content: str = Field(description="Rozbudowany, zrozumiały opis narracyjny dla ludzi (np. 'Postanowiłem zejść do bazy i zweryfikować koszty marketingowe...')")
    tool_name: Optional[str] = Field(None, description="Opcjonalnie nazwa symulowanego narzędzia (np. 'SQL', 'Corporate Memory')")
    tool_args: Optional[str] = Field(None, description="Opcjonalnie użyty kod, parametry lub wynik")

class TextResponse(BaseModel):
    response_type: Literal["chat"]
    message: str = Field(description="Zwięzła odpowiedź tekstowa, gdy nie potrzeba wykresów.")
    agent_timeline: list[AgentTimelineStep] = Field(default_factory=list, description="Ułóż spójną oś czasu tłumaczącą krok po kroku jak doszedłeś do odpowiedzi - od fazy Triage do końca.")

class DashboardResponse(BaseModel):
    response_type: Literal["dashboard"]
    hero_recommendation: Optional[str] = Field(None, description="Twarde, 1-2 zdaniowe podsumowanie. To jest 'Główna Latarnia' prezentowana na górze raportu.")
    executive_summary: str = Field(description="Podsumowanie zarządcze sytuacji.")
    # Legacy fields — frontend renderuje tylko hero_recommendation, executive_summary, layout, agent_timeline.
    # Opcjonalne aby Gemini nie failował validacji gdy nie zmieści się w tokenach / kontekście.
    major_action_plan: Optional[CfoActionPlan] = Field(default=None, description="[Opcjonalne] Plan działania zidentyfikowany przez agentów AI.")
    scenarios_analysis: list[CfoScenarioAnalysis] = Field(default_factory=list, description="[Opcjonalne] Analizy scenariuszowe wykonane przez AI.")
    layout: list[UIBlock] = Field(description="Lista komponentów interfejsu (wykresy, tabele, KPI). MIN 1 blok.")
    agent_timeline: list[AgentTimelineStep] = Field(default_factory=list, description="Zbuduj narracyjną oś czasu (Timeline) krok-po-kroku, tłumacząc co Ty oraz inni agenci robiliście w tle (jakie SQL szły do bazy). Zrozumiale, dla ludzi.")

class MajorCfoOutput(BaseModel):
    response: Union[TextResponse, DashboardResponse] = Field(description="Wybierz chat dla prostej odpowiedzi lub dashboard dla analiz")

class AgenticCfoResponse(BaseModel):
    response: Union[TextResponse, DashboardResponse]

class MajorCfoLayoutOutput(BaseModel):
    """Output Majora CFO zawierający Generative UI layout."""
    internal_thought_process: str = Field(
        description="PRYWATNE: Synteza raportów 3 Sub-Agentów. Policz Złoty Środek. Wybierz tryb (Inwestycja/Diagnostyka). Uzasadnij wybór klocków."
    )
    executive_summary: str = Field(
        description="Twarde, 1-2 zdaniowe podsumowanie Drogi Majora. To jest 'Główna Latarnia' prezentowana na górze raportu."
    )
    layout: list[UIBlock] = Field(
        description="Tablica klocków UI. Ułóż je w logicznej kolejności zgodnie z regułami Constrained Autonomy."
    )

# ─────────────────────────────────────────────────────────────────────────────
# DOCUMENT INDEXER — Structured Output (Mały Agent Indeksujący)
# ─────────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
# MORNING BRIEFING — Proactive Night Shift Schemas
# ─────────────────────────────────────────────────────────────────────────────

class ProactiveInsightSchema(BaseModel):
    title: str = Field(description="Krótki, uderzający tytuł insightu (np. 'Cashflow pod presją: DSO rośnie')")
    description: str = Field(description="Zwięzły, analityczny opis problemu/szansy z twardymi danymi")
    suggested_prompt: str = Field(description="Komenda-prompt uruchamiająca pełną analizę agentów CFO (np. 'Przeanalizuj wzrost DSO o 12 dni...')")
    urgency: str = Field(description="Pilność: 'low', 'medium' lub 'high'")

class DailyBriefingSchema(BaseModel):
    summary: str = Field(description="Jedno zdanie podsumowania porannej odprawy — executive brief dla CFO (max 3 zdania, twarde liczby, najważniejszy sygnał dnia)")
    insights: list[ProactiveInsightSchema] = Field(description="Dokładnie 3 insighty z Porannej Odprawy")

class ProactiveInsightResponse(BaseModel):
    id: UUID
    company_id: UUID
    title: str
    description: str
    suggested_prompt: str
    urgency: str
    summary: str | None = None
    insight_type: str = "morning"  # tax_eye | cfo_radar | morning
    legal_basis: str | None = None
    estimated_savings: float | None = None
    source_invoice_id: UUID | None = None
    is_dismissed: bool = False
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)

class MorningBriefingResponse(BaseModel):
    summary: str | None = None
    insights: list[ProactiveInsightResponse]

class InsightsSummaryResponse(BaseModel):
    total_insights: int
    total_savings_pln: float
    tax_eye_count: int
    cfo_radar_count: int
    top_insight: ProactiveInsightResponse | None = None


# ─────────────────────────────────────────────────────────────────────────────
# CONVERSATION / CHAT — Ciągły czat CFO
# ─────────────────────────────────────────────────────────────────────────────

class ConversationCreate(BaseModel):
    title: Optional[str] = None  # Jeśli None, auto-generowany z pierwszej wiadomości

class ConversationResponse(BaseModel):
    id: UUID
    company_id: UUID
    user_id: UUID
    title: Optional[str]
    category: Optional[str] = None
    summary: Optional[str]
    message_count: int
    is_archived: bool
    created_at: datetime
    updated_at: datetime
    model_config = ConfigDict(from_attributes=True)

class ConversationListItem(BaseModel):
    """Lekki schemat do listy wątków w sidebarze."""
    id: UUID
    title: Optional[str]
    category: Optional[str] = None
    message_count: int
    is_archived: bool
    created_at: datetime
    updated_at: datetime
    model_config = ConfigDict(from_attributes=True)

class MessageCreate(BaseModel):
    content: str
    document_ids: list[str] = Field(default_factory=list, description="IDs dokumentów załączonych do wiadomości")

class MessageResponse(BaseModel):
    id: UUID
    conversation_id: UUID
    role: str
    content: str
    metadata_json: Optional[Dict[str, Any]] = Field(None, alias="metadata_json")
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)

class ConversationWithMessages(BaseModel):
    """Pełny wątek z wiadomościami."""
    id: UUID
    company_id: UUID
    user_id: UUID
    title: Optional[str]
    summary: Optional[str]
    message_count: int
    is_archived: bool
    created_at: datetime
    updated_at: datetime
    messages: list[MessageResponse]
    model_config = ConfigDict(from_attributes=True)


# ─────────────────────────────────────────────────────────────────────────────
# BILLING / STRIPE
# ─────────────────────────────────────────────────────────────────────────────

class BillingCheckoutRequest(BaseModel):
    price_id: Optional[str] = None
    success_url: Optional[str] = None
    cancel_url: Optional[str] = None


class BillingCheckoutResponse(BaseModel):
    session_id: str
    checkout_url: str


class BillingPortalRequest(BaseModel):
    return_url: Optional[str] = None


class BillingPortalResponse(BaseModel):
    portal_url: str


class BillingInvoiceItem(BaseModel):
    id: str
    number: Optional[str] = None
    status: Optional[str] = None
    amount_paid: Decimal
    currency: str
    created_at: datetime
    invoice_pdf_url: Optional[str] = None
    hosted_invoice_url: Optional[str] = None


class BillingStatusResponse(BaseModel):
    stripe_customer_id: Optional[str] = None
    stripe_subscription_id: Optional[str] = None
    price_id: Optional[str] = None
    status: str = "inactive"
    currency: Optional[str] = None
    interval: Optional[str] = None
    amount: Optional[Decimal] = None
    cancel_at_period_end: bool = False
    current_period_end: Optional[datetime] = None
    next_charge_amount: Optional[Decimal] = None
    next_charge_date: Optional[datetime] = None
    invoices: list[BillingInvoiceItem] = Field(default_factory=list)


class BillingPlanItemResponse(BaseModel):
    stripe_product_id: str
    stripe_price_id: str
    name: str
    description: Optional[str] = None
    currency: str
    interval: Optional[str] = None
    interval_count: Optional[int] = None
    amount: Optional[Decimal] = None
    is_active: bool = True


class BillingPlansResponse(BaseModel):
    plans: list[BillingPlanItemResponse] = Field(default_factory=list)


class BillingPlanSyncResponse(BaseModel):
    synced_count: int


class PublicCheckoutRequest(BaseModel):
    email: EmailStr
    price_id: Optional[str] = None


class PublicCheckoutResponse(BaseModel):
    checkout_url: str


class DocumentSummaryOutput(BaseModel):
    """Schema wymuszająca na gemini-2.5-flash zwrócenie skompresowanych faktów finansowych z dokumentu."""
    typ_dokumentu: str = Field(
        description="Typ dokumentu (np. 'Umowa handlowa', 'Wyciąg bankowy mBank 01-03/2026', 'Faktura VAT')"
    )
    wartosc_finansowa: str = Field(
        description="Główna kwota lub zakres kwot z walutą (np. 'Wartość kontraktu: 245,000 PLN netto + VAT')"
    )
    krytyczne_terminy: str = Field(
        description="Daty, deadliny, terminy płatności, okresy wypowiedzenia"
    )
    anomalie_lub_ryzyka: str = Field(
        description="Anomalie, kary umowne, ukryte opłaty, podejrzane transakcje, niekorzystne warunki"
    )
    ai_reasoning: str = Field(
        description="Chain of Thought: dlaczego wyciągnięte dane są istotne dla CFO, co powinno zapalić czerwoną lampkę"
    )
