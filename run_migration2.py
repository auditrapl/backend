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

    engine = create_async_engine(DATABASE_URL)
    async with engine.begin() as conn:
        try:
            await conn.execute(text("ALTER TABLE users ADD COLUMN first_name VARCHAR;"))
            print("Added first_name")
        except Exception as e:
            print(f"Failed to add first_name: {e}")
        try:
            await conn.execute(text("ALTER TABLE users ADD COLUMN last_name VARCHAR;"))
            print("Added last_name")
        except Exception as e:
            print(f"Failed to add last_name: {e}")
            
    await engine.dispose()

if __name__ == "__main__":
    asyncio.run(main())
