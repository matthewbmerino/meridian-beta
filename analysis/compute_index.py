"""Compute Index — the spot price of compute as a financialized commodity.

Pure functions — no data/ or backend/ imports (analysis-layer invariant #1).

The thesis: compute is becoming a financialized commodity. NVIDIA raising
PE/bank funding and depreciation schedules extending (smaller models don't
need the full new stack) mean the *price of compute* is a tradeable signal.
This module builds a defensible index from per-GPU rental prices:

    index_t = 100 × Σ(w_i × price_median_i,t) / Σ(w_i × price_median_i,base)
    where w_i = tflops_i / Σ(tflops_j)   (efficiency weight)

A GPU that delivers more compute per dollar gets more weight — the index
measures the price of *compute*, not the price of a GPU box.

Companion metrics: median $/GPU-hr, utilization %, TFLOPS-per-dollar
(efficiency), and the p90/p10 price spread (market tightness).

The index follows the Layer-4 composite contract:
{value, confidence, inputs[], change_reason, is_predictive: true, trend?}
with hysteresis to prevent state flicker.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from pathlib import Path

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Hysteresis state (persisted, same pattern as analysis/composites.py)
# ---------------------------------------------------------------------------

_STATE_PATH = Path(__file__).resolve().parent.parent / "data" / "compute_index_state.json"

_HYSTERESIS_MARGIN = 5.0
_CONFIRM_COUNT = 2

# Regime labels for the index's market-tightness state. Thresholds are the
# p90/p10 price-spread values that separate the states.
_TIGHTNESS_THRESHOLDS = [1.5, 2.5, 4.0]
_TIGHTNESS_LABELS = ["Tight", "Normal", "Loose", "Very Loose"]


def _load_state() -> dict:
    try:
        if _STATE_PATH.exists():
            return json.loads(_STATE_PATH.read_text())
    except Exception as e:
        log.warning("Compute-index hysteresis state load failed: %s", e)
    return {}


def _save_state(state: dict):
    try:
        _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _STATE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2))
        tmp.rename(_STATE_PATH)
    except Exception as e:
        log.warning("Compute-index hysteresis state save failed: %s", e)


def _apply_hysteresis(key: str, raw_label: str, score: float,
                      thresholds: Sequence[float], labels: Sequence[str],
                      state: dict, margin: float = _HYSTERESIS_MARGIN) -> tuple[str, str | None]:
    """Apply hysteresis to prevent flickering between states.

    Returns (final_label, trend) where trend is "improving" | "deteriorating" | None.
    Mirrors analysis/composites.py _apply_hysteresis. ``margin`` is the
    score-scale-appropriate transition margin (5.0 for 0-100 composites; the
    tightness composite uses a smaller margin because its score is a price
    spread in the ~1-5 range).
    """
    prev = state.get(key, {})
    prev_label = prev.get("label")
    prev_score = prev.get("score")
    consecutive = prev.get("consecutive", 0)

    trend = None
    if prev_score is not None:
        delta = score - prev_score
        if abs(delta) > 1.5:
            trend = "deteriorating" if delta > 0 else "improving"

    if prev_label is None or prev_label == raw_label:
        state[key] = {"label": raw_label, "score": round(score, 1), "consecutive": consecutive + 1}
        return raw_label, trend

    prev_idx = labels.index(prev_label) if prev_label in labels else -1
    raw_idx = labels.index(raw_label) if raw_label in labels else -1
    if prev_idx < 0 or raw_idx < 0:
        state[key] = {"label": raw_label, "score": round(score, 1), "consecutive": 1}
        return raw_label, trend

    boundary = thresholds[min(prev_idx, raw_idx)] if min(prev_idx, raw_idx) < len(thresholds) else 50
    margin_ok = abs(score - boundary) > margin

    if raw_label == prev.get("pending_label"):
        pending_count = prev.get("pending_count", 0) + 1
    else:
        pending_count = 1

    if margin_ok and pending_count >= _CONFIRM_COUNT:
        state[key] = {"label": raw_label, "score": round(score, 1), "consecutive": 1}
        return raw_label, trend

    state[key] = {
        "label": prev_label, "score": round(score, 1),
        "consecutive": consecutive + 1,
        "pending_label": raw_label, "pending_count": pending_count,
    }
    return prev_label, trend


# ---------------------------------------------------------------------------
# Core composite shape
# ---------------------------------------------------------------------------

def _composite(value, confidence: float, inputs: list[dict],
               change_reason: str, trend: str | None = None) -> dict:
    """Standard composite return shape (matches analysis/composites.py)."""
    result = {
        "value": value,
        "confidence": round(min(1.0, max(0.0, confidence)), 2),
        "inputs": inputs,
        "change_reason": change_reason,
        "is_predictive": True,
    }
    if trend is not None:
        result["trend"] = trend
    return result


# ---------------------------------------------------------------------------
# Index math
# ---------------------------------------------------------------------------

def _tflops_weights(tflops: dict[str, float]) -> dict[str, float]:
    """Efficiency weights: w_i = tflops_i / Σ(tflops_j)."""
    total = sum(tflops.values())
    if total <= 0:
        return {}
    return {gpu: t / total for gpu, t in tflops.items()}


def compute_index_value(
    current_prices: dict[str, float],
    base_prices: dict[str, float],
    tflops: dict[str, float],
) -> float | None:
    """Compute the TFLOPS-weighted index value.

    current_prices: {gpu: median $/GPU-hr} for the current snapshot.
    base_prices:    {gpu: median $/GPU-hr} at the index base date.
    tflops:         {gpu: TFLOPS rating} for efficiency weighting.

    Returns the index value (100 at base) or None when the inputs are
    insufficient (no overlapping GPUs, no weights, zero base).
    """
    weights = _tflops_weights(tflops)
    if not weights:
        return None

    common = [g for g in weights if g in current_prices and g in base_prices]
    if not common:
        return None

    base_num = sum(weights[g] * base_prices[g] for g in common)
    if base_num <= 0:
        return None

    cur_num = sum(weights[g] * current_prices[g] for g in common)
    return round(100.0 * cur_num / base_num, 2)


def compute_index(
    current_prices: dict[str, float],
    base_prices: dict[str, float],
    tflops: dict[str, float],
    utilization: dict[str, float] | None = None,
    price_spread: float | None = None,
    change_reason: str = "Compute market update",
) -> dict:
    """Compute the Compute Index composite.

    Returns the Layer-4 composite contract:
    {value, confidence, inputs[], change_reason, is_predictive: true, trend?}
    plus the companion metrics (median price, utilization, efficiency,
    tightness) and the raw index value.

    Degrades: when the index can't be computed (insufficient data), returns
    a composite with value None and a change_reason explaining why — never
    fabricates a number.
    """
    value = compute_index_value(current_prices, base_prices, tflops)
    if value is None:
        return _composite(
            None, 0.0,
            [{"name": "gpu_prices", "value": len(current_prices)},
             {"name": "base_prices", "value": len(base_prices)}],
            "Insufficient GPU price data to compute the index",
        )

    inputs = [
        {"name": "gpus", "value": len([g for g in tflops if g in current_prices])},
        {"name": "median_price", "value": round(sum(current_prices.values()) / len(current_prices), 2)},
    ]
    if utilization:
        inputs.append({"name": "utilization", "value": round(sum(utilization.values()) / len(utilization), 1)})
    if price_spread is not None:
        inputs.append({"name": "price_spread_p90_p10", "value": round(price_spread, 2)})

    # Confidence: more overlapping GPUs = higher confidence.
    common = [g for g in tflops if g in current_prices and g in base_prices]
    confidence = min(0.95, 0.4 + 0.1 * len(common))

    result = _composite(value, confidence, inputs, change_reason)
    result["median_price"] = round(sum(current_prices.values()) / len(current_prices), 2)
    if utilization:
        result["utilization"] = round(sum(utilization.values()) / len(utilization), 1)
    if price_spread is not None:
        result["price_spread"] = round(price_spread, 2)
    return result


def compute_index_series(
    history: list[dict],
    tflops: dict[str, float],
) -> list[dict]:
    """Build the full index time-series from persisted history.

    history: list of {ts, prices: {gpu: median_price}} snapshots, oldest first.
    tflops:  {gpu: TFLOPS rating}.

    Returns [{ts, value}] where value is the index relative to the FIRST
    snapshot in the series (the base). Skips snapshots with no computable
    value. Never fabricates — a snapshot with insufficient data is omitted.
    """
    if not history:
        return []
    base = history[0].get("prices", {})
    series = []
    for snap in history:
        prices = snap.get("prices", {})
        value = compute_index_value(prices, base, tflops)
        if value is not None:
            series.append({"ts": snap.get("ts"), "value": value})
    return series


def tightness_label(price_spread: float | None) -> str:
    """Classify market tightness from the p90/p10 price spread.

    A wide spread = fragmented market (loose); a narrow spread = tight.
    """
    if price_spread is None:
        return "Unknown"
    if price_spread < _TIGHTNESS_THRESHOLDS[0]:
        return "Tight"
    if price_spread < _TIGHTNESS_THRESHOLDS[1]:
        return "Normal"
    if price_spread < _TIGHTNESS_THRESHOLDS[2]:
        return "Loose"
    return "Very Loose"


def tightness_composite(price_spread: float | None) -> dict:
    """Market-tightness composite with hysteresis (Layer-4 contract)."""
    raw = tightness_label(price_spread)
    state = _load_state()
    label, trend = _apply_hysteresis(
        "tightness", raw, price_spread or 0.0,
        _TIGHTNESS_THRESHOLDS, _TIGHTNESS_LABELS, state,
        margin=0.5,  # price-spread scale, not 0-100
    )
    _save_state(state)
    return _composite(
        label, 0.7,
        [{"name": "price_spread_p90_p10", "value": price_spread}],
        f"Price spread {price_spread:.2f}" if price_spread is not None else "No spread data",
        trend,
    )
