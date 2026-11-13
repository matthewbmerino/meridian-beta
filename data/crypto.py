"""At-rest encryption for Meridian's operator data (AES-256-GCM).

Every PII store (snapshots DB, theses, decisions, imports, prefs, watchlist,
alerts, chats, reports, findings, state files) is encrypted before hitting
disk. The key is derived per-file from the shared secret (data/session.py
_secret) + a per-file random salt via PBKDF2-HMAC-SHA256, so:

  - no key material is stored alongside the ciphertext,
  - one store's ciphertext can't be swapped into another's slot (the AAD is
    bound to the file identity),
  - a lost/leaked data file is unreadable without the secret.

Writers use atomic tmp+rename so a crash never leaves a torn ciphertext. Each
write also keeps exactly one pre-write generation (``<name>.bak``) so a bad
write or a corruption is recoverable, and stamps a keyed digest sidecar
(``<name>.hmac``) so tampering is detectable without decrypting.

A store that exists but cannot be read is a FAILURE, not an empty store. Every
caller must distinguish the two: silently degrading an unreadable store to its
default and then writing the default back is how a real record disappears.

Two modes:
  - text stores (JSON): read_encrypted / write_encrypted
  - SQLite stores (snapshots, gpu): the whole DB file is encrypted at rest as
    ``<name>.db.enc``; while the process runs it is decrypted to the canonical
    ``<name>.db`` path at 0600 (equivalent to process memory for co-tenant
    access) and re-encrypted on shutdown / after writes.
"""

from __future__ import annotations

import functools
import hashlib
import hmac as _hmac
import json
import logging
import os
import threading
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from data.session import _secret, derive_encryption_key

log = logging.getLogger(__name__)

_HEADER_BYTES = b"MERIDIAN1"

# Cache derived keys per (secret-fingerprint, salt) so the PBKDF2 cost is paid
# once per store, not on every read/write. Bounded to one entry per salt (each
# encrypted file carries a unique salt, so the cache stays small).
#
# The SECRET is part of the key. A cache keyed on the salt alone returns a key
# derived under a previous secret when the secret changes mid-process (a data
# root move, a test isolation fixture, a rotate), and every read then fails with
# InvalidTag — a corrupt-looking store that is really a stale cache entry.
_KEY_CACHE: dict[tuple[str, bytes], bytes] = {}
_KEY_CACHE_MAX = 256


def _secret_fingerprint() -> str:
    """Short digest of the active secret — never the secret itself."""
    return hashlib.sha256(_secret()).hexdigest()[:16]

# Decrypted-JSON cache. PBKDF2 at 600k iterations costs ~200ms per store, and a
# single API request reads several stores. Keyed on the file's identity
# (path + mtime + size), so any write invalidates its own entry.
_JSON_CACHE: dict[tuple[str, int, int], dict | list | StoreUnreadable] = {}
_JSON_CACHE_MAX = 64

# Read-error observability: the operator's answer must distinguish "no record"
# from "record unreadable", so callers ask for the reason explicitly.
_LAST_ERROR: dict[str, str] = {}

# One lock per store path. A content write and its digest sidecar are TWO
# separate renames (see write_encrypted), so two writers racing on one store can
# interleave them: writer B's rename lands between writer A's rename and A's
# stamp, and the file then carries A's digest over B's ciphertext. Every later
# read is a false "tampered/truncated" error and the store is unusable while its
# data is perfectly intact — live, a valid Schwab grant was unreadable for
# exactly this reason while every quote thread re-marked the expired token a few
# seconds apart. Serialise readers against writers per path so nobody can
# observe a half-swapped pair.
_PATH_LOCKS: dict[str, threading.RLock] = {}
_PATH_LOCKS_GUARD = threading.Lock()


def _path_lock(path) -> threading.RLock:
    """The reentrant I/O lock for one store path (created on first use)."""
    key = str(path)
    with _PATH_LOCKS_GUARD:
        lock = _PATH_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _PATH_LOCKS[key] = lock
        return lock


def _serialized(fn):
    """Hold the store's per-path I/O lock for the whole call.

    First positional argument is the store path (every decorated function takes
    it first). Reentrant, so a writer that reads its own store is still fine.
    """

    @functools.wraps(fn)
    def wrapper(path, *args, **kwargs):
        with _path_lock(path):
            return fn(path, *args, **kwargs)

    return wrapper


class StoreUnreadable(Exception):
    """A store exists on disk but cannot be decrypted or parsed.

    Raised by the checked readers. Callers that write must treat this as a
    hard stop — writing the fallback over an unreadable store destroys it.
    """


def _key_for(salt: bytes) -> bytes:
    """Derive (or return cached) the AES key for a salt under the CURRENT secret."""
    cache_key = (_secret_fingerprint(), salt)
    cached = _KEY_CACHE.get(cache_key)
    if cached is not None:
        return cached
    key = derive_encryption_key(salt)
    if len(_KEY_CACHE) < _KEY_CACHE_MAX:
        _KEY_CACHE[cache_key] = key
    return key


def _nonce() -> bytes:
    return os.urandom(12)


def encrypt_bytes(data: bytes, identity: str) -> bytes:
    """Encrypt *data* for a named store. Returns header+salt+nonce+ciphertext."""
    salt = os.urandom(16)
    key = _key_for(salt)
    nonce = _nonce()
    aad = f"{_HEADER_BYTES.decode()}:{identity}".encode()
    ct = AESGCM(key).encrypt(nonce, data, aad)
    return _HEADER_BYTES + salt + nonce + ct


def decrypt_bytes(payload: bytes, identity: str) -> bytes:
    """Decrypt a payload produced by encrypt_bytes. Raises on tamper."""
    if not payload.startswith(_HEADER_BYTES):
        raise ValueError("Not a Meridian-encrypted payload")
    off = len(_HEADER_BYTES)
    salt = payload[off:off + 16]
    nonce = payload[off + 16:off + 28]
    ct = payload[off + 28:]
    key = _key_for(salt)
    aad = f"{_HEADER_BYTES.decode()}:{identity}".encode()
    return AESGCM(key).decrypt(nonce, ct, aad)


# ---------------------------------------------------------------------------
# Integrity sidecars: a keyed digest per store, plus one pre-write generation
# ---------------------------------------------------------------------------

def _hmac_path(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".hmac")


def _backup_path(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".bak")


def _digest(payload: bytes) -> str:
    """Keyed digest of a payload. Keyed, so a tamperer cannot re-stamp it."""
    return _hmac.new(_secret(), payload, hashlib.sha256).hexdigest()


def _stamp(path: Path, payload: bytes) -> None:
    tmp = _hmac_path(path).with_suffix(_hmac_path(path).suffix + ".tmp")
    tmp.write_text(_digest(payload), encoding="utf-8")
    tmp.rename(_hmac_path(path))


def _verify_sidecar(path: Path, raw: bytes) -> None:
    """Raise when a sidecar exists and does not match *raw*.

    A missing sidecar is the legacy-plaintext migration path and is accepted.
    A present one means this store claimed integrity, so a mismatch is tampering
    or corruption and must never be served to the app as genuine data.
    """
    sidecar = _hmac_path(path)
    try:
        expected = sidecar.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise StoreUnreadable(f"{path.name}: integrity sidecar unreadable: {exc}") from exc
    if not _hmac.compare_digest(expected, _digest(raw)):
        raise StoreUnreadable(
            f"{path.name}: content does not match its integrity digest "
            "(tampered, truncated, or written by a process without the secret)"
        )


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    """Crash-safe write: the destination is either the old file or the new one.

    ``write_bytes`` truncates before it writes, so a crash mid-write leaves a
    half file whose digest no longer matches — the ``InvalidTag`` failure mode.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "wb") as fh:
        fh.write(payload)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _looks_like_sqlite(blob: bytes) -> bool:
    # 16 bytes is all a header check needs. Requiring more is how a 512-byte
    # probe slice failed its own test and every read re-decrypted the store.
    return len(blob) >= 16 and blob[:16] == b"SQLite format 3\x00"


@_serialized
def verify_integrity(path: Path) -> dict:
    """Verify the store against its digest sidecar.

    Returns ``{"present", "verified", "reason"}``. ``verified`` is None when
    there is nothing to verify (no store, or no digest written yet).
    """
    if not path.exists():
        return {"present": False, "verified": None, "reason": "no store"}
    sidecar = _hmac_path(path)
    if not sidecar.exists():
        return {"present": True, "verified": None, "reason": "no digest sidecar"}
    expected = sidecar.read_text(encoding="utf-8").strip()
    actual = _digest(path.read_bytes())
    if not _hmac.compare_digest(expected, actual):
        return {
            "present": True,
            "verified": False,
            "reason": "store does not match its digest (tampered or corrupted)",
        }
    return {"present": True, "verified": True, "reason": None}


def restore_backup(path: Path) -> bool:
    """Restore the pre-write generation over the store. Returns whether it ran.

    Deliberately explicit and manual: nothing calls this automatically, because
    silently swapping a store for its predecessor is its own data-loss event.
    """
    backup = _backup_path(path)
    if not backup.exists():
        return False
    backup.replace(path)
    if _hmac_path(path).exists():
        _hmac_path(path).unlink()
    return True


# ---------------------------------------------------------------------------
# Text stores
# ---------------------------------------------------------------------------

@_serialized
def read_encrypted(path: Path, identity: str) -> str | None:
    """Read + decrypt a text store. Returns None when missing, raises otherwise."""
    if not path.exists():
        return None
    raw = path.read_bytes()
    _verify_sidecar(path, raw)
    return decrypt_bytes(raw, identity).decode("utf-8")


@_serialized
def write_encrypted(path: Path, identity: str, text: str) -> None:
    """Encrypt + atomically write a text store, keeping one prior generation."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = encrypt_bytes(text.encode("utf-8"), identity)
    if path.exists():
        try:
            # Exactly one prior generation, replaced each write.
            _backup_path(path).write_bytes(path.read_bytes())
        except OSError:
            pass
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(payload)
    tmp.rename(path)
    try:
        _stamp(path, payload)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Raw bytes (CSV import files)
# ---------------------------------------------------------------------------

@_serialized
def read_bytes_store(path: Path, identity: str) -> bytes | None:
    """Read + decrypt a raw-bytes store. Returns None when missing."""
    if not path.exists():
        return None
    raw = path.read_bytes()
    _verify_sidecar(path, raw)
    if raw.startswith(_HEADER_BYTES):
        return decrypt_bytes(raw, identity)
    return raw  # legacy plaintext (migrated on next write)


@_serialized
def write_bytes_store(path: Path, identity: str, data: bytes) -> None:
    """Encrypt + atomically write a raw-bytes store, stamping its sidecar."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = encrypt_bytes(data, identity)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(payload)
    tmp.rename(path)
    try:
        _stamp(path, payload)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# JSON stores (with one-time legacy-plaintext migration)
# ---------------------------------------------------------------------------

@_serialized
def read_json(path: Path, identity: str) -> dict | list | None:
    """Read a JSON store, decrypting if encrypted.

    Transparently handles a legacy plaintext file (pre-encryption): reads it
    as-is so no data is lost. The next write_encrypted migrates it.

    When a ``.hmac`` sidecar exists, the payload must match it: a store that
    cannot prove its own integrity is not served as genuine data.

    Raises StoreUnreadable when the file exists but cannot be decrypted or
    parsed — the caller must NOT treat that as an empty store.
    """
    if not path.exists():
        return None
    try:
        st = path.stat()
        key = (str(path), st.st_mtime_ns, st.st_size)
    except OSError:
        key = None
    if key is not None and key in _JSON_CACHE:
        cached = _JSON_CACHE[key]
        if isinstance(cached, StoreUnreadable):
            raise cached
        return cached

    raw = path.read_bytes()
    _verify_sidecar(path, raw)
    try:
        if raw.startswith(_HEADER_BYTES):
            text = decrypt_bytes(raw, identity).decode("utf-8")
        else:
            text = raw.decode("utf-8", errors="replace")  # legacy plaintext
        parsed = json.loads(text)
    except Exception as e:
        err = StoreUnreadable(f"{path.name}: {type(e).__name__}: {e}")
        _LAST_ERROR[str(path)] = str(e)
        if key is not None:
            _cache_put(key, err)
        raise err
    _LAST_ERROR.pop(str(path), None)
    if key is not None:
        _cache_put(key, parsed)
    return parsed


def _cache_put(key: tuple[str, int, int], value) -> None:
    if len(_JSON_CACHE) >= _JSON_CACHE_MAX:
        # Cheap eviction: drop the oldest-inserted entry.
        _JSON_CACHE.pop(next(iter(_JSON_CACHE)), None)
    _JSON_CACHE[key] = value


def invalidate(path: Path) -> None:
    """Drop any cached read for *path* (used after an out-of-band write)."""
    for key in [k for k in _JSON_CACHE if k[0] == str(path)]:
        _JSON_CACHE.pop(key, None)
    _LAST_ERROR.pop(str(path), None)


def write_json(path: Path, identity: str, data: dict | list) -> None:
    """Encrypt + atomically write a JSON store."""
    write_encrypted(path, identity, json.dumps(data, indent=2, default=str, sort_keys=True))
    invalidate(path)


def load_json_store(path: Path, identity: str, default):
    """Read a JSON store, falling back to *default* ONLY when it does not exist.

    Kept for callers that can genuinely tolerate a missing store. A store that
    exists but cannot be read is never silently replaced by the default — that
    is how a record disappears. Use ``read_json`` when the caller needs to
    distinguish the two.
    """
    try:
        data = read_json(path, identity)
        return data if data is not None else default
    except StoreUnreadable:
        raise


def store_error(path: Path) -> str | None:
    """The last read error for a store, if any (observability hook)."""
    return _LAST_ERROR.get(str(path))


def read_state(path: Path, identity: str, default):
    """(value, error) — the read that never lies about which one happened."""
    try:
        data = read_json(path, identity)
        return (data if data is not None else default), None
    except StoreUnreadable as e:
        return default, str(e)


# ---------------------------------------------------------------------------
# SQLite whole-file stores (encrypted at rest, decrypted while running)
# ---------------------------------------------------------------------------

def enc_path(path: Path) -> Path:
    """Path of the at-rest ciphertext for a SQLite file."""
    return path.with_suffix(path.suffix + ".enc")


def is_encrypted_at_rest(path: Path) -> bool:
    return enc_path(path).exists()


def decrypt_sqlite_to_runtime(path: Path, identity: str) -> None:
    """Ensure the runtime plaintext DB exists at *path* (0600).

    If the ciphertext exists and the plaintext doesn't, decrypt it. If the
    plaintext exists and the ciphertext doesn't, encrypt it now (migration).
    The plaintext lives only while the process runs; re-encrypt on shutdown.

    When BOTH exist, a plaintext that is not even a SQLite file is discarded
    and re-decrypted. The check is the 16-byte header ONLY: opening a live DB
    (``PRAGMA integrity_check``) while the service holds it in WAL mode can
    fail on the lock and would delete a perfectly good working copy.
    """
    cipher = enc_path(path)
    if cipher.exists():
        if path.exists():
            head = path.read_bytes()[:512]
            if not _looks_like_sqlite(head):
                log.warning("discarding unusable plaintext %s (%d bytes)", path.name, path.stat().st_size)
                path.unlink(missing_ok=True)
        if not path.exists():
            plain = decrypt_bytes(cipher.read_bytes(), identity)
            if not _looks_like_sqlite(plain):
                raise StoreUnreadable(f"{path.name}: decrypted payload is not a SQLite file")
            _atomic_write_bytes(path, plain)
    elif path.exists():
        # One-time migration of a legacy plaintext DB.
        _atomic_write_bytes(cipher, encrypt_bytes(path.read_bytes(), identity))
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def encrypt_sqlite_from_runtime(path: Path, identity: str) -> None:
    """Re-encrypt the runtime plaintext DB to its at-rest ciphertext.

    Checkpoints any WAL sidecar into the main file first, then encrypts and
    removes the runtime plaintext (and its -wal/-shm sidecars) so nothing
    plaintext survives on disk once the process stops.
    """
    import sqlite3

    if not path.exists():
        return
    try:
        conn = sqlite3.connect(path)
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()
    except sqlite3.Error:
        pass
    env_path = enc_path(path)
    payload = encrypt_bytes(path.read_bytes(), identity)
    if env_path.exists():
        try:
            # Exactly one prior generation, replaced each write.
            _backup_path(env_path).write_bytes(env_path.read_bytes())
            # …and a second generation when the store SHRANK. A collapse (a
            # decrypt that produced an empty DB, then a clean shutdown) would
            # otherwise overwrite the only good copy in one step.
            if len(payload) * 2 < env_path.stat().st_size:
                prev = env_path.with_suffix(env_path.suffix + ".prev")
                prev.write_bytes(env_path.read_bytes())
                log.error(
                    "%s shrank %.1fx (%d -> %d bytes); previous generation kept at %s",
                    env_path.name, env_path.stat().st_size / max(len(payload), 1),
                    env_path.stat().st_size, len(payload), prev.name,
                )
        except OSError:
            pass
    _atomic_write_bytes(env_path, payload)
    try:
        _stamp(env_path, payload)
    except OSError:
        pass
    path.unlink(missing_ok=True)
    for sidecar in (path.with_suffix(path.suffix + "-wal"), path.with_suffix(path.suffix + "-shm")):
        sidecar.unlink(missing_ok=True)
