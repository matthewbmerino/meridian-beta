"""Performance at the daily close — the market's P&L with transfers excluded.

PRIVATE — reads the operator's real snapshot history. ``require_auth``,
fail-closed, like every other portfolio surface.

Why this endpoint exists: ``/api/snapshots/portfolio`` returns a BALANCE
series, and a deposit is a rise in that balance exactly like a winning day. A
$620,000 house-proceeds transfer therefore read as the best day on record.
This endpoint returns the same closes *decomposed* — ``pnl`` is what holding
the book earned, ``flow`` is money that arrived or left, and ``index`` is
built from ``pnl`` alone. ``analysis/performance.py`` holds the arithmetic and
the reasoning; this module only reads the store and shapes the response.
"""

from fastapi import APIRouter, Depends, Query

from backend.auth import require_auth
from data.cache import ttl_cache

router = APIRouter(
    prefix="/api/performance",
    tags=["performance"],
    dependencies=[Depends(require_auth)],
)


@router.get("/daily")
def daily_performance(days: int | None = Query(None, ge=1, le=3650)):
    """Daily closes with market P&L, transfers, and the return index.

    ``days`` windows the series (calendar days back from the last close) and
    rebases the index to 100 at the window's first close; omit it for the
    whole history.
    """
    return _cached_daily_performance(days)


def _daily_performance(days: int | None) -> dict:
    from analysis.performance import build_days, slice_range, summarize
    from data.snapshots import load_portfolio_history, load_position_history

    closes = load_portfolio_history()
    positions = load_position_history()
    window = slice_range(build_days(closes, positions), days)
    return {
        "tz": "America/New_York",
        "days": window,
        "metrics": summarize(window),
    }


# The store only changes when a snapshot lands (every 5 minutes at most) and
# a rebuild walks the whole position history — cache it.
_cached_daily_performance = ttl_cache(ttl_seconds=300)(_daily_performance)
