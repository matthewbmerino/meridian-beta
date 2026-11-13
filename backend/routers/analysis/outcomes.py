"""Desk scorecard endpoints — the Decision Book's outcome library.

PRIVATE — outcomes resolve the operator's decisions against the GLOBAL
decision/anchor store. Gated by require_auth (fail-closed, matches the
private-data-gate audit check).
"""

import logging

from fastapi import Depends, Query
from pydantic import BaseModel

from backend.auth import require_auth
from backend.routers.analysis._common import router

log = logging.getLogger(__name__)


class ResolveBody(BaseModel):
    pass


@router.post("/outcomes/resolve", dependencies=[Depends(require_auth)])
def resolve_outcomes_endpoint(body: ResolveBody | None = None):
    """Resolve every outcome whose forecast window has closed. Idempotent."""
    from data.outcomes import resolve_outcomes
    return resolve_outcomes()


@router.post("/outcomes/rescore", dependencies=[Depends(require_auth)])
def rescore_outcomes_endpoint(dry_run: bool = Query(default=True)):
    """Re-derive stored verdicts from their raw returns under the CURRENT rule.

    Win/loss is a derived layer; the returns are the record. When the scoring
    rule is corrected this is what stops the historical book from reporting the
    old rule's numbers as fact. Defaults to a dry run so the diff is reviewable.
    """
    from data.outcomes import rescore_cells
    return rescore_cells(dry_run=dry_run)


@router.get("/outcomes", dependencies=[Depends(require_auth)])
def outcomes_list(
    limit: int = Query(default=200, le=500),
    min_obs: int = Query(default=30, le=100),
):
    """Desk scorecard by horizon + the resolvable outcome detail.

    ``min_obs`` defaults to the operator's own configured floor (the same value
    the calibration endpoint and the sizing pipeline use) rather than a second,
    more permissive number — one threshold, one answer.
    """
    from analysis.outcomes import scorecard
    from data.outcomes import list_outcomes, store_health

    try:
        min_obs = _operator_min_obs(min_obs)
    except Exception:
        pass
    outcomes = list_outcomes(limit)
    health = store_health()
    return {
        "scorecard": scorecard(outcomes, min_obs=min_obs),
        "n_outcomes": len(outcomes),
        "n_resolved": health.get("resolved_cells"),
        "cells_by_horizon": health.get("cells_by_horizon"),
        "min_obs": min_obs,
    }


def _operator_min_obs(fallback: int) -> int:
    """The operator's configured minimum-N floor, when one is set."""
    try:
        from data.prefs import get_prefs
        return int(get_prefs().get("calibration_operator_min_obs") or fallback)
    except Exception:
        return fallback


@router.get("/outcomes/calibration", dependencies=[Depends(require_auth)])
def outcomes_calibration(
    horizon: str = Query(default="20d"),
    min_obs: int = Query(default=30, le=500),
):
    """Operator win-rate buckets in the calibration shape — feeds hybrid sizing.

    Returns None for the buckets when too few resolved observations exist
    (the sizing pipeline must degrade to the logistic prior, never fabricate).
    """
    from analysis.outcomes import operator_bucket_stats
    from data.outcomes import list_outcomes
    buckets = operator_bucket_stats(list_outcomes(), horizon=horizon, min_obs=min_obs)
    if buckets is None:
        return {"available": False, "buckets": None, "min_obs": min_obs}
    return {
        "available": True,
        "buckets": {
            str(lo): {
                "bucket_lo": b.bucket_lo,
                "bucket_hi": b.bucket_hi,
                "n_obs": b.n_obs,
                "win_rate": round(b.win_rate, 4),
                "payoff_ratio": round(b.payoff_ratio, 4),
            }
            for lo, b in sorted(buckets.items())
        },
        "min_obs": min_obs,
    }


@router.get("/outcomes/record", dependencies=[Depends(require_auth)])
def outcomes_record(
    limit: int = Query(default=500, le=2000),
    horizon: str = Query(default="20d"),
):
    """Black-book export: every resolved decision + anchor + outcome.

    The portable, auditable track record — what a fund would show an LP. The
    operator owns this export unconditionally.

    Reads the horizon cell through the shared accessor so BOTH stored shapes
    work. The previous nested-only read returned ``null`` for every row of the
    flat records, i.e. for every genuinely resolved forecast in the book.
    """
    from data.outcomes import horizon_cell, list_outcomes
    records = []
    for o in list_outcomes(limit=limit):
        cell = horizon_cell(o, horizon)
        records.append({
            "id": o.get("id"),
            "decision_id": o.get("decision_id"),
            "ts": o.get("decision_ts"),
            "symbol": o.get("symbol"),
            "type": o.get("type"),
            "action": o.get("action"),
            "book": o.get("book"),
            "direction": o.get("direction"),
            "size": o.get("size"),
            "conviction": o.get("conviction"),
            "constraint": o.get("constraint"),
            "source": o.get("source"),
            "actor": o.get("actor"),
            "anchor": o.get("anchor"),
            "outcome": cell,
            "all_horizons": {
                h: horizon_cell(o, h) for h in ("1d", "5d", "20d", "60d")
            },
        })
    return {"horizon": horizon, "records": records, "n": len(records)}
