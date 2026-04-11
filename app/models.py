import uuid
from datetime import datetime
from sqlalchemy import String, Text, DateTime, Numeric, Boolean, Integer, Uuid, JSON, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.ext.asyncio import AsyncAttrs
from app.database import Base

class Company(AsyncAttrs, Base):
    __tablename__ = "companies"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String, nullable=False)
    nip: Mapped[str] = mapped_column(String, unique=True, index=True, nullable=False)
    industry: Mapped[str | None] = mapped_column(String, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    ksef_token: Mapped[str | None] = mapped_column(String, nullable=True)
    total_budget: Mapped[float | None] = mapped_column(Numeric(10, 2), nullable=True)
    legal_form: Mapped[str | None] = mapped_column(String, nullable=True)
    taxation_form: Mapped[str | None] = mapped_column(String, nullable=True)
    vat_status: Mapped[str | None] = mapped_column(String, nullable=True)
    employee_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    zus_type: Mapped[str | None] = mapped_column(String, nullable=True)
    stripe_customer_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    stripe_subscription_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    stripe_price_id: Mapped[str | None] = mapped_column(String, nullable=True)
    billing_status: Mapped[str] = mapped_column(String, default="inactive")
    billing_currency: Mapped[str | None] = mapped_column(String, nullable=True)
    billing_interval: Mapped[str | None] = mapped_column(String, nullable=True)
    billing_amount: Mapped[float | None] = mapped_column(Numeric(10, 2), nullable=True)
    billing_current_period_end: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    billing_cancel_at_period_end: Mapped[bool] = mapped_column(Boolean, default=False)
    billing_last_event_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    users: Mapped[list["User"]] = relationship("User", back_populates="company")
    invoices: Mapped[list["Invoice"]] = relationship("Invoice", back_populates="company")
    billing_subscriptions: Mapped[list["BillingSubscription"]] = relationship(
        "BillingSubscription",
        back_populates="company",
        cascade="all, delete-orphan",
    )
    billing_payments: Mapped[list["BillingPayment"]] = relationship(
        "BillingPayment",
        back_populates="company",
        cascade="all, delete-orphan",
    )
    billing_invoices: Mapped[list["BillingInvoice"]] = relationship(
        "BillingInvoice",
        back_populates="company",
        cascade="all, delete-orphan",
    )
    departments: Mapped[list["Department"]] = relationship("Department", back_populates="company", cascade="all, delete-orphan")
    documents: Mapped[list["Document"]] = relationship("Document", back_populates="company", cascade="all, delete-orphan")
    company_documents: Mapped[list["CompanyDocument"]] = relationship("CompanyDocument", back_populates="company", cascade="all, delete-orphan")
    company_contexts: Mapped[list["CompanyContextModel"]] = relationship("CompanyContextModel", back_populates="company", cascade="all, delete-orphan")
    context_snapshots: Mapped[list["ContextSnapshotModel"]] = relationship("ContextSnapshotModel", back_populates="company", cascade="all, delete-orphan")

class CompanyContextModel(AsyncAttrs, Base):
    """
    Fizyczna globalna pamięć korporacyjna powiązana z firmą.
    Przechowuje zamrożony stan agenta (obiekt JSON oparty na schemacie Pydantic).

    3-warstwowa architektura:
    - context_data: legacy blob (zachowany dla backward compat)
    - canonical_data: L1 — tożsamość i struktura firmy (wolno-zmienne)
    - operational_data: L2 — bieżący stan operacyjny (szybko-zmienne)
    """
    __tablename__ = "company_contexts"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"), nullable=False, unique=True, index=True)
    context_data: Mapped[dict] = mapped_column(JSON, nullable=False)  # Legacy — zachowany dla backward compat
    last_updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Warstwa 1 — Kontekst Kanoniczny (tożsamość, model biznesu, strategia)
    canonical_data: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    canonical_version: Mapped[int] = mapped_column(Integer, default=1)
    canonical_updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Warstwa 2 — Kontekst Operacyjny (bieżący stan finansowy, ryzyka, cele)
    operational_data: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    operational_version: Mapped[int] = mapped_column(Integer, default=1)
    operational_updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    company: Mapped["Company"] = relationship("Company", back_populates="company_contexts")


class ContextSnapshotModel(AsyncAttrs, Base):
    """
    Warstwa 3 — historyczne snapshoty kontekstu operacyjnego.
    Tygodniowe (bez AI) i miesięczne (z AI summary) zamrożenia stanu L2.
    """
    __tablename__ = "context_snapshots"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"), nullable=False, index=True)
    period_type: Mapped[str] = mapped_column(String, nullable=False)  # 'weekly' | 'monthly'
    period_start: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    period_end: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    operational_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)  # Zamrożony OperationalContext
    ai_summary: Mapped[str | None] = mapped_column(Text, nullable=True)  # AI synthesis (tylko monthly)
    key_metrics: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # { cash_on_hand, mrr, net_burn_rate }
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    company: Mapped["Company"] = relationship("Company", back_populates="context_snapshots")

class Department(AsyncAttrs, Base):
    __tablename__ = "departments"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    monthly_limit: Mapped[float | None] = mapped_column(Numeric(10, 2), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    company: Mapped["Company"] = relationship("Company", back_populates="departments")
    invoices: Mapped[list["Invoice"]] = relationship("Invoice", back_populates="department")

class User(AsyncAttrs, Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True) # From Supabase Auth
    email: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    first_name: Mapped[str | None] = mapped_column(String, nullable=True)
    last_name: Mapped[str | None] = mapped_column(String, nullable=True)
    company_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("companies.id"), nullable=True)
    stripe_customer_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    stripe_subscription_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    company: Mapped["Company"] = relationship("Company", back_populates="users")
    invoices: Mapped[list["Invoice"]] = relationship("Invoice", back_populates="user")
    billing_subscriptions: Mapped[list["BillingSubscription"]] = relationship(
        "BillingSubscription",
        back_populates="user",
    )
    billing_payments: Mapped[list["BillingPayment"]] = relationship(
        "BillingPayment",
        back_populates="user",
    )
    billing_invoices: Mapped[list["BillingInvoice"]] = relationship(
        "BillingInvoice",
        back_populates="user",
    )


class BillingPlan(AsyncAttrs, Base):
    __tablename__ = "billing_plans"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    stripe_product_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    stripe_price_id: Mapped[str] = mapped_column(String, nullable=False, unique=True, index=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    currency: Mapped[str] = mapped_column(String, default="usd")
    interval: Mapped[str | None] = mapped_column(String, nullable=True)
    interval_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    amount: Mapped[float | None] = mapped_column(Numeric(10, 2), nullable=True)
    metadata_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    subscriptions: Mapped[list["BillingSubscription"]] = relationship("BillingSubscription", back_populates="plan")


class BillingSubscription(AsyncAttrs, Base):
    __tablename__ = "billing_subscriptions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"), nullable=False, index=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    plan_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("billing_plans.id"), nullable=True, index=True)
    stripe_subscription_id: Mapped[str] = mapped_column(String, nullable=False, unique=True, index=True)
    stripe_customer_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    status: Mapped[str] = mapped_column(String, default="incomplete")
    current_period_start: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    current_period_end: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    cancel_at_period_end: Mapped[bool] = mapped_column(Boolean, default=False)
    canceled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    trial_end: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    company: Mapped["Company"] = relationship("Company", back_populates="billing_subscriptions")
    user: Mapped["User"] = relationship("User", back_populates="billing_subscriptions")
    plan: Mapped["BillingPlan"] = relationship("BillingPlan", back_populates="subscriptions")
    payments: Mapped[list["BillingPayment"]] = relationship(
        "BillingPayment",
        back_populates="subscription",
        cascade="all, delete-orphan",
    )
    invoices: Mapped[list["BillingInvoice"]] = relationship(
        "BillingInvoice",
        back_populates="subscription",
        cascade="all, delete-orphan",
    )


class BillingPayment(AsyncAttrs, Base):
    __tablename__ = "billing_payments"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"), nullable=False, index=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    subscription_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("billing_subscriptions.id"), nullable=True, index=True)
    stripe_invoice_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    stripe_payment_intent_id: Mapped[str | None] = mapped_column(String, nullable=True, unique=True, index=True)
    amount: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String, default="USD")
    status: Mapped[str] = mapped_column(String, default="pending")
    paid_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    failure_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    company: Mapped["Company"] = relationship("Company", back_populates="billing_payments")
    user: Mapped["User"] = relationship("User", back_populates="billing_payments")
    subscription: Mapped["BillingSubscription"] = relationship("BillingSubscription", back_populates="payments")


class BillingInvoice(AsyncAttrs, Base):
    __tablename__ = "billing_invoices"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"), nullable=False, index=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    subscription_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("billing_subscriptions.id"), nullable=True, index=True)
    stripe_invoice_id: Mapped[str] = mapped_column(String, nullable=False, unique=True, index=True)
    number: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default="draft")
    amount_due: Mapped[float | None] = mapped_column(Numeric(10, 2), nullable=True)
    amount_paid: Mapped[float | None] = mapped_column(Numeric(10, 2), nullable=True)
    currency: Mapped[str] = mapped_column(String, default="USD")
    invoice_pdf_url: Mapped[str | None] = mapped_column(String, nullable=True)
    hosted_invoice_url: Mapped[str | None] = mapped_column(String, nullable=True)
    period_start: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    period_end: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    due_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    company: Mapped["Company"] = relationship("Company", back_populates="billing_invoices")
    user: Mapped["User"] = relationship("User", back_populates="billing_invoices")
    subscription: Mapped["BillingSubscription"] = relationship("BillingSubscription", back_populates="invoices")


class StripeWebhookEventLog(AsyncAttrs, Base):
    __tablename__ = "stripe_webhook_event_logs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    event_id: Mapped[str] = mapped_column(String, nullable=False, unique=True, index=True)
    event_type: Mapped[str] = mapped_column(String, nullable=False, index=True)
    company_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("companies.id"), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default="received")
    customer_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    received_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    company: Mapped["Company"] = relationship("Company")

class Invoice(AsyncAttrs, Base):
    __tablename__ = "invoices"

    # Identity
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"), nullable=False)
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)

    # File Data
    filename: Mapped[str] = mapped_column(String, nullable=False)
    file_url: Mapped[str | None] = mapped_column(String, nullable=True)
    ksef_xml: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Financials (KSeF)
    ksef_number: Mapped[str | None] = mapped_column(String, unique=True, nullable=True)
    internal_number: Mapped[str | None] = mapped_column(String, nullable=True)
    contractor_name: Mapped[str | None] = mapped_column(String, nullable=True)
    contractor_nip: Mapped[str | None] = mapped_column(String, nullable=True)
    net_amount: Mapped[float | None] = mapped_column(Numeric(10, 2), nullable=True)
    vat_amount: Mapped[float | None] = mapped_column(Numeric(10, 2), nullable=True)
    gross_amount: Mapped[float | None] = mapped_column(Numeric(10, 2), nullable=True)
    currency: Mapped[str] = mapped_column(String, default="PLN")
    issue_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Status & Categories
    status: Mapped[str] = mapped_column(String, default="pending")
    invoice_type: Mapped[str] = mapped_column(String, default="expense")
    is_vat_exempt: Mapped[bool] = mapped_column(Boolean, default=False)
    department_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("departments.id"), nullable=True)
    category_name: Mapped[str | None] = mapped_column(String, nullable=True)
    is_paid: Mapped[bool] = mapped_column(Boolean, default=False)
    due_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    payment_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    payment_days: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # AI & Audit
    audit_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    risk_score: Mapped[int] = mapped_column(Integer, default=0)
    ai_analysis_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    parsed_data: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    company: Mapped["Company"] = relationship("Company", back_populates="invoices")
    user: Mapped["User"] = relationship("User", back_populates="invoices")
    department: Mapped["Department"] = relationship("Department", back_populates="invoices")
    audit_logs: Mapped[list["AuditLog"]] = relationship("AuditLog", back_populates="invoice", cascade="all, delete-orphan")

class AuditLog(AsyncAttrs, Base):
    __tablename__ = "audit_logs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    invoice_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("invoices.id"), nullable=False, index=True)
    actor: Mapped[str] = mapped_column(String, nullable=False, default="Gemini_2.0_Flash")
    action: Mapped[str] = mapped_column(String, nullable=False)  # audit_started, audit_completed, audit_failed
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    invoice: Mapped["Invoice"] = relationship("Invoice", back_populates="audit_logs")

class Report(AsyncAttrs, Base):
    __tablename__ = "reports"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"), nullable=False, index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    template_name: Mapped[str] = mapped_column(String, nullable=False)
    parameters: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    generated_sql: Mapped[str] = mapped_column(Text, nullable=False)
    data_records: Mapped[list | None] = mapped_column(JSON, nullable=True)
    narrative_text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    company: Mapped["Company"] = relationship("Company")
    user: Mapped["User"] = relationship("User")

class Notification(AsyncAttrs, Base):
    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"), nullable=False, index=True)
    department_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("departments.id"), nullable=True)
    type: Mapped[str] = mapped_column(String, nullable=False) # 'WARNING', 'CRITICAL'
    title: Mapped[str] = mapped_column(String, nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    is_read: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    company: Mapped["Company"] = relationship("Company")
    department: Mapped["Department"] = relationship("Department")

class Document(AsyncAttrs, Base):
    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"), nullable=False, index=True)
    filename: Mapped[str] = mapped_column(String, nullable=False)
    file_type: Mapped[str] = mapped_column(String, nullable=False)
    filepath: Mapped[str] = mapped_column(String, nullable=False) # WebDAV relative path
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    upload_date: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    company: Mapped["Company"] = relationship("Company", back_populates="documents")

class StrategyHistory(AsyncAttrs, Base):
    __tablename__ = "strategy_history"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"), nullable=False, index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    date_from: Mapped[str | None] = mapped_column(String, nullable=True)
    date_to: Mapped[str | None] = mapped_column(String, nullable=True)
    response_data: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    company: Mapped["Company"] = relationship("Company")
    user: Mapped["User"] = relationship("User")

class SharedCfoReport(AsyncAttrs, Base):
    __tablename__ = "shared_cfo_reports"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("companies.id"), nullable=True, index=True)
    prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    report_data: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    company: Mapped["Company"] = relationship("Company")

class ProactiveInsight(AsyncAttrs, Base):
    """Proaktywne insighty generowane przez nocną zmianę (CRON 03:00)."""
    __tablename__ = "proactive_insights"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    suggested_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    urgency: Mapped[str] = mapped_column(String, nullable=False, default="medium")  # low | medium | high
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)  # tylko w pierwszym insighcie na odprawę
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    company: Mapped["Company"] = relationship("Company")


class CompanyDocument(AsyncAttrs, Base):
    """Indeksowane dokumenty firmowe z AI summary (Mały Agent Indeksujący)."""
    __tablename__ = "company_documents"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"), nullable=False, index=True)
    file_name: Mapped[str] = mapped_column(String, nullable=False)
    file_url: Mapped[str] = mapped_column(String, nullable=False)  # WebDAV relative path or full URL
    document_type: Mapped[str | None] = mapped_column(String, nullable=True)  # e.g. "Umowa", "Faktura", "Wyciąg"
    ai_summary: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # JSONB output from gemini-2.5-flash
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    company: Mapped["Company"] = relationship("Company", back_populates="company_documents")
