"""
Warstwa 1 — Kontekst Kanoniczny (L1)
Przechowuje wolno-zmieniające się fakty o tożsamości i strukturze firmy:
tożsamość prawna, reżim podatkowy, model biznesowy, strategia, unit economics, sezonowość.
"""
import os
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from typing import Optional, List

from pydantic import BaseModel, Field
from google import genai
from google.genai import types
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.models import CompanyContextModel
from app.services.context_manager import StrategicCompass, UnitEconomics


# ==============================================================================
# MODELE PYDANTIC — L1 (Kanoniczny)
# ==============================================================================

class CompanyIdentity(BaseModel):
    name: Optional[str] = Field(None, description="Pełna nazwa firmy (np. 'TechSoft Sp. z o.o.')")
    nip: Optional[str] = Field(None, description="NIP firmy (10 cyfr)")
    legal_form: Optional[str] = Field(None, description="Forma prawna: 'Sp. z o.o.', 'JDG', 'S.A.', 'Fundacja'")
    vat_status: Optional[str] = Field(None, description="Status VAT: 'Czynny podatnik VAT', 'Zwolniony z VAT'")
    accounting_method: Optional[str] = Field(None, description="Metoda księgowania: 'Pełna księgowość', 'KPiR', 'Ryczałt ewidencjonowany'")


class TaxRegime(BaseModel):
    taxation_form: Optional[str] = Field(None, description="Forma opodatkowania: 'CIT estoński', 'CIT 19%', 'CIT 9%', 'Liniowy 19%', 'Ryczałt', 'Skala podatkowa'")
    vat_rate: Optional[str] = Field(None, description="Dominująca stawka VAT: '23%', '8%', '5%', 'ZW'")
    jpk_obligation: Optional[bool] = Field(None, description="Czy firma ma obowiązek JPK_VAT")
    cit_rate: Optional[float] = Field(None, description="Stawka CIT w procentach (np. 9.0, 19.0). None dla JDG i CIT estoński")
    pit_form: Optional[str] = Field(None, description="Forma PIT (tylko JDG): 'Liniowy 19%', 'Skala podatkowa', 'Ryczałt'")
    zus_type: Optional[str] = Field(None, description="Typ ZUS: 'pelny', 'preferencyjny', 'maly_zus_plus', 'ulga_na_start'")
    employee_count: Optional[int] = Field(None, description="Liczba pracowników (0 = brak)")
    vat_frequency: Optional[str] = Field(None, description="Częstotliwość VAT: 'monthly', 'quarterly'")
    special_obligations: List[str] = Field(default_factory=list, description="Specjalne obowiązki podatkowe: 'pfron', 'akcyza', 'cukrowa', 'malpki', 'nieruchomosci', 'srodki_transportu', 'srodowiskowa', 'produktowa', 'bdo', 'bankowy', 'gry', 'wht', 'danina_solidarnosciowa', 'min_podatek_dochodowy', 'budynki', 'tonazowy', 'paliwowa', 'rolny', 'audiowizualna'")


class BusinessModelDetail(BaseModel):
    segment: Optional[str] = Field(None, description="Segment rynku: 'B2B', 'B2C', 'B2G', 'B2B2C'")
    model_type: Optional[str] = Field(None, description="Typ modelu: 'SaaS', 'Marketplace', 'Usługi', 'Produkcja', 'Handel'")
    revenue_model: Optional[str] = Field(None, description="Model przychodowy: 'Subskrypcja roczna', 'Jednorazowa', 'Prowizja', 'Projekt'")
    industry: Optional[str] = Field(None, description="Branża: 'IT/Software', 'E-commerce', 'Budownictwo', 'Fintech'")


class CanonicalContext(BaseModel):
    """
    Kontekst kanoniczny — wolno-zmieniające się fakty o tym KIM i CZYM jest firma.
    Aktualizowany ręcznie lub przez AI podczas indeksowania dokumentów.
    """
    company_identity: CompanyIdentity = Field(default_factory=CompanyIdentity)
    tax_regime: TaxRegime = Field(default_factory=TaxRegime)
    business_model: BusinessModelDetail = Field(default_factory=BusinessModelDetail)
    strategic_compass: StrategicCompass = Field(default_factory=StrategicCompass)
    unit_economics: UnitEconomics = Field(default_factory=UnitEconomics)
    seasonality_and_patterns: List[str] = Field(default_factory=list)
    canonical_version: int = Field(default=1)


# ==============================================================================
# LOGIKA MERGE
# ==============================================================================

def merge_canonical(old: CanonicalContext, new: CanonicalContext) -> CanonicalContext:
    """Nowa niezerowa wartość zawsze wygrywa. seasonality_and_patterns: append + dedupe."""
    merged = deepcopy(old)

    # company_identity
    if new.company_identity.name:
        merged.company_identity.name = new.company_identity.name
    if new.company_identity.nip:
        merged.company_identity.nip = new.company_identity.nip
    if new.company_identity.legal_form:
        merged.company_identity.legal_form = new.company_identity.legal_form
    if new.company_identity.vat_status:
        merged.company_identity.vat_status = new.company_identity.vat_status
    if new.company_identity.accounting_method:
        merged.company_identity.accounting_method = new.company_identity.accounting_method

    # tax_regime
    if new.tax_regime.taxation_form:
        merged.tax_regime.taxation_form = new.tax_regime.taxation_form
    if new.tax_regime.vat_rate:
        merged.tax_regime.vat_rate = new.tax_regime.vat_rate
    if new.tax_regime.jpk_obligation is not None:
        merged.tax_regime.jpk_obligation = new.tax_regime.jpk_obligation
    if new.tax_regime.cit_rate is not None:
        merged.tax_regime.cit_rate = new.tax_regime.cit_rate
    if new.tax_regime.pit_form:
        merged.tax_regime.pit_form = new.tax_regime.pit_form
    if new.tax_regime.zus_type:
        merged.tax_regime.zus_type = new.tax_regime.zus_type
    if new.tax_regime.employee_count is not None:
        merged.tax_regime.employee_count = new.tax_regime.employee_count
    if new.tax_regime.vat_frequency:
        merged.tax_regime.vat_frequency = new.tax_regime.vat_frequency
    # special_obligations — append + dedupe
    if new.tax_regime.special_obligations:
        existing_so = {o.lower() for o in merged.tax_regime.special_obligations}
        for ob in new.tax_regime.special_obligations:
            if ob.lower() not in existing_so:
                merged.tax_regime.special_obligations.append(ob)
                existing_so.add(ob.lower())

    # business_model
    if new.business_model.segment:
        merged.business_model.segment = new.business_model.segment
    if new.business_model.model_type:
        merged.business_model.model_type = new.business_model.model_type
    if new.business_model.revenue_model:
        merged.business_model.revenue_model = new.business_model.revenue_model
    if new.business_model.industry:
        merged.business_model.industry = new.business_model.industry

    # strategic_compass
    if new.strategic_compass.primary_goal:
        merged.strategic_compass.primary_goal = new.strategic_compass.primary_goal
    if new.strategic_compass.strategy:
        merged.strategic_compass.strategy = new.strategic_compass.strategy

    # unit_economics
    if new.unit_economics.ltv is not None:
        merged.unit_economics.ltv = new.unit_economics.ltv
    if new.unit_economics.cac is not None:
        merged.unit_economics.cac = new.unit_economics.cac
    if new.unit_economics.gross_margin_pct is not None:
        merged.unit_economics.gross_margin_pct = new.unit_economics.gross_margin_pct
    if new.unit_economics.churn_pct is not None:
        merged.unit_economics.churn_pct = new.unit_economics.churn_pct

    # seasonality_and_patterns — append + dedupe
    existing_lower = {p.lower() for p in merged.seasonality_and_patterns}
    for pattern in new.seasonality_and_patterns:
        if pattern.lower() not in existing_lower:
            merged.seasonality_and_patterns.append(pattern)
            existing_lower.add(pattern.lower())

    merged.canonical_version = old.canonical_version + 1
    return merged


# ==============================================================================
# AI EKSTRAKCJA
# ==============================================================================

async def extract_canonical_from_text(text: str) -> CanonicalContext:
    """Gemini extractor skupiony wyłącznie na L1 — strukturalnych, wolno-zmieniających się faktach."""
    client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))

    system_instruction = """Jesteś specjalistą od profilowania firm. Analizujesz dokumenty i wyciągasz TYLKO strukturalne, wolno-zmieniające się fakty o tożsamości i strukturze firmy.

ZASADY:
1. Nie wymyślaj. Jeśli w tekście nie ma informacji — zostaw null.
2. Skupiaj się na: formie prawnej, NIP, reżimie podatkowym, modelu biznesowym, strategii, unit economics, sezonowości.
3. NIE wyciągaj bieżących danych finansowych (gotówka, MRR, ryzyka) — to należy do kontekstu operacyjnego.
4. Bądź zwięzły i precyzyjny.
5. Wykrywaj obowiązki specjalne: jeśli w tekście pojawiają się wzmianki o opłacie cukrowej, opłacie od małpek, akcyzie, PFRON, podatku od nieruchomości, podatku od środków transportu, opłacie środowiskowej, opłacie produktowej, BDO, podatku bankowym, podatku od gier, WHT, daninie solidarnościowej, minimalnym podatku dochodowym, podatku od budynków, podatku tonażowym, opłacie paliwowej, podatku rolnym, opłacie audiowizualnej — dodaj odpowiedni identyfikator (snake_case) do pola special_obligations w tax_regime.
6. Wykrywaj typ ZUS: jeśli dokument wspomina o uldze na start, preferencyjnym ZUS, Małym ZUS Plus — ustaw pole zus_type ('ulga_na_start', 'preferencyjny', 'maly_zus_plus', 'pelny').
7. Wykrywaj liczbę pracowników: jeśli w dokumencie jest mowa o zatrudnieniu N osób, ustaw employee_count."""

    response = client.models.generate_content(
        model='gemini-2.5-flash',
        contents=text,
        config=types.GenerateContentConfig(
            system_instruction=system_instruction,
            response_mime_type="application/json",
            response_schema=CanonicalContext,
            temperature=0.0,
        ),
    )
    return CanonicalContext.model_validate_json(response.text)


# ==============================================================================
# OPERACJE DB
# ==============================================================================

async def get_or_create_canonical(db: AsyncSession, company_id: uuid.UUID) -> CanonicalContext:
    result = await db.execute(
        select(CompanyContextModel).where(CompanyContextModel.company_id == company_id)
    )
    record = result.scalar_one_or_none()

    if not record:
        empty = CanonicalContext()
        record = CompanyContextModel(
            company_id=company_id,
            context_data={},
            canonical_data=empty.model_dump(),
        )
        db.add(record)
        await db.commit()
        await db.refresh(record)

    if record.canonical_data is None:
        return CanonicalContext()

    try:
        return CanonicalContext.model_validate(record.canonical_data)
    except Exception:
        return CanonicalContext()


async def update_canonical(
    db: AsyncSession,
    company_id: uuid.UUID,
    updates: dict,
) -> CanonicalContext:
    """Manualna aktualizacja L1 bez LLM (z UI lub zewnętrznych narzędzi)."""
    result = await db.execute(
        select(CompanyContextModel).where(CompanyContextModel.company_id == company_id)
    )
    record = result.scalar_one_or_none()

    if not record:
        current = CanonicalContext()
        record = CompanyContextModel(
            company_id=company_id,
            context_data={},
            canonical_data=current.model_dump(),
        )
        db.add(record)
    else:
        try:
            current = CanonicalContext.model_validate(record.canonical_data or {})
        except Exception:
            current = CanonicalContext()

    # Zastosuj aktualizacje przez model_copy
    updated_data = current.model_dump()
    _deep_update(updated_data, updates)
    updated = CanonicalContext.model_validate(updated_data)
    updated.canonical_version = current.canonical_version + 1

    record.canonical_data = updated.model_dump()
    record.canonical_updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(record)
    return updated


async def process_and_upsert_canonical(
    db: AsyncSession,
    company_id: uuid.UUID,
    text_source: str,
) -> CanonicalContext:
    """Ekstrakcja AI + merge + zapis L1."""
    old = await get_or_create_canonical(db, company_id)
    new = await extract_canonical_from_text(text_source)
    merged = merge_canonical(old, new)

    result = await db.execute(
        select(CompanyContextModel).where(CompanyContextModel.company_id == company_id)
    )
    record = result.scalar_one_or_none()
    if record:
        record.canonical_data = merged.model_dump()
        record.canonical_updated_at = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(record)

    return merged


def _deep_update(base: dict, updates: dict) -> None:
    """Rekurencyjna aktualizacja zagnieżdżonego słownika (in-place)."""
    for key, value in updates.items():
        if isinstance(value, dict) and key in base and isinstance(base[key], dict):
            _deep_update(base[key], value)
        else:
            base[key] = value
