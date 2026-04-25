"""
DEPRECATED: Plik legacy — zachowany dla backward compatibility podczas migracji.

Nowe implementacje:
- Warstwa 1 (Kanoniczny): app/services/context_canonical.py
- Warstwa 2 (Operacyjny): app/services/context_operational.py
- Warstwa 3 (Snapshoty): app/services/context_snapshots.py
- Agregator dla agentów: app/services/context_assembler.py

Użyj process_and_upsert_operational zamiast process_and_upsert_context.
"""
import os
from typing import List, Optional, Literal
from pydantic import BaseModel, Field
from google import genai
from google.genai import types

# ==============================================================================
# KROK 1: SCHEMAT PYDANTIC (Globalny Stan Firmy - Pamięć Korporacyjna)
# ==============================================================================

class StrategicCompass(BaseModel):
    primary_goal: Optional[str] = Field(None, description="Główny cel firmy, np. 'Runda A Q2 2027'")
    strategy: Optional[str] = Field(None, description="Krótka strategia, np. 'Growth over Profit'")

class CompanyProfile(BaseModel):
    legal_form: Optional[str] = Field(None, description="Forma prawna działalności, np. 'Sp. z o.o.', 'JDG', 'S.A.'")
    taxation_form: Optional[str] = Field(None, description="Forma opodatkowania, np. 'CIT estoński', 'Ryczałt 12%', 'VAT zwolniony'")
    industry: Optional[str] = Field(None, description="Branża, np. 'SaaS B2B', 'E-commerce', 'Budownictwo'")
    business_model: Optional[str] = Field(None, description="Krótki opis modelu biznesowego, np. 'Sprzedaż subskrypcji rocznych', 'Marketplace'")

class LiveFinancialPulse(BaseModel):
    cash_on_hand: Optional[float] = Field(None, description="Dostępna gotówka w obecnej walucie rozliczeniowej (np. PLN)")
    credit_line: Optional[float] = Field(None, description="Dostępna, niewykorzystana linia kredytowa")
    mrr: Optional[float] = Field(None, description="MRR (Monthly Recurring Revenue)")
    net_burn_rate: Optional[float] = Field(None, description="Miesięczne całkowite koszty przewyższające przychody (Net Burn Rate)")

class UnitEconomics(BaseModel):
    ltv: Optional[float] = Field(None, description="Customer Lifetime Value (LTV)")
    cac: Optional[float] = Field(None, description="Customer Acquisition Cost (CAC)")
    gross_margin_pct: Optional[float] = Field(None, description="Marża brutto (Gross Margin) w procentach, np. 62.5")
    churn_pct: Optional[float] = Field(None, description="Miesięczny wskaźnik utraty klientów (Churn) w procentach, np. 2.1")

class RiskFlag(BaseModel):
    description: str = Field(description="Zwięzły opis ryzyka, np. 'Wysoka koncentracja przychodów'")
    severity: Literal['high', 'medium', 'low'] = Field(description="Krytyczność ryzyka dla ciągłości biznesowej")

class CompanyContext(BaseModel):
    """
    Ewoluujący kontekst operacyjny firmy ('Pamięć AI' / Context Manager).
    Zbudowany ze strzępków informacji, umów i raportów przesyłanych w czasie rzeczywistym.
    """
    strategic_compass: Optional[StrategicCompass] = Field(default_factory=StrategicCompass)
    company_profile: Optional[CompanyProfile] = Field(default_factory=CompanyProfile)
    live_financial_pulse: Optional[LiveFinancialPulse] = Field(default_factory=LiveFinancialPulse)
    unit_economics: Optional[UnitEconomics] = Field(default_factory=UnitEconomics)
    seasonality_and_patterns: List[str] = Field(default_factory=list, description="Zaobserwowane wzorce i cykliczności (np. 'Q4 to 40% przychodów')")
    active_risk_flags: List[RiskFlag] = Field(default_factory=list, description="Bieżące flagi i zagrożenia zidentyfikowane w dokumentach")


# ==============================================================================
# KROK 2: PROMPT DLA CONTEXT BUILDERA (Agent Indeksujący / Extractor)
# ==============================================================================

async def extract_company_context(document_text: str) -> CompanyContext:
    """
    Ekstraktuje ustrukturyzowane fakty finansowe z dostarczonego dokumentu
    i syntetyzuje je do postaci standardowego schematu CompanyContext.
    Wykorzystuje Structured Outputs (JSON Schema) z Google Gemini.
    """
    client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))

    system_instruction = """Jesteś Dyrektorem ds. Strategii (CSO), Głównym Księgowym i Analitykiem Biznesowym.
Otrzymujesz surowy tekst z dokumentów firmy. Twoim zadaniem jest ekstrakcja twardych faktów biznesowych i aktualizacja Aktywnego Kontekstu Operacyjnego.

ZASADY:
1. Nie wymyślaj danych. Jeśli w dokumencie nie ma informacji zostaw te pola puste (null).
2. Wyciągnij Profil Firmy: Jeśli to możliwe, wskaż formę prawną (np. Sp. z o.o., JDG), formę opodatkowania (VAT, CIT, PIT, ryczałt), branżę oraz model biznesowy. Zwracaj na to baczną uwagę w umowach.
3. Szukaj wzorców: sezonowości, cykle sprzedaży, zatorów płatniczych.
4. Definiuj ryzyka: Jeśli dokument wspomina, że 40% przychodów to jeden klient, dodaj to jako 'Wysoka koncentracja przychodów' z severity 'high'.
5. Bądź zwięzły. Wyciągaj esencję."""

    # Flash dla ekstrakcji — Pro zarezerwowany WYŁĄCZNIE dla CFO synthesis
    response = client.models.generate_content(
        model='gemini-2.5-flash',
        contents=document_text,
        config=types.GenerateContentConfig(
            system_instruction=system_instruction,
            response_mime_type="application/json",
            response_schema=CompanyContext,
            temperature=0.0, # 0.0, bo zależy nam na faktografii, a nie "kreatywnym zmyślaniu" (hallucinations)
        ),
    )
    
    # Automatyczna walidacja typu (Pydantic <-> JSON), defensywnie
    from app.services._json_parse import safe_model_validate_json
    return safe_model_validate_json(CompanyContext, response.text)


# ==============================================================================
# KROK 3: LOGIKA AKTUALIZACJI (Merge / Upsert do Globalnego Mózgu)
# ==============================================================================

def merge_company_context(old_context: CompanyContext, new_context: CompanyContext) -> CompanyContext:
    """
    Łączy stary kontekst (z bazy danych) z nowo wyekstrahowanym strumieniem faktów z dokumentów.
    Zasada "Małego Agenta": Jeśli nowa wartość z dokumentu nie jest null, dyskretnie nadpisuje starą, 
    pozostawiając resztę nienaruszoną. 
    """
    # Głęboka kopia, by nie mutować obiektu oryginalnie wyciągniętego z bazy
    merged = old_context.model_copy(deep=True)

    # --- 1. Strategic Compass (Nadpisywanie nowymi założeniami) ---
    if new_context.strategic_compass:
        if new_context.strategic_compass.primary_goal:
            merged.strategic_compass.primary_goal = new_context.strategic_compass.primary_goal
        if new_context.strategic_compass.strategy:
            merged.strategic_compass.strategy = new_context.strategic_compass.strategy
            
    # --- 1.5. Company Profile (Aktualizacja profilu operacyjnego/prawnego) ---
    if new_context.company_profile:
        if new_context.company_profile.legal_form:
            merged.company_profile.legal_form = new_context.company_profile.legal_form
        if new_context.company_profile.taxation_form:
            merged.company_profile.taxation_form = new_context.company_profile.taxation_form
        if new_context.company_profile.industry:
            merged.company_profile.industry = new_context.company_profile.industry
        if new_context.company_profile.business_model:
            merged.company_profile.business_model = new_context.company_profile.business_model

    # --- 2. Live Financial Pulse (Nadpisywanie twardych kwot "tu i teraz") ---
    if new_context.live_financial_pulse:
        if new_context.live_financial_pulse.cash_on_hand is not None:
            merged.live_financial_pulse.cash_on_hand = new_context.live_financial_pulse.cash_on_hand
        if new_context.live_financial_pulse.credit_line is not None:
            merged.live_financial_pulse.credit_line = new_context.live_financial_pulse.credit_line
        if new_context.live_financial_pulse.mrr is not None:
            merged.live_financial_pulse.mrr = new_context.live_financial_pulse.mrr
        if new_context.live_financial_pulse.net_burn_rate is not None:
            merged.live_financial_pulse.net_burn_rate = new_context.live_financial_pulse.net_burn_rate

    # --- 3. Unit Economics (Rewizja wskaźników mikro) ---
    if new_context.unit_economics:
        if new_context.unit_economics.ltv is not None:
            merged.unit_economics.ltv = new_context.unit_economics.ltv
        if new_context.unit_economics.cac is not None:
            merged.unit_economics.cac = new_context.unit_economics.cac
        if new_context.unit_economics.gross_margin_pct is not None:
            merged.unit_economics.gross_margin_pct = new_context.unit_economics.gross_margin_pct
        if new_context.unit_economics.churn_pct is not None:
            merged.unit_economics.churn_pct = new_context.unit_economics.churn_pct

    # --- 4. Seasonality & Patterns (Dopisywanie nowej wiedzy historycznej) ---
    if new_context.seasonality_and_patterns:
        existing_patterns = set(p.lower() for p in merged.seasonality_and_patterns)
        for pattern in new_context.seasonality_and_patterns:
            if pattern.lower() not in existing_patterns:
                # Zapamiętujemy nowy patern na stałe (np. "wakacje obniżają MRR o 10%")
                merged.seasonality_and_patterns.append(pattern)

    # --- 5. Active Risk Flags (Kierowanie nowych alarmów na początek kolejki) ---
    if new_context.active_risk_flags:
        # Odfiltrowujemy te alerty, o których już wiemy, żeby nie tworzyć pętli spamu
        existing_risks_desc = set(r.description.lower() for r in merged.active_risk_flags)
        
        novel_risks = [r for r in new_context.active_risk_flags if r.description.lower() not in existing_risks_desc]
        
        # Ostatnio zidentyfikowane flagi ryzyka (Najświeższe na top!)
        merged.active_risk_flags = novel_risks + merged.active_risk_flags

    return merged
# ==============================================================================
# KROK 4: WARSTWA BAZY DANYCH (FastAPI / SQLAlchemy)
# ==============================================================================

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.models import CompanyContextModel
import uuid

async def get_or_create_context(db: AsyncSession, company_id: uuid.UUID) -> CompanyContextModel:
    """
    Pobiera istniejący rekord kontekstu dla firmy. Jeśli nie istnieje - tworzy pusty.
    """
    result = await db.execute(select(CompanyContextModel).where(CompanyContextModel.company_id == company_id))
    db_context = result.scalar_one_or_none()

    if not db_context:
        # Pusty obiekt wybitnego domyślnego stanu
        empty_pydantic = CompanyContext()
        db_context = CompanyContextModel(
            company_id=company_id,
            context_data=empty_pydantic.model_dump()
        )
        db.add(db_context)
        await db.commit()
        await db.refresh(db_context)

    return db_context

async def process_and_upsert_context(db: AsyncSession, company_id: uuid.UUID, text_source: str) -> CompanyContext:
    """
    Kluczowa funkcja:
    1. Pobiera stary stan pamięci corporacyjnej.
    2. Ekstrahuje nowe fakty z tekstu.
    3. Merguje stary stan z nowym (smart upsert).
    4. Nadpisuje JSONB z nową wizją w bazie PostgreSQL.
    """
    # 1. Pobranie z bazy lub utworzenie pustego
    db_record = await get_or_create_context(db, company_id)
    
    # 2. Utworzenie obiektu pythonowego (parsowanie z JSON)
    try:
        old_context_pydantic = CompanyContext.model_validate(db_record.context_data)
    except Exception:
        old_context_pydantic = CompanyContext() # Awaryjny spadochron
        
    # 3. Odpytujemy Agenta Indeksującego Gemini by przeczytał text_source i znalazł fakty
    new_context_pydantic = await extract_company_context(text_source)
    
    # 4. Magia łączenia ("Mały Agent ewoluuje wiedzę")
    merged_context_pydantic = merge_company_context(old_context_pydantic, new_context_pydantic)
    
    # 5. Zapis fizyczny z powrotem do bazy jako JSON
    db_record.context_data = merged_context_pydantic.model_dump()
    await db.commit()
    await db.refresh(db_record)
    
    return merged_context_pydantic

async def sync_passive_metrics(db: AsyncSession, company_id: uuid.UUID, metrics_payload: dict) -> CompanyContext:
    """
    KROK 5: Pasywne API (Integracje na żywo).
    Funkcja zastępcza (placeholder) przeznaczona na odbieranie danych z webhooków
    (np. Stripe, Paddle, Fakturownia) lub cyklicznych CRON jobów.
    
    Oczekuje na metrics_payload w formie prostego słownika:
    {
        "mrr": 182000.0,
        "cash_on_hand": 450000.0
    }
    """
    db_record = await get_or_create_context(db, company_id)
    
    try:
        current_context = CompanyContext.model_validate(db_record.context_data)
    except Exception:
        current_context = CompanyContext()

    if current_context.live_financial_pulse is None:
        current_context.live_financial_pulse = LiveFinancialPulse()
        
    # Szybka aktualizacja znanych metryk wprost z Payloada Webhooka - pomijamy LLM, czysty twardy fakt!
    if "mrr" in metrics_payload:
        current_context.live_financial_pulse.mrr = metrics_payload["mrr"]
    if "cash_on_hand" in metrics_payload:
        current_context.live_financial_pulse.cash_on_hand = metrics_payload["cash_on_hand"]
    if "net_burn_rate" in metrics_payload:
        current_context.live_financial_pulse.net_burn_rate = metrics_payload["net_burn_rate"]

    db_record.context_data = current_context.model_dump()
    await db.commit()
    await db.refresh(db_record)
    
    return current_context
