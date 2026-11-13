"""Brinson-style performance attribution.

Answers: where did the portfolio's return come from relative to a benchmark —
allocation (weighting decisions), selection (stock-picking within a sector),
and interaction (the joint effect)?

Pure functions — no data fetching. Accepts a position-history DataFrame
(per-symbol weights + returns per period) and a benchmark return series, and
returns the Brinson decomposition. Follows the analysis-layer invariants:
NaN-safe outputs, type-hinted public functions, degrade-don't-fabricate.

Methodology (Brinson, Hood & Beebower 1986; Brinson & Fachler 1985):

    R_p - R_b = Allocation + Selection + Interaction

    Allocation_i   = (w_p,i - w_b,i) * R_b,i
    Selection_i    = w_b,i * (R_p,i - R_b,i)
    Interaction_i  = (w_p,i - w_b,i) * (R_p,i - R_b,i)

where w_p,i / w_b,i are portfolio/benchmark weights in sector i and R_p,i /
R_b,i are the sector returns. The benchmark is a sector-weighted index (e.g.
SPY sector returns); when only a single benchmark return is available, the
decomposition reduces to allocation vs selection on the whole portfolio.
"""

from __future__ import annotations

import pandas as pd

from analysis._common import safe_float


def _unavailable(total_return: float = 0.0, benchmark_return: float = 0.0) -> dict:
    """The shared 'insufficient data' result shape.

    Every early-return in brinson_attribution degrades to this same dict so
    the response contract can't drift between branches.
    """
    return {
        "available": False,
        "total_return": safe_float(total_return),
        "benchmark_return": safe_float(benchmark_return),
        "excess_return": 0.0,
        "allocation": 0.0,
        "selection": 0.0,
        "interaction": 0.0,
        "by_sector": [],
    }


def brinson_attribution(
    position_history: pd.DataFrame,
    benchmark_returns: pd.Series,
    sector_map: dict[str, str] | None = None,
) -> dict:
    """Brinson decomposition of portfolio return vs a benchmark.

    position_history: DataFrame with columns [ts, symbol, market_value]
        (one row per symbol per timestamp). Weights are derived from market
        values per timestamp.
    benchmark_returns: pd.Series of benchmark daily returns indexed by date.
    sector_map: optional {symbol: sector} to group positions. When None, the
        whole portfolio is treated as a single sector.

    Returns:
        {
          "total_return": float,          # portfolio return over the window
          "benchmark_return": float,
          "excess_return": float,
          "allocation": float,            # weighting decisions
          "selection": float,             # stock-picking within sectors
          "interaction": float,           # joint effect
          "by_sector": [ {sector, allocation, selection, interaction, total} ],
          "available": bool,
        }
    """
    if position_history is None or position_history.empty:
        return _unavailable()

    # --- Build per-period portfolio weights and returns ---
    df = position_history.copy()
    if "ts" not in df.columns or "symbol" not in df.columns or "market_value" not in df.columns:
        return _unavailable()

    df["ts"] = pd.to_datetime(df["ts"], utc=True, errors="coerce")
    df = df.dropna(subset=["ts"])
    if df.empty:
        return _unavailable()

    # Portfolio return over the window: NAV at last vs first timestamp.
    # Use the position market values as a proxy for NAV.
    nav_by_ts = df.groupby("ts")["market_value"].sum()
    if len(nav_by_ts) < 2:
        return _unavailable()
    nav_first = float(nav_by_ts.iloc[0])
    nav_last = float(nav_by_ts.iloc[-1])
    if nav_first <= 0:
        return _unavailable()
    total_return = nav_last / nav_first - 1.0

    # Benchmark return over the same window.
    bench = benchmark_returns.dropna()
    if bench.empty:
        return _unavailable(total_return)
    bench_return = float((1 + bench).prod() - 1)

    # --- Brinson decomposition ---
    # Weights at the start of the window (first timestamp) and returns over
    # the window per symbol.
    first_ts = nav_by_ts.index[0]
    start_rows = df[df["ts"] == first_ts]
    start_mv = start_rows.groupby("symbol")["market_value"].sum()
    total_mv = float(start_mv.sum())
    if total_mv <= 0:
        return _unavailable(total_return, bench_return)

    # Per-symbol return over the window: last MV / first MV - 1.
    last_ts = nav_by_ts.index[-1]
    last_rows = df[df["ts"] == last_ts]
    last_mv = last_rows.groupby("symbol")["market_value"].sum()
    symbols = list(start_mv.index)
    sym_returns = {}
    for s in symbols:
        first = float(start_mv.get(s, 0.0))
        last = float(last_mv.get(s, 0.0))
        if first > 0:
            sym_returns[s] = last / first - 1.0
        else:
            sym_returns[s] = 0.0

    # Group into sectors (or a single "Portfolio" sector).
    if sector_map:
        sectors = sorted({sector_map.get(s, "_unknown") for s in symbols})
    else:
        sectors = ["Portfolio"]

    # Benchmark sector returns: when no sector breakdown is available, use the
    # whole benchmark return for every sector (a conservative assumption that
    # makes selection the dominant term).
    by_sector = []
    alloc_total = 0.0
    sel_total = 0.0
    inter_total = 0.0
    for sec in sectors:
        sec_syms = [s for s in symbols if (sector_map or {}).get(s, "Portfolio") == sec]
        if not sec_syms:
            continue
        w_p = sum(float(start_mv.get(s, 0.0)) for s in sec_syms) / total_mv
        # Portfolio sector return: MV-weighted average of member returns.
        sec_mv = sum(float(start_mv.get(s, 0.0)) for s in sec_syms)
        r_p = sum(float(start_mv.get(s, 0.0)) * sym_returns[s] for s in sec_syms) / sec_mv if sec_mv > 0 else 0.0
        # Benchmark sector weight/return: without a sector breakdown, assume
        # the benchmark is equally weighted across sectors with the same
        # return as the whole benchmark.
        w_b = 1.0 / len(sectors)
        r_b = bench_return

        allocation = (w_p - w_b) * r_b
        selection = w_b * (r_p - r_b)
        interaction = (w_p - w_b) * (r_p - r_b)
        alloc_total += allocation
        sel_total += selection
        inter_total += interaction
        by_sector.append({
            "sector": sec,
            "weight": round(w_p, 4),
            "return": round(r_p, 4),
            "allocation": round(allocation, 4),
            "selection": round(selection, 4),
            "interaction": round(interaction, 4),
            "total": round(allocation + selection + interaction, 4),
        })

    excess = total_return - bench_return
    return {
        "available": True,
        "total_return": round(total_return, 4),
        "benchmark_return": round(bench_return, 4),
        "excess_return": round(excess, 4),
        "allocation": round(alloc_total, 4),
        "selection": round(sel_total, 4),
        "interaction": round(inter_total, 4),
        "by_sector": by_sector,
    }
