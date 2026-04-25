"""
AI Wizard do onboardingu — dwa równoległe calle per wiadomość:
1. Conversation stream (temp 0.4) — prowadzi dialog
2. Structured extraction (temp 0.0) — wyciąga dane do Pydantic schema
"""
import asyncio
import json
import logging
from typing import AsyncGenerator, Optional, List

from google import genai
from google.genai import types
from pydantic import BaseModel, Field

from app.services.context_manager import StrategicCompass, UnitEconomics
from app.prompts.onboarding_prompts import (
    WIZARD_CONVERSATION_PROMPT,
    WIZARD_EXTRACTION_PROMPT,
)

logger = logging.getLogger(__name__)

FLASH_MODEL = "gemini-2.5-flash"


# ==============================================================================
# SCHEMA EKSTRAKCJI
# ==============================================================================

class WizardExtractionResult(BaseModel):
    """Ustrukturyzowane dane wyekstrahowane z rozmowy wizarda."""
    taxation_form: Optional[str] = Field(None, description="Forma opodatkowania: 'CIT estoński', 'CIT 19%', 'CIT 9%', 'Liniowy 19%', 'Ryczałt', 'Skala podatkowa'")
    model_type: Optional[str] = Field(None, description="Typ modelu: 'SaaS', 'Marketplace', 'Usługi', 'Produkcja', 'Handel'")
    segment: Optional[str] = Field(None, description="Segment rynku: 'B2B', 'B2C', 'B2G', 'B2B2C'")
    revenue_model: Optional[str] = Field(None, description="Model przychodowy: 'Subskrypcja', 'Jednorazowa', 'Prowizja', 'Projekt'")
    industry: Optional[str] = Field(None, description="Branża firmy")
    employee_count: Optional[int] = Field(None, description="Liczba pracowników")
    zus_type: Optional[str] = Field(None, description="Typ ZUS: 'pelny', 'preferencyjny', 'maly_zus_plus', 'ulga_na_start'")
    accounting_method: Optional[str] = Field(None, description="Metoda księgowania: 'Pełna księgowość', 'KPiR', 'Ryczałt ewidencjonowany'")
    strategic_goals: Optional[str] = Field(None, description="Cele strategiczne na 6-12 miesięcy")
    monthly_revenue: Optional[float] = Field(None, description="Orientacyjna miesięczna sprzedaż PLN")
    monthly_costs: Optional[float] = Field(None, description="Orientacyjne miesięczne koszty PLN")
    mrr: Optional[float] = Field(None, description="MRR jeśli model subskrypcyjny PLN")


# ==============================================================================
# CONVERSATION STREAM
# ==============================================================================

async def wizard_conversation_stream(
    message: str,
    history: list[dict],
    company_context: dict,
) -> AsyncGenerator[str, None]:
    """
    Gemini 2.5-Flash, temp 0.4, streaming.
    Prowadzi dialog z userem w naturalnym języku.
    """
    # Buduj historię konwersacji jako tekst
    conv_text = ""
    for msg in history:
        role = "Klient" if msg["role"] == "user" else "Doradca"
        conv_text += f"{role}: {msg['content']}\n"
    conv_text += f"Klient: {message}\n"

    system_prompt = WIZARD_CONVERSATION_PROMPT.format(
        company_context=json.dumps(company_context, ensure_ascii=False, default=str),
        conversation_history=conv_text,
    )

    client = genai.Client()

    response = await client.aio.models.generate_content_stream(
        model=FLASH_MODEL,
        contents=f"Odpowiedz jako Doradca na ostatnią wiadomość klienta.",
        config=types.GenerateContentConfig(
            system_instruction=system_prompt,
            temperature=0.4,
            max_output_tokens=800,
        ),
    )

    async for chunk in response:
        if chunk.text:
            yield chunk.text


# ==============================================================================
# STRUCTURED EXTRACTION
# ==============================================================================

async def wizard_extract_structured(
    message: str,
    history: list[dict],
) -> WizardExtractionResult:
    """
    Gemini 2.5-Flash, temp 0.0, Pydantic structured output.
    Wyciąga ustrukturyzowane dane z CAŁEJ rozmowy (nie tylko ostatniej wiadomości).
    """
    # Buduj pełny tekst konwersacji
    conv_text = ""
    for msg in history:
        role = "Klient" if msg["role"] == "user" else "Doradca"
        conv_text += f"{role}: {msg['content']}\n"
    conv_text += f"Klient: {message}\n"

    prompt = WIZARD_EXTRACTION_PROMPT.format(conversation_text=conv_text)

    client = genai.Client()

    response = await client.aio.models.generate_content(
        model=FLASH_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=WizardExtractionResult,
            temperature=0.0,
        ),
    )

    from app.services._json_parse import safe_model_validate_json
    return safe_model_validate_json(WizardExtractionResult, response.text)
