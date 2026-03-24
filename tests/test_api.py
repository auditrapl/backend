import pytest
import uuid
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.pool import StaticPool
from app.main import app
from app.database import get_db, Base
from app.api import invoices

# Setup in-memory SQLite database for testing
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

@pytest.mark.asyncio
async def test_create_invoice(client):
    invoice_data = {
        "filename": "test_invoice_super.pdf",
        "organization_id": str(uuid.uuid4()),
        "net_amount": 100.50,
        "currency": "EUR",
        "contractor_name": "Test Contractor",
        "status": "pending"
    }
    response = await client.post("/invoices/", json=invoice_data)
    
    assert response.status_code == 201
    data = response.json()
    assert data["filename"] == "test_invoice_super.pdf"
    assert float(data["net_amount"]) == 100.50
    assert data["contractor_name"] == "Test Contractor"
    assert "id" in data
    assert "created_at" in data
