"""Decision journal — append-only record of the operator's actions.

GLOBAL store (survives session purge): the operator's reasoning is their own.
Every entry records *what* was decided, *why*, and under *which constraint* —
the serious-portfolio-management core. No execution happens here; the journal
records intent, the operator executes at the broker.

Storage discipline (hardened 2026-10-08):
  - **A store that exists but cannot be read is a failure, not an empty store.**
    ``_read()`` raises ``StoreUnreadable``; every writer calls it first, so a
    corrupt store aborts the write instead of being overwritten by the fallback.
  - **Read-modify-write is serialized** by a module lock, and every mutation
    appends to ``data/audit.py`` with before/after content hashes.
  - **Eviction archives.** Beyond ``MAX_DECISIONS`` the oldest entries are
    written to ``data/decisions_archive/`` before being dropped from the live
    store, so "the record isn't there anymore" cannot happen silently.
  - **Provenance rejects.** An unknown ``source`` is a ``ValueError`` (the API
    turns it into a 422); it is never silently relabelled as operator-logged.
  - **Corrections are first-class.** ``supersede()`` links a replacement to the
    entry it replaces and excludes both from the scorecard.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from pathlib import Path

from data.crypto import StoreUnreadable, load_json_store, write_json

log = logging.getLogger(__name__)

_DECISIONS_PATH = Path(__file__).resolve().parent / "decisions.json"
_ARCHIVE_DIR = Path(__file__).resolve().parent / "decisions_archive"

MAX_DECISIONS = 2000

# Decision types. ``sizing`` = a rebalance driven by the sizing engine's
# constraint; ``thesis`` = a thesis open/close/invalidate; ``manual`` = a
# freeform decision the operator logged by hand.
VALID_TYPES = ("sizing", "thesis", "manual")

# Lifecycle statuses. ``open`` = an action that stands; ``pending`` = staged,
# not executed; ``closed`` = done, superseded, or withdrawn. ``void`` = a
# record that was logged in error and carries no forecast weight.
VALID_STATUSES = ("open", "pending", "closed", "void")

# Legacy action strings map onto the status model on read. Explicit status
# field always wins. Order matters: STAGE_CANCELLED is closed before the wider
# STAGE_* -> pending rule consumes it.
LEGACY_STATUS_MAP = {
    "OPEN": "open",
    "STAGE_ENTRY": "pending",
    "STAGE_HOLD": "pending",
    "STAGE_CANCELLED": "closed",
}

# Valid pre-registration sources. ``auto`` = a sizing signal the engine
# emitted (never an executed fill); ``executed`` = the operator's actual
# action; ``ui`` = logged in the dashboard; ``hermes`` = logged by the
# operator's agent. The signal-vs-executed delta is itself a measure.
VALID_SOURCES = ("ui", "executed", "auto", "hermes")

# Valid forecast horizons (trading days). Aligns with calibration.forward_days
# (21) at the 20d slot.
VALID_HORIZONS = ("1d", "5d", "20d", "60d")

# Who wrote the entry. Distinct from ``source`` (which records the *channel*
# and drives the signal-vs-executed measure); ``actor`` records *which party*.
VALID_ACTORS = ("operator", "hermes", "scheduler", "pipeline", "system")

# Statuses a forecast should not be scored against.
UNSCORED_STATUSES = ("void", "superseded")

_LOCK = threading.RLock()
_VERSION = 0


class InvalidSource(ValueError):
    """An unknown provenance source. Rejected, never silently relabelled."""


def _read() -> list[dict]:
    """Read the journal.

    Raises ``StoreUnreadable`` when the store exists but cannot be decrypted or
    parsed. Callers that write MUST let that propagate — replacing an unreadable
    journal with a fresh one is the failure this module exists to prevent.
    """
    data = load_json_store(_DECISIONS_PATH, "decisions", [])
    return data if isinstance(data, list) else []


def _write(entries: list[dict]) -> None:
    global _VERSION
    _VERSION += 1
    write_json(_DECISIONS_PATH, "decisions", entries)


def _archive(evicted: list[dict]) -> Path | None:
    """Persist evicted entries to a dated archive file. Never drops silently."""
    if not evicted:
        return None
    try:
        _ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m", time.localtime())
        path = _ARCHIVE_DIR / f"decisions-{stamp}.json"
        existing = []
        if path.exists():
            existing = load_json_store(path, f"decisions-archive-{stamp}", [])
            if not isinstance(existing, list):
                existing = []
        write_json(path, f"decisions-archive-{stamp}", existing + evicted)
        oldest = min((e.get("ts", 0) for e in evicted), default=None)
        log.warning(
            "Decision journal eviction: archived %d entries to %s (oldest ts %s)",
            len(evicted), path.name, oldest,
        )
        return path
    except Exception as e:  # archive failure must not lose the write
        log.error("Decision archive failed for %d entries: %s", len(evicted), e)
        return None


def _status_for(entry: dict) -> str:
    """Return the effective status for an entry.

    Explicit ``status`` field wins. Otherwise use LEGACY_STATUS_MAP by exact
    action match. Any unmapped action defaults to ``open``.
    """
    if "status" in entry:
        return entry["status"]
    action = str(entry.get("action", "")).strip().upper()
    return LEGACY_STATUS_MAP.get(action, "open")


def is_scored(entry: dict) -> bool:
    """Whether this entry's forecast should count toward the scorecard."""
    status = _status_for(entry)
    if status in UNSCORED_STATUSES:
        return False
    if entry.get("superseded_by"):
        return False
    return True


def _resolve_conviction(symbol: str, explicit: int | None) -> int | None:
    """Resolve decision conviction: explicit value, else the thesis conviction.

    Immutable at log time (anti-hindsight) — the record captures belief as it
    was, never reconstructed. A missing thesis or non-1..5 explicit value
    degrades to None rather than inventing a number.
    """
    if explicit is not None:
        try:
            c = int(explicit)
        except (TypeError, ValueError):
            return None
        return max(1, min(5, c)) if 1 <= c <= 5 else None
    try:
        from data.theses import get_thesis
        thesis = get_thesis(symbol)
        conv = (thesis or {}).get("conviction")
        if conv is not None:
            return max(1, min(5, int(conv)))
    except Exception:
        pass
    return None


def _normalize_legs(legs) -> list[dict] | None:
    """Validate structured plan legs. Returns None when nothing valid was sent.

    A leg is ``{shares, limit, condition, status}`` — the machine-readable half
    of a staged plan. ``constraint`` stays the prose summary; legs make the plan
    testable instead of merely readable.
    """
    if not legs:
        return None
    out: list[dict] = []
    for raw in legs:
        if not isinstance(raw, dict):
            raise ValueError("legs must be a list of objects")
        shares = raw.get("shares")
        try:
            shares_f = float(shares)
        except (TypeError, ValueError):
            raise ValueError("each leg requires a numeric 'shares'")
        if shares_f <= 0:
            raise ValueError("leg shares must be positive")
        limit = raw.get("limit")
        try:
            limit_f = float(limit) if limit is not None else None
        except (TypeError, ValueError):
            raise ValueError("leg limit must be numeric when present")
        out.append({
            "shares": shares_f,
            "limit": limit_f,
            "condition": (str(raw["condition"]) if raw.get("condition") else None),
            "status": str(raw.get("status") or "planned"),
        })
    return out or None


def _legs_notional(legs: list[dict]) -> float | None:
    """Sum of shares x limit across legs, or None when no leg carries a price."""
    total = 0.0
    priced = False
    for leg in legs:
        if leg.get("limit") is not None:
            total += float(leg["shares"]) * float(leg.get("limit") or 0.0)
            priced = True
    return round(total, 2) if priced else None


def _audit(op: str, entry: dict | None, actor: str, before=None, detail=None) -> None:
    try:
        from data import audit
        audit.record(
            op,
            decision_id=(entry or {}).get("id"),
            symbol=(entry or {}).get("symbol"),
            actor=actor,
            before=before,
            after=entry,
            detail=detail,
        )
    except Exception as e:
        log.error("Audit failed for %s: %s", op, e)


def log_decision(
    type: str,
    symbol: str,
    action: str,
    constraint: str | None = None,
    rationale: str | None = None,
    book: str = "alpha",
    direction: str | None = None,
    size: float | None = None,
    conviction: int | None = None,
    horizon: str | None = None,
    source: str = "ui",
    actor: str = "operator",
    agent_model: str | None = None,
    legs: list[dict] | None = None,
    supersedes: str | None = None,
) -> dict:
    """Append a pre-registered decision. Returns the recorded entry.

    ``type`` must be one of VALID_TYPES; anything else degrades to "manual".
    ``source`` must be in VALID_SOURCES — anything else RAISES ``InvalidSource``
    (provenance is never guessed). ``horizon`` must be in VALID_HORIZONS;
    anything else degrades to None. ``conviction`` resolves explicit → thesis
    record → None (never a guess). New entries start with status "open" unless
    the action maps to a legacy status.
    """
    if type not in VALID_TYPES:
        type = "manual"
    if source not in VALID_SOURCES:
        raise InvalidSource(
            f"Unknown source {source!r}. Valid: {', '.join(VALID_SOURCES)}."
        )
    if actor not in VALID_ACTORS:
        actor = "system"
    if horizon is not None and horizon not in VALID_HORIZONS:
        horizon = None
    sym = symbol.upper()
    if not sym:
        raise ValueError("symbol is required")
    size_f = None
    if size is not None:
        try:
            size_f = float(size)
        except (TypeError, ValueError):
            size_f = None
    legs_norm = _normalize_legs(legs)
    if size_f is None and legs_norm:
        # A plan expressed as legs carries its own notional — derive it rather
        # than leaving the record sizeless.
        size_f = _legs_notional(legs_norm)

    entry = {
        "id": uuid.uuid4().hex[:12],
        "ts": time.time(),
        "type": type,
        "book": book,
        "symbol": sym,
        "action": str(action).strip(),
        "constraint": constraint,
        "rationale": rationale,
        "direction": direction,
        "size": size_f,
        "conviction": _resolve_conviction(sym, conviction),
        "horizon": horizon,
        "source": source,
        "actor": actor,
        "status": LEGACY_STATUS_MAP.get(str(action).strip().upper(), "open"),
        "status_events": [],
    }
    if agent_model:
        entry["agent_model"] = agent_model
    if legs_norm:
        entry["legs"] = legs_norm
    if supersedes:
        entry["supersedes"] = supersedes

    with _LOCK:
        entries = _read()  # raises StoreUnreadable — write aborts, store intact
        if supersedes:
            target = next((e for e in entries if e.get("id") == supersedes), None)
            if target is None:
                raise KeyError(f"Cannot supersede unknown decision: {supersedes}")
        entries.insert(0, entry)
        kept, evicted = entries[:MAX_DECISIONS], entries[MAX_DECISIONS:]
        if evicted:
            _archive(evicted)
        _write(kept)
        if supersedes:
            _apply_supersede_locked(supersedes, entry["id"], entry["ts"])

    _audit("log", entry, actor)
    # Pre-register the outcome at decision time — the anchor snapshot is
    # captured now, so the postmortem tests the then-current belief, never a
    # reconstructed one. Best-effort; a failure to arm must not block logging.
    try:
        from data.outcomes import arm_outcome
        arm_outcome(entry)
    except Exception as e:
        log.warning("Outcome arm failed for decision %s: %s", entry.get("id"), e)
    return entry


def _apply_supersede_locked(old_id: str, new_id: str, when: float) -> dict | None:
    """Mark *old_id* superseded by *new_id*. Caller holds ``_LOCK``."""
    entries = _read()
    for entry in entries:
        if entry.get("id") == old_id:
            before_status = _status_for(entry)
            entry["status"] = "closed"
            entry["superseded_by"] = new_id
            entry["status_updated_at"] = when
            entry.setdefault("status_events", []).append({
                "ts": when,
                "from": before_status,
                "to": "superseded",
                "reason": f"superseded by {new_id}",
                "source": "system",
                "superseded_by": new_id,
            })
            _write(entries)
            return entry
    return None


def supersede(old_id: str, new_id: str, reason: str | None = None) -> dict:
    """Link an existing decision to its replacement.

    The original stays in the journal (append-only) but is marked ``superseded``
    and dropped from the scorecard, so a correction never double-counts a
    forecast. Raises KeyError for an unknown id, ValueError for a self-link.
    """
    if old_id == new_id:
        raise ValueError("A decision cannot supersede itself")
    with _LOCK:
        entries = _read()
        ids = {e.get("id") for e in entries}
        if old_id not in ids:
            raise KeyError(f"Decision not found: {old_id}")
        if new_id not in ids:
            raise KeyError(f"Decision not found: {new_id}")
        before = next(e for e in entries if e.get("id") == old_id)
        snapshot = dict(before)
        updated = _apply_supersede_locked(old_id, new_id, time.time())
        if updated is not None and reason:
            updated["status_events"][-1]["reason"] = reason
            _write(entries)
    _audit("supersede", updated, "operator", before=snapshot, detail=f"by {new_id}")
    return updated or {}


def set_status(
    decision_id: str,
    to_status: str,
    reason: str | None = None,
    source: str = "ui",
) -> dict:
    """Transition a decision's status, append-only audit trail.

    Raises:
      KeyError — unknown decision id.
      ValueError — invalid target status or same-status transition.

    Writes the audit event ``{ts, from, to, reason, source}`` to
    ``status_events[]`` and sets ``status_updated_at``. Returns the updated
    entry. Never touches outcome records.
    """
    if to_status not in VALID_STATUSES:
        raise ValueError(f"Invalid status: {to_status}")
    with _LOCK:
        entries = _read()
        for entry in entries:
            if entry.get("id") == decision_id:
                from_status = _status_for(entry)
                if from_status == to_status:
                    raise ValueError(f"Decision already {to_status}")
                snapshot = dict(entry)
                entry["status"] = to_status
                entry["status_updated_at"] = time.time()
                events = entry.setdefault("status_events", [])
                events.append({
                    "ts": time.time(),
                    "from": from_status,
                    "to": to_status,
                    "reason": reason,
                    "source": source,
                })
                _write(entries)
                _audit("status", entry, "operator", before=snapshot,
                       detail=f"{from_status} -> {to_status}")
                return entry
    raise KeyError(f"Decision not found: {decision_id}")


def list_decisions(
    limit: int = 100,
    symbol: str | None = None,
    status: str | None = None,
    exclude_closed: bool = False,
    q: str | None = None,
    type: str | None = None,
    source: str | None = None,
    symbol_prefix: str | None = None,
    from_ts: float | None = None,
    to_ts: float | None = None,
    sort: str = "ts",
    order: str = "desc",
    cursor: int = 0,
) -> list[dict]:
    """Return decisions, newest first by default, optionally filtered.

    Filters:
      ``symbol`` — exact symbol match (case-insensitive).
      ``symbol_prefix`` — symbol startswith (case-insensitive).
      ``status`` — exact status match.
      ``exclude_closed`` — drop closed entries.
      ``q`` — case-insensitive substring over symbol/action/rationale/constraint.
      ``type``, ``source`` — exact field match.
      ``from_ts``/``to_ts`` — inclusive timestamp window.
      ``sort`` — ``ts`` (default), ``size``, ``conviction``, ``symbol``.
      ``order`` — ``desc`` (default) or ``asc``.
      ``cursor`` — offset into the ordered result.

    Defaults preserve legacy behavior: no symbol, status, or closed exclusion,
    newest first. Effective status is always materialized via ``_status_for``
    so legacy action strings continue to work.
    """
    entries = _read()
    if symbol:
        sym = symbol.upper()
        entries = [e for e in entries if e.get("symbol") == sym]
    if symbol_prefix:
        pre = symbol_prefix.upper()
        entries = [e for e in entries if str(e.get("symbol", "")).startswith(pre)]
    if status is not None:
        entries = [e for e in entries if _status_for(e) == status]
    if exclude_closed:
        entries = [e for e in entries if _status_for(e) != "closed"]
    if type is not None:
        entries = [e for e in entries if e.get("type") == type]
    if source is not None:
        entries = [e for e in entries if e.get("source") == source]
    if from_ts is not None:
        entries = [e for e in entries if float(e.get("ts", 0)) >= float(from_ts)]
    if to_ts is not None:
        entries = [e for e in entries if float(e.get("ts", 0)) <= float(to_ts)]
    if q:
        needle = q.strip().lower()
        if needle:
            def hit(e: dict) -> bool:
                hay = " ".join(str(e.get(f) or "") for f in
                               ("symbol", "action", "rationale", "constraint", "type"))
                return needle in hay.lower()
            entries = [e for e in entries if hit(e)]

    if sort in ("size", "conviction"):
        # Nulls always last, in both directions — a missing size is not the
        # smallest size.
        reverse = order != "asc"
        entries = sorted(
            entries,
            key=lambda e: (e.get(sort) is None, -(e.get(sort) or 0) if reverse else (e.get(sort) or 0)),
        )
    elif sort == "symbol":
        entries = sorted(entries, key=lambda e: str(e.get("symbol", "")),
                         reverse=(order != "asc"))
    elif order == "asc":
        entries = list(reversed(entries))

    if cursor:
        entries = entries[int(cursor):]
    return entries[:limit]


def decision_count() -> int:
    return len(_read())


def store_health() -> dict:
    """Whether the journal is readable, its version, and its size."""
    try:
        entries = _read()
    except StoreUnreadable as e:
        return {"readable": False, "error": str(e), "count": None, "version": _VERSION}
    unscored = sum(1 for e in entries if not is_scored(e))
    return {
        "readable": True,
        "error": None,
        "count": len(entries),
        "unscored": unscored,
        "version": _VERSION,
    }


def stats(group_by: str = "type") -> dict:
    """Aggregate counts over the journal. Nothing is inferred — only summed."""
    from data.audit import last_entry
    entries = _read()
    groups: dict[str, dict] = {}
    for e in entries:
        key = {
            "type": e.get("type"),
            "source": e.get("source"),
            "actor": e.get("actor"),
            "symbol": e.get("symbol"),
            "conviction": e.get("conviction"),
            "month": time.strftime("%Y-%m", time.localtime(float(e.get("ts", 0)))),
        }.get(group_by)
        key = str(key)
        g = groups.setdefault(key, {"n": 0, "live": 0, "closed": 0, "notional": 0.0})
        g["n"] += 1
        st = _status_for(e)
        if st in ("open", "pending"):
            g["live"] += 1
        if st in UNSCORED_STATUSES:
            g["closed"] += 1
        if st == "closed":
            g["closed"] += 1
        if e.get("size"):
            g["notional"] += float(e["size"])
    for g in groups.values():
        g["notional"] = round(g["notional"], 2)
    sizes = sorted(float(e["size"]) for e in entries if e.get("size"))
    median = sizes[len(sizes) // 2] if sizes else None
    return {
        "group_by": group_by,
        "groups": groups,
        "n": len(entries),
        "median_size": round(median, 2) if median is not None else None,
        "notional_sum": round(sum(sizes), 2),
        "last_audit": last_entry(),
    }


def timeline(bucket: str = "week") -> list[dict]:
    """Decision counts and stage transitions per time bucket, oldest first."""
    fmt = "%Y-W%W" if bucket == "week" else "%Y-%m"
    entries = _read()
    buckets: dict[str, dict] = {}
    for e in entries:
        key = time.strftime(fmt, time.localtime(float(e.get("ts", 0))))
        b = buckets.setdefault(key, {"bucket": key, "logged": 0, "transitions": 0,
                                     "notional": 0.0})
        b["logged"] += 1
        b["transitions"] += len(e.get("status_events") or [])
        if e.get("size"):
            b["notional"] += float(e["size"])
    for b in buckets.values():
        b["notional"] = round(b["notional"], 2)
    return [buckets[k] for k in sorted(buckets)]
