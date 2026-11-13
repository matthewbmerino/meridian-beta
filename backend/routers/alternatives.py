"""Alternatives endpoints — illiquid real-asset positions + stakes roll-up.

PRIVATE — alternatives are GLOBAL (the operator's possessions are their own).
Gated by require_auth (fail-closed).

The page is a viewer over TWO stores:
  - ``data/alternatives.py`` (watch/card/property/vehicle/other + append-only
    marks) — authoritative here,
  - sportsbook stakes live in ``data/external_cash.py`` (kind=sportsbook) —
    read-only here, never a second ledger.
"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from backend.auth import require_auth
from data.cache import ttl_cache

router = APIRouter(
    prefix="/api/alternatives",
    tags=["alternatives"],
    dependencies=[Depends(require_auth)],
)


class ItemBody(BaseModel):
    kind: str = "other"
    description: str
    detail: str | None = None
    cost: float | None = None
    acquired_at: str | None = None
    source_note: str | None = None
    psa_cert: str | None = None
    refs: list[str] | None = None


class ValuationBody(BaseModel):
    value: float
    as_of: str
    source: str
    note: str | None = None


@router.get("")
def list_alternatives():
    """All items with their latest mark + delta, the stakes, and the rollup."""
    return _cached_list_alternatives()


def _list_alternatives_compute():
    from data import external_cash
    from data.alternatives import latest_valuations, list_items, rollup

    latest = latest_valuations()
    items = []
    for item in list_items():
        mark = latest.get(item["id"])
        delta = None
        if mark is not None and item.get("cost"):
            delta = round(float(mark["value"]) - float(item["cost"]), 2)
        items.append({**item, "latest_valuation": mark, "delta_vs_cost": delta})

    totals = external_cash.get_cash_totals()
    stakes = {
        "total": totals["illiquid"],
        "entries": [
            e for e in external_cash.list_external_cash()
            # normalize first: legacy rows carry no kind field and count as
            # bank (liquid) — a raw .get("kind") would file $105K of bank
            # cash under stakes.
            if external_cash.normalize_kind(e.get("kind")) not in external_cash.LIQUID_KINDS
        ],
    }
    return {"items": items, "stakes": stakes, "rollup": rollup()}


# TTL-cached: alternatives board over slowly-moving data.
_cached_list_alternatives = ttl_cache(ttl_seconds=300)(_list_alternatives_compute)


@router.post("")
def add_alternative(body: ItemBody):
    from data.alternatives import add_item
    try:
        item = add_item(
            kind=body.kind,
            description=body.description,
            detail=body.detail,
            cost=body.cost,
            acquired_at=body.acquired_at,
            source_note=body.source_note,
            psa_cert=body.psa_cert,
            refs=body.refs,
        )
    except ValueError as e:
        return {"error": str(e)}
    return {"item": item}


@router.patch("/{item_id}")
def update_alternative(item_id: str, body: ItemBody):
    from data.alternatives import update_item
    try:
        item = update_item(
            item_id,
            kind=body.kind,
            description=body.description,
            detail=body.detail,
            cost=body.cost,
            acquired_at=body.acquired_at,
            source_note=body.source_note,
            psa_cert=body.psa_cert,
            refs=body.refs,
        )
    except ValueError as e:
        return {"error": str(e)}
    if item is None:
        return {"error": "item not found"}
    return {"item": item}


@router.delete("/{item_id}")
def remove_alternative(item_id: str):
    from data.alternatives import remove_item
    return {"ok": remove_item(item_id)}


@router.post("/{item_id}/valuations")
def add_valuation(item_id: str, body: ValuationBody):
    from data.alternatives import add_valuation
    try:
        mark = add_valuation(
            item_id, body.value, body.as_of, body.source, body.note
        )
    except ValueError as e:
        return {"error": str(e)}
    return {"valuation": mark}

class ResearchBody(BaseModel):
    # When true, a tight/wide-valued (never confidence=none) result is
    # recorded immediately with the research's own source + as_of.
    # confidence=none (no recorded sale) NEVER records — the item stays at
    # basis and the response carries the illiquidity evidence.
    record: bool = False


@router.post("/{item_id}/research")
def research_alternative(item_id: str, body: ResearchBody):
    """Run the local Hermes agent to value this item and (optionally) record
    the mark with the research's own provenance. Synchronous (up to ~4min);
    the page awaits it. confidence=none (no recorded sale at grade) never
    records — an unknown exit price is a risk, not a hidden upside."""
    from data.alternatives import get_item

    item = get_item(item_id)
    if item is None:
        return {"error": "item not found"}
    from agent.valuation_research import research_value
    try:
        contract = research_value(
            item["kind"], item["description"],
            detail=item.get("detail") or None,
            cert=(item.get("psa_cert") or None) if item["kind"] == "card" else None,
        )
    except ValueError as e:
        return {"error": str(e), "recoverable": True}

    recorded = None
    value = float(contract["value"])
    if body.record and contract.get("confidence") in ("tight", "wide") and value > 0:
        from data.alternatives import add_valuation
        # Keep the FULL evidence in the response; the mark row itself gets a
        # bounded source (200 chars) with the overflow + caveat preserved in
        # note — a rejected record would otherwise discard good research.
        full_source = str(contract["source"])
        mark_source = full_source[:200]
        overflow = full_source[200:] + " :: " + str(contract.get("caveat") or "")
        note = overflow[:400]
        try:
            recorded = add_valuation(
                item_id, value, str(contract["as_of"]),
                mark_source, note=note or None,
            )
        except ValueError as e:
            return {**contract, "error": f"research ok, record failed: {e}"}

    return {
        "value": value,
        "value_low": contract.get("value_low"),
        "value_high": contract.get("value_high"),
        "as_of": contract.get("as_of"),
        "source": contract.get("source"),
        "confidence": contract.get("confidence"),
        "caveat": contract.get("caveat"),
        "session": contract.get("session"),
        "recorded": recorded,
        "item": item,
    }
