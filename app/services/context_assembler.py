"""
Agregator kontekstu — łączy L1 + L2 + L3 w sformatowany tekst
gotowy do wstrzyknięcia w prompty agentów AI (CFO, Ryzyko, Podatek, itp.)
"""
import uuid
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.models import CompanyContextModel
from app.services.context_canonical import CanonicalContext, get_or_create_canonical
from app.services.context_operational import OperationalContext, get_or_create_operational
from app.services.context_snapshots import get_trend_context_text


def _fmt(value, unit: str = "", fallback: str = "n/d") -> str:
    if value is None:
        return fallback
    if isinstance(value, float):
        formatted = f"{value:,.0f}".replace(",", " ")
        return f"{formatted} {unit}".strip()
    return str(value)


def _format_canonical(ctx: CanonicalContext) -> str:
    lines = ["== WARSTWA 1: TOŻSAMOŚĆ FIRMY (Kanon) =="]

    identity = ctx.company_identity
    parts = []
    if identity.name:
        parts.append(identity.name)
    if identity.nip:
        parts.append(f"NIP: {identity.nip}")
    if identity.legal_form:
        parts.append(identity.legal_form)
    if identity.vat_status:
        parts.append(identity.vat_status)
    if identity.accounting_method:
        parts.append(identity.accounting_method)
    lines.append(f"[Firma] {' | '.join(parts) if parts else 'Brak danych'}")

    tax = ctx.tax_regime
    tax_parts = []
    if tax.taxation_form:
        tax_parts.append(tax.taxation_form)
    if tax.vat_rate:
        tax_parts.append(f"VAT {tax.vat_rate}")
    if tax.cit_rate is not None:
        tax_parts.append(f"CIT {tax.cit_rate}%")
    if tax.jpk_obligation is not None:
        tax_parts.append("JPK: tak" if tax.jpk_obligation else "JPK: nie")
    lines.append(f"[Podatki] {' | '.join(tax_parts) if tax_parts else 'Brak danych'}")

    bm = ctx.business_model
    bm_parts = []
    if bm.segment:
        bm_parts.append(bm.segment)
    if bm.model_type:
        bm_parts.append(bm.model_type)
    if bm.revenue_model:
        bm_parts.append(bm.revenue_model)
    if bm.industry:
        bm_parts.append(f"Branża: {bm.industry}")
    lines.append(f"[Model] {' | '.join(bm_parts) if bm_parts else 'Brak danych'}")

    sc = ctx.strategic_compass
    sc_parts = []
    if sc.primary_goal:
        sc_parts.append(f"Cel: {sc.primary_goal}")
    if sc.strategy:
        sc_parts.append(f"Strategia: {sc.strategy}")
    # Add source annotation
    sources = getattr(ctx, 'field_sources', None) or {}
    sc_source = sources.get("strategic_compass")
    if sc_source and hasattr(sc_source, 'origin'):
        sc_parts.append(f"(zrodlo: {sc_source.origin})")
    lines.append(f"[Strategia] {' | '.join(sc_parts) if sc_parts else 'Brak danych'}")

    ue = ctx.unit_economics
    ue_parts = []
    if ue.ltv is not None:
        ue_parts.append(f"LTV: {_fmt(ue.ltv, 'PLN')}")
    if ue.cac is not None:
        ue_parts.append(f"CAC: {_fmt(ue.cac, 'PLN')}")
    if ue.gross_margin_pct is not None:
        ue_parts.append(f"Marża: {ue.gross_margin_pct}%")
    if ue.churn_pct is not None:
        ue_parts.append(f"Churn: {ue.churn_pct}%/msc")
    if ue_parts:
        lines.append(f"[Unit Economics] {' | '.join(ue_parts)}")

    if ctx.seasonality_and_patterns:
        lines.append(f"[Sezonowość] {' • '.join(ctx.seasonality_and_patterns[:5])}")

    return "\n".join(lines)


def _format_operational(ctx: OperationalContext) -> str:
    lines = ["== WARSTWA 2: STAN OPERACYJNY (Teraz) =="]

    p = ctx.live_financial_pulse
    pulse_parts = []

    # SafeCash — priorytet, źródło prawdy z faktur (ale NIE saldo bankowe!)
    if p.safecash is not None:
        pulse_parts.append(
            f"SafeCash (szacunek z opłaconych faktur, NIE saldo bankowe): {_fmt(p.safecash, 'PLN')}"
        )
    elif p.cash_on_hand is not None:
        pulse_parts.append(f"Gotówka (ręczna): {_fmt(p.cash_on_hand, 'PLN')}")
    # Gotówka deklarowana — parametr symulacyjny
    if p.simulation_cash is not None:
        pulse_parts.append(f"Gotówka deklarowana (symulacja): {_fmt(p.simulation_cash, 'PLN')}")

    if p.mrr is not None:
        pulse_parts.append(f"MRR: {_fmt(p.mrr, 'PLN')}")
    if p.net_burn_rate is not None:
        pulse_parts.append(f"Burn: {_fmt(p.net_burn_rate, 'PLN/msc')}")
    if p.credit_line is not None:
        pulse_parts.append(f"Linia kredytowa: {_fmt(p.credit_line, 'PLN')}")
    if p.receivables_total is not None:
        pulse_parts.append(f"Należności: {_fmt(p.receivables_total, 'PLN')}")
    if p.payables_total is not None:
        pulse_parts.append(f"Zobowiązania: {_fmt(p.payables_total, 'PLN')}")
    if p.receivables_overdue_30d is not None:
        pulse_parts.append(f"Przeterminowane >30d: {_fmt(p.receivables_overdue_30d, 'PLN')}")
    if p.budget_utilization_pct is not None:
        pulse_parts.append(f"Budżet: {p.budget_utilization_pct}%")
    lines.append(f"[Puls] {' | '.join(pulse_parts) if pulse_parts else 'Brak danych'}")

    tx = ctx.tax_position
    tx_parts = []
    if tx.vat_to_pay is not None:
        tx_parts.append(f"VAT do zapłaty: {_fmt(tx.vat_to_pay, 'PLN')}")
    if tx.cit_estimated is not None:
        tx_parts.append(f"CIT szacowany: {_fmt(tx.cit_estimated, 'PLN')}")
    if tx.next_tax_deadline:
        tx_parts.append(f"Deadline: {tx.next_tax_deadline}")
    if tx.jpk_status:
        tx_parts.append(f"JPK: {tx.jpk_status}")
    if tx_parts:
        lines.append(f"[Podatki] {' | '.join(tx_parts)}")

    # Invoice Metrics — auto-wyliczane z faktur
    im = ctx.invoice_metrics
    im_parts = []
    if im.monthly_revenue is not None or im.monthly_expenses is not None:
        rev = _fmt(im.monthly_revenue, 'PLN') if im.monthly_revenue else '0 PLN'
        exp = _fmt(im.monthly_expenses, 'PLN') if im.monthly_expenses else '0 PLN'
        net = _fmt(im.monthly_net_cashflow, 'PLN') if im.monthly_net_cashflow is not None else 'n/d'
        im_parts.append(f"Bieżący msc: Przychody {rev} | Koszty {exp} | Netto {net}")
    if im.revenue_mom_growth_pct is not None:
        sign = "+" if im.revenue_mom_growth_pct >= 0 else ""
        im_parts.append(f"Wzrost MoM: {sign}{im.revenue_mom_growth_pct}%")
    if im.auto_mrr is not None:
        im_parts.append(f"Auto MRR (śr. 3 msc): {_fmt(im.auto_mrr, 'PLN')}")
    if im.auto_burn_rate is not None:
        im_parts.append(f"Auto Burn (śr. 3 msc): {_fmt(im.auto_burn_rate, 'PLN')}")
    if im.top_contractor_name and im.top_contractor_pct is not None:
        conc = f'Top: "{im.top_contractor_name}" = {im.top_contractor_pct}%'
        if im.top3_concentration_pct is not None:
            conc += f" | Top 3 = {im.top3_concentration_pct}%"
        im_parts.append(f"Koncentracja: {conc}")
    if im.invoice_count is not None:
        im_parts.append(f"Faktur w systemie: {im.invoice_count}")
    if im_parts:
        lines.append(f"[Metryki z faktur] {' | '.join(im_parts[:2])}")
        for part in im_parts[2:]:
            lines.append(f"  {part}")

    # Cashflow Forecast — prognoza + kalendarz + kontrahenci
    cf = ctx.cashflow_forecast
    if cf.direction_score is not None or cf.weekly_projections:
        direction_arrow = "↗" if (cf.direction_score or 0) > 10 else "↘" if (cf.direction_score or 0) < -10 else "→"
        cf_parts = [f"Kierunek: {direction_arrow} {cf.direction_score:+.0f}/100"]
        if cf.runway_weeks is not None:
            cf_parts.append(f"Runway: {cf.runway_weeks} tyg.")
        if cf.safe_burn_limit is not None:
            cf_parts.append(f"Safe Burn Limit: {_fmt(cf.safe_burn_limit, 'PLN/msc')}")
        if cf.ceo_monthly_goal:
            free = (cf.safe_burn_limit or 0) - cf.ceo_monthly_goal
            cf_parts.append(f"Cel szefa: {_fmt(cf.ceo_monthly_goal, 'PLN/msc')} → Wolne: {_fmt(free, 'PLN/msc')}")
        lines.append(f"[Prognoza Cashflow] {' | '.join(cf_parts)}")

        for wp in cf.weekly_projections[:4]:
            lines.append(
                f"  {wp.week_start.strftime('%d.%m')}-{wp.week_end.strftime('%d.%m')}: "
                f"+{_fmt(wp.expected_income, '')} wpływy | "
                f"-{_fmt(wp.expected_expenses, '')} wydatki | "
                f"-{_fmt(wp.tax_obligations, '')} podatki = "
                f"{_fmt(wp.closing_balance, 'PLN')}"
            )

    if cf.tax_calendar:
        lines.append("[Kalendarz podatkowy]")
        for t in cf.tax_calendar[:6]:
            amt = f"~{_fmt(t.estimated_amount, 'PLN')}" if t.estimated_amount else "kwota n/d"
            lines.append(f"  {t.date.strftime('%d.%m')} → {t.label}: {amt}")

    if cf.contractor_reliability:
        lines.append("[Kontrahenci — wiarygodność płatnicza]")
        for c in cf.contractor_reliability[:5]:
            dso = f"DSO: {c.avg_payment_days:.0f} dni" if c.avg_payment_days else "DSO: n/d"
            lines.append(
                f'  "{c.contractor_name}" → {dso} ({c.reliability_score}) | '
                f'{_fmt(c.total_outstanding, "PLN")} outstanding'
            )

    if ctx.active_risk_flags:
        lines.append("[Ryzyka]")
        for r in ctx.active_risk_flags[:5]:  # max 5 ryzyk w promptcie
            lines.append(f"  [{r.severity.upper()}] {r.description}")

    if ctx.current_period_goals:
        goals_text = " • ".join(g.description for g in ctx.current_period_goals[:3])
        lines.append(f"[Cele okresu] {goals_text}")

    if ctx.last_ai_extraction:
        lines.append(f"[Ostatnia ekstrakcja AI] {ctx.last_ai_extraction.strftime('%Y-%m-%d %H:%M')}")

    return "\n".join(lines)


async def assemble_full_context_text(
    db: AsyncSession,
    company_id: uuid.UUID,
    include_snapshots: bool = True,
) -> str:
    """
    Główna funkcja wywoływana przez agentów CFO przed każdym promptem.
    Łączy L1 + L2 + opcjonalnie L3 w sformatowany blok tekstowy.

    Fallback: jeśli canonical_data/operational_data są puste, wraca do legacy context_data.
    """
    result = await db.execute(
        select(CompanyContextModel).where(CompanyContextModel.company_id == company_id)
    )
    record = result.scalar_one_or_none()

    # Fallback do legacy jeśli nowe warstwy jeszcze nie zmigrowane
    if record and (record.canonical_data is None or record.operational_data is None):
        return _format_legacy_context(record)

    canonical = await get_or_create_canonical(db, company_id)
    operational = await get_or_create_operational(db, company_id)

    sections = [
        _format_canonical(canonical),
        "",
        _format_operational(operational),
    ]

    if include_snapshots:
        trend_text = await get_trend_context_text(db, company_id)
        if trend_text:
            sections.extend(["", trend_text])

    return "\n".join(sections)


def _format_legacy_context(record: CompanyContextModel) -> str:
    """Formatuje stary context_data jako fallback podczas przejścia migracyjnego."""
    if not record or not record.context_data:
        return "Brak kontekstu firmowego."

    data = record.context_data
    lines = ["== KONTEKST FIRMOWY (legacy) =="]

    sc = data.get("strategic_compass", {})
    if sc.get("primary_goal"):
        lines.append(f"[Cel] {sc['primary_goal']}")
    if sc.get("strategy"):
        lines.append(f"[Strategia] {sc['strategy']}")

    cp = data.get("company_profile", {})
    profile_parts = [v for v in [cp.get("legal_form"), cp.get("taxation_form"), cp.get("industry")] if v]
    if profile_parts:
        lines.append(f"[Profil] {' | '.join(profile_parts)}")

    fp = data.get("live_financial_pulse", {})
    pulse_parts = []
    if fp.get("cash_on_hand") is not None:
        pulse_parts.append(f"Gotówka: {fp['cash_on_hand']:,.0f} PLN")
    if fp.get("mrr") is not None:
        pulse_parts.append(f"MRR: {fp['mrr']:,.0f} PLN")
    if fp.get("net_burn_rate") is not None:
        pulse_parts.append(f"Burn: {fp['net_burn_rate']:,.0f} PLN")
    if pulse_parts:
        lines.append(f"[Puls] {' | '.join(pulse_parts)}")

    risks = data.get("active_risk_flags", [])
    for r in risks[:3]:
        lines.append(f"  [{r.get('severity', '?').upper()}] {r.get('description', '')}")

    return "\n".join(lines)
