"""Sector intelligence — rotation, dispersion, breadth, flow detection.

Answers: where is capital rotating? Which sectors are accelerating vs
decelerating? Is the market rewarding stock-picking or moving as a herd?

All functions are pure. Sector maps come from data/universe.py but are
passed in as arguments — no data fetching here.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def _group_by_sector(data: dict[str, pd.Series], sector_map: dict[str, str]) -> dict[str, list[str]]:
    """Group the symbols present in *data* by their sector.

    Returns {sector: [symbols]}, only for symbols that appear in both maps.
    """
    sectors: dict[str, list[str]] = {}
    for sym, sector in sector_map.items():
        if sym in data:
            sectors.setdefault(sector, []).append(sym)
    return sectors


def sector_returns(
    returns_dict: dict[str, pd.Series],
    sector_map: dict[str, str],
    weights: dict[str, float] | None = None,
) -> dict[str, pd.Series]:
    """Compute equal-weighted (or market-cap-weighted) daily return per sector.

    Returns {sector_name: pd.Series of daily returns}.
    """
    sectors = _group_by_sector(returns_dict, sector_map)

    result: dict[str, pd.Series] = {}
    for sector, syms in sectors.items():
        df = pd.DataFrame({s: returns_dict[s] for s in syms if s in returns_dict}).dropna(how="all")
        if df.empty:
            continue
        if weights:
            w = np.array([weights.get(s, 1.0) for s in df.columns])
            w = w / w.sum()
            result[sector] = df.fillna(0).dot(w)
        else:
            result[sector] = df.mean(axis=1)

    return result


def sector_rotation_signals(
    returns_dict: dict[str, pd.Series],
    sector_map: dict[str, str],
    fast: int = 5,
    slow: int = 21,
) -> list[dict]:
    """Sector rotation: classify each sector as accelerating, decelerating, leading, or lagging.

    Uses fast/slow momentum crossover.
    Returns list of {sector, fast_mom, slow_mom, signal, score}.
    """
    sec_rets = sector_returns(returns_dict, sector_map)
    results: list[dict] = []

    for sector, series in sec_rets.items():
        if len(series) < slow + 5:
            continue
        fast_ret = float((1 + series.tail(fast)).prod() - 1)
        slow_ret = float((1 + series.tail(slow)).prod() - 1)

        if fast_ret > 0 and fast_ret > slow_ret:
            signal = "ACCELERATING"
        elif fast_ret > 0 and fast_ret <= slow_ret:
            signal = "DECELERATING"
        elif fast_ret <= 0 and fast_ret > slow_ret:
            signal = "RECOVERING"
        else:
            signal = "WEAKENING"

        results.append({
            "sector": sector,
            "fast_mom": round(fast_ret * 100, 2),
            "slow_mom": round(slow_ret * 100, 2),
            "signal": signal,
            "score": round((fast_ret - slow_ret) * 10000, 1),
        })

    results.sort(key=lambda r: r["score"], reverse=True)
    return results


def sector_dispersion(
    returns_dict: dict[str, pd.Series],
    sector_map: dict[str, str],
    window: int = 21,
) -> list[dict]:
    """Cross-sectional return dispersion within each sector.

    High dispersion = stock-picking matters, sector beta less important.
    Low dispersion = sector-level trade is sufficient.

    Returns list of {sector, dispersion, dispersion_pctile, interpretation}.
    """
    sectors = _group_by_sector(returns_dict, sector_map)

    results: list[dict] = []
    for sector, syms in sectors.items():
        df = pd.DataFrame({s: returns_dict[s] for s in syms if s in returns_dict}).dropna(how="all")
        if df.empty or len(df) < window:
            continue

        recent = df.tail(window)
        cum_returns = (1 + recent).prod() - 1
        dispersion = float(cum_returns.std())

        # Historical dispersion for percentile
        rolling_disp = []
        for start in range(0, len(df) - window, 5):
            chunk = df.iloc[start : start + window]
            cr = (1 + chunk).prod() - 1
            rolling_disp.append(float(cr.std()))

        pctile = 0.0
        if rolling_disp:
            pctile = float(np.searchsorted(np.sort(rolling_disp), dispersion) / len(rolling_disp) * 100)

        if pctile > 75:
            interp = "HIGH_DISPERSION"
        elif pctile < 25:
            interp = "LOW_DISPERSION"
        else:
            interp = "NORMAL"

        results.append({
            "sector": sector,
            "count": len(syms),
            "dispersion": round(dispersion * 100, 2),
            "dispersion_pctile": round(pctile, 0),
            "interpretation": interp,
        })

    results.sort(key=lambda r: r["dispersion"], reverse=True)
    return results


def sector_breadth(
    price_dict: dict[str, pd.Series],
    sector_map: dict[str, str],
    sma_window: int = 20,
) -> list[dict]:
    """Breadth per sector: % of stocks above their N-day SMA.

    Returns list of {sector, total, above_sma, breadth_pct, signal}.
    """
    sectors = _group_by_sector(price_dict, sector_map)

    results: list[dict] = []
    for sector, syms in sectors.items():
        above = 0
        total = 0
        for sym in syms:
            prices = price_dict[sym]
            if len(prices) < sma_window + 1:
                continue
            sma = prices.rolling(sma_window).mean()
            total += 1
            if prices.iloc[-1] > sma.iloc[-1]:
                above += 1

        if total == 0:
            continue
        pct = above / total * 100
        if pct >= 70:
            signal = "BROAD_STRENGTH"
        elif pct <= 30:
            signal = "BROAD_WEAKNESS"
        else:
            signal = "MIXED"

        results.append({
            "sector": sector,
            "total": total,
            "above_sma": above,
            "breadth_pct": round(pct, 1),
            "signal": signal,
        })

    results.sort(key=lambda r: r["breadth_pct"], reverse=True)
    return results


def market_regime_breadth(
    returns_dict: dict[str, pd.Series],
    sector_map: dict[str, str],
) -> dict:
    """Overall market regime based on sector breadth and rotation patterns.

    Returns {regime, advancing_sectors, declining_sectors, rotation_score,
             breadth_pct, interpretation}.
    """
    rotation = sector_rotation_signals(returns_dict, sector_map)
    if not rotation:
        return {"regime": "UNKNOWN", "interpretation": "Insufficient data"}

    accelerating = [r for r in rotation if r["signal"] == "ACCELERATING"]
    weakening = [r for r in rotation if r["signal"] == "WEAKENING"]

    n = len(rotation)
    pct_accel = len(accelerating) / n * 100 if n > 0 else 0
    pct_weak = len(weakening) / n * 100 if n > 0 else 0
    avg_score = sum(r["score"] for r in rotation) / n if n > 0 else 0

    if pct_accel >= 60:
        regime = "RISK_ON"
        interp = f"{len(accelerating)}/{n} sectors accelerating — broad risk appetite"
    elif pct_weak >= 60:
        regime = "RISK_OFF"
        interp = f"{len(weakening)}/{n} sectors weakening — defensive positioning favored"
    elif pct_accel >= 40 and pct_weak <= 20:
        regime = "SELECTIVE_RISK"
        interp = "Mixed rotation — selective opportunities in accelerating sectors"
    else:
        regime = "TRANSITIONAL"
        interp = "Sectors diverging — regime shift in progress, watch for confirmation"

    return {
        "regime": regime,
        "advancing_sectors": len(accelerating),
        "declining_sectors": len(weakening),
        "total_sectors": n,
        "rotation_score": round(avg_score, 1),
        "breadth_pct": round(pct_accel, 0),
        "interpretation": interp,
        "top_sectors": [r["sector"] for r in rotation[:3]],
        "bottom_sectors": [r["sector"] for r in rotation[-3:]],
    }
