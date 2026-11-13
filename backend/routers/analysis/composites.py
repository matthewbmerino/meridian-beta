"""Composite + position-intelligence endpoints."""

from fastapi import Depends

from backend.auth import require_auth
from backend.routers.analysis._common import log, router


@router.get("/composites", dependencies=[Depends(require_auth)])
def composites():
    """All forecasting composites (weather, regime, breadth, CaR).

    Serves the scheduler-cached result when fresh (15min TTL, session-keyed)
    so the dashboard and the alert scan show the same values; recomputes on a
    cold cache.
    """
    from backend.services.composites import get_composites
    return get_composites()


@router.get("/position-intel/{symbol}")
def position_intel(symbol: str):
    from analysis.position_intel import position_intelligence
    from analysis.risk import daily_returns
    from analysis.signals import historical_volatility
    from data.market import get_fundamentals, get_price_history

    sym = symbol.upper()
    df = get_price_history(sym)
    if df.empty:
        return {"error": f"No price data for {sym}"}

    close = df["close"]
    sym_returns = daily_returns(df)

    spy_df = get_price_history("SPY")
    bench_returns = daily_returns(spy_df) if not spy_df.empty else None

    hv30 = None
    hv_series = None
    if len(close) >= 31:
        hv_series_full = historical_volatility(close, 30).dropna()
        if not hv_series_full.empty:
            hv30 = float(hv_series_full.iloc[-1]) * 100
            hv_series = hv_series_full.tail(252) * 100 if len(hv_series_full) > 0 else None

    avg_vol_10d = float(df["volume"].tail(10).mean()) if "volume" in df else 0
    avg_vol_3m = float(df["volume"].tail(63).mean()) if "volume" in df else 0
    mcap = None
    try:
        fund = get_fundamentals(sym)
        instruments = fund.get("instruments", [])
        if instruments:
            mcap = instruments[0].get("fundamental", {}).get("marketCap")
    except Exception as e:
        log.warning("Market-cap lookup failed for %s: %s", sym, e)

    result = position_intelligence(
        close_series=close,
        symbol_returns=sym_returns,
        benchmark_returns=bench_returns,
        hv_30=hv30,
        hv_series_252d=hv_series,
        avg_volume_10d=avg_vol_10d,
        avg_volume_3m=avg_vol_3m,
        market_cap=mcap,
    )

    return result
