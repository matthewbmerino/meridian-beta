"""Score-to-edge calibration for the Kelly sizing formula.

The composite signal score (0-100 from analysis/signals.py) blends RSI, trend,
MACD, Bollinger, volume, and volatility-regime indicators.  It is a measure of
*signal strength*, not a calibrated probability.  Kelly (1956) requires:

    edge = b*p - q

where p = win probability, q = 1-p, and b = avg_win / avg_loss (payoff ratio).
Using raw `(score - 50) / 50` as a proxy for edge conflates signal strength
with expected value, producing contradictions like a "STRONG BUY" name getting
a TRIM action.

This module provides three calibration strategies, selectable via
CalibrationConfig.method:

1. "logistic" (default) — A sigmoid that maps composite score -> win
   probability, with the assumption that score=50 is a coin flip and extreme
   scores saturate asymptotically.  Parameters (midpoint, steepness, floor,
   ceiling) are tunable via data/prefs.json.

2. "empirical" — Bucket-based calibration from historical forward returns.
   For each score decile (0-10, 10-20, ..., 90-100), compute the empirical
   win rate and average win/loss magnitudes from 1-year price data.  Produces
   calibrated p, b, q directly.

3. "hybrid" (recommended for production) — Uses logistic as the prior, then
   Bayesian-updates toward the empirical distribution when sufficient sample
   data is available (>= min_obs_per_bucket observations per bucket).

All three produce a (p, b, q) triple.  The Kelly edge is then b*p - q, which
replaces the naive (score-50)/50 in the sizing formula.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class CalibrationConfig:
    """Tunable parameters for score-to-edge calibration.

    All fields are serializable to / from data/prefs.json via the
    ``calibration_`` prefix (e.g. ``calibration_method``).
    """
    method: str = "hybrid"          # "logistic" | "empirical" | "hybrid"

    # --- Logistic parameters ---
    # p(score) = floor + (ceiling - floor) / (1 + exp(-steepness*(score - midpoint)))
    logistic_midpoint: float = 50.0    # score where p = 0.5
    logistic_steepness: float = 0.06   # slope at the midpoint
    logistic_floor: float = 0.30       # min win probability (score=0)
    logistic_ceiling: float = 0.72     # max win probability (score=100)

    # --- Payoff ratio ---
    # Default assumed payoff ratio (avg win / avg loss).  Overridden by
    # empirical data when available.  A ratio of 1.0 means symmetric payoffs;
    # slightly above 1.0 reflects the equity risk premium / upside skew for
    # well-selected names.
    default_payoff_ratio: float = 1.15

    # --- Empirical / hybrid parameters ---
    bucket_width: int = 10             # score bucket size (10 = deciles)
    forward_days: int = 21             # forward return horizon (trading days)
    win_threshold: float = 0.0         # return above this = "win"
    min_obs_per_bucket: int = 30       # min observations to trust empirical data
    hybrid_blend_weight: float = 0.6   # weight on empirical vs logistic prior
                                       # when sufficient data exists


# ---------------------------------------------------------------------------
# Logistic calibration
# ---------------------------------------------------------------------------

def _logistic_win_prob(score: float, cfg: CalibrationConfig) -> float:
    """Sigmoid mapping from composite score to win probability.

    Properties:
    - score=0  -> ~cfg.logistic_floor
    - score=50 -> 0.5 (by construction if midpoint=50 and floor+ceiling=1)
    - score=100 -> ~cfg.logistic_ceiling
    - Monotonically increasing, smooth, differentiable
    """
    x = cfg.logistic_steepness * (score - cfg.logistic_midpoint)
    # Clamp to avoid overflow
    x = max(-20.0, min(20.0, x))
    sigmoid = 1.0 / (1.0 + math.exp(-x))
    return cfg.logistic_floor + (cfg.logistic_ceiling - cfg.logistic_floor) * sigmoid


def logistic_edge(score: float, cfg: CalibrationConfig) -> tuple[float, float, float]:
    """Compute (p, b, q) using the logistic mapping.

    Returns:
        p: calibrated win probability
        b: payoff ratio (uses default)
        q: 1 - p
    """
    p = _logistic_win_prob(score, cfg)
    b = cfg.default_payoff_ratio
    q = 1.0 - p
    return p, b, q


# ---------------------------------------------------------------------------
# Empirical (bucket-based) calibration
# ---------------------------------------------------------------------------

@dataclass
class BucketStats:
    """Statistics for a single score bucket."""
    bucket_lo: int
    bucket_hi: int
    n_obs: int = 0
    win_rate: float = 0.5
    avg_win: float = 0.0
    avg_loss: float = 0.0
    payoff_ratio: float = 1.0


def compute_empirical_buckets(
    score_return_pairs: list[tuple[float, float]],
    cfg: CalibrationConfig,
) -> dict[int, BucketStats]:
    """Compute per-bucket win rate and payoff ratio from historical data.

    Args:
        score_return_pairs: list of (composite_score, forward_return) tuples.
            The forward return is the fractional return over cfg.forward_days
            trading days following the signal date.
        cfg: calibration configuration.

    Returns:
        dict mapping bucket lower bound -> BucketStats.
    """
    # Initialize buckets
    buckets: dict[int, list[float]] = {}
    for lo in range(0, 100, cfg.bucket_width):
        buckets[lo] = []

    # Assign returns to buckets
    for score, ret in score_return_pairs:
        lo = int(score // cfg.bucket_width) * cfg.bucket_width
        lo = min(lo, 100 - cfg.bucket_width)  # clamp score=100 into top bucket
        buckets[lo].append(ret)

    # Compute stats per bucket
    result: dict[int, BucketStats] = {}
    for lo in sorted(buckets.keys()):
        hi = lo + cfg.bucket_width
        returns = buckets[lo]
        bs = BucketStats(bucket_lo=lo, bucket_hi=hi)

        if not returns:
            result[lo] = bs
            continue

        bs.n_obs = len(returns)
        wins = [r for r in returns if r > cfg.win_threshold]
        losses = [r for r in returns if r <= cfg.win_threshold]

        bs.win_rate = len(wins) / len(returns) if returns else 0.5

        if wins:
            bs.avg_win = sum(wins) / len(wins)
        if losses:
            bs.avg_loss = abs(sum(losses) / len(losses))

        # Payoff ratio: avg_win / avg_loss, with guard against zero loss
        if bs.avg_loss > 1e-8:
            bs.payoff_ratio = bs.avg_win / bs.avg_loss
        elif bs.avg_win > 0:
            bs.payoff_ratio = 3.0   # all wins, cap at 3x
        else:
            bs.payoff_ratio = 1.0   # no data, neutral

        result[lo] = bs

    return result


def compute_dated_empirical_buckets(
    dated_pairs: list[tuple[Any, Any, float, float]],
    cutoff: Any,
    cfg: CalibrationConfig,
) -> dict[int, BucketStats]:
    """Build empirical buckets from pairs whose forward window closed by *cutoff*.

    This is the no-lookahead variant used by the walk-forward backtest. Each
    pair carries the date the forward window *ends* (the signal date plus
    ``cfg.forward_days`` trading days). A pair is only usable at time *cutoff*
    if its forward window has fully closed by then — otherwise the bucket
    stats would "know" the forward return the backtest is trying to predict.

    Args:
        dated_pairs: list of (signal_date, forward_end_date, score, forward_return).
            The forward return is the fractional return over cfg.forward_days
            trading days following the signal date.
        cutoff: only pairs with forward_end_date <= cutoff are used.
        cfg: calibration configuration.

    Returns:
        dict mapping bucket lower bound -> BucketStats (same shape as
        ``compute_empirical_buckets``). Early in a backtest few pairs qualify,
        so buckets fall below ``min_obs_per_bucket`` and the edge functions
        degrade to the logistic prior — the intended graceful behavior.
    """
    usable = [(s, r) for (_, end, s, r) in dated_pairs if end <= cutoff]
    return compute_empirical_buckets(usable, cfg)


def empirical_edge(
    score: float,
    bucket_stats: dict[int, BucketStats],
    cfg: CalibrationConfig,
) -> tuple[float, float, float]:
    """Look up (p, b, q) from pre-computed bucket statistics.

    If the relevant bucket has fewer than min_obs_per_bucket observations,
    falls back to the logistic prior.
    """
    lo = int(score // cfg.bucket_width) * cfg.bucket_width
    lo = min(lo, 100 - cfg.bucket_width)

    bs = bucket_stats.get(lo)
    if bs is None or bs.n_obs < cfg.min_obs_per_bucket:
        return logistic_edge(score, cfg)

    p = bs.win_rate
    b = bs.payoff_ratio
    q = 1.0 - p
    return p, b, q


# ---------------------------------------------------------------------------
# Hybrid calibration
# ---------------------------------------------------------------------------

def hybrid_edge(
    score: float,
    bucket_stats: dict[int, BucketStats] | None,
    cfg: CalibrationConfig,
    operator_bucket_stats: dict[int, BucketStats] | None = None,
) -> tuple[float, float, float]:
    """Blend logistic prior with empirical data when available.

    When ``operator_bucket_stats`` is provided and the relevant bucket has
    >= min_obs_per_bucket observations, the operator's own resolved outcomes
    (their measured base rate from the Decision Book) are blended with the
    logistic prior — the personal calibration win. Falls back to the market
    ``bucket_stats``, then entirely to the logistic prior.

    The blend applies independently to p and b:
        p_hybrid = w * p_empirical + (1-w) * p_logistic
        b_hybrid = w * b_empirical + (1-w) * b_default
    """
    p_logistic, b_logistic, _ = logistic_edge(score, cfg)

    stats = operator_bucket_stats if operator_bucket_stats is not None else bucket_stats

    if stats is None:
        return p_logistic, b_logistic, 1.0 - p_logistic

    lo = int(score // cfg.bucket_width) * cfg.bucket_width
    lo = min(lo, 100 - cfg.bucket_width)
    bs = stats.get(lo)

    if bs is None or bs.n_obs < cfg.min_obs_per_bucket:
        return p_logistic, b_logistic, 1.0 - p_logistic

    # Blend weight scales with data quantity: at min_obs we use
    # hybrid_blend_weight, at 3x min_obs we use the full weight.
    data_confidence = min(1.0, bs.n_obs / (3.0 * cfg.min_obs_per_bucket))
    w = cfg.hybrid_blend_weight * data_confidence

    p = w * bs.win_rate + (1.0 - w) * p_logistic
    b = w * bs.payoff_ratio + (1.0 - w) * b_logistic
    q = 1.0 - p
    return p, b, q


# ---------------------------------------------------------------------------
# Unified entry point
# ---------------------------------------------------------------------------

def calibrate_pbq(
    score: float,
    cfg: CalibrationConfig | None = None,
    bucket_stats: dict[int, BucketStats] | None = None,
    operator_bucket_stats: dict[int, BucketStats] | None = None,
) -> tuple[float, float, float]:
    """Compute the calibrated (p, b, q) triple for a composite score.

    This is the single entry point for calibration transparency.  It
    replaces the naive ``max(0, (score - 50) / 50)`` with a principled
    ``b*p - q`` derived from calibrated win probability and payoff ratio.

    Args:
        score: composite signal score (0-100)
        cfg: calibration config (uses defaults if None)
        bucket_stats: pre-computed empirical bucket data (optional;
            only used by "empirical" and "hybrid" methods)
        operator_bucket_stats: the operator's own resolved-outcome buckets
            from the Decision Book (optional; preferred over market buckets
            by the hybrid method when the sample is sufficient)

    Returns:
        (p, b, q): calibrated win probability, payoff ratio, loss probability.
    """
    cfg = cfg or CalibrationConfig()

    if cfg.method == "logistic":
        return logistic_edge(score, cfg)
    elif cfg.method == "empirical":
        stats = operator_bucket_stats if operator_bucket_stats is not None else bucket_stats
        if stats is None:
            return logistic_edge(score, cfg)
        return empirical_edge(score, stats, cfg)
    elif cfg.method == "hybrid":
        return hybrid_edge(score, bucket_stats, cfg, operator_bucket_stats)
    else:
        # Unknown method, fall back to logistic
        return logistic_edge(score, cfg)


def calibrate_edge(
    score: float,
    cfg: CalibrationConfig | None = None,
    bucket_stats: dict[int, BucketStats] | None = None,
    operator_bucket_stats: dict[int, BucketStats] | None = None,
) -> float:
    """Compute the calibrated Kelly edge for a given composite score.

    Kelly edge = b*p - q, floored at 0.0 (no negative sizing).  See
    ``calibrate_pbq`` for the underlying (p, b, q) triple.
    """
    p, b, q = calibrate_pbq(score, cfg, bucket_stats, operator_bucket_stats)
    edge = b * p - q
    return max(0.0, edge)


def calibration_config_from_prefs(prefs_dict: dict) -> CalibrationConfig:
    """Build a CalibrationConfig from the user-prefs dict.

    Keys use the ``calibration_`` prefix. Missing keys fall back to
    CalibrationConfig defaults.
    """
    defaults = CalibrationConfig()
    return CalibrationConfig(
        method=str(prefs_dict.get("calibration_method", defaults.method)),
        logistic_midpoint=float(prefs_dict.get(
            "calibration_logistic_midpoint", defaults.logistic_midpoint)),
        logistic_steepness=float(prefs_dict.get(
            "calibration_logistic_steepness", defaults.logistic_steepness)),
        logistic_floor=float(prefs_dict.get(
            "calibration_logistic_floor", defaults.logistic_floor)),
        logistic_ceiling=float(prefs_dict.get(
            "calibration_logistic_ceiling", defaults.logistic_ceiling)),
        default_payoff_ratio=float(prefs_dict.get(
            "calibration_default_payoff_ratio", defaults.default_payoff_ratio)),
        bucket_width=int(prefs_dict.get(
            "calibration_bucket_width", defaults.bucket_width)),
        forward_days=int(prefs_dict.get(
            "calibration_forward_days", defaults.forward_days)),
        win_threshold=float(prefs_dict.get(
            "calibration_win_threshold", defaults.win_threshold)),
        min_obs_per_bucket=int(prefs_dict.get(
            "calibration_min_obs_per_bucket", defaults.min_obs_per_bucket)),
        hybrid_blend_weight=float(prefs_dict.get(
            "calibration_hybrid_blend_weight", defaults.hybrid_blend_weight)),
    )
