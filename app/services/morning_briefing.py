"""
Nocna Zmiana — Proactive Morning Briefing Generator
Runs daily at 03:00 AM. Generates 3 actionable insights per company.
"""
import logging
import json
import uuid
from datetime import datetime
from google import genai
from google.genai import types
from sqlalchemy import select, delete, distinct
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import AsyncSessionLocal
from app.models import CompanyContextModel, ProactiveInsight
from app.schemas import DailyBriefingSchema

logger = logging.getLogger("morning_briefing")

NIGHT_SHIFT_SYSTEM_PROMPT = (
    "Jesteś 'Majorem CFO' – autonomicznym, bezlitosnym i analitycznym dyrektorem finansowym AI. "
    "Pracujesz na 'nocnej zmianie'. Przeanalizuj Kontekst Firmy i przygotuj Poranną Odprawę. "
    "MUSISZ wygenerować: (1) pole 'summary' — jedno krótkie podsumowanie stanu firmy (2-3 zdania, "
    "twarde liczby, najważniejszy sygnał dnia, pisane jak briefing dla zarządu); "
    "(2) dokładnie 3 insighty. "
    "Szukaj anomalii, ryzyk cashflow, szans na optymalizację (ZUS, SaaS) lub strategii. "
    "'suggested_prompt' musi być komendą, która uruchomi pełną 'Wojnę Domową' agentów. "
    "Język ma być twardy, biznesowy, zero lania wody."
)


async def generate_briefing_for_company(
    db: AsyncSession,
    company_id: uuid.UUID,
    context_data: dict,
) -> None:
    """Generate 3 proactive insights for a single company and save to DB."""
    client = genai.Client()

    context_str = json.dumps(context_data, ensure_ascii=False, default=str)

    try:
        response = await client.aio.models.generate_content(
            model="gemini-2.5-flash",
            contents=f"Kontekst Firmy:\n{context_str}",
            config=types.GenerateContentConfig(
                system_instruction=NIGHT_SHIFT_SYSTEM_PROMPT,
                temperature=0.6,
                response_mime_type="application/json",
                response_schema=DailyBriefingSchema,
            ),
        )

        briefing = DailyBriefingSchema.model_validate_json(response.text)

        # Delete stale insights for this company
        await db.execute(
            delete(ProactiveInsight).where(ProactiveInsight.company_id == company_id)
        )

        # Insert fresh insights — summary zapisywany tylko w pierwszym
        for i, insight in enumerate(briefing.insights[:3]):
            db.add(ProactiveInsight(
                company_id=company_id,
                title=insight.title,
                description=insight.description,
                suggested_prompt=insight.suggested_prompt,
                urgency=insight.urgency,
                summary=briefing.summary if i == 0 else None,
            ))

        await db.commit()
        logger.info(f"[NightShift] Generated 3 insights for company {company_id}")

    except Exception as e:
        await db.rollback()
        logger.error(f"[NightShift] Failed for company {company_id}: {e}", exc_info=True)


async def generate_daily_briefings_for_all_companies() -> None:
    """CRON entry point: iterate all companies with context and generate briefings."""
    logger.info("[NightShift] ═══ Nocna Zmiana rozpoczęta ═══")

    async with AsyncSessionLocal() as db:
        # Fetch all companies that have a context (active companies)
        result = await db.execute(
            select(CompanyContextModel.company_id, CompanyContextModel.context_data)
        )
        rows = result.all()

        if not rows:
            logger.info("[NightShift] No companies with context found. Skipping.")
            return

        logger.info(f"[NightShift] Processing {len(rows)} companies...")

        for company_id, context_data in rows:
            await generate_briefing_for_company(db, company_id, context_data or {})

    logger.info("[NightShift] ═══ Nocna Zmiana zakończona ═══")
