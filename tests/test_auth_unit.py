from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from starlette.requests import Request

from app.auth import _extract_request_token, verify_token


def _credentials(token: str) -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


def _request_with_cookies(cookies: dict[str, str] | None = None) -> Request:
    cookie_header = ""
    if cookies:
        cookie_header = "; ".join(f"{key}={value}" for key, value in cookies.items())

    headers = []
    if cookie_header:
        headers.append((b"cookie", cookie_header.encode("latin-1")))

    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "path": "/",
            "raw_path": b"/",
            "query_string": b"",
            "headers": headers,
            "client": ("testclient", 50000),
            "server": ("testserver", 80),
            "scheme": "http",
            "root_path": "",
        }
    )


@pytest.mark.asyncio
async def test_verify_token_returns_sub_and_email(monkeypatch):
    from app import auth as auth_module

    monkeypatch.setattr(
        auth_module.supabase.auth,
        "get_user",
        lambda token: SimpleNamespace(user=SimpleNamespace(id="f91a8e13-4773-4913-8d07-f16bf12f8a7d", email="u@test.com")),
    )

    payload = await verify_token(_request_with_cookies(), _credentials("valid"))

    assert payload["sub"] == "f91a8e13-4773-4913-8d07-f16bf12f8a7d"
    assert payload["email"] == "u@test.com"


@pytest.mark.asyncio
async def test_verify_token_missing_credentials_raises_401():
    with pytest.raises(HTTPException) as exc:
        await verify_token(_request_with_cookies(), None)

    assert exc.value.status_code == 401
    assert exc.value.detail == "Unauthorized"
    assert exc.value.headers == {"WWW-Authenticate": "Bearer"}


@pytest.mark.asyncio
async def test_verify_token_invalid_user_raises_401(monkeypatch):
    from app import auth as auth_module

    monkeypatch.setattr(auth_module.supabase.auth, "get_user", lambda token: SimpleNamespace(user=None))

    with pytest.raises(HTTPException) as exc:
        await verify_token(_request_with_cookies(), _credentials("invalid"))

    assert exc.value.status_code == 401
    assert exc.value.detail == "Invalid token"


@pytest.mark.asyncio
async def test_verify_token_supabase_error_raises_401(monkeypatch):
    from app import auth as auth_module

    def _raise(token: str):
        raise RuntimeError("supabase unavailable")

    monkeypatch.setattr(auth_module.supabase.auth, "get_user", _raise)

    with pytest.raises(HTTPException) as exc:
        await verify_token(_request_with_cookies(), _credentials("any"))

    assert exc.value.status_code == 401
    assert exc.value.detail == "Unauthorized"


def test_extract_request_token_prefers_bearer_over_cookie():
    request = _request_with_cookies({"access_token": "cookie-token"})
    token = _extract_request_token(request, _credentials("header-token"))
    assert token == "header-token"


def test_extract_request_token_reads_cookie_without_prefix():
    request = _request_with_cookies({"access_token": "cookie-token"})
    token = _extract_request_token(request, None)
    assert token == "cookie-token"


def test_extract_request_token_reads_cookie_with_bearer_prefix():
    request = _request_with_cookies({"access_token": "Bearer prefixed-token"})
    token = _extract_request_token(request, None)
    assert token == "prefixed-token"
