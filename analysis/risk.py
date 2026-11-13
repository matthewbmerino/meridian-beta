"""Risk analysis: beta, correlation, VaR, max drawdown."""

import numpy as np
import pandas as pd


def daily_returns(price_df):
    """Compute daily returns from a price DataFrame (needs 'close' column)."""
    return price_df["close"].pct_change().dropna()


def portfolio_beta(holdings_returns, benchmark_returns):
    """Compute portfolio beta vs a benchmark (e.g. SPY).

    holdings_returns: dict of {symbol: pd.Series of daily returns}
    benchmark_returns: pd.Series of benchmark daily returns
    Returns weighted-average beta.
    """
    if not holdings_returns:
        return {}
    betas = {}
    bench = benchmark_returns.dropna()
    for sym, ret in holdings_returns.items():
        aligned = pd.concat([ret, bench], axis=1, join="inner")
        if len(aligned) < 20:
            betas[sym] = 1.0
            continue
        aligned.columns = ["asset", "bench"]
        local_bench_var = aligned["bench"].var()
        if local_bench_var == 0:
            betas[sym] = 1.0
            continue
        cov = aligned["asset"].cov(aligned["bench"])
        betas[sym] = cov / local_bench_var
    return betas


def weighted_portfolio_beta(betas, weights):
    """Compute weighted-average beta given per-symbol betas and weights."""
    total = 0
    for sym, beta in betas.items():
        total += beta * weights.get(sym, 0)
    return total


def correlation_matrix(returns_dict):
    """Build a correlation matrix from a dict of {symbol: daily returns Series}."""
    if not returns_dict:
        return pd.DataFrame()
    df = pd.DataFrame(returns_dict)
    return df.corr()


def historical_var(returns, confidence=0.95):
    """Historical Value-at-Risk at the given confidence level.

    Returns a negative number representing the loss threshold.
    """
    if returns.empty:
        return 0.0
    return float(np.percentile(returns.dropna(), (1 - confidence) * 100))


def conditional_var(returns, confidence=0.95):
    """Conditional VaR (Expected Shortfall) — average loss beyond VaR.

    More actionable than VaR: tells you the expected loss in the worst cases.
    """
    if returns.empty:
        return 0.0
    threshold = np.percentile(returns.dropna(), (1 - confidence) * 100)
    tail = returns[returns <= threshold]
    return float(tail.mean()) if not tail.empty else float(threshold)


def sharpe_ratio(returns, risk_free_rate=0.0):
    """Annualized Sharpe ratio from daily returns.

    risk_free_rate: annualized (e.g. 0.05 for 5%). Converted to daily internally.
    """
    if returns.empty or len(returns) < 20:
        return 0.0
    daily_rf = (1 + risk_free_rate) ** (1 / 252) - 1
    excess = returns - daily_rf
    mu = excess.mean()
    sigma = excess.std()
    if sigma == 0:
        return 0.0
    return float(mu / sigma * np.sqrt(252))


def sortino_ratio(returns, risk_free_rate=0.0):
    """Annualized Sortino ratio — penalizes only downside volatility.

    Uses the canonical Sortino & Price (1994) definition: downside deviation
    is sqrt(mean(min(excess_i, 0)^2)) over ALL observations, not just the
    negative ones.
    """
    if returns.empty or len(returns) < 20:
        return 0.0
    daily_rf = (1 + risk_free_rate) ** (1 / 252) - 1
    excess = returns - daily_rf
    mu = excess.mean()
    downside_sq = np.minimum(excess, 0) ** 2
    downside_std = np.sqrt(downside_sq.mean())
    if downside_std == 0:
        # No downside variation: infinite Sortino is a perfect-return state.
        # Report 99.99 (a sentinel the frontend renders as "effectively
        # infinite") when returns are positive, 0.0 when not.
        return 99.99 if mu > 0 else 0.0
    return float(mu / downside_std * np.sqrt(252))


def efficient_frontier_cloud(returns_dict, n_portfolios=4000, current_weights=None,
                             risk_free_rate=0.04, seed=42):
    """Empirical long-only frontier via random Dirichlet portfolios.

    Inputs:
        returns_dict: {symbol: pd.Series of daily returns}. Aligned to the
            common date intersection internally.
        n_portfolios: number of random long-only weight vectors to sample.
        current_weights: optional {symbol: weight} for the current portfolio.
            Renormalized to sum to 1 over symbols present in returns_dict.
        risk_free_rate: annualized rf (e.g. 0.04 for 4%) used in Sortino.

    Returns dict (or None if insufficient data):
        portfolios: ndarray (n, 3) of [vol_a, ret_a, sortino_a]
        assets:     list of {symbol, vol, ret, sortino}
        current:    {vol, ret, sortino, weights} or None
        symbols:    list of symbols (column order)
    """
    if not returns_dict or len(returns_dict) < 2:
        return None
    syms = list(returns_dict.keys())
    df = pd.DataFrame(returns_dict).dropna(how="any")
    if df.empty or len(df) < 60:
        return None

    n = len(syms)
    rets = df.values  # (T, N)
    mu_d = rets.mean(axis=0)
    cov_d = np.cov(rets, rowvar=False, ddof=1)
    mu_a = mu_d * 252
    cov_a = cov_d * 252
    daily_rf = (1 + risk_free_rate) ** (1 / 252) - 1
    sqrt_252 = np.sqrt(252)

    # Per-asset metrics
    asset_vols = np.sqrt(np.diag(cov_a))
    assets = []
    for i, s in enumerate(syms):
        col = rets[:, i]
        downside_sq = np.minimum(col - daily_rf, 0) ** 2
        d_std = np.sqrt(downside_sq.mean())
        sortino_a = ((col.mean() - daily_rf) / d_std * sqrt_252) if d_std > 0 else 0.0
        assets.append({"symbol": s, "vol": float(asset_vols[i]),
                       "ret": float(mu_a[i]), "sortino": float(sortino_a)})

    # Random long-only portfolios. alpha<1 spreads samples toward corners
    # (concentrated portfolios) so we cover the full envelope, not just blends.
    rng = np.random.default_rng(seed)
    weights = rng.dirichlet(np.full(n, 0.5), size=n_portfolios)  # (P, N)

    port_returns_a = weights @ mu_a                              # (P,)
    port_vars_a = np.einsum("pi,ij,pj->p", weights, cov_a, weights)
    port_vols_a = np.sqrt(np.maximum(port_vars_a, 0))

    port_daily = rets @ weights.T                                # (T, P)
    port_excess_mean_d = port_daily.mean(axis=0) - daily_rf
    neg_d = np.where(port_daily < 0, port_daily, 0.0)
    port_downside_d = np.sqrt((neg_d ** 2).mean(axis=0))
    port_sortino_a = np.where(
        port_downside_d > 0,
        port_excess_mean_d / np.where(port_downside_d == 0, 1, port_downside_d) * sqrt_252,
        0.0,
    )

    portfolios = np.column_stack([port_vols_a, port_returns_a, port_sortino_a])

    current = None
    if current_weights:
        w = np.array([float(current_weights.get(s, 0.0) or 0.0) for s in syms])
        if w.sum() > 0:
            w = w / w.sum()
            c_daily = rets @ w
            c_ret = float((c_daily.mean()) * 252)
            c_vol = float(c_daily.std(ddof=1) * sqrt_252)
            c_downside_sq = np.minimum(c_daily - daily_rf, 0) ** 2
            c_dstd = np.sqrt(c_downside_sq.mean())
            c_sortino = float(((c_daily.mean() - daily_rf) / c_dstd * sqrt_252)
                              if c_dstd > 0 else 0.0)
            current = {"vol": c_vol, "ret": c_ret, "sortino": c_sortino,
                       "weights": dict(zip(syms, w.tolist()))}

    return {"portfolios": portfolios, "weights": weights, "assets": assets,
            "current": current, "symbols": syms}


def blend_trajectory(current_weights, searched_symbol, returns_dict,
                     risk_free_rate=0.04, n_steps=21):
    """Trajectory (vol, ret, sortino) as we shift current → 100% searched.

    Each step is a long-only convex blend: w(α) = (1-α)·w_current + α·e_searched.
    Returns list of {alpha, vol, ret, sortino}, or None if inputs are unusable.
    """
    if not current_weights or searched_symbol not in returns_dict:
        return None
    syms = list(returns_dict.keys())
    df = pd.DataFrame(returns_dict).dropna(how="any")
    if df.empty or len(df) < 60:
        return None

    rets = df.values
    daily_rf = (1 + risk_free_rate) ** (1 / 252) - 1
    sqrt_252 = np.sqrt(252)

    w_c = np.array([float(current_weights.get(s, 0.0) or 0.0) for s in syms])
    if w_c.sum() == 0:
        return None
    w_c = w_c / w_c.sum()
    e_s = np.array([1.0 if s == searched_symbol else 0.0 for s in syms])

    out = []
    for i in range(n_steps):
        alpha = i / (n_steps - 1)
        w = (1 - alpha) * w_c + alpha * e_s
        d = rets @ w
        ret_a = float(d.mean() * 252)
        vol_a = float(d.std(ddof=1) * sqrt_252)
        neg = d[d < 0]
        d_std = np.sqrt((neg ** 2).mean()) if neg.size else 0.0
        sortino_a = float(((d.mean() - daily_rf) / d_std * sqrt_252)
                          if d_std > 0 else 0.0)
        out.append({"alpha": alpha, "vol": vol_a, "ret": ret_a, "sortino": sortino_a})
    return out


def max_drawdown(price_series):
    """Compute max drawdown from a price series."""
    if price_series.empty:
        return 0.0
    cummax = price_series.cummax()
    drawdown = (price_series - cummax) / cummax.replace(0, np.nan)
    dd_min = drawdown.min()
    return float(dd_min) if not np.isnan(dd_min) else 0.0


def max_drawdown_detail(price_series):
    """Compute max drawdown with full context: dates, duration, recovery.

    Returns a dict with:
        max_drawdown: float (negative)
        peak_date: date of the pre-drawdown high
        trough_date: date of the deepest point
        peak_price: price at the peak
        trough_price: price at the trough
        duration_days: calendar days from peak to trough
        current_dd: current drawdown from the most recent high
        recovery_pct: how much of the max drawdown has been recovered (0-100)
        drawdown_series: full drawdown series for sparkline
    """
    if price_series.empty:
        return None

    cummax = price_series.cummax()
    drawdown = (price_series - cummax) / cummax.replace(0, np.nan)
    dd_min = drawdown.min()

    if np.isnan(dd_min) or dd_min == 0:
        return {
            "max_drawdown": 0.0,
            "peak_date": None,
            "trough_date": None,
            "peak_price": float(price_series.iloc[-1]),
            "trough_price": float(price_series.iloc[-1]),
            "duration_days": 0,
            "current_dd": 0.0,
            "recovery_pct": 100.0,
            "drawdown_series": drawdown,
        }

    trough_idx = drawdown.idxmin()
    # Peak is the cummax value at the trough point — walk back to find when it was set
    peak_val = cummax.loc[trough_idx]
    # Find the last date the price was at that peak before the trough
    pre_trough = price_series.loc[:trough_idx]
    peak_idx = pre_trough[pre_trough == peak_val].index[-1]

    peak_price = float(price_series.loc[peak_idx])
    trough_price = float(price_series.loc[trough_idx])

    duration = (pd.Timestamp(trough_idx) - pd.Timestamp(peak_idx)).days

    # Current drawdown from most recent high
    current_dd = float(drawdown.iloc[-1])

    # Recovery: 0% = still at trough, 100% = fully recovered
    if dd_min == 0:
        recovery_pct = 100.0
    else:
        recovery_pct = max(0.0, min(100.0, (1 - current_dd / dd_min) * 100))

    return {
        "max_drawdown": float(dd_min),
        "peak_date": peak_idx,
        "trough_date": trough_idx,
        "peak_price": peak_price,
        "trough_price": trough_price,
        "duration_days": duration,
        "current_dd": current_dd,
        "recovery_pct": recovery_pct,
        "drawdown_series": drawdown,
    }
