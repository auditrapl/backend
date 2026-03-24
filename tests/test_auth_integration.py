import pytest
import uuid
from datetime import timedelta
from sqlalchemy.future import select
from app.models import User
from tests.utils import create_test_token

@pytest.mark.asyncio
async def test_auth_autosync_new_user(client, db_session):
    """
    Test that a valid token for a non-existent user automatically creates the user in the DB.
    """
    new_user_id = str(uuid.uuid4())
    email = "newuser@example.com"
    token = create_test_token(new_user_id, email)
    
    # Call an endpoint that requires auth (e.g. /auth/me)
    headers = {"Authorization": f"Bearer {token}"}
    response = await client.get("/auth/me", headers=headers)
    
    # 1. Assert Response
    assert response.status_code == 200
    data = response.json()
    assert data["id"] == new_user_id
    assert data["email"] == email
    
    # 2. Assert DB Sync
    # We need to query the DB to verify the record exists
    result = await db_session.execute(select(User).where(User.id == uuid.UUID(new_user_id)))
    user_db = result.scalars().first()
    assert user_db is not None
    assert str(user_db.id) == new_user_id
    assert user_db.email == email

@pytest.mark.asyncio
async def test_auth_existing_user(client, db_session):
    """
    Test that an existing user can login without errors (no duplicate key exceptions).
    """
    # 1. Create user manually
    user_id = uuid.uuid4()
    email = "existing@example.com"
    existing_user = User(id=user_id, email=email)
    db_session.add(existing_user)
    await db_session.commit()
    
    # 2. Login with token matching existing user
    token = create_test_token(str(user_id), email)
    headers = {"Authorization": f"Bearer {token}"}
    response = await client.get("/auth/me", headers=headers)
    
    assert response.status_code == 200
    data = response.json()
    assert data["id"] == str(user_id)

@pytest.mark.asyncio
async def test_auth_invalid_token(client):
    """
    Test that an invalid token (wrong secret) returns 401.
    """
    # Create token with WRONG secret manually
    import jwt
    token = jwt.encode(
        {"sub": str(uuid.uuid4()), "aud": "authenticated"}, 
        "WRONG_SECRET", 
        algorithm="HS256"
    )
    headers = {"Authorization": f"Bearer {token}"}
    response = await client.get("/auth/me", headers=headers)
    
    assert response.status_code == 401

@pytest.mark.asyncio
async def test_auth_expired_token(client):
    """
    Test that an expired token returns 401.
    """
    user_id = str(uuid.uuid4())
    # Create token expired 1 hour ago
    token = create_test_token(user_id, "expired@example.com", expires_delta=timedelta(hours=-1))
    
    headers = {"Authorization": f"Bearer {token}"}
    response = await client.get("/auth/me", headers=headers)
    
    assert response.status_code == 401
