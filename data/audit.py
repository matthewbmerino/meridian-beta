"""Append-only audit log for the decision layer.

Every mutation of the decision journal and the thesis store appends a line
here: who did it, what operation, which object, and the content hash before
and after. The journal is append-only and the status field is the only mutable
value, so the real question an auditor asks is not "what does the record say"
but "did the record change, and who changed it". This store answers that.

Storage: encrypted JSON at ``data/audit.json`` (same at-rest discipline as
every other store — data/crypto.py). Bounded to MAX_AUDIT entries; the oldest
are dropped, which is acceptable for an operational log (the durable record is
the decision journal itself, which is archived, never dropped).
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path

from data.crypto import load_json_store, write_json

log = logging.getLogger(__name__)

_AUDIT_PATH = Path(__file__).resolve().parent / "audit.json"

MAX_AUDIT = 5000

# Actors that may write to the decision layer. ``operator`` typed it in the
# dashboard; ``hermes`` is the operator's agent; ``scheduler`` is an automated
# scan; ``pipeline`` is the Desk Loop commit path.
VALID_ACTORS = ("operator", "hermes", "scheduler", "pipeline", "system")


def content_hash(obj) -> str | None:
    """Stable short hash of a record, for before/after comparison.

    Returns None for a missing record so "did not exist" is distinguishable
    from "exists and hashes to X".
    """
    if obj is None:
        return None
    try:
        blob = json.dumps(obj, sort_keys=True, default=str).encode("utf-8")
    except Exception:
        return None
    return hashlib.sha256(blob).hexdigest()[:16]


def _read() -> list[dict]:
    data = load_json_store(_AUDIT_PATH, "audit", [])
    return data if isinstance(data, list) else []


def _write(entries: list[dict]) -> None:
    try:
        write_json(_AUDIT_PATH, "audit", entries)
    except OSError as e:
        # The audit log must never be the reason a legitimate write fails —
        # but a silent audit gap is itself a finding, so say so at ERROR.
        log.error("Audit write failed: %s", e)


def record(
    op: str,
    *,
    decision_id: str | None = None,
    symbol: str | None = None,
    actor: str = "system",
    before=None,
    after=None,
    detail: str | None = None,
) -> dict:
    """Append one audit entry. Never raises, never blocks the caller."""
    if actor not in VALID_ACTORS:
        actor = "system"
    entry = {
        "ts": time.time(),
        "op": op,
        "actor": actor,
        "decision_id": decision_id,
        "symbol": symbol,
        "before_hash": content_hash(before),
        "after_hash": content_hash(after),
        "detail": detail,
    }
    try:
        entries = _read()
        entries.insert(0, entry)
        _write(entries[:MAX_AUDIT])
    except Exception as e:
        log.error("Audit append failed: %s", e)
    return entry


def list_audit(limit: int = 100, decision_id: str | None = None) -> list[dict]:
    entries = _read()
    if decision_id:
        entries = [e for e in entries if e.get("decision_id") == decision_id]
    return entries[:limit]


def last_entry() -> dict | None:
    entries = _read()
    return entries[0] if entries else None


def count() -> int:
    return len(_read())
