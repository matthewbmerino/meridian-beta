"""Portfolio and position snapshot history endpoints.

PRIVATE — reads the GLOBAL snapshot store (operator's real portfolio history).
Gated by require_auth (fail-closed).
"""

import datetime

from fastapi import APIRouter, Depends, HTTPException, Query

from backend.auth import require_auth
from data.cache import ttl_cache

router = APIRouter(
    prefix="/api/snapshots",
    tags=["snapshots"],
    dependencies=[Depends(require_auth)],
)


def _parse_date(s: str | None) -> datetime.datetime | None:
    if not s:
        return None
    try:
        return datetime.datetime.fromisoformat(s)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid date: {s!r}")


@router.get("/portfolio")
def portfolio_history(
    start: str | None = Query(None),
    end: str | None = Query(None),
):
    return _cached_portfolio_history(start, end)


def _records(df) -> list[dict]:
    """DataFrame rows as JSON-safe dicts.

    A NULL in a float column comes back from pandas as NaN, and Starlette's
    JSON encoder raises on NaN — so one snapshot with an unknown cash balance
    turned the whole history endpoint into a 500. Missing means missing.
    """
    out = df.to_dict(orient="records")
    for row in out:
        for key, value in row.items():
            if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
                row[key] = None
    return out


def _portfolio_history_compute(
    start: str | None = Query(None),
    end: str | None = Query(None),
):
    from data.snapshots import load_portfolio_history
    df = load_portfolio_history(start=_parse_date(start), end=_parse_date(end))
    if df.empty:
        return []
    df["ts"] = df["ts"].dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")
    return _records(df)


# TTL-cached: the portfolio snapshot history re-serialized per request.
_cached_portfolio_history = ttl_cache(ttl_seconds=300)(_portfolio_history_compute)




@router.get("/position/{symbol}")
def position_history(
    symbol: str,
    start: str | None = Query(None),
    end: str | None = Query(None),
):
    from data.snapshots import load_position_history
    df = load_position_history(
        symbol=symbol.upper(),
        start=_parse_date(start),
        end=_parse_date(end),
    )
    if df.empty:
        return []
    df["ts"] = df["ts"].dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")
    return _records(df)


@router.get("/count")
def snapshot_count():
    from data.snapshots import snapshot_count
    return {"count": snapshot_count()}
