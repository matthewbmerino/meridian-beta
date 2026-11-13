"""Walk-forward backtest of the Meridian sizing system.

Validates the question the framework was built to answer: "if I had followed
this sizing system over the last N years, what would my Sortino have been?"

The simulation is a faithful loop, not a static snapshot:

  1. At each rebalance date t, compute each symbol's composite score using
     ONLY data up to t (generate_signals on df.iloc[:t+1]) — no lookahead.
  2. Build PositionInput with the ACTUAL drifted portfolio weights
     (mark-to-market since the last rebalance) — the system's output depends
     on current weights, so the simulation feeds back what is really held.
  3. compute_target_weights -> target weights; rebalance.
  4. Mark to market daily between rebalances; track NAV.
  5. Evaluate: Sortino (primary), Sharpe, max drawdown, CAGR, turnover —
     vs a buy-and-hold benchmark.

Lookahead guards:
  - Scores use only data up to t.
  - Sigma uses only returns up to t.
  - Calibration buckets use only pairs whose forward window closed by t
    (compute_dated_empirical_buckets) — a pair is usable at rebalance t only
    if its forward return is already realized. Early rebalances have few
    closed pairs, so buckets fall below min_obs_per_bucket and the edge
    degrades to the logistic prior. The configured method (default hybrid)
    is validated walk-forward, not forced to logistic.
  - Symbols enter the universe only once they have enough history
    (SMA200 + STBR 140 warmup).

Degradation contract: insufficient symbols/history/NAV -> return an explicit
low-data result, never fabricate a curve.
"""

from __future__ import annotations

import datetime as _dt

import numpy as np
import pandas as pd

from analysis.calibration import compute_dated_empirical_buckets
from analysis.risk import daily_returns, max_drawdown, sharpe_ratio, sortino_ratio
from analysis.signals import generate_signals
from analysis.sizing import PositionInput, SizingConfig, compute_target_weights

# Warmup: SMA200 (200) + STBR 140 + margin. A symbol needs this many bars
# before its score is trustworthy.
_MIN_BARS = 250
# Default rebalance cadence (trading days ~ monthly).
DEFAULT_REBALANCE_DAYS = 21
# Default backtest window (calendar days ~ 5 years).
DEFAULT_WINDOW_DAYS = 1825


def _score_at(price_df: pd.DataFrame, symbol: str) -> float | None:
    """Composite score using only data up to the last bar of price_df."""
    sig = generate_signals(symbol, price_df)
    score = sig.get("composite_score")
    return float(score) if score is not None else None


def _sigma_at(price_df: pd.DataFrame) -> float | None:
    """Annualized vol from returns up to the last bar."""
    rets = daily_returns(price_df)
    if len(rets) < 20:
        return None
    return float(rets.std() * (252 ** 0.5))


def _precompute_dated_pairs(
    price_data: dict[str, pd.DataFrame],
    cfg: SizingConfig,
) -> list[tuple[object, object, float, float]]:
    """Precompute (signal_date, forward_end_date, score, forward_return) pairs.

    Sampled the same way the live /sizing endpoint samples (step 5, from bar
    50), so the backtest's empirical buckets match production's. Each pair
    carries the date its forward window closes, enabling the no-lookahead
    filter at rebalance time.
    """
    forward_days = cfg.calibration.forward_days
    pairs: list[tuple[object, object, float, float]] = []
    for sym, df in price_data.items():
        if df is None or df.empty or "close" not in df.columns:
            continue
        close = df["close"].values
        idx = df.index
        max_i = len(df) - forward_days
        for i in range(50, max_i, 5):
            sig = generate_signals(sym, df.iloc[:i + 1])
            score = sig.get("composite_score")
            if score is None:
                continue
            fwd_ret = float(close[i + forward_days] / close[i] - 1)
            pairs.append((idx[i], idx[i + forward_days], float(score), fwd_ret))
    return pairs


def _align_prices(
    price_data: dict[str, pd.DataFrame],
    start: _dt.datetime | None,
    end: _dt.datetime | None,
) -> dict[str, pd.DataFrame]:
    """Filter each symbol's history to the window and drop empty/short series."""
    out: dict[str, pd.DataFrame] = {}
    for sym, df in price_data.items():
        if df is None or df.empty or "close" not in df.columns:
            continue
        df = df.sort_index()
        if start is not None:
            df = df[df.index >= start]
        if end is not None:
            df = df[df.index <= end]
        if len(df) >= _MIN_BARS:
            out[sym] = df
    return out


def _build_inputs(aligned, weights, date) -> list[PositionInput]:
    """Build PositionInputs for a rebalance using only data up to *date*.

    Each symbol's score and sigma use only the history at or before the
    rebalance date — no lookahead.
    """
    inputs: list[PositionInput] = []
    for sym in aligned:
        df = aligned[sym]
        hist = df.loc[:date]
        if len(hist) < _MIN_BARS:
            continue
        score = _score_at(hist, sym)
        sigma = _sigma_at(hist)
        if score is None:
            continue
        inputs.append(PositionInput(
            symbol=sym,
            score=score,
            current_weight=weights.get(sym, 0.0),
            sigma=sigma,
        ))
    return inputs


def _rebalance(inputs, weights, nav, cfg, bucket_stats, date):
    """One rebalance: run the sizing pipeline and return the new state.

    Returns (new_weights, actions, turnover, cash_pct, constraint_counts).
    """
    results = compute_target_weights(inputs, nav, cfg, bucket_stats)
    new_weights: dict[str, float] = {}
    actions: list[dict] = []
    constraint_counts: dict[str, int] = {}
    cash_pct = 1.0 - sum(r.target_weight for r in results)

    for r in results:
        new_weights[r.symbol] = r.target_weight
        constraint_counts[r.constraint] = constraint_counts.get(r.constraint, 0) + 1
        if r.action != "HOLD":
            actions.append({
                "date": str(date.date()),
                "symbol": r.symbol,
                "action": r.action,
                "from_weight": round(r.current_weight, 4),
                "to_weight": round(r.target_weight, 4),
                "delta_dollars": round(r.delta_dollars, 0),
                "constraint": r.constraint,
            })

    # Turnover = half the sum of |delta_w| (one side of the trade).
    turnover = 0.5 * sum(
        abs(new_weights.get(s, 0.0) - weights.get(s, 0.0)) for s in set(weights) | set(new_weights)
    )
    return new_weights, actions, turnover, cash_pct, constraint_counts


def _mark_to_market(weights, closes, dates, i) -> float:
    """Daily portfolio return from the current weights and close prices."""
    date = dates[i]
    day_ret = 0.0
    for sym, w in weights.items():
        if w <= 0:
            continue
        px = closes.loc[date, sym]
        prev_px = closes.loc[dates[i - 1], sym] if i > 0 else px
        if prev_px and prev_px > 0 and not np.isnan(px):
            day_ret += w * (px / prev_px - 1)
    return day_ret


def _compute_metrics(equity_curve, bench_nav, dates, total_turnover, n_rebalances,
                     cash_samples, constraint_counts, n_symbols, cfg) -> dict:
    """Evaluation metrics from the simulated equity curve."""
    nav_series = pd.Series([p["nav"] for p in equity_curve])
    rets = nav_series.pct_change().dropna()
    bench_series = pd.Series([float(b) for b in bench_nav.values])
    bench_rets = bench_series.pct_change().dropna()

    total_return = nav_series.iloc[-1] / nav_series.iloc[0] - 1
    years = len(nav_series) / 252
    cagr = (nav_series.iloc[-1] / nav_series.iloc[0]) ** (1 / years) - 1 if years > 0 else 0.0
    dd = max_drawdown(nav_series)

    return {
        "sortino": round(sortino_ratio(rets), 3),
        "sharpe": round(sharpe_ratio(rets), 3),
        "max_drawdown_pct": round(dd * 100, 2),
        "cagr_pct": round(cagr * 100, 2),
        "total_return_pct": round(total_return * 100, 2),
        "benchmark_total_return_pct": round((bench_series.iloc[-1] / bench_series.iloc[0] - 1) * 100, 2),
        "benchmark_sortino": round(sortino_ratio(bench_rets), 3),
        "annualized_turnover": round(total_turnover / max(1, years), 2),
        "n_rebalances": n_rebalances,
        "n_symbols": n_symbols,
        "window_days": (dates[-1] - dates[0]).days,
        "avg_cash_pct": round(float(np.mean(cash_samples) * 100), 1) if cash_samples else None,
        "constraint_mix": dict(sorted(constraint_counts.items(), key=lambda kv: -kv[1])),
        "calibration_method": cfg.calibration.method,
    }


def _insufficient(error: str) -> dict:
    """The shared low-data result shape."""
    return {
        "error": error,
        "equity_curve": [], "benchmark_curve": [], "metrics": None, "rebalances": [],
    }


def run_backtest(
    symbols: list[str],
    price_data: dict[str, pd.DataFrame],
    cfg: SizingConfig | None = None,
    start: _dt.datetime | None = None,
    end: _dt.datetime | None = None,
    rebalance_days: int = DEFAULT_REBALANCE_DAYS,
    initial_capital: float = 100_000.0,
) -> dict:
    """Run the walk-forward sizing backtest.

    Args:
        symbols: candidate universe (only those with enough history are used).
        price_data: {symbol: OHLCV DataFrame with DatetimeIndex + 'close'}.
        cfg: sizing config (defaults used if None). Calibration runs the
            *configured* method walk-forward (default hybrid): empirical
            buckets are built only from dated pairs whose forward window has
            closed by each rebalance date, so there is no forward-return
            lookahead (see compute_dated_empirical_buckets).
        start/end: window bounds (None = use full available history).
        rebalance_days: rebalance cadence in trading days.
        initial_capital: starting NAV.

    Returns a dict with equity_curve, benchmark_curve, metrics, and rebalances.
    """
    cfg = cfg or SizingConfig()

    aligned = _align_prices(price_data, start, end)
    if len(aligned) < 2:
        return _insufficient("Insufficient data — need at least 2 symbols with enough history")

    # Precompute dated score/forward-return pairs once. The walk-forward
    # bucket filter at each rebalance uses only pairs whose forward window
    # has closed by that date — no lookahead.
    dated_pairs = _precompute_dated_pairs(aligned, cfg)

    # Common trading calendar across all symbols.
    common_dates = None
    for df in aligned.values():
        idx = df.index
        common_dates = idx if common_dates is None else common_dates.intersection(idx)
    if common_dates is None or len(common_dates) < _MIN_BARS:
        return _insufficient("Insufficient overlapping history")
    common_dates = common_dates.sort_values()
    dates = list(common_dates)

    # Close prices aligned to the common calendar.
    closes = pd.DataFrame({sym: df["close"].reindex(common_dates) for sym, df in aligned.items()})

    # Benchmark: equal-weight buy-and-hold of the universe (rebalanced once at
    # the start) — a fair, simple reference.
    bench_start = closes.iloc[0]
    bench_weights = bench_start / bench_start.sum()
    bench_nav = (closes / closes.iloc[0]).dot(bench_weights) * initial_capital

    # Portfolio state.
    weights: dict[str, float] = {}  # symbol -> fraction of NAV
    nav = initial_capital
    equity_curve: list[dict] = []
    rebalances: list[dict] = []
    last_rebalance_idx = 0
    total_turnover = 0.0
    n_rebalances = 0
    cash_samples: list[float] = []       # 1 - invested at each rebalance
    constraint_counts: dict[str, int] = {}  # binding constraint -> rebalances

    for i, date in enumerate(dates):
        # --- Rebalance at cadence (and at the first bar) ---
        if i == 0 or (i - last_rebalance_idx) >= rebalance_days:
            inputs = _build_inputs(aligned, weights, date)
            if inputs:
                # Walk-forward calibration: only pairs whose forward window
                # closed by this rebalance date. Early rebalances fall below
                # min_obs_per_bucket and degrade to the logistic prior.
                bucket_stats = None
                if cfg.calibration.method != "logistic":
                    bucket_stats = compute_dated_empirical_buckets(
                        dated_pairs, date, cfg.calibration,
                    )
                new_weights, actions, turnover, cash_pct, constraint_deltas = _rebalance(
                    inputs, weights, nav, cfg, bucket_stats, date,
                )
                total_turnover += turnover
                n_rebalances += 1
                cash_samples.append(cash_pct)
                for k, v in constraint_deltas.items():
                    constraint_counts[k] = constraint_counts.get(k, 0) + v
                weights = new_weights
                last_rebalance_idx = i
                if actions:
                    rebalances.append({"date": str(date.date()), "actions": actions})

        # --- Mark to market daily ---
        day_ret = _mark_to_market(weights, closes, dates, i)
        nav *= (1.0 + day_ret)
        equity_curve.append({"date": str(date.date()), "nav": round(nav, 2)})

    if len(equity_curve) < 2:
        return _insufficient("Insufficient data to produce an equity curve")

    metrics = _compute_metrics(
        equity_curve, bench_nav, dates, total_turnover, n_rebalances,
        cash_samples, constraint_counts, len(aligned), cfg,
    )

    return {
        "error": None,
        "equity_curve": equity_curve,
        "benchmark_curve": [
            {"date": str(d.date()), "nav": round(float(b), 2)}
            for d, b in zip(dates, bench_nav.values)
        ],
        "metrics": metrics,
        "rebalances": rebalances,
    }
