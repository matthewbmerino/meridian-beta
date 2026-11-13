"""Auth endpoints: login, logout, status — single-operator password + cookie."""

from fastapi import APIRouter, Cookie, HTTPException, Request, Response
from pydantic import BaseModel

from data import auth

router = APIRouter(prefix="/api/auth", tags=["auth"])

_COOKIE_NAME = "meridian_auth"
_COOKIE_PATH = "/"
_COOKIE_MAX_AGE = auth.AUTH_COOKIE_TTL_SECONDS


class LoginBody(BaseModel):
    password: str


class AuthStatus(BaseModel):
    locked: bool
    authenticated: bool


def _client_ip(request: Request) -> str:
    """Best-effort client address for throttle bookkeeping (not security)."""
    return request.client.host if request.client else "unknown"


def _cookie_secure() -> bool:
    """Secure cookies need HTTPS. The free edition binds to loopback and sets
    MERIDIAN_COOKIE_SECURE=0; the hosted deploy leaves the default."""
    import os
    return os.getenv("MERIDIAN_COOKIE_SECURE", "1").strip().lower() not in ("0", "false", "no")


def _set_auth_cookie(response: Response, value: str) -> None:
    # HttpOnly + Secure + SameSite=Strict: not readable by JS, only sent on
    # same-site requests over HTTPS. This is the XSS-resistant transport.
    response.set_cookie(
        key=_COOKIE_NAME,
        value=value,
        max_age=_COOKIE_MAX_AGE,
        path=_COOKIE_PATH,
        httponly=True,
        secure=_cookie_secure(),
        samesite="strict",
    )


def _clear_auth_cookie(response: Response) -> None:
    response.delete_cookie(key=_COOKIE_NAME, path=_COOKIE_PATH)


@router.get("/status")
def auth_status(
    response: Response,
    meridian_auth: str | None = Cookie(default=None, alias=_COOKIE_NAME),
):
    """Report whether the API is locked and whether this session is authed."""
    locked = not auth.is_password_set()
    authenticated = bool(meridian_auth) and auth.validate_cookie(meridian_auth)
    return AuthStatus(locked=locked, authenticated=authenticated)


@router.post("/login")
def login(body: LoginBody, request: Request, response: Response):
    """Check the password; on success set the HttpOnly login cookie.

    Brute-force protected: repeated failures from a client trigger exponential
    backoff (data/auth.py).
    """
    client = _client_ip(request)
    if not auth.is_password_set():
        raise HTTPException(status_code=403, detail="No password configured — API is locked")
    if auth.is_throttled(client):
        raise HTTPException(status_code=429, detail="Too many attempts — try again later")

    if not auth.verify_password(body.password):
        auth.record_failure(client)
        raise HTTPException(status_code=401, detail="Incorrect password")

    auth.clear_failures(client)
    _set_auth_cookie(response, auth.issue_cookie(client))
    return {"authenticated": True}


@router.post("/logout")
def logout(response: Response):
    """Clear the login cookie."""
    _clear_auth_cookie(response)
    return {"ok": True}
