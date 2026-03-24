import asyncio
import os
from sqlalchemy.ext.asyncio import create_async_engine
from dotenv import load_dotenv
from sqlalchemy import text

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+asyncpg://", 1)

async def run_migration():
    engine = create_async_engine(DATABASE_URL)
    async with engine.begin() as conn:
        print("Running migration...")
        try:
            await conn.execute(text("ALTER TABLE companies ADD COLUMN total_budget NUMERIC(10, 2);"))
            print("Successfully added total_budget column.")
        except Exception as e:
            print(f"Error (maybe column already exists?): {e}")
    await engine.dispose()

if __name__ == "__main__":
    asyncio.run(run_migration())
