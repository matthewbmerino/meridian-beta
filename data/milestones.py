"""Earned-milestone ledger — the Desk Loop's honest progress record.

GLOBAL store (single operator), encrypted at rest (data/crypto.py), mirroring
the pipeline/theses/outcomes precedent. A milestone is earned once and never
un-earned (append-only): the ledger records *when* the operator first did a
thing, so the surface can show real progress rather than a re-derivable state
that flickers. ``earn`` is idempotent — a milestone already present is a no-op.

Milestone definitions live in ``analysis/engagement.MILESTONES`` (pure
conditions); this module only persists what that engine reports as due.
"""

from __future__ import annotations

import time
from pathlib import Path

from data.crypto import load_json_store, write_json

_MILESTONES_PATH = Path(__file__).resolve().parent / "milestones.json"

MAX_MILESTONES = 200


def _read() -> dict[str, dict]:
    data = load_json_store(_MILESTONES_PATH, "milestones", {})
    return data if isinstance(data, dict) else {}


def _write(data: dict[str, dict]) -> None:
    try:
        write_json(_MILESTONES_PATH, "milestones", data)
    except OSError:
        pass


def get_earned() -> list[dict]:
    """Earned milestones, newest-first."""
    data = _read()
    items = list(data.values())
    items.sort(key=lambda m: m.get("earned_at", 0), reverse=True)
    return items


def earn(due: list[dict]) -> list[dict]:
    """Record any due milestone not already earned. Returns the newly earned.

    ``due`` is the list from ``analysis.engagement.milestones_due`` — each a
    ``{id, label, description}``. Idempotent: ids already in the ledger are
    skipped, so calling this on every status read never duplicates.
    """
    data = _read()
    now = time.time()
    newly: list[dict] = []
    for m in due or []:
        mid = m.get("id")
        if not mid or mid in data:
            continue
        record = {
            "id": mid,
            "label": m.get("label", mid),
            "description": m.get("description", ""),
            "earned_at": now,
        }
        data[mid] = record
        newly.append(record)
    if newly:
        _prune(data)
        _write(data)
    return newly


def has_earned(milestone_id: str) -> bool:
    """Whether a milestone has been earned."""
    return milestone_id in _read()


def _prune(data: dict[str, dict]) -> None:
    """Bound the ledger, dropping the oldest earned milestones."""
    if len(data) <= MAX_MILESTONES:
        return
    ordered = sorted(data.items(), key=lambda kv: kv[1].get("earned_at", 0))
    for mid, _ in ordered[: len(data) - MAX_MILESTONES]:
        data.pop(mid, None)
