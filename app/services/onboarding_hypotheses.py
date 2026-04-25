"""
Generowanie faktów i hipotez z faktur podczas onboardingu.
Fakty: z InvoiceMetrics (bez AI). Hipotezy: 1 call Gemini, structured output.
"""
import json
import logging
import re
import uuid
from typing import Optional, Any
from datetime import datetime

from google import genai
from google.genai import types
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from app.models import CompanyContextModel, Invoice
from app.services.context_operational import OperationalContext
from app.services.context_canonical import CanonicalContext
from app.prompts.onboarding_prompts import HYPOTHESES_GENERATION_PROMPT

logger = logging.getLogger(__name__)

FLASH_MODEL = "gemini-2.5-flash"


# ==============================================================================
# MODELE
# ==============================================================================

class TopContractor(BaseModel):
    name: str
    nip: Optional[str] = None
    total: float
    invoice_count: int


class OnboardingFacts(BaseModel):
    """Fakty z InvoiceMetrics — obliczone, nie AI."""
    safecash: Optional[float] = None
    mrr: Optional[float] = None
    burn_rate: Optional[float] = None
    top_contractors: list[TopContractor] = []
    invoice_count: int = 0
    revenue_this_month: Optional[float] = None
    expenses_this_month: Optional[float] = None


class OnboardingHypothesis(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4())[:8])
    text: str
    confidence: float = Field(ge=0.0, le=1.0)
    category: str  # contractor_pattern | revenue_model | cost_structure | seasonality
    source_invoices: list[str] = []  # invoice IDs
    context_field: str  # which L1/L2 field this would update
    proposed_value: Optional[Any] = None


class HypothesesResponse(BaseModel):
    hypotheses: list[OnboardingHypothesis] = []


class OnboardingReview(BaseModel):
    facts: OnboardingFacts
    hypotheses: list[OnboardingHypothesis]


# ==============================================================================
# FAKTY (bez AI)
# ==============================================================================

async def generate_onboarding_facts(
    db: AsyncSession,
    company_id: uuid.UUID,
) -> OnboardingFacts:
    """Oblicz fakty z faktur — bez AI, czyste zapytania SQL."""
    # Kontekst operacyjny (jeśli istnieje)
    ctx_result = await db.execute(
        select(CompanyContextModel).where(CompanyContextModel.company_id == company_id)
    )
    ctx = ctx_result.scalar_one_or_none()
    operational = None
    if ctx and ctx.operational_data:
        try:
            operational = OperationalContext.model_validate(ctx.operational_data)
        except Exception:
            pass

    # Liczba faktur
    count_result = await db.execute(
        select(func.count(Invoice.id)).where(Invoice.company_id == company_id)
    )
    invoice_count = count_result.scalar() or 0

    # Top kontrahenci (przychody)
    top_q = await db.execute(
        select(
            Invoice.contractor_name,
            Invoice.contractor_nip,
            func.sum(Invoice.gross_amount).label("total"),
            func.count(Invoice.id).label("cnt"),
        )
        .where(Invoice.company_id == company_id, Invoice.invoice_type == "income")
        .group_by(Invoice.contractor_name, Invoice.contractor_nip)
        .order_by(func.sum(Invoice.gross_amount).desc())
        .limit(5)
    )
    top_contractors = [
        TopContractor(
            name=row.contractor_name or "Nieznany",
            nip=row.contractor_nip,
            total=float(row.total or 0),
            invoice_count=row.cnt,
        )
        for row in top_q.fetchall()
    ]

    # Metryki z L2 (jeśli dostępne)
    safecash = None
    mrr = None
    burn_rate = None
    revenue = None
    expenses = None

    if operational:
        pulse = operational.live_financial_pulse
        metrics = operational.invoice_metrics
        safecash = pulse.safecash
        mrr = metrics.auto_mrr or pulse.mrr
        burn_rate = metrics.auto_burn_rate or pulse.net_burn_rate
        revenue = metrics.monthly_revenue
        expenses = metrics.monthly_expenses

    return OnboardingFacts(
        safecash=safecash,
        mrr=mrr,
        burn_rate=burn_rate,
        top_contractors=top_contractors,
        invoice_count=invoice_count,
        revenue_this_month=revenue,
        expenses_this_month=expenses,
    )


# ==============================================================================
# HIPOTEZY (1 call Gemini)
# ==============================================================================

async def generate_onboarding_hypotheses(
    db: AsyncSession,
    company_id: uuid.UUID,
    canonical: CanonicalContext,
) -> list[OnboardingHypothesis]:
    """Generuj hipotezy o firmie na podstawie wzorców faktur."""
    # Pobierz faktury do analizy (max 50)
    inv_result = await db.execute(
        select(Invoice)
        .where(Invoice.company_id == company_id)
        .order_by(Invoice.issue_date.desc())
        .limit(50)
    )
    invoices = inv_result.scalars().all()

    if len(invoices) < 3:
        return []  # Za mało danych na hipotezy

    # Przygotuj dane faktur do promptu
    invoice_data = []
    for inv in invoices:
        invoice_data.append({
            "id": str(inv.id)[:8],
            "type": inv.invoice_type,
            "contractor": inv.contractor_name,
            "contractor_nip": inv.contractor_nip,
            "gross": float(inv.gross_amount) if inv.gross_amount else None,
            "net": float(inv.net_amount) if inv.net_amount else None,
            "category": inv.category_name,
            "date": inv.issue_date.isoformat() if inv.issue_date else None,
            "is_paid": inv.is_paid,
        })

    prompt = HYPOTHESES_GENERATION_PROMPT.format(
        invoice_data=json.dumps(invoice_data, ensure_ascii=False),
        canonical_context=json.dumps(canonical.model_dump(), ensure_ascii=False, default=str),
    )

    client = genai.Client()

    try:
        response = await client.aio.models.generate_content(
            model=FLASH_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=HypothesesResponse,
                temperature=0.0,
            ),
        )
        from app.services._json_parse import safe_model_validate_json
        result = safe_model_validate_json(HypothesesResponse, response.text)
        return _filter_hallucinated_hypotheses(result.hypotheses, invoice_data)
    except Exception as e:
        logger.error(f"Hypothesis generation failed: {e}")
        return []


def _filter_hallucinated_hypotheses(
    hypotheses: list[OnboardingHypothesis],
    invoice_data: list[dict],
) -> list[OnboardingHypothesis]:
    """
    Usuń hipotezy zawierające NIPy lub nazwy kontrahentów których nie ma w rzeczywistych fakturach.
    Chroni przed halucynacjami Gemini (np. wygenerowanie NIPu zagranicznego IE/DE/FR).
    """
    real_nips = {inv.get("contractor_nip") for inv in invoice_data if inv.get("contractor_nip")}
    real_names_lower = {
        (inv.get("contractor") or "").lower().strip()
        for inv in invoice_data
        if inv.get("contractor")
    }

    nip_pattern = re.compile(r"\b([A-Z]{2}?\d{7,12}[A-Z]?)\b")

    valid: list[OnboardingHypothesis] = []
    for h in hypotheses:
        # Wyciągnij wszystkie NIP-podobne tokeny z tekstu hipotezy
        mentioned_nips = set(nip_pattern.findall(h.text))
        hallucinated_nip = False
        for nip in mentioned_nips:
            # Pomijaj krótkie numery które mogą być kwotami
            if len(nip) < 7:
                continue
            if nip not in real_nips:
                logger.warning(f"Dropping hallucinated hypothesis (unknown NIP {nip}): {h.text[:100]}")
                hallucinated_nip = True
                break
        if hallucinated_nip:
            continue

        # Sprawdź czy cytowane nazwy kontrahentów istnieją — gdy hipoteza wspomina "Sp. z o.o." / "SA"
        text_lower = h.text.lower()
        if ("sp. z o.o." in text_lower or "sp. z o. o." in text_lower or " sa " in text_lower or "s.a." in text_lower):
            # Jeśli wspomniano firmę z formą prawną — co najmniej jedna nazwa z faktur musi być w tekście
            if real_names_lower and not any(
                name and name in text_lower for name in real_names_lower
            ):
                logger.warning(f"Dropping hypothesis (unknown contractor name): {h.text[:100]}")
                continue

        valid.append(h)

    return valid
