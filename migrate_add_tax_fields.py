"""
Migracja: dodaje pola podatkowe do tabeli companies.
Uruchom: python migrate_add_tax_fields.py

Pola: legal_form, taxation_form, vat_status, employee_count, zus_type
"""
import asyncio
import os
from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text

load_dotenv()
DATABASE_URL = os.environ.get("DATABASE_URL")

STATEMENTS = [
    # Companies: pola podatkowe
    "ALTER TABLE companies ADD COLUMN IF NOT EXISTS legal_form VARCHAR;",
    "ALTER TABLE companies ADD COLUMN IF NOT EXISTS taxation_form VARCHAR;",
    "ALTER TABLE companies ADD COLUMN IF NOT EXISTS vat_status VARCHAR;",
    "ALTER TABLE companies ADD COLUMN IF NOT EXISTS employee_count INTEGER;",
    "ALTER TABLE companies ADD COLUMN IF NOT EXISTS zus_type VARCHAR;",
    # Invoices: brakujące kolumny cashflow
    "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS due_date TIMESTAMP;",
    "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS payment_date TIMESTAMP;",
    "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS payment_days INTEGER;",
]


async def main():
    if not DATABASE_URL:
        print("ERROR: DATABASE_URL not set")
        return

    engine = create_async_engine(DATABASE_URL)

    async with engine.begin() as conn:
        for stmt in STATEMENTS:
            print(f"  Running: {stmt}")
            await conn.execute(text(stmt))

    print("Migration complete: tax fields added to companies table.")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
