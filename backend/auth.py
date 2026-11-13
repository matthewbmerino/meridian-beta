"""Private-surface auth dependency — fail-closed gate for all /api/*.

Every route except /api/auth and /api/health sits behind this dependency:
the operator's login cookie (HttpOnly, signed) must validate. Fail-closed:
no password configured -> the API stays locked (403), never served open.

Service access: a bearer token (Authorization: Bearer <MERIDIAN_API_TOKEN>)
grants the same surface as a login cookie, for agents and scripts that
cannot hold an HttpOnly cookie. Either credential validates; neither
present -> 401.
"""

from __future__ import annotations

from fastapi import Cookie, Header, HTTPException, status

from data import auth


def require_auth(
    meridian_auth: str | None = Cookie(default=None, alias="meridian_auth"),
    authorization: str | None = Header(default=None),
) -> None:
    """Reject the request unless a valid login cookie or service token is present.

    The operator's HttpOnly login cookie is the primary credential; the
    bearer token is the service path for agents/scripts. Both are checked
    constant-time; neither present -> 401. Fail-closed: no password
    configured -> 403, never served open.
    """
    if not auth.is_password_set():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Private data is locked — no password configured",
        )
    if meridian_auth and auth.validate_cookie(meridian_auth):
        return
    if authorization and authorization.startswith("Bearer "):
        token = authorization.removeprefix("Bearer ").strip()
        if auth.verify_api_token(token):
            return
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication required",
    )
