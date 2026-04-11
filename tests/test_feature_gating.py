import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api.deps import infer_plan_tier, require_feature
from app.database import Base
from app.models import Company, User


TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


@pytest.fixture
async def db_session():
    engine = create_async_engine(
        TEST_DATABASE_URL,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    testing_session = async_sessionmaker(
        autocommit=False,
        autoflush=False,
        bind=engine,
        expire_on_commit=False,
    )

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with testing_session() as session:
        yield session

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)

    await engine.dispose()


@pytest.mark.asyncio
async def test_require_feature_blocks_without_company(db_session):
    user = User(id=uuid.uuid4(), email="no-company@example.com", company_id=None)
    db_session.add(user)
    await db_session.commit()

    dependency = require_feature("cfo.simulate")

    with pytest.raises(HTTPException) as exc_info:
        await dependency(current_user=user, db=db_session)

    assert exc_info.value.status_code == 402
    assert exc_info.value.detail["reason"] == "no_company"


@pytest.mark.asyncio
async def test_require_feature_allows_pro_feature(db_session):
    company = Company(
        id=uuid.uuid4(),
        name="Pro Co",
        nip="1234567890",
        billing_status="active",
    )
    user = User(id=uuid.uuid4(), email="pro@example.com", company_id=company.id)
    db_session.add_all([company, user])
    await db_session.commit()

    dependency = require_feature("cfo.simulate")
    result = await dependency(current_user=user, db=db_session)

    assert result["plan"] == "pro"
    assert result["feature"] == "cfo.simulate"


@pytest.mark.asyncio
async def test_require_feature_blocks_tier_restriction(db_session):
    company = Company(
        id=uuid.uuid4(),
        name="Pro Co 2",
        nip="1234567891",
        billing_status="active",
    )
    user = User(id=uuid.uuid4(), email="pro2@example.com", company_id=company.id)
    db_session.add_all([company, user])
    await db_session.commit()

    dependency = require_feature("cfo.share")

    with pytest.raises(HTTPException) as exc_info:
        await dependency(current_user=user, db=db_session)

    assert exc_info.value.status_code == 402
    assert exc_info.value.detail["reason"] == "tier_restriction"


@pytest.mark.asyncio
async def test_require_feature_blocks_with_grace_period_reason(db_session):
    company = Company(
        id=uuid.uuid4(),
        name="Grace Co",
        nip="1234567892",
        billing_status="past_due",
    )
    user = User(id=uuid.uuid4(), email="grace@example.com", company_id=company.id)
    db_session.add_all([company, user])
    await db_session.commit()

    dependency = require_feature("cfo.share")

    with pytest.raises(HTTPException) as exc_info:
        await dependency(current_user=user, db=db_session)

    assert exc_info.value.status_code == 402
    assert exc_info.value.detail["reason"] == "subscription_grace_period"


def test_infer_plan_tier_inactive_is_free():
    company = Company(name="X", nip="1234567893", billing_status="canceled")
    assert infer_plan_tier(company, plan_name="enterprise") == "free"
