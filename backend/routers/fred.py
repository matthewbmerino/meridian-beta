"""FRED macroeconomic data endpoints."""

from fastapi import APIRouter, Depends, HTTPException

from backend.auth import require_auth

router = APIRouter(prefix="/api/fred", tags=["fred"])


@router.get("/environment")
def macro_environment():
    from data.fred import market_environment
    env = market_environment()
    result = {}
    for key, val in env.items():
        if key == "term_structure":
            result[key] = val
            continue
        clean = {k: v for k, v in val.items() if k != "series"}
        if "trend" in clean:
            clean["trend"] = clean["trend"][-60:]
        result[key] = clean
    return result


@router.get("/yield-curve")
def yield_curve():
    from data.fred import get_term_structure
    term = get_term_structure()
    return [{"tenor": t[0], "current": t[1], "six_months_ago": t[2]} for t in term]


@router.get("/series/{series_id}", dependencies=[Depends(require_auth)])
def fred_series(series_id: str, days: int = 365):
    """Raw time series for a catalogued FRED series (M2, CPI, VIX, ...).

    Mirrors the /api/gpu/trends contract: an ``available`` boolean degraded
    state plus a ``[{date, value}]`` series. Unknown series IDs 404.
    """
    from data.fred import FRED_SERIES, get_series
    meta = FRED_SERIES.get(series_id.upper())
    if meta is None:
        raise HTTPException(status_code=404, detail=f"Unknown FRED series: {series_id}")
    s = get_series(series_id.upper(), days=days)
    if s.empty:
        return {"available": False, "series": [], "unit": meta["unit"], "label": meta["label"]}
    points = [
        {"date": idx.strftime("%Y-%m-%d"), "value": float(val)}
        for idx, val in s.items()
    ]
    return {"available": True, "series": points, "unit": meta["unit"], "label": meta["label"]}
