import asyncio
from app.database import engine, Base
import app.models  # To ensure models are loaded

async def main():
    async with engine.begin() as conn:
        print("Creating new tables...")
        # Create all will only create tables that do not exist, it won't drop anything.
        await conn.run_sync(Base.metadata.create_all)
        print("Done!")

if __name__ == "__main__":
    asyncio.run(main())
