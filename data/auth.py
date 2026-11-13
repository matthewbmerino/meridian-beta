"""Single-operator auth: bcrypt password + signed login cookie.

Meridian is single-operator. There is one password (bcrypt-hashed, stored at
data/.auth_credentials or set via MERIDIAN_AUTH_PASSWORD) and one signed
HttpOnly session cookie. Every /api/* route except /api/auth and /api/health
is behind it. Fail-closed: no password configured -> the API stays locked.

Brute-force protection: exponential backoff after failed attempts, tracked
in-memory per client (bounded window). Combined with the nginx login rate
zone, this makes password guessing impractical.

The at-rest encryption key derives from the same shared secret (data/session.py
_secret) so it is never stored alongside the data it protects.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import threading
import time
from pathlib import Path

_AUTH_CRED_FILE = Path(__file__).resolve().parent / ".auth_credentials"

# The free edition runs on loopback over plain HTTP. A Secure cookie is fine in
# Chrome on localhost but unreliable in Safari and impossible over a LAN IP;
# the install sets this to 0 and binds to 127.0.0.1.
_COOKIE_SECURE = os.getenv("MERIDIAN_COOKIE_SECURE", "1").strip().lower() not in (
    "0", "false", "no"
)

# Login cookie lifetime. Long enough that the operator isn't re-logging-in
# constantly; bounded so a leaked cookie eventually expires.
AUTH_COOKIE_TTL_SECONDS = 14 * 24 * 3600  # 14 days

# Brute-force backoff: after FAIL_LIMIT failures, reject attempts for
# BACKOFF_SECONDS * 2^(failures - FAIL_LIMIT).
FAIL_LIMIT = 5
BACKOFF_BASE_SECONDS = 5
_BACKOFF_WINDOW_SECONDS = 15 * 60

# bcrypt cost factor. 12 is the production default (OWASP); tests lower it via
# conftest to keep the suite fast.
_BCRYPT_ROUNDS = 12

_lock = threading.Lock()
_failures: dict[str, list[float]] = {}


# ---------------------------------------------------------------------------
# Password configuration
# ---------------------------------------------------------------------------

def is_password_set() -> bool:
    """True when a password is configured (env var or persisted file)."""
    if os.getenv("MERIDIAN_AUTH_PASSWORD"):
        return True
    return _AUTH_CRED_FILE.exists()


def set_password(password: str) -> None:
    """Set (or reset) the operator password. Persisted bcrypt-hashed, 0600."""
    import bcrypt

    if not password or len(password) < 12:
        raise ValueError("Password must be at least 12 characters")
    hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=_BCRYPT_ROUNDS)).decode()
    _AUTH_CRED_FILE.write_text(hashed + "\n")
    try:
        os.chmod(_AUTH_CRED_FILE, 0o600)
    except OSError:
        pass


def _stored_hash() -> str | None:
    env_pw = os.getenv("MERIDIAN_AUTH_PASSWORD")
    if env_pw:
        return env_pw  # plaintext in env is compared via verify below
    if not _AUTH_CRED_FILE.exists():
        return None
    try:
        return _AUTH_CRED_FILE.read_text().strip()
    except OSError:
        return None


def _credential_version() -> str:
    """Short digest of the CURRENT credential, bound into every cookie.

    Rotating the password (or the MERIDIAN_AUTH_PASSWORD env value) changes this
    digest, so cookies issued before the rotation stop validating. Without it a
    stolen cookie survives its full TTL and there is no supported way to evict
    it: the signing secret is shared with the at-rest encryption keys, so
    rotating that would re-key every store instead of just expiring sessions.
    """
    stored = _stored_hash() or ""
    return hashlib.sha256(stored.encode("utf-8")).hexdigest()[:16]


def verify_password(password: str) -> bool:
    """Constant-time check of a submitted password against the configured one."""
    import bcrypt

    env_pw = os.getenv("MERIDIAN_AUTH_PASSWORD")
    if env_pw:
        return hmac.compare_digest(password, env_pw)
    stored = _stored_hash()
    if not stored:
        return False
    try:
        return bcrypt.checkpw(password.encode(), stored.encode())
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Brute-force backoff
# ---------------------------------------------------------------------------

def _client_key(client: str) -> str:
    return client or "unknown"


def is_throttled(client: str) -> bool:
    """True when *client* must be rejected due to recent failures."""
    now = time.time()
    key = _client_key(client)
    with _lock:
        recent = [t for t in _failures.get(key, []) if now - t < _BACKOFF_WINDOW_SECONDS]
        _failures[key] = recent
        if len(recent) < FAIL_LIMIT:
            return False
        excess = len(recent) - FAIL_LIMIT + 1
        backoff = BACKOFF_BASE_SECONDS * (2 ** excess)
        return (now - recent[0]) < backoff


def record_failure(client: str) -> None:
    now = time.time()
    key = _client_key(client)
    with _lock:
        recent = [t for t in _failures.get(key, []) if now - t < _BACKOFF_WINDOW_SECONDS]
        recent.append(now)
        _failures[key] = recent


def clear_failures(client: str) -> None:
    with _lock:
        _failures.pop(_client_key(client), None)


# ---------------------------------------------------------------------------
# Signed cookie
# ---------------------------------------------------------------------------

def _secret() -> bytes:
    """Shared HMAC secret (data/session.py)."""
    from data.session import _secret
    return _secret()


def _encode_client(client: str) -> str:
    """Base64-encode the client id so it can't contain '.' (the token separator)."""
    return base64.urlsafe_b64encode(client.encode()).rstrip(b"=").decode()


def _decode_client(encoded: str) -> str:
    try:
        return base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode()
    except Exception:
        return ""


def issue_cookie(client: str) -> str:
    """Sign a login cookie for *client*: {b64(client)}.{expiry}.{hmac}.

    The client id (an IP address like 127.0.0.1) is base64-encoded so it can
    never contain '.', which is the token separator.
    """
    expiry = int(time.time()) + AUTH_COOKIE_TTL_SECONDS
    client_b64 = _encode_client(client)
    payload = f"auth:{client_b64}:{expiry}:{_credential_version()}"
    sig = hmac.new(_secret(), payload.encode(), hashlib.sha256).digest()
    sig_b64 = base64.urlsafe_b64encode(sig).rstrip(b"=").decode()
    return f"{client_b64}.{expiry}.{sig_b64}"


def validate_cookie(cookie: str) -> bool:
    """Validate a login cookie's signature, expiry, and credential version.

    A cookie minted before a password rotation fails here even though its
    signature is intact: the version component no longer matches the current
    credential. That is the revocation path.
    """
    if not cookie:
        return False
    parts = cookie.split(".")
    if len(parts) != 3:
        return False
    client_b64, expiry_str, sig_b64 = parts
    if not _decode_client(client_b64):
        return False
    try:
        expiry = int(expiry_str)
    except ValueError:
        return False
    if time.time() > expiry:
        return False
    expected = hmac.new(
        _secret(),
        f"auth:{client_b64}:{expiry_str}:{_credential_version()}".encode(),
        hashlib.sha256,
    ).digest()
    try:
        provided = base64.urlsafe_b64decode(sig_b64 + "=" * (-len(sig_b64) % 4))
    except Exception:
        return False
    return hmac.compare_digest(expected, provided)


# ---------------------------------------------------------------------------
# Service token (agent/script access)
# ---------------------------------------------------------------------------

def _api_token() -> str | None:
    """The configured service token, or None when unset.

    Read from MERIDIAN_API_TOKEN (root-only /etc/meridian/env in prod).
    A token is optional: when unset, only the login cookie grants access.
    """
    return os.getenv("MERIDIAN_API_TOKEN") or None


def is_api_token_set() -> bool:
    """True when a service token is configured."""
    return _api_token() is not None


def verify_api_token(token: str) -> bool:
    """Constant-time check of a submitted bearer token.

    The token is a high-entropy secret (32+ random bytes) compared with
    hmac.compare_digest — no timing oracle, same discipline as the cookie
    signature. Empty/unset token -> False.
    """
    configured = _api_token()
    if not configured or not token:
        return False
    return hmac.compare_digest(token, configured)
