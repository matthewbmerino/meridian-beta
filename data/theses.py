"""Position theses — the operator's recorded reasoning for each holding.

GLOBAL store (single operator): the operator's reasoning is their own.
Mirrors the snapshots/GPU precedent — a fixed path under ``data/``.

A thesis is the decision layer's core record: *why* a position is held, at
what conviction, and what would invalidate it. Without it Meridian is a
monitor; with it, a decision system.

Storage: JSON at ``data/theses.json``, keyed by symbol. Atomic tmp-rename
writes (same pattern as ``data/watchlist.py`` / ``data/prefs.py``).
"""

from __future__ import annotations

import time
from pathlib import Path

from data.crypto import load_json_store, write_json

_THESES_PATH = Path(__file__).resolve().parent / "theses.json"

# A thesis older than this many days without review is "stale".
REVIEW_INTERVAL_DAYS = 7

# Conviction scale: 1 (speculative) .. 5 (highest). Insider-selling is only
# checked as an invalidation trigger at or above this conviction.
INSIDER_CHECK_CONVICTION = 4

# Compute-linked theses flag when the Compute Index drops this much from its
# recent peak (a structural repricing of the theme's core input).
COMPUTE_DROP_PCT = 10.0

# Two independent fields replace the dual-purpose compute_linked footgun:
#   compute_exposed  -> feeds the informational Compute Exposure panel
#   compute_index_gate -> the retired Compute Index review trigger (backlog #10)
_FIELDS = (
    "thesis",
    "conviction",
    "invalidation_triggers",
    "target_price",
    "compute_exposed",
    "compute_index_gate",
    "theme",
)


def _normalize_theme(v) -> str | None:
    """Trim + collapse a theme string, resolve aliases to canonical, None for
    empty. The theme registry is the theses store — a position's theme is a
    field on its recorded thesis. Canonical resolution lives in
    ``data/theme_registry.py`` (kept separate so group-time resolution can
    import it without the theses store's crypto deps)."""
    if v is None:
        return None
    from data.theme_registry import canonical_theme
    s = canonical_theme(v)
    return s or None


def _read() -> dict[str, dict]:
    data = load_json_store(_THESES_PATH, "theses", {})
    return data if isinstance(data, dict) else {}


def _write(data: dict[str, dict]) -> None:
    try:
        write_json(_THESES_PATH, "theses", data)
    except OSError:
        pass


def _migrate_compute_flags(record: dict) -> dict:
    """Split the dual-purpose ``compute_linked`` flag into two unambiguous fields.

    ``compute_exposed`` carries the descriptive meaning (is this name linked to
    compute?) and ``compute_index_gate`` is always forced to ``False`` because
    the operator retired the Compute Index as a capital gate on 2026-09-14
    (backlog #10). A legacy record with ``compute_linked`` alone still resolves
    correctly via ``_resolve_compute_linked``.
    """
    legacy = record.get("compute_linked")
    if "compute_exposed" not in record and legacy is not None:
        record["compute_exposed"] = bool(legacy)
    if "compute_index_gate" not in record:
        # Gate was retired; never re-arm on migration.
        record["compute_index_gate"] = False
    return record


def _resolve_compute_linked(record: dict | None) -> bool:
    """Backward-compatible read for legacy ``compute_linked``.

    Prefer the split fields; fall back to the legacy flag only when present.
    This lets clients and code paths not yet updated still see the right value.
    """
    if record is None:
        return False
    if "compute_exposed" in record or "compute_index_gate" in record:
        return bool(record.get("compute_exposed") or record.get("compute_index_gate"))
    return bool(record.get("compute_linked", False))


def _now() -> float:
    return time.time()


def get_theses() -> dict[str, dict]:
    """Return all theses keyed by symbol."""
    return {sym: _migrate_compute_flags(dict(record)) for sym, record in _read().items()}


def get_thesis(symbol: str) -> dict | None:
    """Return one thesis, or None."""
    record = _read().get(symbol.upper())
    if record is None:
        return None
    return _migrate_compute_flags(dict(record))


def upsert_thesis(symbol: str, thesis: dict) -> dict:
    """Create or update a thesis for *symbol*.

    Only known fields are persisted (allowlist — a client can't inject
    arbitrary keys). ``updated_at`` is stamped on every write; ``created_at``
    and ``last_reviewed_at`` are preserved on update.
    """
    sym = symbol.upper()
    data = _read()
    existing = data.get(sym, {})
    now = _now()

    record = {
        "symbol": sym,
        "thesis": str(thesis.get("thesis", "")).strip(),
        "conviction": int(thesis.get("conviction", 3)),
        "invalidation_triggers": [
            str(t).strip() for t in thesis.get("invalidation_triggers", []) if str(t).strip()
        ],
        "target_price": _coerce_float(thesis.get("target_price")),
        "compute_exposed": bool(thesis.get("compute_exposed", thesis.get("compute_linked", False))),
        "compute_index_gate": False,
        "theme": _normalize_theme(thesis.get("theme")),
        "created_at": existing.get("created_at", now),
        "updated_at": now,
        "last_reviewed_at": existing.get("last_reviewed_at"),
    }
    # Clamp conviction to the 1..5 scale.
    record["conviction"] = max(1, min(5, record["conviction"]))

    data[sym] = record
    _write(data)
    return record


def delete_thesis(symbol: str) -> bool:
    """Remove a thesis. Returns True if one was removed."""
    sym = symbol.upper()
    data = _read()
    if sym not in data:
        return False
    del data[sym]
    _write(data)
    return True


def mark_reviewed(symbol: str) -> dict | None:
    """Stamp ``last_reviewed_at`` on a thesis. Returns the updated record."""
    sym = symbol.upper()
    data = _read()
    if sym not in data:
        return None
    data[sym]["last_reviewed_at"] = _now()
    _write(data)
    return data[sym]


def _coerce_float(v) -> float | None:
    if v is None or v == "":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None  # reject NaN
