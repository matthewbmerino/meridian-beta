"""Cross-asset relationship discovery.

Answers: which tickers lead/lag each other? Which move together? Where are
the statistical linkages that reveal supply chains, capital flows, and
thematic exposure before consensus pricing reflects them?

All functions are pure — they accept returns dicts/DataFrames and return
structured results. No data fetching.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def cross_correlation(
    returns_a: pd.Series,
    returns_b: pd.Series,
    max_lag: int = 15,
) -> dict[int, float]:
    """Cross-correlation between two return series at different lag offsets.

    Positive lag means A leads B (A at t predicts B at t+lag).
    Returns {lag: correlation} for lags in [-max_lag, +max_lag].
    """
    aligned = pd.concat([returns_a, returns_b], axis=1, join="inner").dropna()
    if len(aligned) < 30:
        return {}
    a, b = aligned.iloc[:, 0].values, aligned.iloc[:, 1].values
    result: dict[int, float] = {}
    n = len(a)
    for lag in range(-max_lag, max_lag + 1):
        if lag > 0:
            x, y = a[: n - lag], b[lag:]
        elif lag < 0:
            x, y = a[-lag:], b[: n + lag]
        else:
            x, y = a, b
        if len(x) < 20:
            continue
        c = np.corrcoef(x, y)[0, 1]
        if np.isfinite(c):
            result[lag] = round(float(c), 4)
    return result


def find_lead_lag_pairs(
    returns_dict: dict[str, pd.Series],
    min_corr: float = 0.3,
    max_lag: int = 10,
) -> list[dict]:
    """Find all pairs where one ticker statistically leads another.

    Returns list of {leader, follower, lag, correlation, concurrent_corr}.
    Only includes pairs where lagged correlation > concurrent + min_improvement.
    """
    symbols = list(returns_dict.keys())
    pairs: list[dict] = []
    min_improvement = 0.05

    for i, sym_a in enumerate(symbols):
        for sym_b in symbols[i + 1 :]:
            cc = cross_correlation(returns_dict[sym_a], returns_dict[sym_b], max_lag)
            if not cc:
                continue
            concurrent = cc.get(0, 0)
            best_lag = max(cc, key=lambda k: abs(cc[k]))
            best_corr = cc[best_lag]

            if abs(best_corr) < min_corr:
                continue
            if abs(best_corr) < abs(concurrent) + min_improvement:
                continue

            if best_lag > 0:
                leader, follower, lag = sym_a, sym_b, best_lag
            else:
                leader, follower, lag = sym_b, sym_a, -best_lag

            pairs.append({
                "leader": leader,
                "follower": follower,
                "lag_days": lag,
                "lagged_corr": round(best_corr, 4),
                "concurrent_corr": round(concurrent, 4),
                "improvement": round(abs(best_corr) - abs(concurrent), 4),
            })

    pairs.sort(key=lambda p: abs(p["lagged_corr"]), reverse=True)
    return pairs


def correlation_clusters(
    returns_dict: dict[str, pd.Series],
    n_clusters: int = 5,
) -> dict[str, int]:
    """Cluster tickers by return correlation similarity.

    Uses agglomerative clustering on (1 - |correlation|) distance with
    average linkage (UPGMA). Pure numpy — no scipy dependency. The exact
    linkage method is a heuristic for grouping correlated assets; average
    linkage is robust to the outliers that single linkage chases and less
    prone to the chaining of complete linkage.

    Returns {symbol: cluster_id}.
    """
    symbols = list(returns_dict.keys())
    if len(symbols) < n_clusters:
        return {s: 0 for s in symbols}

    df = pd.DataFrame(returns_dict).dropna(how="any")
    if len(df) < 30:
        return {s: 0 for s in symbols}

    corr = df.corr().values
    dist = 1.0 - np.abs(corr)
    np.fill_diagonal(dist, 0)

    labels = _agglomerative_average_linkage(dist, n_clusters)
    return {sym: int(lbl) for sym, lbl in zip(symbols, labels)}


def _agglomerative_average_linkage(dist: np.ndarray, n_clusters: int) -> np.ndarray:
    """Average-linkage (UPGMA) agglomerative clustering on a distance matrix.

    Returns an array of cluster ids (0..n_clusters-1) aligned to the input
    rows. Pure numpy implementation of the classic algorithm: repeatedly merge
    the two closest clusters, updating distances by the weighted average of
    the merged clusters' distances to every other cluster.
    """
    n = dist.shape[0]
    # Each active cluster is a set of original indices.
    clusters: list[set[int]] = [{i} for i in range(n)]
    # Working distance matrix between active clusters (indexed by position in `clusters`).
    # Diagonal is inf so argmin never selects a cluster against itself.
    d = dist.copy()
    np.fill_diagonal(d, np.inf)

    while len(clusters) > n_clusters:
        # Find the closest pair of active clusters.
        i, j = np.unravel_index(np.argmin(d), d.shape)
        if i > j:
            i, j = j, i

        merged = clusters[i] | clusters[j]
        merged_size = len(merged)

        # New distance from the merged cluster to every other active cluster k:
        # weighted average of d[i][k] and d[j][k] by cluster size (UPGMA).
        new_row = np.empty(len(clusters))
        for k in range(len(clusters)):
            if k == i or k == j:
                new_row[k] = np.inf
            else:
                new_row[k] = (
                    len(clusters[i]) * d[i, k] + len(clusters[j]) * d[j, k]
                ) / merged_size

        # Rebuild the active cluster list and distance matrix without i and j.
        keep = [k for k in range(len(clusters)) if k != i and k != j]
        clusters = [clusters[k] for k in keep]
        d = d[np.ix_(keep, keep)]

        # Append the merged cluster.
        new_row = new_row[keep]
        clusters.append(merged)
        d = np.vstack([np.hstack([d, new_row.reshape(-1, 1)]), np.append(new_row, np.inf).reshape(1, -1)])

    # Assign cluster ids by final cluster order.
    labels = np.zeros(n, dtype=int)
    for cid, cluster in enumerate(clusters):
        for idx in cluster:
            labels[idx] = cid
    return labels


def correlation_network(
    returns_dict: dict[str, pd.Series],
    min_corr: float = 0.4,
    window: int = 63,
) -> dict:
    """Build correlation network: nodes = symbols, edges = significant correlations.

    Returns {
        nodes: [{symbol, cluster}],
        edges: [{source, target, correlation, rolling_change}],
        clusters: {cluster_id: [symbols]},
    }
    """
    symbols = list(returns_dict.keys())
    df = pd.DataFrame(returns_dict).dropna(how="any")
    if len(df) < window + 10:
        return {"nodes": [], "edges": [], "clusters": {}}

    full_corr = df.corr()
    recent = df.tail(window)
    recent_corr = recent.corr() if len(recent) >= 20 else full_corr

    n_clusters = max(2, min(8, len(symbols) // 4))
    clusters = correlation_clusters(returns_dict, n_clusters)

    cluster_groups: dict[int, list[str]] = {}
    for sym, cid in clusters.items():
        cluster_groups.setdefault(cid, []).append(sym)

    nodes = [{"symbol": s, "cluster": clusters.get(s, 0)} for s in symbols]
    edges: list[dict] = []

    for i, sym_a in enumerate(symbols):
        for j in range(i + 1, len(symbols)):
            sym_b = symbols[j]
            c_full = float(full_corr.loc[sym_a, sym_b])
            c_recent = float(recent_corr.loc[sym_a, sym_b])

            if abs(c_recent) < min_corr:
                continue

            edges.append({
                "source": sym_a,
                "target": sym_b,
                "correlation": round(c_recent, 4),
                "full_period_corr": round(c_full, 4),
                "rolling_change": round(c_recent - c_full, 4),
            })

    edges.sort(key=lambda e: abs(e["correlation"]), reverse=True)

    return {
        "nodes": nodes,
        "edges": edges,
        "clusters": {str(k): v for k, v in cluster_groups.items()},
    }


def correlation_regime_shifts(
    returns_dict: dict[str, pd.Series],
    window: int = 63,
    lookback: int = 252,
    threshold: float = 0.15,
) -> list[dict]:
    """Detect correlation regime changes: pairs whose recent rolling
    correlation has shifted materially from their trailing baseline.

    A pair's *recent* correlation is the rolling correlation over the last
    ``window`` days; its *baseline* is the average correlation over the
    preceding ``lookback - window`` days. A shift beyond ``threshold`` in
    either direction is a regime change — the pair's diversification
    benefit (or concentration risk) has structurally changed.

    Returns list of {pair, symbol_a, symbol_b, recent, baseline, delta,
    direction} sorted by |delta| descending. ``direction`` is
    "CONVERGING" (correlation rose) or "DIVERGING" (correlation fell).
    """
    symbols = list(returns_dict.keys())
    shifts: list[dict] = []

    for i, sym_a in enumerate(symbols):
        for sym_b in symbols[i + 1 :]:
            aligned = pd.concat(
                [returns_dict[sym_a], returns_dict[sym_b]], axis=1, join="inner"
            ).dropna()
            if len(aligned) < lookback:
                continue

            recent = aligned.tail(window)
            baseline = aligned.iloc[: -window]

            c_recent = float(recent.iloc[:, 0].corr(recent.iloc[:, 1]))
            c_baseline = float(baseline.iloc[:, 0].corr(baseline.iloc[:, 1]))

            if not np.isfinite(c_recent) or not np.isfinite(c_baseline):
                continue

            delta = c_recent - c_baseline
            if abs(delta) < threshold:
                continue

            shifts.append({
                "pair": f"{sym_a}-{sym_b}",
                "symbol_a": sym_a,
                "symbol_b": sym_b,
                "recent": round(c_recent, 4),
                "baseline": round(c_baseline, 4),
                "delta": round(delta, 4),
                "direction": "CONVERGING" if delta > 0 else "DIVERGING",
            })

    shifts.sort(key=lambda s: abs(s["delta"]), reverse=True)
    return shifts


def relative_strength_divergences(
    returns_dict: dict[str, pd.Series],
    benchmark_returns: pd.Series,
    lookback: int = 63,
) -> list[dict]:
    """Find tickers diverging from their normal relative strength vs benchmark.

    Returns list of {symbol, rs_current, rs_historical, divergence, signal}.
    Positive divergence = currently outperforming vs historical pattern.
    """
    results: list[dict] = []
    bench = benchmark_returns.dropna()

    for sym, ret in returns_dict.items():
        aligned = pd.concat([ret, bench], axis=1, join="inner").dropna()
        if len(aligned) < lookback * 2:
            continue

        recent = aligned.tail(lookback)
        historical = aligned.iloc[:-lookback]

        rs_current = float(recent.iloc[:, 0].mean() / max(recent.iloc[:, 1].mean(), 1e-8))
        rs_historical = float(historical.iloc[:, 0].mean() / max(historical.iloc[:, 1].mean(), 1e-8))

        divergence = rs_current - rs_historical

        if abs(divergence) < 0.1:
            signal = "INLINE"
        elif divergence > 0:
            signal = "OUTPERFORMING"
        else:
            signal = "UNDERPERFORMING"

        results.append({
            "symbol": sym,
            "rs_current": round(rs_current, 3),
            "rs_historical": round(rs_historical, 3),
            "divergence": round(divergence, 3),
            "signal": signal,
        })

    results.sort(key=lambda r: abs(r["divergence"]), reverse=True)
    return results


def cointegration_pairs(
    price_dict: dict[str, pd.Series],
    p_threshold: float = 0.05,
) -> list[dict]:
    """Find cointegrated pairs (mean-reverting spreads) for pairs trading.

    Uses Engle-Granger two-step test. Returns pairs where the spread
    is stationary (p < threshold).
    """
    try:
        from statsmodels.tsa.stattools import adfuller
    except ImportError:
        return []

    symbols = list(price_dict.keys())
    pairs: list[dict] = []

    for i, sym_a in enumerate(symbols):
        for j in range(i + 1, len(symbols)):
            sym_b = symbols[j]
            aligned = pd.concat(
                [price_dict[sym_a], price_dict[sym_b]], axis=1, join="inner"
            ).dropna()
            if len(aligned) < 60:
                continue

            a, b = aligned.iloc[:, 0], aligned.iloc[:, 1]

            # OLS regression: A = beta * B + alpha + residual
            beta = float(np.cov(a, b)[0, 1] / np.var(b))
            spread = a - beta * b
            spread_mean = float(spread.mean())
            spread_std = float(spread.std())

            try:
                adf_stat, p_value, *_ = adfuller(spread.values, maxlag=10)
            except Exception:
                continue

            if p_value >= p_threshold:
                continue

            current_z = float((spread.iloc[-1] - spread_mean) / spread_std) if spread_std > 0 else 0

            pairs.append({
                "symbol_a": sym_a,
                "symbol_b": sym_b,
                "hedge_ratio": round(beta, 4),
                "p_value": round(p_value, 4),
                "adf_stat": round(float(adf_stat), 4),
                "spread_z": round(current_z, 3),
                "half_life": _half_life(spread),
                "signal": "LONG_SPREAD" if current_z < -2 else "SHORT_SPREAD" if current_z > 2 else "NEUTRAL",
            })

    pairs.sort(key=lambda p: p["p_value"])
    return pairs


def _half_life(spread: pd.Series) -> float | None:
    """Estimate mean-reversion half-life of a spread via AR(1) regression."""
    spread = spread.dropna()
    if len(spread) < 20:
        return None
    delta = spread.diff().dropna()
    lagged = spread.shift(1).dropna()
    aligned = pd.concat([delta, lagged], axis=1, join="inner")
    x, y = aligned.iloc[:, 1].values, aligned.iloc[:, 0].values

    phi = float(np.cov(x, y)[0, 1] / np.var(x)) if np.var(x) > 0 else 0
    if phi >= 0:
        return None
    hl = -np.log(2) / np.log(1 + phi)
    return round(float(hl), 1) if np.isfinite(hl) and hl > 0 else None
