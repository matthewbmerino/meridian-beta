"""Risk analysis endpoints: correlation, VaR, frontier, stress, blend, drawdown, greeks."""

from fastapi import Depends

from backend.auth import require_auth
from backend.routers.analysis._common import (
    CorrelationBody,
    FrontierBody,
    StressBody,
    VaRBody,
    _cached_correlation_matrix,
    _cached_frontier_cloud,
    _safe_float,
    log,
    router,
)
from data.cache import ttl_cache


def _coverage_block(requested: list[str], resolved: list[str]) -> dict:
    """Standard coverage/degraded block for analysis endpoints."""
    requested_upper = [s.upper() for s in requested]
    resolved_upper = {s.upper() for s in resolved}
    unresolved = [s for s in requested_upper if s not in resolved_upper]
    return {
        "requested": len(requested_upper),
        "resolved": len(resolved_upper),
        "unresolved": unresolved,
        "degraded": len(unresolved) > 0,
        "note": (
            "All requested symbols resolved."
            if not unresolved else
            f"Could not resolve {len(unresolved)} symbol(s): {', '.join(unresolved)}. "
            "Missing price history creates a data gap, not a safe-zero correlation."
        ),
    }


@router.post("/correlation")
def correlation(body: CorrelationBody):
    import numpy as np

    requested = body.symbols
    corr = _cached_correlation_matrix(tuple(s.upper() for s in requested))
    coverage = _coverage_block(requested, list(corr.columns))

    if corr.empty:
        return {
            "symbols": [],
            "matrix": [],
            "avg_correlation": None,
            "coverage": coverage,
        }

    syms = list(corr.columns)
    n = len(syms)
    mat = [[_safe_float(v, default=0.0) for v in row] for row in corr.values]

    w = np.array([body.weights[i] if body.weights and i < len(body.weights) else 1.0 / n
                  for i in range(n)])
    w = w / w.sum()

    weighted_sum = 0.0
    weight_sum = 0.0
    for i in range(n):
        for j in range(i + 1, n):
            pair_w = w[i] * w[j]
            weighted_sum += mat[i][j] * pair_w
            weight_sum += pair_w

    avg_corr = float(weighted_sum / weight_sum) if weight_sum > 0 else 0.0

    return {
        "symbols": syms,
        "matrix": mat,
        "avg_correlation": round(avg_corr, 4),
        "coverage": coverage,
    }


@router.post("/var")
def value_at_risk(body: VaRBody):
    import numpy as np
    import pandas as pd

    from analysis.risk import conditional_var, daily_returns, historical_var
    from data.market import get_price_history

    all_returns = {}
    aligned_weights = []
    resolved = []
    for sym, w in zip(body.symbols, body.weights):
        df = get_price_history(sym.upper())
        if not df.empty:
            all_returns[sym.upper()] = daily_returns(df)
            aligned_weights.append(w)
            resolved.append(sym)

    coverage = _coverage_block(body.symbols, resolved)

    if not all_returns:
        return {
            "var_95": 0,
            "cvar_95": 0,
            "sample_days": 0,
            "coverage": coverage,
        }

    ret_df = pd.DataFrame(all_returns).dropna()
    weights = np.array(aligned_weights)
    weights = weights / weights.sum()
    port_returns = ret_df.dot(weights)

    return {
        "var_95": round(float(historical_var(port_returns, body.confidence)), 6),
        "cvar_95": round(float(conditional_var(port_returns, body.confidence)), 6),
        "sample_days": len(port_returns),
        "coverage": coverage,
    }


@router.post("/frontier")
def efficient_frontier(body: FrontierBody):
    from data.account import get_combined_positions, get_combined_summary

    result = _cached_frontier_cloud(tuple(s.upper() for s in body.symbols), body.n_portfolios)
    if result is None:
        return {"portfolios": [], "assets": [], "current": None, "symbols": []}

    current_weights = None
    try:
        positions = get_combined_positions()
        summary = get_combined_summary(positions)
        nav = summary.get("net_liquidation", 0)
        if not positions.empty and nav > 0:
            equity_pos = positions[positions["asset_type"] != "OPTION"]
            current_weights = {}
            for _, row in equity_pos.iterrows():
                sym = row["symbol"]
                if sym in result["symbols"]:
                    current_weights[sym] = float(row.get("market_value", 0)) / nav
    except Exception as e:
        log.warning("Frontier current-weights derivation failed: %s", e)

    portfolios_arr = result["portfolios"]
    weights_arr = result["weights"]
    return {
        "portfolios": [
            {
                "vol": _safe_float(row[0]),
                "ret": _safe_float(row[1]),
                "sortino": _safe_float(row[2]),
                "w": [round(float(w), 4) for w in weights_arr[i]],
            }
            for i, row in enumerate(portfolios_arr)
        ],
        "assets": result["assets"],
        "current": result["current"],
        "symbols": result["symbols"],
    }


@router.get("/blend/{symbol}")
def blend(symbol: str):
    """Vol/ret/sortino trajectory as the portfolio shifts toward 100% of `symbol`."""
    from analysis.risk import blend_trajectory, daily_returns
    from data.account import get_combined_positions, get_combined_summary
    from data.market import get_price_history

    positions = get_combined_positions()
    summary = get_combined_summary(positions)
    nav = summary.get("net_liquidation", 0)
    if positions.empty or nav <= 0:
        return {"symbol": symbol.upper(), "available": False, "trajectory": []}

    equities = positions[positions["asset_type"] != "OPTION"]
    returns_dict = {}
    current_weights = {}
    for _, row in equities.iterrows():
        sym = row["symbol"]
        df = get_price_history(sym)
        if df.empty:
            continue
        returns_dict[sym] = daily_returns(df)
        current_weights[sym] = float(row.get("market_value", 0)) / nav

    if symbol.upper() not in returns_dict:
        return {"symbol": symbol.upper(), "available": False, "trajectory": []}

    traj = blend_trajectory(current_weights, symbol.upper(), returns_dict)
    if traj is None:
        return {"symbol": symbol.upper(), "available": False, "trajectory": []}

    return {
        "symbol": symbol.upper(),
        "available": True,
        "trajectory": [
            {
                "alpha": round(t["alpha"], 3),
                "vol": round(t["vol"], 4),
                "ret": round(t["ret"], 4),
                "sortino": round(t["sortino"], 3),
            }
            for t in traj
        ],
    }


@router.get("/drawdown-detail", dependencies=[Depends(require_auth)])
def drawdown_detail():
    import pandas as pd

    from analysis.risk import max_drawdown_detail
    from data.snapshots import load_portfolio_history

    df = load_portfolio_history()
    if df.empty:
        return {"error": "No snapshot data"}

    nav_series = pd.Series(df["net_liquidation"].values, index=pd.to_datetime(df["ts"]))
    result = max_drawdown_detail(nav_series)
    if result is None:
        return {"max_drawdown": 0, "peak_date": None, "trough_date": None}

    dd_series = result.pop("drawdown_series", None)
    dd_list = []
    if dd_series is not None and not dd_series.empty:
        dd_list = [
            {"date": str(idx)[:10], "drawdown": round(_safe_float(val), 4)}
            for idx, val in dd_series.items()
        ]

    return {
        "max_drawdown": _safe_float(result.get("max_drawdown")),
        "peak_date": str(result["peak_date"])[:10] if result.get("peak_date") else None,
        "trough_date": str(result["trough_date"])[:10] if result.get("trough_date") else None,
        "peak_price": _safe_float(result.get("peak_price")),
        "trough_price": _safe_float(result.get("trough_price")),
        "duration_days": result.get("duration_days", 0),
        "current_dd": _safe_float(result.get("current_dd")),
        "recovery_pct": _safe_float(result.get("recovery_pct")),
        "drawdown_series": dd_list,
    }


@router.get("/attribution", dependencies=[Depends(require_auth)])
def attribution():
    """Brinson-style performance attribution vs a benchmark.

    Uses the position-snapshot history (per-symbol market values over time)
    and SPY as the benchmark. Returns allocation/selection/interaction effects
    per sector. Degrades to available:false when history is insufficient.
    """
    return _cached_attribution()


def _attribution_compute():
    from analysis.attribution import brinson_attribution
    from analysis.risk import daily_returns
    from data.market import get_price_history
    from data.snapshots import load_position_history

    ph = load_position_history()
    if ph.empty:
        return {"available": False, "error": "No position snapshot history"}

    spy_df = get_price_history("SPY")
    if spy_df.empty:
        return {"available": False, "error": "SPY benchmark unavailable"}
    bench = daily_returns(spy_df)

    sector_map = {}
    try:
        from data.reference import get_sector_map
        symbols = [s for s in ph["symbol"].unique() if s]
        sector_map = get_sector_map(symbols) or {}
    except Exception as e:
        log.warning("Sector map fetch failed (attribution): %s", e)

    result = brinson_attribution(ph, bench, sector_map=sector_map or None)
    return result


# TTL-cached: reads the snapshot history + SPY; only changes when a new snapshot lands.
_cached_attribution = ttl_cache(ttl_seconds=300)(_attribution_compute)


@router.get("/greeks")
def portfolio_greeks():
    from analysis.options import aggregate_greeks
    from data.account import get_combined_positions

    positions = get_combined_positions()
    if positions.empty:
        return {"delta": 0, "gamma": 0, "theta": 0, "vega": 0}

    result = aggregate_greeks(positions)
    return {k: _safe_float(v) for k, v in result.items()}


@router.post("/stress")
def stress_test(body: StressBody):
    from analysis.risk import daily_returns, portfolio_beta, weighted_portfolio_beta
    from analysis.stress import monte_carlo_var, parametric_stress
    from data.account import get_combined_positions, get_combined_summary
    from data.market import get_price_history, get_quotes

    positions = get_combined_positions()
    summary = get_combined_summary(positions)
    nav = summary.get("net_liquidation", 0)

    if positions.empty or nav <= 0:
        return {
            "scenarios": [],
            "monte_carlo": None,
            "portfolio_beta": 0.0,
            "beta_adjusted_exposure": 0.0,
            "betas": {},
            "coverage": _coverage_block([], []),
        }

    equity_pos = positions[positions["asset_type"] != "OPTION"]
    requested_symbols: list[str] = []
    if body.symbols:
        requested_symbols = [s.upper() for s in body.symbols]
        equity_pos = equity_pos[equity_pos["symbol"].isin(requested_symbols)]
    else:
        requested_symbols = [s for s in equity_pos["symbol"].unique() if s]

    returns_dict = {}
    weights = {}
    for _, row in equity_pos.iterrows():
        sym = row["symbol"]
        df = get_price_history(sym)
        if df.empty:
            continue
        returns_dict[sym] = daily_returns(df)
        weights[sym] = float(row.get("market_value", 0)) / nav

    coverage = _coverage_block(requested_symbols, list(returns_dict.keys()))

    if not returns_dict:
        return {
            "scenarios": [],
            "monte_carlo": None,
            "portfolio_beta": 0.0,
            "beta_adjusted_exposure": 0.0,
            "betas": {},
            "coverage": coverage,
        }

    syms = list(returns_dict.keys())

    spy_df = get_price_history("SPY")
    betas = {}
    if not spy_df.empty:
        bench_returns = daily_returns(spy_df)
        betas = portfolio_beta(returns_dict, bench_returns)

    metadata: dict[str, dict] = {}
    try:
        from data.reference import get_ticker_details
        for s in syms:
            details = get_ticker_details(s)
            if details:
                m = metadata.setdefault(s, {})
                if details.get("sic_description"):
                    m["sic_description"] = details["sic_description"]
                if details.get("market_cap"):
                    m["market_cap"] = details["market_cap"]
    except Exception as e:
        log.warning("Ticker-details metadata fetch failed: %s", e)

    syms_missing_cap = [s for s in syms if "market_cap" not in metadata.get(s, {})]
    if syms_missing_cap:
        try:
            quotes = get_quotes(syms_missing_cap)
            for s in syms_missing_cap:
                q = quotes.get(s, {})
                fund = q.get("fundamental", {})
                shares = fund.get("sharesOutstanding", 0)
                price = q.get("quote", {}).get("lastPrice", 0) or q.get("quote", {}).get("mark", 0)
                if shares and price:
                    metadata.setdefault(s, {})["market_cap"] = float(shares * price)
        except Exception as e:
            log.warning("Quote-derived market-cap fallback failed: %s", e)

    scenarios = parametric_stress(returns_dict, weights, betas, metadata=metadata)
    mc = monte_carlo_var(returns_dict, weights,
                         simulations=body.simulations, horizon=body.horizon)

    port_beta = _safe_float(weighted_portfolio_beta(betas, weights))
    beta_adj = _safe_float(port_beta * sum(weights.values()))
    per_sym_betas = {s: round(_safe_float(betas.get(s, 1.0)), 3) for s in syms}

    return {
        "scenarios": scenarios,
        "monte_carlo": mc,
        "portfolio_beta": round(port_beta, 3),
        "beta_adjusted_exposure": round(beta_adj, 4),
        "betas": per_sym_betas,
        "coverage": coverage,
    }
