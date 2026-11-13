"""Opportunity discovery — deterministic fresh-setup ranking.

Pure: accepts already-computed per-symbol signal dicts (the sonar scan shape,
``analysis.signals.generate_signals`` + an ``stbr_label``), plus the symbols the
operator is already tracking, and returns a ranked opportunity feed. No fetching,
no LLM — every candidate is a transparent additive function of the signals.

The Desk Loop needs a *filling* mechanism: when browsing is done, something must
surface the next setups. This is that surface — the strongest fresh technical
setups across the scanned universe, excluding names already held or already in
the pipeline. Determinism is deliberate (see the Desk Loop plan): discovery is
never generated, so it can always be defended to the formula level.
"""

from __future__ import annotations

# STBR labels that mark a crowded / blow-off trade. Starting a position into one
# is the classic way to buy a top, so discovery penalizes them rather than
# rewarding a strong-looking tape.
_RISKY_STBR = {"Risky", "Super Risky", "Bubble Pop"}

# Below this score a name is not worth surfacing — the feed stays signal, not noise.
MIN_DISCOVERY_SCORE = 55.0


def _num(v) -> float | None:
    try:
        if v is None:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def score_setup(sig: dict) -> dict:
    """Score one symbol's fresh-setup quality from its signals.

    Starts from the composite technical score and applies transparent additive
    adjustments. Returns ``{score, setup, reasons[]}`` where ``reasons`` are
    short operator-facing strings for why the name surfaced. Score is clamped
    to [0, 100].
    """
    reasons: list[str] = []
    score = _num(sig.get("composite_score")) or 0.0

    rsi = _num(sig.get("rsi"))
    macd = sig.get("macd_signal")
    bb = sig.get("bb_signal")
    vol = sig.get("volume_signal")
    cross = sig.get("sma_crossover")
    stbr_label = sig.get("stbr_label")

    if rsi is not None:
        if rsi <= 30:
            score += 10
            reasons.append(f"oversold (RSI {rsi:.0f})")
        elif rsi >= 75:
            score -= 8
            reasons.append(f"overbought (RSI {rsi:.0f})")

    if macd == "BULLISH":
        score += 8
        reasons.append("MACD bullish cross")
    elif macd == "BEARISH":
        score -= 6
        reasons.append("MACD bearish cross")

    if bb == "BELOW_LOWER":
        score += 6
        reasons.append("below lower band")
    elif bb == "ABOVE_UPPER":
        score -= 5
        reasons.append("above upper band")

    if vol == "UNUSUAL_HIGH":
        score += 5
        reasons.append("volume surge")

    if cross == "GOLDEN_CROSS":
        score += 6
        reasons.append("golden cross")
    elif cross == "DEATH_CROSS":
        score -= 6
        reasons.append("death cross")

    if stbr_label in _RISKY_STBR:
        score -= 12
        reasons.append(f"bubble risk ({stbr_label})")

    score = max(0.0, min(100.0, score))
    return {"score": round(score, 1), "setup": _classify(sig, score), "reasons": reasons}


def _classify(sig: dict, score: float) -> str:
    """Label the kind of setup — a deterministic read of the signal shape."""
    rsi = _num(sig.get("rsi"))
    macd = sig.get("macd_signal")
    bb = sig.get("bb_signal")
    cross = sig.get("sma_crossover")
    vol = sig.get("volume_signal")
    composite = _num(sig.get("composite_score")) or 0.0
    dist = _num(sig.get("sma_50_dist_pct"))

    if rsi is not None and rsi <= 35 and macd != "BEARISH":
        return "Oversold Reversal"
    if composite >= 70 and (macd == "BULLISH" or bb == "ABOVE_UPPER" or cross == "GOLDEN_CROSS"):
        return "Momentum Breakout"
    if dist is not None and -10 <= dist <= 0 and composite >= 55:
        return "Trend Pullback"
    if vol == "UNUSUAL_HIGH":
        return "Volume Surge"
    return "Steady Signal"


def rank_candidates(
    signals: list[dict],
    exclude: set[str] | None = None,
    tracked: set[str] | None = None,
    limit: int = 12,
    min_score: float = MIN_DISCOVERY_SCORE,
) -> list[dict]:
    """Rank scanned signals into a discovery feed.

    ``exclude`` — symbols to drop entirely (held positions, existing pipeline
    items); ``tracked`` — symbols to keep but flag (watchlist). Sorted by setup
    score, then composite score, then symbol for a stable order. Only names at
    or above ``min_score`` are returned, capped at ``limit``.
    """
    exclude = {s.upper() for s in (exclude or set())}
    tracked = {s.upper() for s in (tracked or set())}
    out: list[dict] = []

    for sig in signals:
        sym = str(sig.get("symbol") or "").upper()
        if not sym or sym in exclude:
            continue
        scored = score_setup(sig)
        if scored["score"] < min_score:
            continue
        out.append({
            "symbol": sym,
            "score": scored["score"],
            "setup": scored["setup"],
            "reasons": scored["reasons"],
            "composite_score": _num(sig.get("composite_score")),
            "composite_label": sig.get("composite_label"),
            "rsi": _num(sig.get("rsi")),
            "stbr_label": sig.get("stbr_label"),
            "current_price": _num(sig.get("current_price")),
            "tracked": sym in tracked,
        })

    out.sort(key=lambda c: (-c["score"], -(c["composite_score"] or 0), c["symbol"]))
    return out[: max(0, limit)]
