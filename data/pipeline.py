"""Idea pipeline — the operator's hunt from spotted idea to resolved outcome.

GLOBAL store (single operator): the operator's hunt is their own. Mirrors the
theses/decisions/outcomes precedent — a fixed path under ``data/``, encrypted
at rest (data/crypto.py).

A pipeline item is the spine of the Desk Loop: one record per symbol carrying
the stage it has reached, where it came from, the operator's notes, and the
links to the artifacts the hunt produced (thesis, decisions, research reports).
The stages are ordered; ``advance_stage`` records every transition so the
dossier can show how the idea developed.

Storage: JSON at ``data/pipeline.json``, keyed by symbol. Atomic tmp-rename
writes (same pattern as ``data/theses.py``).
"""

from __future__ import annotations

import time
from pathlib import Path

from data.crypto import load_json_store, write_json

_PIPELINE_PATH = Path(__file__).resolve().parent / "pipeline.json"

MAX_ITEMS = 2000

VALID_STAGES = ("spotted", "researching", "sizing", "committed", "resolved")

STAGE_ORDER = {stage: i for i, stage in enumerate(VALID_STAGES)}

VALID_ORIGINS = ("manual", "sonar", "watchlist", "compass", "research", "discovery")

_FIELDS = (
    "stage",
    "origin",
    "notes",
    "conviction",
    "target_price",
    "tags",
)


def _read() -> dict[str, dict]:
    data = load_json_store(_PIPELINE_PATH, "pipeline", {})
    return data if isinstance(data, dict) else {}


def _write(data: dict[str, dict]) -> None:
    try:
        write_json(_PIPELINE_PATH, "pipeline", data)
    except OSError:
        pass


def _now() -> float:
    return time.time()


def _normalize_stage(stage: str | None) -> str:
    if stage in VALID_STAGES:
        return stage
    return "spotted"


def _normalize_origin(origin: str | None) -> str:
    if origin in VALID_ORIGINS:
        return origin
    return "manual"


def _coerce_conviction(v) -> int | None:
    if v is None:
        return None
    try:
        c = int(v)
    except (TypeError, ValueError):
        return None
    return max(1, min(5, c))


def _coerce_target(v) -> float | None:
    if v is None or v == "":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None


def _clean_tags(tags) -> list[str]:
    if not isinstance(tags, list):
        return []
    out: list[str] = []
    for t in tags:
        s = " ".join(str(t).split())
        if s and s not in out:
            out.append(s)
    return out[:20]


def get_pipeline() -> dict[str, dict]:
    """Return every pipeline item keyed by symbol."""
    return _read()


def get_item(symbol: str) -> dict | None:
    """Return one pipeline item, or None."""
    return _read().get(symbol.upper())


def upsert_item(symbol: str, item: dict | None = None) -> dict:
    """Create or update a pipeline item. Returns the record.

    Only known fields are persisted (allowlist). ``created_at`` is preserved on
    update; ``stage`` default is ``spotted``. A stage change made through this
    upsert is *recorded* in ``stage_history`` so the dossier timeline stays
    honest (use ``advance_stage`` for intent-explicit transitions).
    """
    item = item or {}
    sym = symbol.upper()
    data = _read()
    existing = data.get(sym)
    now = _now()

    if existing is None:
        record = {
            "symbol": sym,
            "stage": _normalize_stage(item.get("stage")),
            "origin": _normalize_origin(item.get("origin")),
            "notes": str(item.get("notes", "")).strip(),
            "conviction": _coerce_conviction(item.get("conviction")),
            "target_price": _coerce_target(item.get("target_price")),
            "tags": _clean_tags(item.get("tags")),
            "thesis_ref": None,
            "decision_ids": [],
            "report_ids": [],
            "created_at": now,
            "updated_at": now,
            "stage_history": [{"from": None, "to": _normalize_stage(item.get("stage")), "ts": now}],
        }
    else:
        record = dict(existing)
        for key in ("notes", "tags"):
            if key in item:
                record[key] = str(item["notes"]).strip() if key == "notes" else _clean_tags(item["tags"])
        if "conviction" in item:
            record["conviction"] = _coerce_conviction(item["conviction"])
        if "target_price" in item:
            record["target_price"] = _coerce_target(item["target_price"])
        if "origin" in item:
            record["origin"] = _normalize_origin(item["origin"])
        if "stage" in item:
            new_stage = _normalize_stage(item["stage"])
            if new_stage != record.get("stage"):
                record.setdefault("stage_history", []).append(
                    {"from": record.get("stage"), "to": new_stage, "ts": now}
                )
                record["stage"] = new_stage
        record.setdefault("tags", [])
        record.setdefault("decision_ids", [])
        record.setdefault("report_ids", [])
        record.setdefault("thesis_ref", None)
        record["updated_at"] = now

    data[sym] = record
    _prune(data)
    _write(data)
    return record


def advance_stage(symbol: str, stage: str) -> dict | None:
    """Move an item to *stage*, recording the transition. None if not found.

    An invalid stage degrades to no-op (returns the current record). Advancing
    to the same stage is a no-op.
    """
    sym = symbol.upper()
    data = _read()
    record = data.get(sym)
    if record is None:
        return None
    new_stage = _normalize_stage(stage)
    if new_stage == record.get("stage"):
        return record
    now = _now()
    record.setdefault("stage_history", []).append(
        {"from": record.get("stage"), "to": new_stage, "ts": now}
    )
    record["stage"] = new_stage
    record["updated_at"] = now
    data[sym] = record
    _write(data)
    return record


def link_decision(symbol: str, decision_id: str) -> dict | None:
    """Attach a Decision Book id to the item (idempotent)."""
    return _link_id(symbol, "decision_ids", decision_id)


def link_report(symbol: str, report_id: str) -> dict | None:
    """Attach a saved research report id to the item (idempotent)."""
    return _link_id(symbol, "report_ids", report_id)


def link_thesis(symbol: str, thesis_ref: str | None = None) -> dict | None:
    """Mark the item as having a recorded thesis. Returns the record."""
    sym = symbol.upper()
    data = _read()
    record = data.get(sym)
    if record is None:
        return None
    record["thesis_ref"] = thesis_ref or sym
    record["updated_at"] = _now()
    data[sym] = record
    _write(data)
    return record


def _link_id(symbol: str, field: str, value: str | None) -> dict | None:
    if not value:
        return None
    sym = symbol.upper()
    data = _read()
    record = data.get(sym)
    if record is None:
        return None
    ids = list(record.get(field) or [])
    if value not in ids:
        ids.append(value)
    record[field] = ids
    record["updated_at"] = _now()
    data[sym] = record
    _write(data)
    return record


def remove_item(symbol: str) -> bool:
    """Remove an item. Returns True if one was removed."""
    sym = symbol.upper()
    data = _read()
    if sym not in data:
        return False
    del data[sym]
    _write(data)
    return True


def _prune(data: dict[str, dict]) -> None:
    """Bound the store, dropping the least-recently-updated resolved items."""
    if len(data) <= MAX_ITEMS:
        return
    ordered = sorted(
        data.items(),
        key=lambda kv: (0 if kv[1].get("stage") == "resolved" else 1, kv[1].get("updated_at", 0)),
    )
    for sym, _ in ordered[: len(data) - MAX_ITEMS]:
        data.pop(sym, None)
