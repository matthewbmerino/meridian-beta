"""Desk scorecard — how much skill does the operator actually have?

Pure functions over resolved outcome records (data/outcomes.py). This is the
institutional currency of the Decision Book:

  - **IC** (information coefficient) — corr(score, forward return) across
    resolved outcomes at a horizon (Grinold-Kahn: skill = IC x sqrt(breadth)).
  - **Decision Sharpe** — mean(excess return) / std(excess return), where
    excess is SIGNED BY INTENT: a long call's forward return minus SPY, a
    defensive call's benchmark minus return (value booked by dodging the
    decline). Without the flip the mean punishes the calls it should credit.
  - **Two-sided ledger** — adds vs defensive, each vs benchmark.
  - **Skill vs luck** — Wilson interval on the win rate: only a result whose
    lower bound exceeds 0.5 is certified as skill, not noise. The interval is
    reported, not just the bound, so thin data reads as thin.
  - **Conviction hierarchy** — hit rate by conviction tier (1-5). If 5s don't
    beat 1s, conviction is noise.
  - **Cell accounting** — every horizon reports how many cells were resolved,
    pending, no_data and unclassified. n counts SCORED cells only; the rest are
    visible beside it rather than silently dropped.

Honesty contract (root AGENTS.md, composite rule): every metric returns
{value, n, significant, unverified, state, note} — a small sample is a thin-data
row, never a fabricated confidence. No NaN in output dicts (use None).

Also exposes ``operator_bucket_stats`` — the operator's own win-rate buckets in
the exact shape analysis.calibration consumes, so the sizing pipeline's
hybrid_edge can blend the operator's base rate with the logistic prior.
"""

from __future__ import annotations

import math

import numpy as np

from data.outcomes import (
    CELL_RESOLVED,
    DEFAULT_HORIZONS,
    cell_status,
    classify_intent,
    horizon_cell,
)


def _wilson_lower_bound(wins: int, n: int, z: float = 1.96) -> float | None:
    """Wilson score-interval lower bound on a win rate. None when n == 0."""
    lo, _hi = _wilson_interval(wins, n, z)
    return lo


def _wilson_interval(wins: int, n: int, z: float = 1.96):
    """Full Wilson interval (lo, hi). (None, None) when n == 0."""
    if n <= 0:
        return None, None
    p = wins / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def _safe_ic(x: list[float], y: list[float]) -> float | None:
    """Pearson correlation between decision score and forward return, NaN-guarded.

    The information coefficient (Grinold-Kahn skill measure). None when
    degenerated (< 5 non-NaN pairs, or a zero-variance series).
    """
    if len(x) < 5 or len(x) != len(y):
        return None
    a = np.asarray(x, dtype=float)
    b = np.asarray(y, dtype=float)
    mask = ~(np.isnan(a) | np.isnan(b))
    a, b = a[mask], b[mask]
    if len(a) < 5 or np.std(a) == 0 or np.std(b) == 0:
        return None
    return float(np.corrcoef(a, b)[0, 1])


def _metric(value, n: int, min_obs: int) -> dict:
    """Wrap a value in the honesty contract (thin data is never fact)."""
    insufficient = n < min_obs
    return {
        "value": value,
        "n": n,
        "significant": not insufficient,
        "unverified": insufficient,
        "state": "insufficient" if insufficient else "ok",
        "note": (f"insufficient — {n} of {min_obs} observations" if insufficient else None),
    }


def _cell_counts(outcomes: list[dict], horizon: str) -> dict:
    """Every cell at a horizon by its own status — the honest denominator."""
    counts = {"resolved": 0, "pending": 0, "no_data": 0, "unclassified": 0}
    for o in outcomes:
        cell = horizon_cell(o, horizon)
        if cell is None:
            counts["pending"] += 1
            continue
        st = str(cell.get("status") or CELL_RESOLVED)
        counts[st] = counts.get(st, 0) + 1
    return counts


def _resolved_at(outcomes: list[dict], horizon: str) -> list[tuple[dict, dict]]:
    """[(outcome, horizon_cell)] for cells actually SCORED at this horizon.

    Not merely "a dict with a return": an ``unclassified`` cell carries a
    return but no verdict, so counting it would inflate n while contributing
    nothing to the win rate.
    """
    out = []
    for o in outcomes:
        if cell_status(o, horizon) != CELL_RESOLVED:
            continue
        cell = horizon_cell(o, horizon)
        if not isinstance(cell, dict) or cell.get("ret") is None:
            continue
        out.append((o, cell))
    return out


def _intent(o: dict) -> str:
    """The shared classifier — never a second, divergent rule."""
    return classify_intent(o.get("action", ""), o.get("direction"))


def _defensive(o: dict) -> bool:
    """True for trim/sell/close/reduce/exit decisions (two-sided ledger)."""
    return _intent(o) == "defensive"


def _excess(e: dict, o: dict | None = None) -> float | None:
    """Forward return minus benchmark, SIGNED BY INTENT.

    A defensive decision that dodged a decline is a win, so its excess is what
    it avoided: benchmark minus return. Without the flip, the Sharpe mean
    punishes exactly the decisions it should credit.
    """
    if e.get("bench_ret") is None:
        return None
    raw = float(e["ret"]) - float(e["bench_ret"])
    if o is not None and _defensive(o):
        return -raw
    return raw


def _breakout_ledger(entries: list[tuple[dict, dict]], defensive: bool) -> dict:
    sub = [(o, e) for o, e in entries if _defensive(o) == defensive]
    if not sub:
        return {"n": 0, "win_rate": None, "excess_mean": None}
    wins = sum(1 for _, e in sub if e.get("win"))
    excesses = [x for o, e in sub if (x := _excess(e, o)) is not None]
    return {
        "n": len(sub),
        "win_rate": round(wins / len(sub), 4),
        "excess_mean": round(float(np.mean(excesses)), 6) if excesses else None,
    }


def _slice(entries: list[tuple[dict, dict]], key_fn) -> dict:
    """{bucket: {n, win_rate}} grouped by a key (regime/theme/conviction)."""
    buckets: dict[str, list] = {}
    for o, e in entries:
        k = key_fn(o)
        buckets.setdefault(str(k) if k else "unknown", []).append((o, e))
    out = {}
    for k, sub in sorted(buckets.items(), key=lambda kv: -len(kv[1])):
        wins = sum(1 for _, e in sub if e.get("win"))
        out[k] = {"n": len(sub), "win_rate": round(wins / len(sub), 4)}
    return out


def scorecard(outcomes: list[dict], min_obs: int = 10) -> dict:
    """Full desk scorecard keyed by horizon.

    Returns {horizon: {n, cells, win_rate, ic, decision_sharpe, sharpe_basis,
    wilson_lower, wilson_hi, adds, defensive, by_regime, by_theme,
    by_conviction}} — scalars wrapped in the honesty metric. Thin data
    (< min_obs) is reported, never certified.
    """
    result: dict[str, dict] = {}
    for h in DEFAULT_HORIZONS:
        counts = _cell_counts(outcomes, h)
        entries = _resolved_at(outcomes, h)
        n = len(entries)
        if n == 0:
            result[h] = {
                "n": 0,
                "cells": counts,
                "win_rate": _metric(None, 0, min_obs),
                "ic": _metric(None, 0, min_obs),
                "wilson_lower": _metric(None, 0, min_obs),
                "wilson_hi": None,
                "decision_sharpe": _metric(None, 0, min_obs),
                "sharpe_basis": f"per_{h}_period",
                "adds": {"n": 0, "win_rate": None, "excess_mean": None},
                "defensive": {"n": 0, "win_rate": None, "excess_mean": None},
                "by_regime": {},
                "by_theme": {},
                "by_conviction": {},
            }
            continue

        wins = sum(1 for _, e in entries if e.get("win"))
        scores = []
        rets = []
        for o, e in entries:
            s = (o.get("anchor") or {}).get("score")
            scores.append(float(s) if s is not None else float("nan"))
            rets.append(float(e["ret"]))
        excess = [x for o, e in entries if (x := _excess(e, o)) is not None]
        dsharpe = None
        if len(excess) >= 2:
            sd = float(np.std(excess))
            if sd > 1e-9:
                dsharpe = float(np.mean(excess)) / sd

        wilson = _wilson_lower_bound(wins, n)
        w_lo, w_hi = _wilson_interval(wins, n)

        result[h] = {
            "n": n,
            "cells": counts,
            "win_rate": _metric(round(wins / n, 4), n, min_obs),
            "ic": _metric(_safe_ic(scores, rets), n, min_obs),
            "decision_sharpe": _metric(dsharpe, n, min_obs),
            # The basis is part of the number: a 1-day Sharpe and a 5-day Sharpe
            # are not the same unit, so they are never compared bare.
            "sharpe_basis": f"per_{h}_period",
            "wilson_lower": _metric(round(wilson, 4) if wilson is not None else None, n, min_obs),
            "wilson_hi": round(w_hi, 4) if w_hi is not None else None,
            "adds": _breakout_ledger(entries, defensive=False),
            "defensive": _breakout_ledger(entries, defensive=True),
            "by_regime": _slice(entries, lambda o: (o.get("anchor") or {}).get("regime")),
            "by_theme": _slice(entries, lambda o: (o.get("anchor") or {}).get("theme")),
            "by_conviction": _slice(entries, lambda o: o.get("conviction")),
        }
    return result


def operator_bucket_stats(
    outcomes: list[dict], horizon: str = "20d", bucket_width: int = 10, min_obs: int = 30
):
    """Operator win-rate buckets in analysis.calibration's BucketStats shape.

    Feeds the sizing pipeline's hybrid_edge with the operator's own base rate.
    Returns None when too few resolved observations exist — the caller must
    degrade to the logistic prior (never fabricate a bucket from thin data).
    """
    from analysis.calibration import CalibrationConfig, compute_empirical_buckets

    pairs = []
    for o, e in _resolved_at(outcomes, horizon):
        s = (o.get("anchor") or {}).get("score")
        if s is None:
            continue
        pairs.append((float(s), float(e["ret"])))
    if len(pairs) < min_obs:
        return None
    cfg = CalibrationConfig(bucket_width=bucket_width)
    return compute_empirical_buckets(pairs, cfg)
