"""Shared composite computation + single-operator cache.

Both the scheduler's alert scan and the ``/api/analysis/composites`` endpoint
compute the same composite stack. This module is the single implementation,
so the alert scan and the dashboard can never diverge — a stale-but-consistent
composite is better than two divergent computations.

The scheduler populates the cache every 15min (its alert-scan cadence); the
endpoint serves the cached result when fresh and recomputes only on a cold
cache. Single operator: one global cache, no session keying.
"""

from __future__ import annotations

import logging
import time

log = logging.getLogger(__name__)

# Matches the scheduler's alert-scan cadence.
_CACHE_TTL_SECONDS = 15 * 60

# {"ts": float, "data": dict}
_cache: dict = {}


def compute_composites() -> dict:
    """Compute the full composite stack (superset: breadth_200 + cvar)."""
    import numpy as np
    import pandas as pd

    from analysis.composites import all_composites
    from analysis.risk import conditional_var, daily_returns, historical_var
    from analysis.signals import sma
    from data.account import get_combined_positions, get_combined_summary
    from data.market import get_price_history
    from data.snapshots import load_portfolio_history

    positions = get_combined_positions()
    summary = get_combined_summary(positions)
    nav = summary.get("net_liquidation", 0)

    snapshots = load_portfolio_history()
    portfolio_vol = 0.15
    current_dd = 0.0
    var_5d = 0.0
    cvar_5d = None

    # Risk statistics come from the FLOW-ADJUSTED daily index, never from the raw
    # balance series. Two measured reasons:
    #   1. the snapshot store runs on a 5-minute cadence (2,502 rows over 78
    #      dates), so pct_change(5) was a 25-MINUTE window presented as "5d VaR";
    #   2. the balance includes transfers — the September house-proceeds deposit
    #      was the best day on record, and the 5th percentile of that series is a
    #      deposit, not a bad week. The composite reported $3,318 at risk against
    #      a $330,798 book while the KPI strip beside it showed VaR -5.46%.
    # analysis.performance.build_days() already collapses to one row per calendar
    # close and builds `index` from market P&L alone; diff that instead.
    try:
        from analysis.performance import build_days
        from data.snapshots import load_position_history

        days = build_days(snapshots, load_position_history())
        levels = pd.Series(
            [float(d["index"]) for d in days if d.get("index") is not None],
            dtype=float,
        )
        levels = levels[levels > 0]
        if len(levels) > 20:
            day_rets = levels.pct_change().dropna()
            if len(day_rets) > 20:
                portfolio_vol = float(day_rets.std() * np.sqrt(252))
                if not np.isfinite(portfolio_vol):
                    portfolio_vol = 0.15
            cummax = levels.cummax()
            dd = (levels - cummax) / cummax.replace(0, np.nan)
            current_dd = float(dd.iloc[-1]) if not dd.empty else 0.0
            rolling_5d = levels.pct_change(periods=5).dropna()
            if len(rolling_5d) > 20:
                var_5d = historical_var(rolling_5d, 0.95)
                cvar_5d = conditional_var(rolling_5d, 0.95)
    except Exception as exc:
        # A risk statistic must never take the surface down with it.
        log.warning("flow-adjusted risk series unavailable: %s", exc)

    if not np.isfinite(current_dd):
        current_dd = 0.0

    vix_level = None
    credit_spread_val = None
    credit_spread_avg = None
    yield_curve_val = None
    try:
        from data.fred import get_credit_spread, get_vix, get_yield_curve
        vix_series = get_vix(days=30)
        if not vix_series.empty:
            vix_level = float(vix_series.iloc[-1])
        cs = get_credit_spread(days=730)
        if not cs.empty:
            credit_spread_val = float(cs.iloc[-1])
            credit_spread_avg = float(cs.mean())
        yc = get_yield_curve(days=30)
        if not yc.empty:
            yield_curve_val = float(yc.iloc[-1])
    except Exception as e:
        log.warning("Macro feed fetch failed (composites): %s", e)

    spy_df = get_price_history("SPY")
    market_returns = daily_returns(spy_df) if not spy_df.empty else pd.Series(dtype=float)

    breadth_50 = None
    breadth_200 = None
    if not positions.empty:
        equity_pos = positions[positions["asset_type"] != "OPTION"]
        above_50 = 0
        above_200 = 0
        total = 0
        for _, row in equity_pos.iterrows():
            sym = row["symbol"]
            try:
                pdf = get_price_history(sym)
                if pdf.empty:
                    continue
                close = pdf["close"]
                total += 1
                if len(close) >= 50:
                    sma_50 = float(sma(close, 50).iloc[-1])
                    if float(close.iloc[-1]) > sma_50:
                        above_50 += 1
                if len(close) >= 200:
                    sma_200 = float(sma(close, 200).iloc[-1])
                    if float(close.iloc[-1]) > sma_200:
                        above_200 += 1
            except Exception as e:
                log.debug("Breadth price-history fetch failed for %s: %s", sym, e)
                continue
        if total > 0:
            breadth_50 = above_50 / total * 100
            breadth_200 = above_200 / total * 100

    return all_composites(
        portfolio_vol=portfolio_vol,
        vix_level=vix_level,
        current_drawdown_pct=current_dd,
        breadth_pct_above_sma50=breadth_50,
        breadth_pct_above_sma200=breadth_200,
        market_returns=market_returns,
        portfolio_var_5d_95=var_5d,
        portfolio_nav=nav,
        cvar_5d_95=cvar_5d,
        credit_spread=credit_spread_val,
        credit_spread_avg=credit_spread_avg,
        yield_curve_spread=yield_curve_val,
    )


def get_composites() -> dict:
    """Serve composites for the operator, recomputing if stale.

    Single-operator: a single global cache (no session keying). The scheduler
    populates it on its 15-min alert scan; the endpoint serves the cached
    result when fresh and recomputes on a cold cache.
    """
    now = time.time()
    if _cache and (now - _cache["ts"]) < _CACHE_TTL_SECONDS:
        return _cache["data"]
    data = compute_composites()
    _cache.clear()
    _cache["ts"] = now
    _cache["data"] = data
    return data


def invalidate_cache() -> None:
    """Drop all cached composites (used by tests)."""
    _cache.clear()
