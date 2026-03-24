import asyncio
from sqlalchemy import text
from app.database import engine

async def migrate():
    async with engine.begin() as conn:
        try:
            await conn.execute(text("ALTER TABLE invoices ADD COLUMN invoice_type VARCHAR DEFAULT 'expense'"))
            print("Added invoice_type column")
        except Exception as e:
            print(f"invoice_type might exist: {e}")
            
        try:
            await conn.execute(text("ALTER TABLE invoices ADD COLUMN is_vat_exempt BOOLEAN DEFAULT FALSE"))
            print("Added is_vat_exempt column")
        except Exception as e:
            print(f"is_vat_exempt might exist: {e}")

if __name__ == "__main__":
    asyncio.run(migrate())
