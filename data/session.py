"""Single-operator data root + shared secrets.

Meridian is single-operator: there are no sessions, no anonymous visitors,
and no per-visitor isolation. Every store lives under a fixed, private
``data/`` root and persists. This module resolves that root and holds the
shared HMAC secret (used to sign the operator's login cookie and to derive
the at-rest encryption key).

The previous per-session model (data/sessions/{id}/, 24h purge, anonymous
read/write) was deleted in the 2026-08-16 single-operator hardening — it was
the root cause of the cross-session data leak.
"""

from __future__ import annotations

import hashlib
import os
import secrets
from pathlib import Path

_ROOT = Path(__file__).resolve().parent


def data_root() -> Path:
    """Return the global data directory (created on first access)."""
    _ROOT.mkdir(parents=True, exist_ok=True)
    return _ROOT


def _secret() -> bytes:
    """Return the HMAC signing secret (persisted, 0600, gitignored).

    Prefers MERIDIAN_SESSION_SECRET; otherwise a file data/.session_secret.
    Never logs or exposes the secret.
    """
    env = os.getenv("MERIDIAN_SESSION_SECRET")
    if env:
        return env.encode()
    secret_file = _ROOT / ".session_secret"
    try:
        if secret_file.exists():
            return secret_file.read_bytes().strip()
        secret = secrets.token_bytes(32)
        secret_file.write_bytes(secret)
        try:
            os.chmod(secret_file, 0o600)
        except OSError:
            pass
        return secret
    except OSError:
        return secrets.token_bytes(32)


# PBKDF2 iteration count for key derivation. 600k is OWASP's recommendation
# for PBKDF2-HMAC-SHA256; tests lower it via conftest to keep the suite fast.
PBKDF2_ITERATIONS = 600_000


def derive_encryption_key(salt: bytes) -> bytes:
    """Derive a 32-byte AES-GCM key from the shared secret + a per-file salt.

    Each encrypted store uses its own random salt (stored alongside the
    ciphertext), so one store's ciphertext can't be swapped into another's
    slot. PBKDF2-HMAC-SHA256, PBKDF2_ITERATIONS rounds.
    """
    return hashlib.pbkdf2_hmac(
        "sha256", _secret(), salt, PBKDF2_ITERATIONS, dklen=32
    )
