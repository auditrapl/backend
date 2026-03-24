"""
Jednorazowy skrypt migracji danych: context_data → canonical_data + operational_data.
Idempotentny: pomija firmy, które już mają canonical_data ustawione.

Użycie:
    cd auditra
    python migrate_to_3layer.py
"""
import asyncio
import os
import sys
from pathlib import Path

# Upewnij się że app jest w path
sys.path.insert(0, str(Path(__file__).parent))

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy import select

from app.models import CompanyContextModel


async def migrate_company(session: AsyncSession, record: CompanyContextModel) -> str:
    """
    Mapuje istniejący context_data na canonical_data + operational_data.
    Zwraca status: 'migrated' | 'skipped' | 'error'.
    """
    if record.canonical_data is not None:
        return "skipped"

    data = record.context_data or {}

    # --- L1: Kanoniczny ---
    company_profile = data.get("company_profile", {}) or {}
    strategic_compass = data.get("strategic_compass", {}) or {}
    unit_economics = data.get("unit_economics", {}) or {}
    seasonality = data.get("seasonality_and_patterns", []) or []

    canonical_data = {
        "company_identity": {
            "name": None,
            "nip": None,
            "legal_form": company_profile.get("legal_form"),
            "vat_status": company_profile.get("taxation_form"),
            "accounting_method": None,
        },
        "tax_regime": {
            "taxation_form": company_profile.get("taxation_form"),
            "vat_rate": None,
            "jpk_obligation": None,
            "cit_rate": None,
        },
        "business_model": {
            "segment": None,
            "model_type": company_profile.get("business_model"),
            "revenue_model": None,
            "industry": company_profile.get("industry"),
        },
        "strategic_compass": {
            "primary_goal": strategic_compass.get("primary_goal"),
            "strategy": strategic_compass.get("strategy"),
        },
        "unit_economics": {
            "ltv": unit_economics.get("ltv"),
            "cac": unit_economics.get("cac"),
            "gross_margin_pct": unit_economics.get("gross_margin_pct"),
            "churn_pct": unit_economics.get("churn_pct"),
        },
        "seasonality_and_patterns": seasonality,
        "canonical_version": 1,
    }

    # --- L2: Operacyjny ---
    live_pulse = data.get("live_financial_pulse", {}) or {}
    risk_flags = data.get("active_risk_flags", []) or []

    operational_data = {
        "live_financial_pulse": {
            "cash_on_hand": live_pulse.get("cash_on_hand"),
            "credit_line": live_pulse.get("credit_line"),
            "mrr": live_pulse.get("mrr"),
            "net_burn_rate": live_pulse.get("net_burn_rate"),
            "receivables_total": None,
            "payables_total": None,
            "receivables_overdue_30d": None,
            "vat_position": None,
            "budget_utilization_pct": None,
            "last_ksef_sync": None,
            "last_erp_sync": None,
        },
        "tax_position": {
            "vat_to_pay": None,
            "cit_estimated": None,
            "next_tax_deadline": None,
            "jpk_status": None,
        },
        "current_period_goals": [],
        "active_risk_flags": risk_flags,
        "operational_version": 1,
        "last_ai_extraction": None,
    }

    record.canonical_data = canonical_data
    record.operational_data = operational_data
    return "migrated"


async def main():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("Błąd: brak zmiennej DATABASE_URL")
        sys.exit(1)

    engine = create_async_engine(
        database_url,
        echo=False,
        connect_args={"statement_cache_size": 0},
    )
    AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(CompanyContextModel))
        records = result.scalars().all()

    print(f"Znaleziono {len(records)} rekordów company_contexts")

    migrated = skipped = errors = 0
    for record in records:
        async with AsyncSessionLocal() as session:
            # Pobierz świeży rekord w tej sesji
            r = await session.get(CompanyContextModel, record.id)
            if r is None:
                continue
            status = await migrate_company(session, r)
            if status == "migrated":
                await session.commit()
                migrated += 1
            elif status == "skipped":
                skipped += 1
            else:
                errors += 1
            print(f"  company_id={r.company_id} → {status}")

    print(f"\nMigracja zakończona: {migrated} zmigrowanych, {skipped} pominiętych, {errors} błędów")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
