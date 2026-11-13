"""Theses + decision journal endpoints.

PRIVATE — theses and decisions are GLOBAL (the operator's reasoning is their
own). Gated by require_auth (fail-closed).
"""

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from backend.auth import require_auth
from data.cache import ttl_cache
from data.decisions import VALID_STATUSES, InvalidSource

router = APIRouter(prefix="/api", tags=["theses"], dependencies=[Depends(require_auth)])


class ThesisBody(BaseModel):
    thesis: str = ""
    conviction: int = 3
    invalidation_triggers: list[str] = []
    target_price: float | None = None
    compute_exposed: bool = False
    compute_index_gate: bool = False
    # Keep accepting the legacy dual-purpose flag for backward compatibility.
    compute_linked: bool | None = None
    theme: str | None = None


class LegBody(BaseModel):
    shares: float
    limit: float | None = None
    condition: str | None = None
    status: str | None = None


class DecisionBody(BaseModel):
    type: str = "manual"
    symbol: str
    action: str
    constraint: str | None = None
    rationale: str | None = None
    book: str = "alpha"
    direction: str | None = None
    size: float | None = None
    conviction: int | None = None
    horizon: str | None = None
    source: str = "ui"
    actor: str = "operator"
    agent_model: str | None = None
    legs: list[LegBody] | None = None


class DecisionStatusPatch(BaseModel):
    status: str
    reason: str | None = None
    source: str = "ui"


class SupersedeBody(BaseModel):
    superseded_by: str
    reason: str | None = None


@router.get("/theses")
def list_theses():
    from data.theses import get_theses
    return {"theses": get_theses()}


@router.get("/themes/canonical")
def canonical_themes():
    """Canonical theme names + in-use counts — ThesisPanel's datalist source."""
    from data.theme_registry import canonical_theme_set
    from data.theses import get_theses

    in_use: dict[str, int] = {}
    for record in get_theses().values():
        t = record.get("theme")
        if t:
            in_use[t] = in_use.get(t, 0) + 1
    canonical = canonical_theme_set()
    # In-use names not yet in the registry pass through too (free-text is legal).
    for t in in_use:
        if t not in canonical:
            canonical.append(t)
    return {"themes": sorted(canonical), "in_use": in_use}


@router.get("/theses/reviews")
def list_thesis_reviews():
    """One batched review pass for the whole thesis set.

    Replaces the frontend's per-symbol Promise.all fan-out (N parallel calls
    on dashboard load), which nginx rate-limits into 503s -> client-side
    "Application error". Static path MUST stay registered above
    /theses/{symbol}/... or FastAPI captures it as a path param.
    """
    from backend.services.theses import evaluate_thesis_review
    from data.theses import get_theses
    theses = get_theses()
    if not theses:
        return {"reviews": {}}

    # Sequential it was 110s cold for a full book: each review does its own
    # price/earnings/target lookups. Four workers keeps the per-symbol work
    # inside the feeds' courtesy limits while cutting the cold pass ~4x; the
    # result is cached per symbol afterwards, so this only ever pays once.
    def _one(sym: str) -> tuple[str, dict]:
        try:
            return sym, evaluate_thesis_review(sym, theses[sym])
        except Exception:  # one bad symbol must not kill the batch
            return sym, {"available": False, "error": f"review failed for {sym}"}

    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=4) as pool:
        reviews = dict(pool.map(_one, sorted(theses)))
    return {"reviews": reviews}


@router.get("/theses/{symbol}")
def get_thesis(symbol: str):
    from data.theses import get_thesis
    thesis = get_thesis(symbol)
    if thesis is None:
        return {"thesis": None}
    return {"thesis": thesis}


@router.put("/theses/{symbol}")
async def upsert_thesis(symbol: str, body: ThesisBody, request: Request):
    from data.theses import upsert_thesis
    payload = body.model_dump()
    raw = await request.json() or {}
    # Translate legacy dual-purpose flag into the split fields only when a
    # client sends ``compute_linked`` without sending ``compute_exposed``.
    if raw.get("compute_linked") is not None and "compute_exposed" not in raw:
        payload["compute_exposed"] = raw["compute_linked"]
    record = upsert_thesis(symbol, payload)
    return {"thesis": record}


@router.delete("/theses/{symbol}")
def delete_thesis(symbol: str):
    from data.theses import delete_thesis
    return {"deleted": delete_thesis(symbol)}


@router.get("/theses/{symbol}/review")
def review_thesis(symbol: str):
    from backend.services.theses import evaluate_thesis_review
    from data.theses import get_thesis
    thesis = get_thesis(symbol)
    if thesis is None:
        return {"available": False, "error": f"No thesis for {symbol}"}
    return evaluate_thesis_review(symbol, thesis)


@router.post("/theses/{symbol}/review")
def mark_thesis_reviewed(symbol: str):
    from data.theses import mark_reviewed
    record = mark_reviewed(symbol)
    if record is None:
        return {"available": False, "error": f"No thesis for {symbol}"}
    return {"thesis": record}


# ---------------------------------------------------------------------------
# Decision journal
#
# Static paths (/decisions/stats, /decisions/timeline, /decisions/export,
# /decisions/integrity) MUST stay registered above /decisions/{decision_id}/...
# or FastAPI captures them as path params.
# ---------------------------------------------------------------------------

@router.get("/decisions/stats")
def decisions_stats(group_by: str = Query(default="type")):
    """Aggregate counts over the journal: n, live, closed, notional, median size."""
    return _cached_decisions_stats(group_by)


def _decisions_stats_compute(group_by: str = Query(default="type")):
    from data.decisions import stats
    try:
        return stats(group_by=group_by)
    except Exception as e:
        return {"available": False, "error": str(e), "group_by": group_by}


# TTL-cached: journal rollups over an append-only store.
_cached_decisions_stats = ttl_cache(ttl_seconds=120)(_decisions_stats_compute)


@router.get("/decisions/timeline")
def decisions_timeline(bucket: str = Query(default="week")):
    """Decision counts and stage transitions per time bucket, oldest first."""
    from data.decisions import timeline
    return {"bucket": bucket, "buckets": timeline(bucket=bucket)}


@router.get("/decisions/export", response_class=PlainTextResponse)
def decisions_export(
    format: str = Query(default="md"),
    symbol: str | None = None,
    include_closed: bool = True,
    limit: int = Query(default=500, le=2000),
):
    """Portable export of the journal — markdown or JSON.

    The record is the operator's; they own it unconditionally and can take it
    out in one call.
    """
    from data.decisions import list_decisions
    from data.export import decisions_to_json, decisions_to_markdown

    entries = list_decisions(
        limit=limit, symbol=symbol, exclude_closed=not include_closed, order="asc", sort="ts"
    )
    if format == "json":
        return decisions_to_json(entries)
    return decisions_to_markdown(entries)


@router.get("/decisions/integrity")
def decisions_integrity():
    """Whether the decision layer's own record is intact.

    Reports store readability and version, orphaned outcome records (an outcome
    whose decision is not in the journal), unarmed decisions, unresolvable
    forecast cells, and the last audit entry. ``healthy`` is False when anything
    is wrong — the platform says so about itself rather than looking fine.
    """
    return _cached_decisions_integrity()


def _decisions_integrity_compute():
    from data import audit
    from data.decisions import decision_count, list_decisions, store_health
    from data.outcomes import list_outcomes
    from data.outcomes import store_health as outcomes_health

    issues: list[dict] = []
    try:
        journal = store_health()
        entries = list_decisions(limit=5000)
    except Exception as e:
        return {
            "healthy": False,
            "journal": {"readable": False, "error": str(e)},
            "issues": [{"kind": "store_unreadable", "detail": str(e)}],
        }
    outcomes_health_info = outcomes_health()
    outcomes = list_outcomes(limit=10000)
    journal_ids = {e.get("id") for e in entries}

    orphans = [o for o in outcomes if o.get("decision_id") not in journal_ids]
    for o in orphans:
        resolved = [
            h for h in ("1d", "5d", "20d", "60d")
            if isinstance((o.get("horizons") or {}).get(h), dict)
        ]
        issues.append({
            "kind": "orphaned_outcome",
            "outcome_id": o.get("id"),
            "decision_id": o.get("decision_id"),
            "symbol": o.get("symbol"),
            "action": o.get("action"),
            "decision_ts": o.get("decision_ts"),
            "resolved_horizons": resolved,
            "detail": "outcome record has no journal entry — its forecast is not visible in any decision view",
        })

    outcome_ids = {o.get("decision_id") for o in outcomes}
    unarmed = [e for e in entries if e.get("id") not in outcome_ids]
    for e in unarmed:
        issues.append({
            "kind": "unarmed_decision",
            "decision_id": e.get("id"),
            "symbol": e.get("symbol"),
            "detail": "journal entry has no outcome record — it can never be scored",
        })

    no_data = []
    for o in outcomes:
        for h in ("1d", "5d", "20d", "60d"):
            cell = (o.get("horizons") or {}).get(h)
            if isinstance(cell, dict) and cell.get("status") == "no_data":
                no_data.append({
                    "outcome_id": o.get("id"),
                    "symbol": o.get("symbol"),
                    "horizon": h,
                    "reason": cell.get("reason"),
                })
    for nd in no_data:
        issues.append({"kind": "no_data_cell", "detail": "forecast window closed but unresolvable", **nd})

    if not journal.get("readable", True):
        issues.append({"kind": "store_unreadable", "detail": journal.get("error")})
    if not outcomes_health_info.get("readable", True):
        issues.append({"kind": "store_unreadable", "detail": outcomes_health_info.get("error")})

    return {
        "healthy": len(issues) == 0,
        "journal": {**journal, "count": decision_count()},
        "outcomes": outcomes_health_info,
        "issues": issues,
        "counts": {
            "orphaned_outcomes": len(orphans),
            "unarmed_decisions": len(unarmed),
            "no_data_cells": len(no_data),
        },
        "last_audit": audit.last_entry(),
        "audit_entries": audit.count(),
    }


# TTL-cached: journal integrity scan.
_cached_decisions_integrity = ttl_cache(ttl_seconds=120)(_decisions_integrity_compute)


@router.patch("/decisions/{decision_id}/status")
def patch_decision_status(decision_id: str, body: DecisionStatusPatch):
    from data.decisions import set_status

    if body.status not in VALID_STATUSES:
        raise HTTPException(status_code=422, detail=f"Invalid status: {body.status}")
    try:
        updated = set_status(
            decision_id, body.status, reason=body.reason, source=body.source
        )
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Decision not found: {decision_id}")
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return {"decision": updated}


@router.post("/decisions/{decision_id}/supersede")
def supersede_decision(decision_id: str, body: SupersedeBody):
    """Replace a decision with a later one, without deleting either.

    The correction mechanism the journal previously lacked: the original stays
    in the record (append-only) but is marked superseded and excluded from the
    scorecard, so a re-logged decision never double-counts its forecast.
    """
    from data.decisions import supersede

    try:
        updated = supersede(decision_id, body.superseded_by, reason=body.reason)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return {"decision": updated}


def _attach_forecasts(entries: list[dict]) -> list[dict]:
    """Join each decision to its own forecast record.

    The decisions store holds no outcome data, so a journal row can only show
    its forecast if the two stores are joined here. Without it the surface has
    nothing to display and the record stops being a learning loop.
    """
    try:
        from data.outcomes import outcomes_by_decision
        by_decision = outcomes_by_decision()
    except Exception:
        return entries
    for e in entries:
        horizons = by_decision.get(str(e.get("id") or ""))
        if horizons:
            e["horizons"] = horizons
    return entries


@router.get("/decisions")
def list_decisions(
    limit: int = Query(default=100, le=500),
    symbol: str | None = None,
    status: str | None = None,
    exclude_closed: bool = False,
    symbol_history: bool = False,
    q: str | None = None,
    type: str | None = None,
    source: str | None = None,
    symbol_prefix: str | None = None,
    from_ts: float | None = None,
    to_ts: float | None = None,
    sort: str = "ts",
    order: str = "desc",
    cursor: int = Query(default=0, ge=0),
):
    from data.decisions import list_decisions

    if status is not None and status not in VALID_STATUSES:
        raise HTTPException(status_code=422, detail=f"Invalid status: {status}")

    entries = list_decisions(
        limit=limit, symbol=symbol, status=status, exclude_closed=exclude_closed,
        q=q, type=type, source=source, symbol_prefix=symbol_prefix,
        from_ts=from_ts, to_ts=to_ts, sort=sort, order=order, cursor=cursor,
    )
    entries = _attach_forecasts(entries)

    if symbol_history and symbol:
        # Chronological oldest-first, plus current status and counts.
        chronological = list(reversed(entries))
        from data.decisions import _status_for
        counts = {s: 0 for s in VALID_STATUSES}
        for e in chronological:
            s = _status_for(e)
            counts[s] = counts.get(s, 0) + 1
        current = _status_for(entries[0]) if entries else "open"
        return {
            "symbol": symbol.upper(),
            "entries": chronological,
            "current_status": current,
            "counts_by_status": counts,
        }

    total = len(entries) + int(cursor)
    return {
        "decisions": entries,
        "count": len(entries),
        "cursor": cursor,
        "next_cursor": (int(cursor) + len(entries)) if len(entries) == limit else None,
        "has_more": len(entries) == limit,
        "total": total,
    }


@router.post("/decisions")
def log_decision(body: DecisionBody):
    from data.decisions import log_decision
    try:
        entry = log_decision(
            type=body.type,
            symbol=body.symbol,
            action=body.action,
            constraint=body.constraint,
            rationale=body.rationale,
            book=body.book,
            direction=body.direction,
            size=body.size,
            conviction=body.conviction,
            horizon=body.horizon,
            source=body.source,
            actor=body.actor,
            agent_model=body.agent_model,
            legs=[leg.model_dump() for leg in body.legs] if body.legs else None,
        )
    except InvalidSource as e:
        # Provenance is exact or it is nothing: never silently relabel a writer.
        raise HTTPException(status_code=422, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return {"decision": entry}
