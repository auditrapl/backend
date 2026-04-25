"""
One-off verifier: czy chart cashflow (79/81/82/126k) zgadza się z DB.
Oczekiwane z UI: Kwiecień 2026 → Przychody 280 440, Wydatki 154 611, Netto 125 829.
"""
import asyncio
import os
from decimal import Decimal
from dotenv import load_dotenv
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

load_dotenv()
DATABASE_URL = os.environ["DATABASE_URL"]

UI_APRIL = {"income": Decimal("280440"), "expense": Decimal("154611"), "net": Decimal("125829")}
UI_CHART_HINT = [79_000, 81_000, 82_000, 126_000]


async def main():
    engine = create_async_engine(
        DATABASE_URL,
        connect_args={"statement_cache_size": 0, "prepared_statement_cache_size": 0},
    )
    async with engine.connect() as conn:
        companies = (await conn.execute(text("""
            SELECT c.id, c.name, COUNT(i.id) AS inv_count
            FROM companies c
            LEFT JOIN invoices i ON i.company_id = c.id
                AND i.issue_date >= NOW() - INTERVAL '6 months'
            GROUP BY c.id, c.name
            HAVING COUNT(i.id) > 0
            ORDER BY inv_count DESC
        """))).all()

        if not companies:
            print("Brak firm z fakturami w ostatnich 6 miesiącach.")
            return

        print("Firmy z fakturami w ostatnich 6 msc:")
        for c in companies:
            print(f"  {c.id}  {c.name}  ({c.inv_count} faktur)")

        target = companies[0].id
        print(f"\n>> Analizuje: {target} ({companies[0].name})\n")

        rows = (await conn.execute(text("""
            SELECT
              TO_CHAR(issue_date, 'YYYY-MM') AS msc,
              SUM(CASE WHEN invoice_type='income'  THEN net_amount   ELSE 0 END) AS przych_net,
              SUM(CASE WHEN invoice_type='expense' THEN net_amount   ELSE 0 END) AS wyd_net,
              SUM(CASE WHEN invoice_type='income'  THEN gross_amount ELSE 0 END) AS przych_gross,
              SUM(CASE WHEN invoice_type='expense' THEN gross_amount ELSE 0 END) AS wyd_gross,
              SUM(CASE WHEN invoice_type='income'  THEN net_amount   ELSE -net_amount   END) AS netto_net,
              SUM(CASE WHEN invoice_type='income'  THEN gross_amount ELSE -gross_amount END) AS netto_gross,
              COUNT(*) AS liczba
            FROM invoices
            WHERE company_id = :cid
              AND issue_date >= NOW() - INTERVAL '6 months'
              AND net_amount IS NOT NULL
            GROUP BY 1
            ORDER BY 1
        """), {"cid": target})).all()

        if not rows:
            print("Brak danych per miesiąc.")
            return

        print(f"{'Miesiąc':<10} {'Przych NET':>12} {'Przych GROSS':>14} {'Wyd NET':>12} {'Wyd GROSS':>12} {'Netto NET':>12} {'Netto GROSS':>14} {'N':>4}")
        print("-" * 100)
        for r in rows:
            print(f"{r.msc:<10} {float(r.przych_net or 0):>12,.0f} {float(r.przych_gross or 0):>14,.0f} {float(r.wyd_net or 0):>12,.0f} {float(r.wyd_gross or 0):>12,.0f} {float(r.netto_net or 0):>12,.0f} {float(r.netto_gross or 0):>14,.0f} {r.liczba:>4}")

        print("\n— Weryfikacja Kwietnia 2026 vs metric_grid z UI —")
        apr = next((r for r in rows if r.msc == "2026-04"), None)
        if apr:
            for label, ui_val, db_val in [
                ("Przychody", UI_APRIL["income"],  Decimal(str(apr.przych_gross or 0))),
                ("Wydatki",   UI_APRIL["expense"], Decimal(str(apr.wyd_gross   or 0))),
                ("Netto",     UI_APRIL["net"],     Decimal(str(apr.netto_gross or 0))),
            ]:
                ok = abs(db_val - ui_val) < 1
                print(f"  {label:<10} UI={float(ui_val):>10,.0f}  GROSS DB={float(db_val):>10,.0f}   {'OK (gross)' if ok else 'MISMATCH'}")

        print(f"\n— Wartości chart z UI (hint): {UI_CHART_HINT} —")
        print(f"  Netto NET   z DB: {[round(float(r.netto_net or 0) / 1000) for r in rows]}")
        print(f"  Netto GROSS z DB: {[round(float(r.netto_gross or 0) / 1000) for r in rows]}")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
