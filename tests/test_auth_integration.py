import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app.models import User


def _mock_supabase_user(monkeypatch, user_id: str | None, email: str | None, *, raises: Exception | None = None):
    from app import auth as auth_module

    def fake_get_user(token: str):
        if raises is not None:
            raise raises
        if user_id is None:
            return SimpleNamespace(user=None)
        return SimpleNamespace(user=SimpleNamespace(id=user_id, email=email))

    monkeypatch.setattr(auth_module.supabase.auth, "get_user", fake_get_user)


@pytest.mark.asyncio
async def test_auth_autosync_new_user_creates_local_user(client, db_session, monkeypatch):
    new_user_id = str(uuid.uuid4())
    email = "newuser@example.com"
    _mock_supabase_user(monkeypatch, new_user_id, email)

    response = await client.get("/auth/me", headers={"Authorization": "Bearer valid-token"})

    assert response.status_code == 200
    data = response.json()
    assert data["id"] == new_user_id
    assert data["email"] == email

    result = await db_session.execute(select(User).where(User.id == uuid.UUID(new_user_id)))
    user_db = result.scalars().first()
    assert user_db is not None
    assert str(user_db.id) == new_user_id
    assert user_db.email == email


@pytest.mark.asyncio
async def test_auth_existing_user_does_not_duplicate_record(client, db_session, monkeypatch):
    user_id = uuid.uuid4()
    email = "existing@example.com"
    db_session.add(User(id=user_id, email=email))
    await db_session.commit()

    _mock_supabase_user(monkeypatch, str(user_id), email)

    response = await client.get("/auth/me", headers={"Authorization": "Bearer valid-token"})

    assert response.status_code == 200
    data = response.json()
    assert data["id"] == str(user_id)
    assert data["email"] == email

    count_result = await db_session.execute(
        select(func.count()).select_from(User).where(User.id == user_id)
    )
    assert count_result.scalar_one() == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    ["/", "/auth/me", "/invoices", "/departments"],
)
async def test_protected_endpoints_require_bearer_token(client, path):
    response = await client.get(path)
    assert response.status_code == 401
    assert response.headers.get("www-authenticate") == "Bearer"


@pytest.mark.asyncio
async def test_auth_rejects_invalid_token_from_supabase(client, monkeypatch):
    _mock_supabase_user(monkeypatch, None, None)

    response = await client.get("/auth/me", headers={"Authorization": "Bearer invalid-token"})
    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid token"


@pytest.mark.asyncio
async def test_auth_rejects_when_supabase_client_throws(client, monkeypatch):
    _mock_supabase_user(monkeypatch, None, None, raises=RuntimeError("network down"))

    response = await client.get("/auth/me", headers={"Authorization": "Bearer any-token"})
    assert response.status_code == 401
    assert response.json()["detail"] == "Unauthorized"


@pytest.mark.asyncio
async def test_auth_rejects_non_uuid_subject_payload(client, monkeypatch):
    _mock_supabase_user(monkeypatch, "not-a-uuid", "broken@example.com")

    response = await client.get("/auth/me", headers={"Authorization": "Bearer valid-token"})
    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid token payload"


@pytest.mark.asyncio
async def test_auth_accepts_cookie_token_without_authorization_header(client, monkeypatch):
    user_id = str(uuid.uuid4())
    _mock_supabase_user(monkeypatch, user_id, "cookieuser@example.com")

    client.cookies.set("access_token", "cookie-token")
    response = await client.get("/auth/me")
    assert response.status_code == 200
    assert response.json()["id"] == user_id
    client.cookies.clear()


@pytest.mark.asyncio
async def test_session_endpoint_sets_httponly_cookie_and_enables_auth(client, monkeypatch):
    user_id = str(uuid.uuid4())
    _mock_supabase_user(monkeypatch, user_id, "session@example.com")

    set_session = await client.post("/auth/session", json={"access_token": "session-token"})
    assert set_session.status_code == 200
    assert set_session.json()["message"] == "Session cookie set"

    set_cookie_header = set_session.headers.get("set-cookie", "")
    assert "access_token=session-token" in set_cookie_header
    assert "HttpOnly" in set_cookie_header

    me_response = await client.get("/auth/me")
    assert me_response.status_code == 200
    assert me_response.json()["id"] == user_id


@pytest.mark.asyncio
async def test_logout_clears_cookie_and_blocks_followup_request(client, monkeypatch):
    user_id = str(uuid.uuid4())
    _mock_supabase_user(monkeypatch, user_id, "logout@example.com")

    set_session = await client.post("/auth/session", json={"access_token": "logout-token"})
    assert set_session.status_code == 200

    logout = await client.post("/auth/logout")
    assert logout.status_code == 200
    assert logout.json()["message"] == "Session cookie cleared"

    post_logout = await client.get("/auth/me")
    assert post_logout.status_code == 401
