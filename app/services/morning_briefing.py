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
    "Pracujesz na 'nocnej zmianie'. Przeanalizuj Kontekst Firmy i przygotuj Poranną Odprawę.\n\n"
    "MUSISZ wygenerować: (1) pole 'summary' — jedno krótkie podsumowanie stanu firmy (2-3 zdania, "
    "twarde liczby, najważniejszy sygnał dnia); "
    "(2) dokładnie 3 insighty.\n\n"
    "PRIORYTET INSIGHTOW:\n"
    "1. PODATKI: jesli otrzymujesz ALERTY PODATKOWE z audytu faktur — uzyj ich jako insight #1. "
    "Zawsze podaj podstawe prawna (artykul ustawy) i szacunkowa oszczednosc w PLN.\n"
    "2. PLYNNOSC: ryzyka cashflow, zatory platnicze, przeterminowane faktury.\n"
    "3. STRATEGIA: szanse na optymalizacje (ZUS, CIT, IP Box), dywersyfikacja klientow.\n\n"
    "'suggested_prompt' musi byc komenda ktora uruchomi pelna analize agentow CFO. "
    "Jezyk: twardy, biznesowy, zero lania wody. Kazdy insight z konkretna liczba w PLN."
)


async def generate_briefing_for_company(
    db: AsyncSession,
    company_id: uuid.UUID,
    context_data: dict,
) -> None:
    """Generate 3 proactive insights for a single company and save to DB."""
    # Guard: nie generuj briefingu z pustego kontekstu — Gemini halucynuje kwoty
    if not context_data:
        logger.info(f"[NightShift] Skipping company {company_id}: empty context_data")
        return

    canonical = context_data.get("canonical", {})
    operational = context_data.get("operational", {})
    has_identity = bool(canonical.get("company_identity", {}).get("name"))
    has_financials = bool(operational.get("live_financial_pulse", {}).get("mrr"))
    if not has_identity and not has_financials:
        logger.info(f"[NightShift] Skipping company {company_id}: no identity or financials in context")
        return

    client = genai.Client()

    context_str = json.dumps(context_data, ensure_ascii=False, default=str)

    # ── Wzbogac kontekst o dane z audytow faktur (Tax Eye) ──
    tax_context = ""
    try:
        from app.models import Invoice
        from sqlalchemy import func

        # Top 5 faktur z najwyzszym risk_score
        risky = await db.execute(
            select(Invoice.contractor_name, Invoice.gross_amount, Invoice.risk_score, Invoice.category_name)
            .where(Invoice.company_id == company_id, Invoice.risk_score > 30)
            .order_by(Invoice.risk_score.desc())
            .limit(5)
        )
        risky_rows = risky.fetchall()

        # Suma estimated_savings z tax_eye insightow (ostatnie 30 dni)
        from datetime import timedelta
        savings_result = await db.execute(
            select(func.sum(ProactiveInsight.estimated_savings))
            .where(
                ProactiveInsight.company_id == company_id,
                ProactiveInsight.insight_type == "tax_eye",
                ProactiveInsight.estimated_savings.isnot(None),
                ProactiveInsight.created_at >= datetime.utcnow() - timedelta(days=30),
            )
        )
        total_savings = savings_result.scalar() or 0

        if risky_rows or total_savings > 0:
            parts = ["\n\nALERTY PODATKOWE Z AUDYTU FAKTUR:"]
            if total_savings > 0:
                parts.append(f"Laczne zidentyfikowane oszczednosci (30 dni): {total_savings:,.0f} PLN")
            for row in risky_rows:
                parts.append(f"- {row.contractor_name}: {row.gross_amount:,.0f} PLN, risk={row.risk_score}/100, kat={row.category_name}")
            tax_context = "\n".join(parts)
    except Exception as tex:
        logger.warning(f"[NightShift] Tax context enrichment failed: {tex}")

    try:
        response = await client.aio.models.generate_content(
            model="gemini-2.5-flash",
            contents=f"Kontekst Firmy:\n{context_str}{tax_context}",
            config=types.GenerateContentConfig(
                system_instruction=NIGHT_SHIFT_SYSTEM_PROMPT,
                temperature=0.2,  # Niska temp → spójny, przewidywalny format briefingu
                response_mime_type="application/json",
                response_schema=DailyBriefingSchema,
            ),
        )

        from app.services._json_parse import safe_model_validate_json
        briefing = safe_model_validate_json(DailyBriefingSchema, response.text)

        # Delete stale MORNING insights only (keep tax_eye + cfo_radar)
        await db.execute(
            delete(ProactiveInsight).where(
                ProactiveInsight.company_id == company_id,
                ProactiveInsight.insight_type == "morning",
            )
        )

        # Insert fresh morning insights
        for i, insight in enumerate(briefing.insights[:3]):
            db.add(ProactiveInsight(
                company_id=company_id,
                title=insight.title,
                description=insight.description,
                suggested_prompt=insight.suggested_prompt,
                urgency=insight.urgency,
                insight_type="morning",
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


async def send_morning_emails_for_all_companies() -> None:
    """CRON entry point (07:00): wysyła pre-generowane briefings emailem do userów."""
    from app.models import User, Company
    from app.services.email_service import send_briefing_email

    logger.info("[MorningEmail] ═══ Wysyłka emaili rozpoczęta ═══")

    async with AsyncSessionLocal() as db:
        # Pobierz wszystkie insighty z summary (wygenerowane o 03:00)
        result = await db.execute(
            select(ProactiveInsight)
            .where(ProactiveInsight.summary.isnot(None))
            .order_by(ProactiveInsight.created_at.desc())
        )
        summary_insights = result.scalars().all()

        if not summary_insights:
            logger.info("[MorningEmail] No briefings to send.")
            return

        # Grupuj po company_id
        by_company: dict[uuid.UUID, list] = {}
        for ins in summary_insights:
            by_company.setdefault(ins.company_id, []).append(ins)

        for company_id, insights_list in by_company.items():
            # Pobierz summary z pierwszego insightu
            summary = insights_list[0].summary or ""

            # Pobierz wszystkie insighty tej firmy (nie tylko z summary)
            all_result = await db.execute(
                select(ProactiveInsight)
                .where(ProactiveInsight.company_id == company_id)
                .order_by(ProactiveInsight.created_at.desc())
                .limit(3)
            )
            all_insights = all_result.scalars().all()

            # Pobierz nazwę firmy
            company_result = await db.execute(
                select(Company).where(Company.id == company_id)
            )
            company = company_result.scalar_one_or_none()
            company_name = company.name if company else "Twoja firma"

            # Pobierz userów firmy
            users_result = await db.execute(
                select(User).where(User.company_id == company_id)
            )
            users = users_result.scalars().all()

            if not users:
                logger.info(f"[MorningEmail] No users for company {company_id}, skipping")
                continue

            insights_data = [
                {
                    "title": ins.title,
                    "description": ins.description,
                    "urgency": ins.urgency,
                }
                for ins in all_insights
            ]

            # Fetch top tax_eye insight for email banner
            top_tax = None
            try:
                tax_result = await db.execute(
                    select(ProactiveInsight)
                    .where(
                        ProactiveInsight.company_id == company_id,
                        ProactiveInsight.insight_type == "tax_eye",
                        ProactiveInsight.is_dismissed == False,
                        ProactiveInsight.estimated_savings.isnot(None),
                    )
                    .order_by(ProactiveInsight.estimated_savings.desc())
                    .limit(1)
                )
                tax_ins = tax_result.scalar_one_or_none()
                if tax_ins:
                    top_tax = {
                        "title": tax_ins.title,
                        "description": tax_ins.description,
                        "legal_basis": tax_ins.legal_basis,
                        "estimated_savings": float(tax_ins.estimated_savings) if tax_ins.estimated_savings else None,
                    }
            except Exception:
                pass

            for user in users:
                send_briefing_email(
                    to_email=user.email,
                    company_name=company_name,
                    summary=summary,
                    insights=insights_data,
                    top_tax_insight=top_tax,
                )

    logger.info("[MorningEmail] ═══ Wysyłka zakończona ═══")
