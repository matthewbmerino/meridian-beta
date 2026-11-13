"""Opportunity feed — deterministic fresh-setup discovery.

PRIVATE — the feed excludes the operator's holdings and pipeline, so it reads
global private stores and is gated by require_auth (fail-closed, matches the
private-data-gate audit check). Ranking itself is pure (analysis/discovery.py);
this module only assembles inputs from the sonar cache + stores.
"""

import logging

from fastapi import APIRouter, Depends, Query

from backend.auth import require_auth

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["discovery"], dependencies=[Depends(require_auth)])


def _held_symbols() -> set[str]:
    try:
        from data.account import get_combined_positions
        positions = get_combined_positions()
        if positions is None or positions.empty:
            return set()
        return {str(s).upper() for s in positions["symbol"].dropna().unique() if s}
    except Exception as e:
        log.warning("Discovery holdings load failed: %s", e)
        return set()


def _pipeline_symbols() -> set[str]:
    try:
        from data.pipeline import get_pipeline
        return {s.upper() for s in get_pipeline().keys()}
    except Exception as e:
        log.warning("Discovery pipeline load failed: %s", e)
        return set()


def _watchlist_symbols() -> set[str]:
    try:
        from data.watchlist import get_watchlist
        return {str(s).upper() for s in get_watchlist()}
    except Exception as e:
        log.warning("Discovery watchlist load failed: %s", e)
        return set()


@router.get("/discovery")
def discovery(
    universe: str = Query("S&P 500"),
    limit: int = Query(default=12, ge=1, le=50),
):
    """Rank the scanned universe into a fresh-setup opportunity feed.

    Reads the warm sonar cache (the 15-min scan), excludes held and
    pipeline names, flags watchlist names, and returns the top setups with
    transparent reasons. Never fabricates: an empty/cold cache returns an
    explicit unavailable state.
    """
    from analysis.discovery import rank_candidates
    from backend.routers.sonar import _scan_cache

    cached = _scan_cache.get(universe)
    if not cached or not cached.get("results"):
        return {
            "available": False,
            "universe": universe,
            "scanned": 0,
            "candidates": [],
            "reason": "No warm scan for this universe yet — the 15-minute scan will populate it",
        }

    held = _held_symbols()
    pipeline = _pipeline_symbols()
    exclude = held | pipeline
    tracked = _watchlist_symbols()

    candidates = rank_candidates(
        cached["results"],
        exclude=exclude,
        tracked=tracked,
        limit=limit,
    )
    return {
        "available": True,
        "universe": universe,
        "scanned": len(cached["results"]),
        "excluded_held": len(held),
        "excluded_pipeline": len(pipeline),
        "candidates": candidates,
        "timestamp": cached.get("timestamp"),
    }
