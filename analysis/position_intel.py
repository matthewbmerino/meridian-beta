"""Position intelligence: per-position analytics beyond basic P&L.

Layer 2 of the metrics framework. Pure functions — no data/ or backend/ imports.
"""

import numpy as np
import pandas as pd


def ma_distances(close_series: pd.Series) -> dict:
    """Distance from current price to key SMAs as percentages.

    Positive = above SMA, negative = below.
    """
    if close_series.empty:
        return {}

    current = float(close_series.iloc[-1])
    result: dict[str, float | None] = {}
    for window in (20, 50, 100, 200):
        if len(close_series) >= window:
            ma = float(close_series.rolling(window).mean().iloc[-1])
            if ma > 0 and not np.isnan(ma):
                result[f"sma_{window}_dist_pct"] = round((current - ma) / ma * 100, 2)
            else:
                result[f"sma_{window}_dist_pct"] = None
        else:
            result[f"sma_{window}_dist_pct"] = None
    return result


def relative_strength(symbol_returns: pd.Series, benchmark_returns: pd.Series,
                      window: int = 63) -> float | None:
    """Rolling relative strength ratio vs benchmark over `window` trading days.

    RS > 1 = outperforming, RS < 1 = underperforming.
    Uses cumulative return ratio over the window.
    """
    aligned = pd.concat([symbol_returns, benchmark_returns], axis=1, join="inner").dropna()
    if len(aligned) < window:
        return None

    aligned.columns = ["sym", "bench"]
    sym_cum = (1 + aligned["sym"].tail(window)).prod()
    bench_cum = (1 + aligned["bench"].tail(window)).prod()

    if bench_cum == 0:
        return None
    return round(float(sym_cum / bench_cum), 4)


def vol_regime(hv_30: float | None, hv_series_252d: pd.Series | None = None) -> str:
    """Classify current volatility regime based on percentile rank.

    If a 252-day HV series is provided, uses percentile rank.
    Otherwise falls back to absolute thresholds.
    """
    if hv_30 is None:
        return "Unknown"

    if hv_series_252d is not None and len(hv_series_252d) >= 60:
        pctile = float((hv_series_252d < hv_30).mean() * 100)
        if pctile >= 90:
            return "Extreme"
        if pctile >= 75:
            return "High"
        if pctile >= 25:
            return "Normal"
        return "Low"

    if hv_30 >= 60:
        return "Extreme"
    if hv_30 >= 35:
        return "High"
    if hv_30 >= 12:
        return "Normal"
    return "Low"


def liquidity_score(avg_volume_10d: float, avg_volume_3m: float,
                    market_cap: float | None = None) -> float:
    """0-100 composite liquidity metric.

    Components:
    - Volume consistency: 10d avg / 3m avg (40% weight)
    - Absolute volume tier (40% weight)
    - Market cap tier (20% weight, if available)
    """
    score = 0.0

    # Volume consistency — ratio near 1.0 is healthy
    if avg_volume_3m > 0:
        consistency = min(avg_volume_10d / avg_volume_3m, 2.0)
        score += min(consistency / 1.2, 1.0) * 40
    else:
        score += 20

    # Absolute volume tier
    if avg_volume_10d >= 5_000_000:
        score += 40
    elif avg_volume_10d >= 1_000_000:
        score += 32
    elif avg_volume_10d >= 500_000:
        score += 24
    elif avg_volume_10d >= 100_000:
        score += 16
    elif avg_volume_10d >= 10_000:
        score += 8
    else:
        score += 2

    # Market cap tier
    if market_cap is not None:
        if market_cap >= 200e9:
            score += 20
        elif market_cap >= 10e9:
            score += 16
        elif market_cap >= 2e9:
            score += 12
        elif market_cap >= 300e6:
            score += 6
        else:
            score += 2
    else:
        score += 10

    return round(min(100.0, max(0.0, score)), 1)


def position_intelligence(close_series: pd.Series,
                          symbol_returns: pd.Series,
                          benchmark_returns: pd.Series | None = None,
                          hv_30: float | None = None,
                          hv_series_252d: pd.Series | None = None,
                          avg_volume_10d: float = 0,
                          avg_volume_3m: float = 0,
                          market_cap: float | None = None) -> dict:
    """Aggregate all position intelligence into a single dict."""
    result = {
        "ma_distances": ma_distances(close_series),
        "vol_regime": vol_regime(hv_30, hv_series_252d),
        "liquidity": liquidity_score(avg_volume_10d, avg_volume_3m, market_cap),
    }

    if benchmark_returns is not None:
        result["relative_strength"] = relative_strength(
            symbol_returns, benchmark_returns
        )
    else:
        result["relative_strength"] = None

    return result
