"""Forecasting composites: model-based heuristics for portfolio state.

Layer 4 of the metrics framework. Every composite returns:
    {value, confidence, inputs[], change_reason, is_predictive: True}

Pure functions — no data/ or backend/ imports.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Hysteresis state persistence
# ---------------------------------------------------------------------------
_STATE_PATH = Path(__file__).resolve().parent.parent / "data" / "composite_state.json"

_WEATHER_LABELS = ["Calm", "Watchful", "Elevated", "Storm"]
_REGIME_LABELS = ["Trending", "Choppy", "Euphoric", "Distribution", "Panic-Rebound"]

# Score must exceed threshold by this margin to transition to a new state
_HYSTERESIS_MARGIN = 5.0
# Must hold the new state for this many consecutive evaluations before switching
_CONFIRM_COUNT = 2


def _load_state() -> dict:
    try:
        if _STATE_PATH.exists():
            return json.loads(_STATE_PATH.read_text())
    except Exception as e:
        log.warning("Composite hysteresis state load failed: %s", e)
    return {}


def _save_state(state: dict):
    try:
        _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _STATE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2))
        tmp.rename(_STATE_PATH)
    except Exception as e:
        log.warning("Composite hysteresis state save failed: %s", e)


def _apply_hysteresis(key: str, raw_label: str, score: float,
                      thresholds: Sequence[float], labels: Sequence[str],
                      state: dict) -> tuple[str, str | None]:
    """Apply hysteresis to prevent flickering between states.

    Returns (final_label, trend) where trend is "improving" | "deteriorating" | None.
    """
    prev = state.get(key, {})
    prev_label = prev.get("label")
    prev_score = prev.get("score")
    consecutive = prev.get("consecutive", 0)

    # Determine trend from score movement
    trend = None
    if prev_score is not None:
        delta = score - prev_score
        if abs(delta) > 1.5:
            trend = "deteriorating" if delta > 0 else "improving"

    if prev_label is None or prev_label == raw_label:
        state[key] = {"label": raw_label, "score": round(score, 1), "consecutive": consecutive + 1}
        return raw_label, trend

    # Raw label differs from previous — check if it crosses with margin
    prev_idx = labels.index(prev_label) if prev_label in labels else -1
    raw_idx = labels.index(raw_label) if raw_label in labels else -1
    if prev_idx < 0 or raw_idx < 0:
        state[key] = {"label": raw_label, "score": round(score, 1), "consecutive": 1}
        return raw_label, trend

    boundary = thresholds[min(prev_idx, raw_idx)] if min(prev_idx, raw_idx) < len(thresholds) else 50
    margin_ok = abs(score - boundary) > _HYSTERESIS_MARGIN

    if raw_label == prev.get("pending_label"):
        pending_count = prev.get("pending_count", 0) + 1
    else:
        pending_count = 1

    if margin_ok and pending_count >= _CONFIRM_COUNT:
        state[key] = {"label": raw_label, "score": round(score, 1), "consecutive": 1}
        return raw_label, trend

    # Hold previous label
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
    """Standard composite return shape."""
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
# Portfolio Weather
# ---------------------------------------------------------------------------

# Input names shared by the weather table and _is_elevated — a rename in one
# place can no longer silently disable elevated-detection.
W_PORTFOLIO_VOL = "Portfolio Vol"
W_VIX = "VIX"
W_DRAWDOWN = "Drawdown"
W_BREADTH = "Breadth"
W_CREDIT_SPREAD = "Credit Spread"
W_YIELD_CURVE = "Yield Curve"


@dataclass
class _WeatherInput:
    """One scoring input of portfolio_weather.

    ``score`` maps the input's display value (percent, VIX level, etc.) to its
    contribution in points. Formulas are preserved verbatim from the
    pre-refactor implementation.
    """
    name: str
    weight: float
    max_score: float
    score: Callable[[float], float]


# Table-driven weather scoring. Formulas are preserved verbatim from the
# pre-refactor implementation — only the accumulation boilerplate is shared.
_WEATHER_INPUTS = [
    _WeatherInput(
        name=W_PORTFOLIO_VOL, weight=0.25, max_score=25.0,
        # value is portfolio vol in percent (portfolio_vol_annualized * 100)
        score=lambda v: min(25, max(0, (v - 8) / 32 * 25)),
    ),
    _WeatherInput(
        name=W_VIX, weight=0.20, max_score=20.0,
        score=lambda v: min(20, max(0, (v - 12) / 38 * 20)),
    ),
    _WeatherInput(
        name=W_DRAWDOWN, weight=0.20, max_score=20.0,
        # value is drawdown in percent (current_drawdown_pct * 100, negative)
        score=lambda v: min(20, max(0, abs(v) / 20 * 20)),
    ),
    _WeatherInput(
        name=W_BREADTH, weight=0.10, max_score=10.0,
        # inverted — low breadth = bad
        score=lambda v: min(10, max(0, (100 - v) / 100 * 10)),
    ),
    _WeatherInput(
        name=W_CREDIT_SPREAD, weight=0.15, max_score=15.0,
        score=lambda v: _credit_spread_score(v, 2.0),
    ),
    _WeatherInput(
        name=W_YIELD_CURVE, weight=0.10, max_score=10.0,
        score=lambda v: _yield_curve_score(v),
    ),
]


def _credit_spread_score(credit_spread: float, credit_spread_avg: float) -> float:
    """Credit-spread contribution (0-15 points). Higher spread = worse."""
    avg = credit_spread_avg if credit_spread_avg is not None else 2.0
    ratio = credit_spread / avg if avg > 0 else 1.0
    if credit_spread > 3.5:
        return 15.0
    if ratio > 1.3:
        return min(15.0, max(0.0, (ratio - 0.8) * 15))
    return min(15.0, max(0.0, (credit_spread - 1.0) / 2.5 * 15))


def _yield_curve_score(yield_curve_spread: float) -> float:
    """Yield-curve contribution (0-10 points). Inverted = worse."""
    if yield_curve_spread < -0.2:
        return 10.0
    if yield_curve_spread < 0.2:
        return min(10.0, max(0.0, (0.5 - yield_curve_spread) / 0.7 * 10))
    return min(10.0, max(0.0, (0.5 - yield_curve_spread) / 0.5 * 5))


def _round_input(name: str, val) -> float:
    """Round an input value for display, preserving each input's precision."""
    return round(float(val), 2) if name in (W_CREDIT_SPREAD, W_YIELD_CURVE) else round(float(val), 1)


def portfolio_weather(portfolio_vol_annualized: float,
                      vix_level: float | None,
                      current_drawdown_pct: float,
                      breadth_pct_above_sma50: float | None = None,
                      credit_spread: float | None = None,
                      credit_spread_avg: float | None = None,
                      yield_curve_spread: float | None = None) -> dict:
    """Overall portfolio weather: Calm / Watchful / Elevated / Storm.

    Inputs:
        portfolio_vol_annualized: realized portfolio vol (e.g., 0.14 = 14%)
        vix_level: current VIX (or None if unavailable)
        current_drawdown_pct: current drawdown from peak (negative, e.g., -0.05)
        breadth_pct_above_sma50: % of holdings above their 50-day SMA (0-100)
        credit_spread: BAA-10Y credit spread (higher = worse)
        credit_spread_avg: 2-year average credit spread for comparison
        yield_curve_spread: 10Y-2Y yield spread (negative = inverted)
    """
    # Input values per weather input, keyed by name. None = unavailable.
    vol_pct = portfolio_vol_annualized * 100
    values: dict[str, float | None] = {
        W_PORTFOLIO_VOL: vol_pct,
        W_VIX: vix_level,
        W_DRAWDOWN: current_drawdown_pct * 100,
        W_BREADTH: breadth_pct_above_sma50,
        W_CREDIT_SPREAD: credit_spread,
        W_YIELD_CURVE: yield_curve_spread,
    }

    score = 0.0
    max_score = 0.0
    inputs: list[dict[str, object]] = []
    available = 0

    for spec in _WEATHER_INPUTS:
        name = spec.name
        weight = spec.weight
        val = values[name]
        # Credit Spread needs the average as a secondary parameter.
        if name == W_CREDIT_SPREAD and val is not None:
            avg = credit_spread_avg if credit_spread_avg is not None else 2.0
            contribution = _credit_spread_score(float(val), avg)
        elif val is not None:
            contribution = spec.score(float(val))
        else:
            contribution = None

        if contribution is not None:
            score += contribution
            max_score += spec.max_score
            available += 1
            inputs.append({"name": name, "value": _round_input(name, val), "weight": weight})
        else:
            inputs.append({"name": name, "value": None, "weight": weight})

    # Normalize to 0-100 scale
    score = (score / max_score * 100) if max_score > 0 else 50

    # Thresholds on the 0-100 normalized scale
    thresholds = [20, 45, 70]
    if score >= 70:
        raw_label = "Storm"
    elif score >= 45:
        raw_label = "Elevated"
    elif score >= 20:
        raw_label = "Watchful"
    else:
        raw_label = "Calm"

    # Apply hysteresis
    state = _load_state()
    label, trend = _apply_hysteresis(
        "weather", raw_label, score, thresholds, _WEATHER_LABELS, state
    )
    _save_state(state)

    # Build reason with specifics
    elevated_inputs = [str(i["name"]) for i in inputs if i["value"] is not None and _is_elevated(i)]
    if label == "Storm":
        reason = f"Multiple risk signals elevated — {', '.join(elevated_inputs[:3]) or 'high overall score'}"
    elif label == "Elevated":
        reason = f"Above-normal risk from {', '.join(elevated_inputs[:2]) or 'volatility/drawdown'}"
    elif label == "Watchful":
        if elevated_inputs:
            reason = f"Moderate conditions — {elevated_inputs[0]} slightly elevated"
        else:
            reason = "Moderate conditions — some risk factors slightly elevated"
    else:
        reason = "Low volatility, shallow drawdown, healthy breadth, tight credit"

    if trend:
        reason += f" ({trend})"

    confidence = 0.45 + 0.09 * available

    return _composite(label, confidence, inputs, reason, trend)


def _is_elevated(inp: dict) -> bool:
    """Heuristic: is this input contributing meaningfully to the weather score?"""
    name = inp["name"]
    val = inp["value"]
    if val is None:
        return False
    if name == W_PORTFOLIO_VOL and val > 20:
        return True
    if name == W_VIX and val > 22:
        return True
    if name == W_DRAWDOWN and val < -5:
        return True
    if name == W_BREADTH and val < 40:
        return True
    if name == W_CREDIT_SPREAD and val > 2.5:
        return True
    if name == W_YIELD_CURVE and val < 0:
        return True
    return False


# ---------------------------------------------------------------------------
# Risk Regime
# ---------------------------------------------------------------------------

def risk_regime(market_returns: pd.Series, lookback: int = 60,
                credit_spread: float | None = None,
                credit_spread_avg: float | None = None,
                yield_curve_spread: float | None = None,
                vix_level: float | None = None) -> dict:
    """Market risk regime classification with cross-asset confirmation.

    Uses 60-day return characteristics plus credit/rates signals:
    Trending / Choppy / Euphoric / Distribution / Panic-Rebound

    market_returns: daily returns of a broad index (SPY or portfolio).
    """
    if market_returns.empty or len(market_returns) < lookback:
        return _composite("Unknown", 0.3, [], "Insufficient data for regime classification")

    window = market_returns.tail(lookback)
    cum_ret = float((1 + window).prod() - 1)
    vol = float(window.std() * np.sqrt(252))
    skew = float(window.skew())
    _ac = window.autocorr(lag=1) if len(window) > 2 else 0.0
    autocorr = float(_ac) if pd.notna(_ac) else 0.0
    neg_days_pct = float((window < 0).mean() * 100)

    inputs = [
        {"name": "60d Return", "value": round(cum_ret * 100, 1), "weight": 0.20},
        {"name": "Annualized Vol", "value": round(vol * 100, 1), "weight": 0.20},
        {"name": "Skewness", "value": round(skew, 2), "weight": 0.15},
        {"name": "Autocorrelation", "value": round(autocorr, 2), "weight": 0.10},
        {"name": "% Down Days", "value": round(neg_days_pct, 1), "weight": 0.10},
    ]

    # Cross-asset signals as confirmation/contradiction
    credit_stress = False
    credit_calm = False
    if credit_spread is not None:
        avg = credit_spread_avg if credit_spread_avg is not None else 2.0
        credit_stress = credit_spread > avg * 1.3 or credit_spread > 3.0
        credit_calm = credit_spread < avg * 0.9
        inputs.append({"name": "Credit Spread", "value": round(credit_spread, 2), "weight": 0.10})
    else:
        inputs.append({"name": "Credit Spread", "value": None, "weight": 0.10})

    curve_inverted = False
    if yield_curve_spread is not None:
        curve_inverted = yield_curve_spread < -0.1
        inputs.append({"name": "Yield Curve", "value": round(yield_curve_spread, 2), "weight": 0.08})
    else:
        inputs.append({"name": "Yield Curve", "value": None, "weight": 0.08})

    vix_elevated = False
    if vix_level is not None:
        vix_elevated = vix_level > 25
        inputs.append({"name": "VIX", "value": round(vix_level, 1), "weight": 0.07})
    else:
        inputs.append({"name": "VIX", "value": None, "weight": 0.07})

    # Score-based classification with cross-asset adjustments
    # Base score: 0 = strong trending, 100 = extreme stress
    regime_score = _regime_score(cum_ret, vol, skew, autocorr, neg_days_pct,
                                 credit_stress, credit_calm, curve_inverted, vix_elevated)

    # Classification from score. Each entry: (label, condition, reason-builder,
    # base confidence). The first matching branch wins, exactly as the original
    # if/elif chain did.
    classification = [
        ("Euphoric",
         lambda: cum_ret > 0.12 and vol < 0.15 and not credit_stress,
         lambda: ("Exceptional returns with compressed vol"
                  + (" — credit confirms risk-on" if credit_calm else " — late-cycle complacency risk")),
         lambda: 0.70),
        ("Trending",
         lambda: cum_ret > 0.05 and vol < 0.20 and skew > -0.5 and not credit_stress,
         lambda: "Positive drift with contained volatility"
                 + (" — broad risk-on confirmed by tight credit" if credit_calm else ""),
         lambda: 0.75),
        ("Panic-Rebound",
         lambda: cum_ret < -0.08 and vol > 0.25,
         lambda: "Sharp selloff with elevated vol — potential snap-back"
                 + (", credit stress confirms real risk" if credit_stress else ""),
         lambda: 0.60),
        ("Distribution",
         lambda: cum_ret < -0.03 and neg_days_pct > 55,
         lambda: "Negative drift with persistent selling"
                 + (" — credit widening confirms institutional de-risking" if credit_stress
                    else (" — inverted curve adds recession signal" if curve_inverted else "")),
         lambda: 0.75 if credit_stress else (0.70 if curve_inverted else 0.60)),
        ("Choppy",
         lambda: vol > 0.20 and abs(cum_ret) < 0.05,
         lambda: "High vol with no directional conviction"
                 + (" — credit stress suggests downside bias" if credit_stress else ""),
         lambda: 0.65),
        ("Trending",
         lambda: cum_ret > 0.02 and vol < 0.25,
         lambda: "Moderate positive drift with manageable volatility",
         lambda: 0.60),
        ("Distribution",
         lambda: cum_ret < -0.02,
         lambda: "Negative drift — cautious positioning warranted",
         lambda: 0.55),
        ("Choppy",
         lambda: True,
         lambda: "No clear regime — mixed signals across equities"
                 + (" but credit is flashing caution" if credit_stress else ""),
         lambda: 0.50),
    ]
    for raw_label, cond, reason_fn, conf_fn in classification:
        if cond():
            reason = reason_fn()
            base_conf = conf_fn()
            break

    # Boost confidence when cross-asset signals confirm
    cross_confirms = 0
    if credit_spread is not None:
        cross_confirms += 1
    if yield_curve_spread is not None:
        cross_confirms += 1
    if vix_level is not None:
        cross_confirms += 1
    confidence = base_conf + 0.05 * cross_confirms

    # Hysteresis
    thresholds = [25, 45, 65, 80]
    state = _load_state()
    label, trend = _apply_hysteresis(
        "regime", raw_label, regime_score, thresholds, _REGIME_LABELS, state
    )
    _save_state(state)

    if trend:
        reason += f" ({trend})"

    return _composite(label, confidence, inputs, reason, trend)


def _regime_score(cum_ret: float, vol: float, skew: float, autocorr: float,
                  neg_days_pct: float, credit_stress: bool, credit_calm: bool,
                  curve_inverted: bool, vix_elevated: bool) -> float:
    """Regime stress score 0-100 from the window's return characteristics.

    0 = strong trending, 100 = extreme stress. Components: return (30),
    vol (25), skew (15), autocorrelation (10), down-days (10), plus
    cross-asset adjustments (credit ±8, curve +5, VIX +6).
    """
    regime_score = max(0, min(30, (0.05 - cum_ret) / 0.20 * 30))
    regime_score += max(0, min(25, (vol - 0.10) / 0.30 * 25))
    regime_score += max(0, min(15, (-skew - 0.0) / 1.5 * 15))
    regime_score += max(0, min(10, (0.0 - autocorr) / 0.3 * 10))
    regime_score += max(0, min(10, (neg_days_pct - 45) / 20 * 10))

    if credit_stress:
        regime_score += 8
    elif credit_calm:
        regime_score -= 4
    if curve_inverted:
        regime_score += 5
    if vix_elevated:
        regime_score += 6

    return max(0, min(100, regime_score))


def momentum_breadth(pct_above_sma50: float | None = None,
                     pct_above_sma200: float | None = None) -> dict:
    """Momentum breadth composite: 0-100 score of market/portfolio breadth health.

    Higher = more names participating in the trend (healthy).
    Lower = narrow leadership (fragile).
    """
    inputs: list[dict[str, object]] = []
    score = 50.0
    weight_sum = 0.0

    if pct_above_sma50 is not None:
        score_50 = pct_above_sma50
        inputs.append({"name": "% > SMA 50", "value": round(pct_above_sma50, 1), "weight": 0.60})
        score = score_50 * 0.60
        weight_sum += 0.60
    else:
        inputs.append({"name": "% > SMA 50", "value": None, "weight": 0.60})

    if pct_above_sma200 is not None:
        score_200 = pct_above_sma200
        inputs.append({"name": "% > SMA 200", "value": round(pct_above_sma200, 1), "weight": 0.40})
        if weight_sum > 0:
            score += score_200 * 0.40
            weight_sum += 0.40
        else:
            score = score_200
            weight_sum = 1.0
    else:
        inputs.append({"name": "% > SMA 200", "value": None, "weight": 0.40})

    if weight_sum > 0:
        score = score / weight_sum
    else:
        return _composite(50.0, 0.2, inputs, "No breadth data available")

    score = round(min(100.0, max(0.0, score)), 1)

    if score >= 75:
        reason = "Broad participation — most holdings trending above key MAs"
    elif score >= 50:
        reason = "Moderate breadth — majority participating but not uniform"
    elif score >= 30:
        reason = "Narrow leadership — fewer than half above key MAs"
    else:
        reason = "Weak breadth — most holdings below trend, fragile market structure"

    confidence = 0.5 + 0.3 * (1 if pct_above_sma50 is not None else 0)
    confidence += 0.2 * (1 if pct_above_sma200 is not None else 0)

    return _composite(score, confidence, inputs, reason)


def capital_at_risk(portfolio_var_5d_95: float, portfolio_nav: float,
                    cvar_5d_95: float | None = None) -> dict:
    """Dollar amount at risk over the next 5 trading days.

    portfolio_var_5d_95: 5-day 95% VaR as a decimal (negative).
    portfolio_nav: current net asset value.
    """
    var_dollars = abs(portfolio_var_5d_95) * portfolio_nav
    inputs = [
        {"name": "5d VaR (95%)", "value": round(portfolio_var_5d_95 * 100, 2), "weight": 0.60},
        {"name": "NAV", "value": round(portfolio_nav, 0), "weight": 0.20},
    ]

    if cvar_5d_95 is not None:
        cvar_dollars = abs(cvar_5d_95) * portfolio_nav
        inputs.append({"name": "5d CVaR (95%)", "value": round(cvar_5d_95 * 100, 2), "weight": 0.20})
        reason = f"${var_dollars:,.0f} at risk (95%), ${cvar_dollars:,.0f} expected in tail"
    else:
        reason = f"${var_dollars:,.0f} at risk over next 5 trading days (95% confidence)"

    return _composite(
        round(var_dollars, 0),
        0.70,
        inputs,
        reason,
    )


def all_composites(portfolio_vol: float = 0.15,
                   vix_level: float | None = None,
                   current_drawdown_pct: float = 0.0,
                   breadth_pct_above_sma50: float | None = None,
                   breadth_pct_above_sma200: float | None = None,
                   market_returns: pd.Series | None = None,
                   portfolio_var_5d_95: float = 0.0,
                   portfolio_nav: float = 0.0,
                   cvar_5d_95: float | None = None,
                   credit_spread: float | None = None,
                   credit_spread_avg: float | None = None,
                   yield_curve_spread: float | None = None) -> dict:
    """Compute all composites at once. Convenience aggregator for the API."""
    result = {
        "weather": portfolio_weather(
            portfolio_vol, vix_level, current_drawdown_pct, breadth_pct_above_sma50,
            credit_spread=credit_spread,
            credit_spread_avg=credit_spread_avg,
            yield_curve_spread=yield_curve_spread,
        ),
        "breadth": momentum_breadth(breadth_pct_above_sma50, breadth_pct_above_sma200),
    }

    if market_returns is not None and not market_returns.empty:
        result["regime"] = risk_regime(
            market_returns,
            credit_spread=credit_spread,
            credit_spread_avg=credit_spread_avg,
            yield_curve_spread=yield_curve_spread,
            vix_level=vix_level,
        )
    else:
        result["regime"] = _composite("Unknown", 0.2, [], "No market return data available")

    if portfolio_nav > 0:
        result["capital_at_risk"] = capital_at_risk(
            portfolio_var_5d_95, portfolio_nav, cvar_5d_95
        )

    return result
