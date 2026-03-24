import asyncio
import os
from sqlalchemy.ext.asyncio import create_async_engine
from dotenv import load_dotenv

from app.database import Base
from app.models import Document  # Ensure models are imported so Base knows them

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+asyncpg://", 1)

async def run_migration():
    engine = create_async_engine(DATABASE_URL)
    async with engine.begin() as conn:
        print("Running migration for documents table...")
        try:
            await conn.run_sync(Base.metadata.create_all)
            print("Successfully created tables (if they didn't exist).")
        except Exception as e:
            print(f"Error during migration: {e}")
    await engine.dispose()

if __name__ == "__main__":
    asyncio.run(run_migration())
