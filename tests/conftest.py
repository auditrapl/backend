import sys
import os
import pytest
import asyncio
from fastapi import Depends, FastAPI
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.pool import StaticPool

# 1. Setup Environment + Python Path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Configure env BEFORE importing app modules
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"
os.environ["SUPABASE_URL"] = "https://test-project.supabase.co"
os.environ["SUPABASE_SERVICE_ROLE_KEY"] = "test-service-role-key"

# Now import app modules
from app.api import auth, invoices, departments
from app.auth import verify_token
from app.database import get_db, Base

app = FastAPI(title="Auditra API Test")
app.include_router(auth.router, prefix="/auth", tags=["Auth"])
app.include_router(invoices.router, prefix="/invoices", tags=["Invoices"])
app.include_router(departments.router, prefix="/departments", tags=["Departments"])


@app.get("/")
async def root(_: dict = Depends(verify_token)):
    return {"message": "Welcome to Auditra API"}

# 2. Setup In-Memory DB
TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"
engine = create_async_engine(
    TEST_DATABASE_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = async_sessionmaker(autocommit=False, autoflush=False, bind=engine)

async def override_get_db():
    async with TestingSessionLocal() as session:
        yield session

app.dependency_overrides[get_db] = override_get_db

@pytest.fixture(scope="session")
def event_loop():
    """Create an event loop for async tests."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()

@pytest.fixture(scope="module")
async def client():
    # Create tables
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    
    # Drop tables
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)

@pytest.fixture(scope="function")
async def db_session():
    async with TestingSessionLocal() as session:
        yield session
