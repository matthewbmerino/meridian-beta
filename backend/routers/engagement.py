"""Desk status — discipline, streak, rank, milestones.

PRIVATE — reads the operator's theses, decisions, pipeline, and outcomes, so
it is GLOBAL-store data and gated by require_auth (fail-closed, matches the
private-data-gate audit check). Computation is pure (analysis/engagement.py);
this module only assembles inputs and persists newly-earned milestones.
"""

import logging

from fastapi import APIRouter, Depends, Query

from backend.auth import require_auth

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["engagement"], dependencies=[Depends(require_auth)])


def _prefs() -> dict:
    try:
        from data.prefs import get_prefs
        return get_prefs()
    except Exception:
        return {}


def _min_obs() -> int:
    return int(_prefs().get("calibration_operator_min_obs") or 30)


def _discipline_weights() -> dict:
    p = _prefs()
    return {
        "review_freshness": p.get("discipline_weight_review", 0.40),
        "forecast_precision": p.get("discipline_weight_forecast", 0.35),
        "research_discipline": p.get("discipline_weight_research", 0.25),
    }


def _assemble_status(earn: bool):
    """Build the desk-status payload from the operator's stores.

    ``earn`` persists newly-due milestones; the read-only variant leaves the
    ledger untouched so a page view never mutates state.
    """
    from analysis.engagement import desk_status
    from analysis.outcomes import scorecard as build_scorecard
    from data.decisions import list_decisions
    from data.outcomes import list_outcomes
    from data.pipeline import get_pipeline
    from data.theses import get_theses

    # Lifecycle change 2026-10-07: engagement surface stops showing closed
    # decisions as open desk items; closed decisions are still in the journal.
    decisions = list_decisions(limit=1000, exclude_closed=True)
    theses = list(get_theses().values())
    pipeline = list(get_pipeline().values())
    outcomes = list_outcomes()
    sc = build_scorecard(outcomes, min_obs=_min_obs())

    status = desk_status(
        scorecard=sc,
        decisions=decisions,
        theses=theses,
        pipeline=pipeline,
        outcomes=outcomes,
        min_obs=_min_obs(),
        weights=_discipline_weights(),
    )
    if earn:
        from data.milestones import earn as earn_milestones
        newly = earn_milestones(status["milestones_due"])
        if newly:
            status["newly_earned"] = newly
    return status


@router.get("/engagement/status")
def engagement_status():
    """Desk status: discipline components, streak, rank, milestones-due.

    Earning milestones here (rather than only on a write) keeps the ledger
    current even when progress came from elsewhere (a scheduler resolve).
    """
    return _assemble_status(earn=True)


@router.get("/engagement/milestones")
def engagement_milestones(limit: int = Query(default=50, ge=1, le=200)):
    """Earned-milestone ledger, newest-first."""
    from data.milestones import get_earned
    earned = get_earned()
    return {"milestones": earned[:limit], "count": len(earned)}
