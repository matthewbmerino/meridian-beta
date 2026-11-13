"""Stress testing: parametric scenarios and Monte Carlo VaR.

Pure functions — no data/ or backend/ imports.
"""

from typing import Any

import numpy as np
import pandas as pd

DEFAULT_SCENARIOS: list[dict[str, Any]] = [
    {"name": "Index -5%", "description": "Broad market selloff", "market_shock": -0.05},
    {"name": "Index -10%", "description": "Sharp correction", "market_shock": -0.10},
    {"name": "Top Holding -15%", "description": "Single-name blowup in largest position",
     "market_shock": None, "top_holding_shock": -0.15},
    {"name": "Semis -15%", "description": "Semiconductor sector crash",
     "market_shock": None, "sector_shock": {"keyword": "SEMICONDUCTOR", "shock": -0.15}},
    {"name": "Mega-Cap -10%", "description": "Large-cap rotation",
     "market_shock": None, "cap_shock": {"min_cap": 500e9, "shock": -0.10}},
    {"name": "Vol Spike 2x", "description": "Volatility doubles from current",
     "market_shock": None, "vol_multiplier": 2.0},
]


def parametric_stress(returns_dict: dict[str, pd.Series],
                      weights: dict[str, float],
                      betas: dict[str, float] | None = None,
                      scenarios: list[dict[str, Any]] | None = None,
                      metadata: dict[str, dict] | None = None) -> list[dict]:
    """Run parametric stress scenarios against the portfolio.

    returns_dict: {symbol: daily returns Series}
    weights: {symbol: portfolio weight as fraction}
    betas: {symbol: beta vs market} (optional, defaults to 1.0)
    scenarios: list of scenario dicts (optional, uses defaults)
    metadata: {symbol: {market_cap?, sector_corr?}} for sector/cap scenarios

    Returns list of {scenario, description, portfolio_impact_pct, contributions[]}.
    """
    if not returns_dict or not weights:
        return []

    scenarios = list(scenarios or DEFAULT_SCENARIOS)
    betas = betas or {}
    metadata = metadata or {}

    syms = [s for s in weights if s in returns_dict]
    if not syms:
        return []

    # Normalize weights to sum to 1 over available symbols
    w_sum = sum(weights[s] for s in syms)
    if w_sum == 0:
        return []
    norm_w = {s: weights[s] / w_sum for s in syms}

    # Per-symbol vol for vol-spike scenario
    sym_vols = {}
    for s in syms:
        r = returns_dict[s].dropna()
        sym_vols[s] = float(r.std() * np.sqrt(252)) if len(r) > 20 else 0.25

    # Find top holding
    top_holding = max(syms, key=lambda s: norm_w[s])

    results = []
    for scenario in scenarios:
        name = scenario.get("name", "Custom")
        desc = scenario.get("description", "")
        contributions = []
        total_impact = 0.0

        for s in syms:
            w = norm_w[s]
            beta = betas.get(s, 1.0)
            impact = 0.0

            if scenario.get("market_shock") is not None:
                impact = scenario["market_shock"] * beta

            elif scenario.get("top_holding_shock") is not None:
                if s == top_holding:
                    impact = scenario["top_holding_shock"]
                else:
                    impact = scenario["top_holding_shock"] * 0.15 * beta

            elif scenario.get("sector_shock") is not None:
                sec = scenario["sector_shock"]
                shock = sec["shock"]
                keyword = sec.get("keyword", "").upper()
                sic = (metadata.get(s, {}).get("sic_description") or "").upper()
                if keyword and keyword in sic:
                    impact = shock
                else:
                    impact = shock * 0.2 * beta

            elif scenario.get("cap_shock") is not None:
                cap = scenario["cap_shock"]
                sym_cap = metadata.get(s, {}).get("market_cap", 0)
                if sym_cap >= cap["min_cap"]:
                    impact = cap["shock"]
                else:
                    impact = cap["shock"] * 0.15 * beta

            elif scenario.get("vol_multiplier") is not None:
                vol_mult = scenario["vol_multiplier"]
                daily_vol = sym_vols[s] / np.sqrt(252)
                # Under a vol spike, estimate loss as (vol_mult - 1) daily-vol
                # standard deviations. The 2-sigma assumption corresponds to a
                # ~95th-percentile adverse move during the shock period.
                impact = -daily_vol * (vol_mult - 1) * 2

            else:
                impact = 0.0

            position_impact = w * impact
            total_impact += position_impact
            contributions.append({
                "symbol": s,
                "weight": round(w, 4),
                "position_impact_pct": round(impact * 100, 2),
                "contribution_pct": round(position_impact * 100, 2),
            })

        contributions.sort(key=lambda c: float(c["contribution_pct"]))  # type: ignore[arg-type]

        results.append({
            "scenario": name,
            "description": desc,
            "portfolio_impact_pct": round(total_impact * 100, 2),
            "contributions": contributions,
        })

    return results


def monte_carlo_var(returns_dict: dict[str, pd.Series],
                    weights: dict[str, float],
                    simulations: int = 5000,
                    horizon: int = 5,
                    seed: int | None = None) -> dict | None:
    """Monte Carlo Value-at-Risk simulation.

    Simulates `horizon`-day portfolio returns using bootstrapped daily returns
    with preserved cross-asset correlation structure.

    Returns {var_95, var_99, cvar_95, expected_return, percentiles, distribution}.
    """
    if not returns_dict or not weights:
        return None

    syms = [s for s in weights if s in returns_dict]
    if not syms:
        return None

    df = pd.DataFrame({s: returns_dict[s] for s in syms}).dropna()
    if len(df) < 60:
        return None

    w_arr = np.array([weights.get(s, 0.0) for s in syms])
    w_sum = w_arr.sum()
    if w_sum == 0:
        return None
    w_arr = w_arr / w_sum

    rng = np.random.default_rng(seed)
    data = df.values  # (T, N)
    n_days = len(data)

    # Block bootstrap: sample `horizon` consecutive days, `simulations` times
    sim_returns = np.zeros(simulations)
    for i in range(simulations):
        start = rng.integers(0, n_days - horizon)
        block = data[start:start + horizon]  # (horizon, N)
        daily_port = block @ w_arr  # (horizon,)
        sim_returns[i] = float((1 + daily_port).prod() - 1)

    var_95 = float(np.percentile(sim_returns, 5))
    var_99 = float(np.percentile(sim_returns, 1))
    tail_95 = sim_returns[sim_returns <= var_95]
    cvar_95 = float(tail_95.mean()) if len(tail_95) > 0 else var_95

    percentiles = {
        "p1": round(float(np.percentile(sim_returns, 1)) * 100, 2),
        "p5": round(float(np.percentile(sim_returns, 5)) * 100, 2),
        "p10": round(float(np.percentile(sim_returns, 10)) * 100, 2),
        "p25": round(float(np.percentile(sim_returns, 25)) * 100, 2),
        "p50": round(float(np.percentile(sim_returns, 50)) * 100, 2),
        "p75": round(float(np.percentile(sim_returns, 75)) * 100, 2),
        "p90": round(float(np.percentile(sim_returns, 90)) * 100, 2),
        "p95": round(float(np.percentile(sim_returns, 95)) * 100, 2),
        "p99": round(float(np.percentile(sim_returns, 99)) * 100, 2),
    }

    # Histogram buckets for frontend distribution chart
    hist, edges = np.histogram(sim_returns * 100, bins=50)
    distribution = [
        {"bucket": round(float((edges[i] + edges[i + 1]) / 2), 2), "count": int(hist[i])}
        for i in range(len(hist))
    ]

    return {
        "var_95": round(var_95 * 100, 2),
        "var_99": round(var_99 * 100, 2),
        "cvar_95": round(cvar_95 * 100, 2),
        "expected_return": round(float(sim_returns.mean()) * 100, 2),
        "simulations": simulations,
        "horizon_days": horizon,
        "percentiles": percentiles,
        "distribution": distribution,
    }
