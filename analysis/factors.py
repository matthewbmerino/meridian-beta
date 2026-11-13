"""Factor model: market / size / value / momentum / quality exposures.

Closes the README's own admitted gap: "if your top 5 Strong Buys are all
megacap tech, the correlation assumption (rho=0.4) understates the real
risk." This module replaces that blanket assumption with a factor-aware
covariance estimate built from the cross-section of the universe.

Pure functions — no data fetching, no side effects. Accepts returns dicts +
cross-sectional characteristics, returns dicts/DataFrames. Follows the
analysis-layer invariants: NaN-safe outputs, type-hinted public functions,
degrade-don't-fabricate.

Factor returns are computed Fama-French style from the cross-section:
  - market   : the market return series (passed in, e.g. SPY)
  - size     : SMB — small-cap minus large-cap, sorted by market cap
  - value    : HML — high-value minus low-value, sorted by a value metric
               (e.g. earnings yield = EPS/price, or book-to-market)
  - momentum : WML — winners minus losers, sorted by trailing return
  - quality  : high-quality minus low-quality, sorted by a quality metric
               (e.g. ROE, or a composite)

Per-asset exposures are OLS betas of each asset's returns on the factor
returns. Portfolio exposure is the weight-weighted average. The factor-aware
covariance is  B·Σ_f·Bᵀ + D  (systematic + idiosyncratic), which replaces the
rho=0.4 shortcut in sizing.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

FACTOR_NAMES = ["market", "size", "value", "momentum", "quality"]

# Minimum observations for a meaningful OLS exposure regression.
_MIN_OBS = 60
# Minimum cross-section size to build a factor from a characteristic sort.
_MIN_CROSS_SECTION = 4


def _aligned(returns_dict: dict[str, pd.Series]) -> pd.DataFrame:
    """Align per-symbol return series to a common date index (inner join)."""
    if not returns_dict:
        return pd.DataFrame()
    df = pd.DataFrame(returns_dict)
    return df.dropna(how="all")


def _half_split(
    df: pd.DataFrame,
    characteristic: dict[str, float] | None,
    high_is: str = "high",
) -> tuple[pd.Series, pd.Series]:
    """Split the cross-section into two halves by a characteristic.

    Returns (high_series, low_series) where each is the mean return across
    the half's members per date. When ``characteristic`` is None or has fewer
    than ``_MIN_CROSS_SECTION`` members, returns (empty, empty) so callers
    degrade gracefully — there is no median-split fallback.
    """
    if characteristic is None or len(characteristic) < _MIN_CROSS_SECTION:
        return pd.Series(dtype=float), pd.Series(dtype=float)
    syms = [s for s in df.columns if s in characteristic]
    if len(syms) < _MIN_CROSS_SECTION:
        return pd.Series(dtype=float), pd.Series(dtype=float)
    vals = np.array([characteristic[s] for s in syms])
    order = np.argsort(vals)
    n = len(order)
    half = n // 2
    if high_is == "high":
        high_idx, low_idx = order[half:], order[:half]
    else:
        high_idx, low_idx = order[:half], order[half:]
    high = df[syms].iloc[:, high_idx].mean(axis=1)
    low = df[syms].iloc[:, low_idx].mean(axis=1)
    return high, low


def factor_returns(
    returns_dict: dict[str, pd.Series],
    market_returns: pd.Series,
    market_caps: dict[str, float] | None = None,
    value_metrics: dict[str, float] | None = None,
    quality_metrics: dict[str, float] | None = None,
    momentum_window: int = 126,
) -> pd.DataFrame:
    """Build the factor return series from the cross-section.

    Returns a DataFrame indexed by date with columns for each available
    factor (always ``market``; ``size``/``value``/``momentum``/``quality``
    only when their characteristic is provided and the cross-section is large
    enough). Missing factors are simply absent — callers check ``columns``.
    """
    df = _aligned(returns_dict)
    if df.empty or market_returns is None or market_returns.empty:
        return pd.DataFrame()

    aligned_market = market_returns.reindex(df.index).dropna()
    if aligned_market.empty:
        return pd.DataFrame()

    out = pd.DataFrame(index=df.index)
    out["market"] = aligned_market

    # Size factor: small minus big (SMB).
    if market_caps:
        small, big = _half_split(df, market_caps, high_is="low")
        if not small.empty:
            out["size"] = small - big

    # Value factor: high value minus low value (HML).
    if value_metrics:
        high_v, low_v = _half_split(df, value_metrics, high_is="high")
        if not high_v.empty:
            out["value"] = high_v - low_v

    # Quality factor: high quality minus low quality.
    if quality_metrics:
        high_q, low_q = _half_split(df, quality_metrics, high_is="high")
        if not high_q.empty:
            out["quality"] = high_q - low_q

    # Momentum factor: winners minus losers (WML), rebalanced on the trailing
    # window so the sort is always point-in-time.
    if momentum_window > 0 and len(df) > momentum_window:
        log_ret = np.log1p(df)
        mom = log_ret.rolling(momentum_window).sum().apply(np.expm1)
        last_mom = mom.iloc[-1].dropna()
        if len(last_mom) >= _MIN_CROSS_SECTION:
            mom_dict = last_mom.to_dict()
            winners, losers = _half_split(df, mom_dict, high_is="high")
            if not winners.empty:
                out["momentum"] = winners - losers

    return out.dropna(how="all")


def factor_exposures(
    returns_dict: dict[str, pd.Series],
    factor_df: pd.DataFrame,
) -> dict[str, dict[str, float]]:
    """OLS beta of each asset's returns on the factor returns.

    Returns {symbol: {factor: beta, "r2": float, "residual_vol": float}}.
    Assets with fewer than ``_MIN_OBS`` aligned observations are omitted.
    Degrades: a factor column with zero variance is dropped from the
    regression rather than producing a NaN beta.
    """
    if factor_df.empty or not returns_dict:
        return {}
    df = _aligned(returns_dict)
    if df.empty:
        return {}

    factors = factor_df.reindex(df.index).dropna(how="all")
    if factors.empty:
        return {}

    # Drop constant factor columns (zero variance → undefined beta).
    usable = [c for c in factors.columns if factors[c].std() > 1e-12]
    if not usable:
        return {}
    X = factors[usable].values
    Xc = np.column_stack([np.ones(len(X)), X])

    result: dict[str, dict[str, float]] = {}
    for sym in df.columns:
        y = df[sym].reindex(factors.index).dropna()
        if len(y) < _MIN_OBS:
            continue
        # Align X rows to the non-NaN y rows.
        mask = y.index
        Xm = Xc[factors.index.isin(mask)]
        ym = y.values
        if len(ym) < _MIN_OBS or len(Xm) != len(ym):
            continue
        try:
            beta, *_ = np.linalg.lstsq(Xm, ym, rcond=None)
        except np.linalg.LinAlgError:
            continue
        resid = ym - Xm @ beta
        ss_res = float((resid ** 2).sum())
        ss_tot = float(((ym - ym.mean()) ** 2).sum())
        r2 = 1.0 - ss_res / ss_tot if ss_tot > 1e-12 else 0.0
        entry: dict[str, float] = {
            "r2": round(r2, 4),
            "residual_vol": round(float(resid.std(ddof=1) * np.sqrt(252)), 4),
        }
        for i, f in enumerate(usable):
            entry[f] = round(float(beta[i + 1]), 4)
        result[sym] = entry
    return result


def portfolio_factor_exposures(
    exposures: dict[str, dict[str, float]],
    weights: dict[str, float],
) -> dict[str, float]:
    """Weight-weighted aggregate factor exposure for a portfolio.

    Returns {factor: exposure} for every factor present in any asset's
    exposure. Weights are normalized to sum to 1 over the symbols present in
    ``exposures`` (missing symbols contribute nothing).
    """
    syms = [s for s in weights if s in exposures]
    if not syms:
        return {}
    w = np.array([float(weights[s]) for s in syms])
    w = w / w.sum() if w.sum() > 0 else w
    factors = sorted({f for s in syms for f in exposures[s] if f not in ("r2", "residual_vol")})
    out: dict[str, float] = {}
    for f in factors:
        out[f] = round(float(sum(exposures[s].get(f, 0.0) * wi for s, wi in zip(syms, w))), 4)
    return out


def factor_covariance(
    returns_dict: dict[str, pd.Series],
    factor_df: pd.DataFrame,
    exposures: dict[str, dict[str, float]],
) -> pd.DataFrame:
    """Factor-aware covariance matrix: B·Σ_f·Bᵀ + D.

    B is the exposure matrix (assets × factors), Σ_f the factor covariance,
    D the diagonal idiosyncratic variance. This replaces the rho=0.4 blanket
    assumption: two megacap tech names share high market+size exposure, so
    their implied correlation is high even if their pairwise history is short.

    Returns a DataFrame indexed/columned by symbol. Symbols without a full
    exposure set are omitted. Degrades to an empty DataFrame when the factor
    model is unusable.
    """
    if factor_df.empty or not exposures:
        return pd.DataFrame()
    syms = [s for s in exposures if s in returns_dict]
    if len(syms) < 2:
        return pd.DataFrame()

    factors = [f for f in FACTOR_NAMES if f in factor_df.columns]
    if not factors:
        return pd.DataFrame()

    # Build the exposure matrix B (assets × factors), defaulting missing
    # factor betas to 0 (the asset has no measured exposure to that factor).
    B = np.zeros((len(syms), len(factors)))
    for i, s in enumerate(syms):
        for j, f in enumerate(factors):
            B[i, j] = exposures[s].get(f, 0.0)

    # Factor covariance from the factor return series.
    fcov = factor_df[factors].cov().values
    if not np.all(np.isfinite(fcov)):
        return pd.DataFrame()

    # Idiosyncratic variance from residual vol (annualized → daily).
    D = np.zeros(len(syms))
    for i, s in enumerate(syms):
        rv = exposures[s].get("residual_vol", 0.0)
        D[i] = max((rv / np.sqrt(252)) ** 2, 1e-12)

    cov = B @ fcov @ B.T + np.diag(D)
    if not np.all(np.isfinite(cov)):
        return pd.DataFrame()
    return pd.DataFrame(cov, index=syms, columns=syms)


def portfolio_sigma_from_cov(
    weights: dict[str, float],
    cov: pd.DataFrame,
) -> float:
    """Portfolio volatility from a covariance matrix.

    sigma_P = sqrt(wᵀ·Σ·w). Weights are normalized over the symbols present
    in the covariance matrix. Returns 0.0 when the matrix is unusable.
    """
    if cov.empty:
        return 0.0
    syms = [s for s in weights if s in cov.index]
    if not syms:
        return 0.0
    w = np.array([float(weights[s]) for s in syms])
    w = w / w.sum() if w.sum() > 0 else w
    sub = cov.loc[syms, syms].values
    var = float(w @ sub @ w)
    if var <= 0 or not np.isfinite(var):
        return 0.0
    return float(np.sqrt(var))
