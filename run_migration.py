import asyncio
import os
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+asyncpg://", 1)

async def main():
    if not DATABASE_URL:
        print("DATABASE_URL not set")
        return

    engine = create_async_engine(DATABASE_URL, connect_args={"statement_cache_size": 0})
    async with engine.begin() as conn:
        try:
            await conn.execute(text("ALTER TABLE departments ADD COLUMN description TEXT;"))
            print("Added column description to departments")
        except Exception as e:
            print(f"Failed to add description: {e}")
            
    await engine.dispose()

if __name__ == "__main__":
    asyncio.run(main())
