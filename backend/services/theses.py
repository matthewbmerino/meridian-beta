"""Thesis review orchestration — one implementation for the router + scheduler.

Evaluates a thesis's invalidation triggers against live data and returns a
status. Lives in ``backend/services/`` (not ``analysis/``) because it fetches
data — it is orchestration, not pure computation (same boundary as
``sizing_book.py``). The router's ``/api/theses/{symbol}/review`` endpoint and
the scheduler's thesis-alert scan both call this so they can never diverge.

Statuses:
    ok          — no trigger hit, reviewed recently
    at_risk     — a warning-level trigger hit (earnings proximity, insider
                  selling, compute-linked drop)
    invalidated — a hard trigger hit (STBR bubble)
    target_hit  — price reached the upside target (thesis played out — an
                  inform signal to consider taking profit, never a forced loss)
    stale       — not reviewed within REVIEW_INTERVAL_DAYS
"""

from __future__ import annotations

import logging
import time

from data.theses import (
    COMPUTE_DROP_PCT,
    INSIDER_CHECK_CONVICTION,
    REVIEW_INTERVAL_DAYS,
)

log = logging.getLogger(__name__)

# STBR labels that invalidate a thesis (same set the STBR alert scan uses).
_STBR_INVALIDATION_LABELS = ("Bubble", "Bubble Pop", "Overextended")

_EARNINGS_PROXIMITY_DAYS = 5


def _live_price(symbol: str) -> float | None:
    """Last close for *symbol*, or None when unavailable."""
    try:
        from data.market import get_price_history

        df = get_price_history(symbol)
        if df.empty:
            return None
        return float(df["close"].iloc[-1])
    except Exception as e:
        log.debug("Live price fetch failed for %s: %s", symbol, e)
        return None


def _stbr_label(symbol: str) -> str | None:
    """Current STBR risk label for *symbol*, or None."""
    try:
        from analysis.stbr import calculate_stbr, current_risk_label
        from data.market import get_price_history

        df = get_price_history(symbol)
        if df.empty:
            return None
        stbr_df = calculate_stbr(df["close"])
        if stbr_df.empty:
            return None
        label, _ = current_risk_label(float(stbr_df["stbr"].iloc[-1]))
        return label
    except Exception as e:
        log.debug("STBR fetch failed for %s: %s", symbol, e)
        return None


def _earnings_days(symbol: str) -> int | None:
    """Days until next earnings, or None."""
    try:
        import datetime

        from data.earnings import get_ticker_earnings

        e = get_ticker_earnings(symbol)
        if not e or e.get("earnings_date") is None:
            return None
        return (e["earnings_date"] - datetime.datetime.now()).days
    except Exception as e:
        log.debug("Earnings fetch failed for %s: %s", symbol, e)
        return None


def _insider_selling(symbol: str) -> bool:
    """True when recent insider activity is net-negative (selling)."""
    try:
        from data.edgar import get_insider_activity

        ins = get_insider_activity(symbol)
        if not ins:
            return False
        return ins.get("net_sentiment") == "negative"
    except Exception as e:
        log.debug("Insider fetch failed for %s: %s", symbol, e)
        return False


def _compute_drop_pct() -> float | None:
    """Percent drop of the Compute Index from its recent peak, or None.

    Returns None when the index history is insufficient — never fabricates a
    drop. A positive value means the index is below its peak.
    """
    try:
        from analysis.compute_index import compute_index_series
        from data.gpu import GPU_BASKET, load_history

        history = load_history()
        if history.empty:
            return None
        # Rebuild per-snapshot price maps from the flat history frame.
        snapshots: list[dict] = []
        for ts, group in history.groupby("ts"):
            prices = {
                row["gpu_name"]: row["price_median"]
                for _, row in group.iterrows()
                if row["price_median"] is not None
            }
            snapshots.append({"ts": ts, "prices": prices})
        series = compute_index_series(snapshots, GPU_BASKET)
        if len(series) < 2:
            return None
        peak = max(s["value"] for s in series)
        current = series[-1]["value"]
        if peak <= 0:
            return None
        return (peak - current) / peak * 100
    except Exception as e:
        log.debug("Compute-index drop fetch failed: %s", e)
        return None


def evaluate_thesis_review(symbol: str, thesis: dict) -> dict:
    """Evaluate a thesis's invalidation triggers against live data.

    Returns {symbol, status, triggers_hit[], last_reviewed_at, checked_at}.
    Never raises — a failing trigger check degrades to "not hit" and is
    logged, so one bad feed can't abort the whole review.
    """
    sym = symbol.upper()
    triggers: list[str] = []
    status = "ok"

    # --- Upside target (thesis played out) ---
    # The operator's discipline: exits are upside objectives, not downside
    # stops. A price at/above target means the thesis played out — an
    # *inform* signal to consider taking profit, never a forced loss.
    target_price = thesis.get("target_price")
    if target_price is not None:
        price = _live_price(sym)
        if price is not None and price >= target_price:
            triggers.append(f"target reached ({price:.2f} >= {target_price:.2f})")
            status = "target_hit"

    stbr = _stbr_label(sym)
    if stbr in _STBR_INVALIDATION_LABELS:
        triggers.append(f"STBR {stbr}")
        status = "invalidated"

    # --- Warning triggers (at risk) ---
    days = _earnings_days(sym)
    if days is not None and 0 <= days <= _EARNINGS_PROXIMITY_DAYS:
        triggers.append(f"earnings in {days}d")
        if status == "ok":
            status = "at_risk"

    conviction = int(thesis.get("conviction", 3))
    if conviction >= INSIDER_CHECK_CONVICTION and _insider_selling(sym):
        triggers.append("insider selling")
        if status == "ok":
            status = "at_risk"

    # Compute Index gate was retired 2026-09-14 (backlog #10). Only the
    # explicit ``compute_index_gate`` field may re-arm this trigger.
    if thesis.get("compute_index_gate"):
        drop = _compute_drop_pct()
        if drop is not None and drop >= COMPUTE_DROP_PCT:
            triggers.append(f"compute index -{drop:.0f}%")
            if status == "ok":
                status = "at_risk"

    # --- Staleness ---
    last_reviewed = thesis.get("last_reviewed_at")
    if last_reviewed is None:
        status = "stale"
    elif (time.time() - float(last_reviewed)) > REVIEW_INTERVAL_DAYS * 86400:
        status = "stale"

    return {
        "symbol": sym,
        "status": status,
        "triggers_hit": triggers,
        "last_reviewed_at": last_reviewed,
        "checked_at": time.time(),
    }


def review_all_theses() -> list[dict]:
    """Evaluate every thesis. Returns a list of review results."""
    from data.theses import get_theses

    return [
        evaluate_thesis_review(sym, thesis)
        for sym, thesis in get_theses().items()
    ]
