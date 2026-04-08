import os
from typing import TypedDict

from fastapi import HTTPException, Request, Response, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from supabase import Client, create_client


class VerifiedToken(TypedDict):
    sub: str
    email: str | None


security = HTTPBearer(auto_error=False)

AUTH_COOKIE_NAME = os.getenv("AUTH_COOKIE_NAME", "access_token")
AUTH_COOKIE_PATH = os.getenv("AUTH_COOKIE_PATH", "/")
AUTH_COOKIE_DOMAIN = os.getenv("AUTH_COOKIE_DOMAIN")
AUTH_COOKIE_SECURE = os.getenv("AUTH_COOKIE_SECURE", "false").lower() == "true"
AUTH_COOKIE_SAMESITE = os.getenv("AUTH_COOKIE_SAMESITE", "lax")
AUTH_COOKIE_MAX_AGE = int(os.getenv("AUTH_COOKIE_MAX_AGE", "604800"))


def _create_supabase_client() -> Client:
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_KEY")

    if not url or not key:
        raise ValueError("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY (or SUPABASE_KEY) must be set")

    return create_client(url, key)


supabase = _create_supabase_client()


def verify_supabase_token(token: str) -> VerifiedToken:
    try:
        user_response = supabase.auth.get_user(token)
        user = getattr(user_response, "user", None)

        if not user or not getattr(user, "id", None):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token",
                headers={"WWW-Authenticate": "Bearer"},
            )

        return {
            "sub": str(user.id),
            "email": getattr(user, "email", None),
        }
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Bearer"},
        )


def set_auth_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=AUTH_COOKIE_NAME,
        value=token,
        httponly=True,
        secure=AUTH_COOKIE_SECURE,
        samesite=AUTH_COOKIE_SAMESITE,
        max_age=AUTH_COOKIE_MAX_AGE,
        path=AUTH_COOKIE_PATH,
        domain=AUTH_COOKIE_DOMAIN,
    )


def clear_auth_cookie(response: Response) -> None:
    response.delete_cookie(
        key=AUTH_COOKIE_NAME,
        path=AUTH_COOKIE_PATH,
        domain=AUTH_COOKIE_DOMAIN,
    )


def _extract_request_token(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None,
) -> str:
    if credentials is not None and credentials.credentials:
        return credentials.credentials

    token_from_cookie = request.cookies.get(AUTH_COOKIE_NAME)
    if token_from_cookie:
        if token_from_cookie.lower().startswith("bearer "):
            return token_from_cookie[7:].strip()
        return token_from_cookie

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Unauthorized",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def verify_token(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Security(security),
) -> VerifiedToken:
    token = _extract_request_token(request, credentials)
    return verify_supabase_token(token)