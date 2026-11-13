"""Alternatives — illiquid real-asset positions priced outside brokerage rails.

Sportsbook stakes, graded cards, watches, property, vehicles: positions whose
price discovery lives outside a brokerage statement. Marked episodic by outside
evidence (an aggregator's recorded sale, a market index, a signed contract),
never by an automated pricing job. NOT in ``net_liquidation`` — NAV stays
securities + cash + external balances; this is a parallel record until the
operator says otherwise.

``kind`` is a LABEL, not a valuation method: ``watch``/``card``/``property``/
``vehicle``/``other`` all share one shape (basis + append-only marks) and one
discipline (every mark carries its source and date). A house is not a
collectible, but it is exactly the same accounting problem — an illiquid asset
whose only honest price is the last piece of outside evidence.

Storage: encrypted JSON at ``data/alternatives.json`` (single-operator,
private), same crypto pattern as ``data/theses.py`` / ``data/external_cash.py``
— loads through ``data/crypto.py`` so the store is encrypted at rest.

Shapes:
  item      {id, kind, description, detail, cost, acquired_at, source_note,
             psa_cert, refs, created_at, updated_at}
  valuation {id, item_id, value, as_of, source, note, created_at}

Marks are APPEND-ONLY: a valuation never overwrites an earlier one — the
cost->marks history IS the record. Every mark carries a source string; a
mark without one must never be accepted silently.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from pathlib import Path

from data.crypto import load_json_store, write_json

_MAX_TEXT = 400          # description/detail bounds
_MAX_SOURCE = 200
_MAX_REF = 500

KINDS: tuple[str, ...] = ("watch", "card", "property", "vehicle", "pc_part", "other")

# A mark older than this is stale — surfaces grey it (degrade gracefully).
STALE_DAYS = 90


def _file() -> Path:
    from data.session import data_root
    return data_root() / "alternatives.json"


def _read() -> dict:
    data = load_json_store(_file(), "alternatives", {"items": [], "valuations": []})
    return data if isinstance(data, dict) else {"items": [], "valuations": []}


def _write(data: dict) -> None:
    try:
        write_json(_file(), "alternatives", data)
    except OSError:
        pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean_text(v, label: str, max_len: int = _MAX_TEXT, required: bool = False) -> str:
    s = " ".join(str(v).split()) if v is not None else ""
    if required and not s:
        raise ValueError(f"{label} must not be empty")
    if len(s) > max_len:
        raise ValueError(f"{label} must be ≤ {max_len} characters")
    return s


def _clean_money(v, label: str, allow_none: bool = True) -> float | None:
    """Validate a dollar figure: positive finite, or None when allowed."""
    if v is None or v == "":
        if allow_none:
            return None
        raise ValueError(f"{label} must be a positive number")
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise ValueError(f"{label} must be a positive number")
    if not (f > 0) or f != f or f == float("inf"):
        raise ValueError(f"{label} must be a positive finite number")
    return round(f, 2)


def _clean_date(v, label: str, allow_none: bool = True) -> str | None:
    if v is None or v == "":
        if allow_none:
            return None
        raise ValueError(f"{label} must be a YYYY-MM-DD date")
    s = str(v).strip()
    try:
        return date.fromisoformat(s).isoformat()
    except ValueError:
        raise ValueError(f"{label} must be a YYYY-MM-DD date")


def _clean_kind(kind) -> str:
    k = str(kind or "other").strip().lower()
    if k not in KINDS:
        raise ValueError(f"kind must be one of: {', '.join(KINDS)}")
    return k


def _clean_refs(refs) -> list[str]:
    if refs is None:
        return []
    if not isinstance(refs, (list, tuple)):
        raise ValueError("refs must be a list of URLs")
    out = []
    for r in refs:
        s = _clean_text(r, "ref", max_len=_MAX_REF)
        if s:
            out.append(s)
    return out[:10]


# ---------------------------------------------------------------------------
# Items
# ---------------------------------------------------------------------------


def list_items() -> list[dict]:
    """All items, newest first."""
    return list(reversed(_read().get("items", [])))


def get_item(item_id: str) -> dict | None:
    for item in _read().get("items", []):
        if item.get("id") == item_id:
            return dict(item)
    return None


def add_item(
    kind: str,
    description: str,
    detail: str | None = None,
    cost: float | None = None,
    acquired_at: str | None = None,
    source_note: str | None = None,
    psa_cert: str | None = None,
    refs: list[str] | None = None,
) -> dict:
    """Record an alternative asset. Raises ValueError on any invalid field —
    a wrong number must never be accepted silently."""
    item = {
        "id": uuid.uuid4().hex[:12],
        "kind": _clean_kind(kind),
        "description": _clean_text(description, "description", required=True),
        "detail": _clean_text(detail, "detail"),
        "cost": _clean_money(cost, "cost"),
        "acquired_at": _clean_date(acquired_at, "acquired_at"),
        "source_note": _clean_text(source_note, "source_note", max_len=_MAX_SOURCE),
        "psa_cert": _clean_text(psa_cert, "psa_cert", max_len=20),
        "refs": _clean_refs(refs),
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
    }
    data = _read()
    data.setdefault("items", []).append(item)
    _write(data)
    return item


def update_item(item_id: str, **fields) -> dict | None:
    """Update editable fields in place. Preserves created_at; updated_at
    re-stamps. Raises ValueError on invalid input — never partial garbage."""
    item = get_item(item_id)
    if item is None:
        return None
    if "kind" in fields:
        item["kind"] = _clean_kind(fields["kind"])
    if "description" in fields:
        item["description"] = _clean_text(fields["description"], "description", required=True)
    if "detail" in fields:
        item["detail"] = _clean_text(fields["detail"], "detail")
    if "cost" in fields:
        item["cost"] = _clean_money(fields["cost"], "cost")
    if "acquired_at" in fields:
        item["acquired_at"] = _clean_date(fields["acquired_at"], "acquired_at")
    if "source_note" in fields:
        item["source_note"] = _clean_text(fields["source_note"], "source_note", max_len=_MAX_SOURCE)
    if "psa_cert" in fields:
        item["psa_cert"] = _clean_text(fields["psa_cert"], "psa_cert", max_len=20)
    if "refs" in fields:
        item["refs"] = _clean_refs(fields["refs"])
    item["updated_at"] = _now_iso()
    data = _read()
    data["items"] = [item if i.get("id") == item_id else i for i in data.get("items", [])]
    _write(data)
    return item


def remove_item(item_id: str) -> bool:
    """Remove an item AND its valuation history (the marks are meaningless
    without their item). Returns True when removed; idempotent."""
    data = _read()
    items = data.get("items", [])
    remaining = [i for i in items if i.get("id") != item_id]
    if len(remaining) == len(items):
        return False
    data["items"] = remaining
    data["valuations"] = [v for v in data.get("valuations", []) if v.get("item_id") != item_id]
    _write(data)
    return True


# ---------------------------------------------------------------------------
# Valuations (append-only)
# ---------------------------------------------------------------------------


def add_valuation(item_id: str, value: float, as_of: str, source: str, note: str | None = None) -> dict:
    """Record a mark against an item — append-only, never overwrites.

    ``source`` is required: a mark without provenance is a fabricated number
    and must never be accepted silently. Raises ValueError on a bad value,
    date, source, or unknown item.
    """
    if get_item(item_id) is None:
        raise ValueError(f"unknown item: {item_id}")
    valuation = {
        "id": uuid.uuid4().hex[:12],
        "item_id": item_id,
        "value": _clean_money(value, "value", allow_none=False),
        "as_of": _clean_date(as_of, "as_of", allow_none=False),
        "source": _clean_text(source, "source", max_len=_MAX_SOURCE, required=True),
        "note": _clean_text(note, "note"),
        "created_at": _now_iso(),
    }
    data = _read()
    data.setdefault("valuations", []).append(valuation)
    _write(data)
    return valuation


def list_valuations(item_id: str) -> list[dict]:
    """All marks for an item, newest first."""
    return [
        v for v in reversed(_read().get("valuations", []))
        if v.get("item_id") == item_id
    ]


def latest_valuations() -> dict[str, dict]:
    """item_id -> newest mark. The rollup + page read this."""
    out: dict[str, dict] = {}
    for v in _read().get("valuations", []):
        iid = v.get("item_id")
        cur = out.get(iid)
        # created_at is ISO-8601: string max is a correct newest-wins.
        if cur is None or str(v.get("created_at", "")) > str(cur.get("created_at", "")):
            out[iid] = v
    return out


# ---------------------------------------------------------------------------
# Rollup
# ---------------------------------------------------------------------------


def rollup() -> dict:
    """Portfolio view of the alternatives book: cost vs latest marks, with
    staleness surfaced (never hidden). Not net_liquidation — an index here is
    explicitly a separate ledger.

    ``unrealized`` is computed ONLY over items that have BOTH a basis and a
    mark, and ``unrealized_items`` says how many those are. Subtracting a
    total that includes a mark for an item with no recorded cost (a house
    bought before the book existed, say) reports the entire mark as gain —
    a fabricated number wearing the label "unrealized". Items missing one
    side are counted in ``unmarked_count`` / ``costless_count`` instead.

    ``groups`` is the SAME pass aggregated by ``kind`` (the asset class), so
    the page that consolidates "like items" renders the server's arithmetic
    rather than re-summing a copy in the client. Each group carries its own
    comparable-only ``unrealized`` and its ``share`` of the marked book.
    """
    latest = latest_valuations()
    items = list_items()
    total_cost = 0.0
    total_mark = 0.0
    stale_marks = 0
    marked_count = 0
    costless_count = 0
    comparable = 0
    comparable_cost = 0.0
    comparable_mark = 0.0
    groups: dict[str, dict] = {}
    today = date.today()

    def group_for(kind: str) -> dict:
        return groups.setdefault(
            kind,
            {
                "kind": kind,
                "items": 0,
                "marked": 0,
                "unmarked": 0,
                "cost": 0.0,
                "mark": 0.0,
                "stale_marks": 0,
                "_cmp_cost": 0.0,
                "_cmp_mark": 0.0,
                "_cmp": 0,
            },
        )

    for item in items:
        kind = str(item.get("kind") or "other")
        group = group_for(kind)
        group["items"] += 1
        cost = item.get("cost")
        cost_value = float(cost) if isinstance(cost, (int, float)) and cost > 0 else None
        if cost_value is not None:
            total_cost += cost_value
            group["cost"] += cost_value
        mark = latest.get(item["id"])
        if mark is None:
            group["unmarked"] += 1
            continue
        marked_count += 1
        group["marked"] += 1
        value = float(mark["value"])
        total_mark += value
        group["mark"] += value
        if cost_value is not None:
            comparable += 1
            comparable_cost += cost_value
            comparable_mark += value
            group["_cmp"] += 1
            group["_cmp_cost"] += cost_value
            group["_cmp_mark"] += value
        else:
            costless_count += 1
        try:
            as_of = date.fromisoformat(str(mark["as_of"]))
        except (TypeError, ValueError):
            stale_marks += 1
            group["stale_marks"] += 1
            continue
        if (today - as_of).days > STALE_DAYS:
            stale_marks += 1
            group["stale_marks"] += 1

    out_groups: list[dict] = []
    for group in groups.values():
        cmp_n = int(group.pop("_cmp"))
        cmp_cost = float(group.pop("_cmp_cost"))
        cmp_mark = float(group.pop("_cmp_mark"))
        group["unrealized"] = round(cmp_mark - cmp_cost, 2) if cmp_n else None
        group["unrealized_items"] = cmp_n
        group["cost"] = round(group["cost"], 2)
        group["mark"] = round(group["mark"], 2)
        group["share"] = round(group["mark"] / total_mark, 4) if total_mark > 0 else 0.0
        out_groups.append(group)
    out_groups.sort(key=lambda g: (-g["mark"], g["kind"]))

    return {
        "items": len(items),
        "marked_count": marked_count,
        "unmarked_count": len(items) - marked_count,
        "costless_count": costless_count,
        "total_cost": round(total_cost, 2),
        "total_mark": round(total_mark, 2),
        "unrealized": round(comparable_mark - comparable_cost, 2) if comparable else None,
        "unrealized_items": comparable,
        "stale_marks": stale_marks,
        "stale_after_days": STALE_DAYS,
        "groups": out_groups,
    }
