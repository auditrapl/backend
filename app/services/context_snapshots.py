"""
Warstwa 3 — Snapshoty historyczne (L3)
Tygodniowe zamrożenia L2 (bez AI) i miesięczne z AI summary.
Używane przez agentów do analizy trendów i historii finansowej.
"""
import os
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional, List

from google import genai
from google.genai import types
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc

from app.models import ContextSnapshotModel, CompanyContextModel
from app.services.context_operational import OperationalContext


# ==============================================================================
# TWORZENIE SNAPSHOTÓW
# ==============================================================================

def _extract_key_metrics(op: OperationalContext) -> dict:
    """Wyciąga kluczowe metryki z L2 do płaskiego słownika dla szybkich porównań."""
    p = op.live_financial_pulse
    im = op.invoice_metrics
    return {
        "cash_on_hand": p.cash_on_hand,
        "safecash": p.safecash,
        "simulation_cash": p.simulation_cash,
        "mrr": p.mrr,
        "net_burn_rate": p.net_burn_rate,
        "receivables_total": p.receivables_total,
        "payables_total": p.payables_total,
        "vat_to_pay": op.tax_position.vat_to_pay,
        "active_risks_count": len(op.active_risk_flags),
        # InvoiceMetrics — auto z faktur
        "monthly_revenue": im.monthly_revenue,
        "monthly_expenses": im.monthly_expenses,
        "monthly_net_cashflow": im.monthly_net_cashflow,
        "revenue_mom_growth_pct": im.revenue_mom_growth_pct,
        "auto_mrr": im.auto_mrr,
        "auto_burn_rate": im.auto_burn_rate,
        "top_contractor_pct": im.top_contractor_pct,
        "invoice_count": im.invoice_count,
        # Cashflow Forecast
        "direction_score": op.cashflow_forecast.direction_score,
        "safe_burn_limit": op.cashflow_forecast.safe_burn_limit,
        "runway_weeks": op.cashflow_forecast.runway_weeks,
    }


async def create_weekly_snapshot(
    db: AsyncSession,
    company_id: uuid.UUID,
) -> ContextSnapshotModel:
    """
    Tygodniowy snapshot L2 — bez AI, szybki, wywoływany automatycznie w niedzielę.
    Zachowuje bieżący stan operacyjny wraz z kluczowymi metrykami.
    """
    result = await db.execute(
        select(CompanyContextModel).where(CompanyContextModel.company_id == company_id)
    )
    record = result.scalar_one_or_none()

    if record and record.operational_data:
        try:
            operational = OperationalContext.model_validate(record.operational_data)
        except Exception:
            operational = OperationalContext()
    else:
        operational = OperationalContext()

    now = datetime.now(timezone.utc)
    snapshot = ContextSnapshotModel(
        company_id=company_id,
        period_type="weekly",
        period_start=now - timedelta(days=7),
        period_end=now,
        operational_snapshot=operational.model_dump(mode="json"),
        key_metrics=_extract_key_metrics(operational),
    )
    db.add(snapshot)
    await db.commit()
    await db.refresh(snapshot)
    return snapshot


async def create_monthly_snapshot(
    db: AsyncSession,
    company_id: uuid.UUID,
) -> ContextSnapshotModel:
    """
    Miesięczny snapshot L2 — z AI synthesis, wywoływany 1. dnia miesiąca.
    Generuje narracyjne podsumowanie minionego miesiąca.
    """
    result = await db.execute(
        select(CompanyContextModel).where(CompanyContextModel.company_id == company_id)
    )
    record = result.scalar_one_or_none()

    if record and record.operational_data:
        try:
            operational = OperationalContext.model_validate(record.operational_data)
        except Exception:
            operational = OperationalContext()
    else:
        operational = OperationalContext()

    # Pobierz snapshoty tygodniowe z ostatnich ~5 tygodni do kontekstu AI
    recent_weekly = await get_snapshots(db, company_id, period_type="weekly", limit=5)
    trend_text = _format_weekly_for_ai(recent_weekly)

    ai_summary = await _generate_monthly_ai_summary(operational, trend_text)

    now = datetime.now(timezone.utc)
    snapshot = ContextSnapshotModel(
        company_id=company_id,
        period_type="monthly",
        period_start=now - timedelta(days=30),
        period_end=now,
        operational_snapshot=operational.model_dump(mode="json"),
        ai_summary=ai_summary,
        key_metrics=_extract_key_metrics(operational),
    )
    db.add(snapshot)
    await db.commit()
    await db.refresh(snapshot)
    return snapshot


def _format_weekly_for_ai(snapshots: List[ContextSnapshotModel]) -> str:
    if not snapshots:
        return "Brak danych historycznych z ostatnich tygodni."
    lines = []
    for i, s in enumerate(snapshots):
        m = s.key_metrics or {}
        cash = m.get('safecash') or m.get('cash_on_hand', '?')
        mrr = m.get('auto_mrr') or m.get('mrr', '?')
        burn = m.get('auto_burn_rate') or m.get('net_burn_rate', '?')
        parts = [
            f"SafeCash={cash} PLN",
            f"MRR={mrr} PLN",
            f"Burn={burn} PLN",
            f"Ryzyka={m.get('active_risks_count', '?')}",
        ]
        if m.get('monthly_revenue') is not None:
            parts.append(f"Rev={m['monthly_revenue']} PLN")
        if m.get('revenue_mom_growth_pct') is not None:
            parts.append(f"MoM={m['revenue_mom_growth_pct']}%")
        lines.append(
            f"T-{i+1}w ({s.period_end.strftime('%Y-%m-%d') if s.period_end else '?'}): "
            + " | ".join(parts)
        )
    return "\n".join(lines)


async def _generate_monthly_ai_summary(
    current: OperationalContext,
    trend_text: str,
) -> str:
    """Gemini generuje narracyjne podsumowanie miesiąca na podstawie L2 + tygodniowych trendów."""
    client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))

    pulse = current.live_financial_pulse
    current_state = (
        f"Bieżący stan: Gotówka={pulse.cash_on_hand} PLN, MRR={pulse.mrr} PLN, "
        f"Burn={pulse.net_burn_rate} PLN/msc, "
        f"Należności={pulse.receivables_total} PLN, "
        f"VAT do zapłaty={current.tax_position.vat_to_pay} PLN.\n"
        f"Aktywne ryzyka: {len(current.active_risk_flags)} flag.\n"
        f"Cele okresu: {', '.join(g.description for g in current.current_period_goals) or 'brak'}."
    )

    prompt = f"""Napisz zwięzłe podsumowanie finansowe zamkniętego miesiąca (3-5 zdań, po polsku).

Stan bieżący:
{current_state}

Dane tygodniowe z minionych tygodni:
{trend_text}

Skup się na: trendzie gotówki i MRR, stopniu realizacji celów, kluczowych ryzykach. Nie powtarzaj surowych liczb — opisz co one oznaczają dla firmy."""

    response = client.models.generate_content(
        model='gemini-2.5-flash',
        contents=prompt,
        config=types.GenerateContentConfig(temperature=0.3),
    )
    return response.text.strip()


# ==============================================================================
# POBIERANIE I FORMATOWANIE
# ==============================================================================

async def get_snapshots(
    db: AsyncSession,
    company_id: uuid.UUID,
    period_type: Optional[str] = None,
    limit: int = 12,
) -> List[ContextSnapshotModel]:
    """Pobiera snapshoty per firma, opcjonalnie filtrowane wg period_type."""
    q = select(ContextSnapshotModel).where(
        ContextSnapshotModel.company_id == company_id
    ).order_by(desc(ContextSnapshotModel.period_start)).limit(limit)

    if period_type:
        q = q.where(ContextSnapshotModel.period_type == period_type)

    result = await db.execute(q)
    return list(result.scalars().all())


async def get_trend_context_text(db: AsyncSession, company_id: uuid.UUID) -> str:
    """
    Formatuje ostatnie 8 tygodniowych + 3 miesięczne snapshoty jako tekst
    gotowy do wstrzyknięcia w prompt agenta AI.
    """
    weekly = await get_snapshots(db, company_id, period_type="weekly", limit=8)
    monthly = await get_snapshots(db, company_id, period_type="monthly", limit=3)

    lines = ["== WARSTWA 3: HISTORIA I TRENDY =="]

    if weekly:
        lines.append("[Snapshoty tygodniowe]")
        for i, s in enumerate(weekly):
            m = s.key_metrics or {}
            cash = m.get('safecash') or m.get('cash_on_hand', 'n/d')
            mrr = m.get('auto_mrr') or m.get('mrr', 'n/d')
            burn = m.get('auto_burn_rate') or m.get('net_burn_rate', 'n/d')
            rev = m.get('monthly_revenue')
            rev_str = f" | Rev={rev}" if rev is not None else ""
            lines.append(
                f"  T-{i+1}w ({s.period_end.strftime('%Y-%m-%d') if s.period_end else '?'}): "
                f"SafeCash={cash} | "
                f"MRR={mrr} | "
                f"Burn={burn}{rev_str}"
            )
    else:
        lines.append("[Snapshoty tygodniowe] Brak danych")

    if monthly:
        lines.append("[Podsumowania miesięczne]")
        for s in monthly:
            label = s.period_end.strftime("%B %Y") if s.period_end else "?"
            summary = s.ai_summary or "Brak podsumowania."
            lines.append(f"  [{label}] {summary[:300]}")
    else:
        lines.append("[Podsumowania miesięczne] Brak danych")

    return "\n".join(lines)


# ==============================================================================
# FUNKCJE CRON (wywoływane z main.py)
# ==============================================================================

async def trigger_weekly_snapshots_for_all_companies(db_factory) -> None:
    """Wywołuje tygodniowy snapshot dla każdej firmy. Dla APScheduler."""
    from sqlalchemy import select as sa_select
    from app.models import CompanyContextModel as CCM

    async with db_factory() as db:
        result = await db.execute(sa_select(CCM.company_id))
        company_ids = result.scalars().all()

    for company_id in company_ids:
        async with db_factory() as db:
            try:
                await create_weekly_snapshot(db, company_id)
            except Exception as e:
                import logging
                logging.getLogger("auditra").error(
                    f"Weekly snapshot failed for {company_id}: {e}"
                )


async def trigger_monthly_snapshots_for_all_companies(db_factory) -> None:
    """Wywołuje miesięczny snapshot dla każdej firmy. Dla APScheduler."""
    from sqlalchemy import select as sa_select
    from app.models import CompanyContextModel as CCM

    async with db_factory() as db:
        result = await db.execute(sa_select(CCM.company_id))
        company_ids = result.scalars().all()

    for company_id in company_ids:
        async with db_factory() as db:
            try:
                await create_monthly_snapshot(db, company_id)
            except Exception as e:
                import logging
                logging.getLogger("auditra").error(
                    f"Monthly snapshot failed for {company_id}: {e}"
                )
