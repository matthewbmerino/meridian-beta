"""Proactive briefing for Compass — "since you last checked".

Generates a compact text block injected into the Compass system prompt at
fresh-chat start, so Compass leads with what changed for the portfolio since
the operator last used it. Converts Compass from Q&A into a proactive advisor.

Each briefing diffs current state against a persisted anchor in
data/briefing_state.json, then advances the anchor:

  {last_generated_at, last_nav, last_regime, last_weather}

Degradation contract (never fabricate):
  - No prior anchor / no history  -> show current state, omit deltas
  - Missing composite state       -> omit regime/weather lines
  - No alerts / no positions      -> omit those sections entirely

This module is best-effort: build_briefing() never raises and returns an
empty block on any failure so the Compass prompt is never broken by it.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from data.crypto import load_json_store, write_json
from data.session import data_root

log = logging.getLogger(__name__)

_STATE_FILENAME = "briefing_state.json"


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def _path() -> Path:
    return data_root() / _STATE_FILENAME


def load_state() -> dict:
    data = load_json_store(_path(), "briefing_state", {})
    return data if isinstance(data, dict) else {}


def save_state(state: dict):
    try:
        write_json(_path(), "briefing_state", state)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------

def _current_composites() -> dict:
    """Current Weather/Regime labels from the global composite_state.json."""
    try:
        p = Path(__file__).resolve().parent.parent / "data" / "composite_state.json"
        if p.exists():
            data = json.loads(p.read_text())
            return {
                "weather": (data.get("weather") or {}).get("label"),
                "regime": (data.get("regime") or {}).get("label"),
            }
    except Exception as e:
        log.warning("Composite-state read failed (briefing): %s", e)
    return {}


def _nav_now_and_anchor(anchor_ts: float | None) -> tuple[float | None, float | None]:
    """Return (current NAV, NAV at/before the anchor), or None when unavailable."""
    try:
        from data.snapshots import load_portfolio_history
        hist = load_portfolio_history()
        if hist.empty:
            return None, None
        hist = hist.sort_values("ts")
        cur = hist["net_liquidation"].iloc[-1]
        cur = float(cur) if cur is not None and cur == cur else None
        if cur is None or cur <= 0:
            return None, None
        if anchor_ts is None:
            return cur, None
        # Snapshot closest to (but at or before) the anchor timestamp.
        from datetime import datetime, timezone
        anchor_dt = datetime.fromtimestamp(anchor_ts, tz=timezone.utc)
        past = hist[hist["ts"] <= anchor_dt]
        if past.empty:
            return cur, None
        prev = past["net_liquidation"].iloc[-1]
        prev = float(prev) if prev is not None and prev == prev else None
        return cur, prev if prev and prev > 0 else None
    except Exception as e:
        log.warning("Briefing NAV-delta computation failed: %s", e)
        return None, None


def _alerts_since(anchor_ts: float | None) -> list[dict]:
    """Alerts newer than the anchor (all of them when no anchor)."""
    try:
        from data.alerts import list_alerts
        alerts = list_alerts(limit=50)
        if anchor_ts is None:
            return alerts
        return [a for a in alerts if (a.get("ts") or 0) > anchor_ts]
    except Exception as e:
        log.warning("Briefing alerts read failed: %s", e)
        return []


def _urgent_earnings() -> list[dict]:
    """Earnings within 7 days for current holdings."""
    try:
        from data.account import get_combined_positions
        from data.earnings import get_portfolio_earnings
        positions = get_combined_positions()
        if positions is None or positions.empty:
            return []
        equities = positions[positions["asset_type"] != "OPTION"]
        symbols = [s for s in equities["symbol"].unique() if s]
        if not symbols:
            return []
        return [e for e in get_portfolio_earnings(symbols, positions) if e.get("urgent")]
    except Exception as e:
        log.warning("Briefing urgent-earnings computation failed: %s", e)
        return []


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

def build_briefing() -> tuple[str, dict]:
    """Compute the briefing block and the next state to persist.

    Returns (text_block, next_state). The caller persists next_state only
    after injecting the block successfully (compass router).
    """
    state = load_state()
    anchor_ts = state.get("last_generated_at")

    now_nav, anchor_nav = _nav_now_and_anchor(anchor_ts)
    comps = _current_composites()
    alerts = _alerts_since(anchor_ts)
    earnings = _urgent_earnings()

    lines: list[str] = []
    has_baseline = anchor_ts is not None

    # --- NAV delta ---
    if now_nav is not None and now_nav > 0:
        if anchor_nav is not None and anchor_nav > 0:
            pct = (now_nav - anchor_nav) / anchor_nav * 100
            lines.append(f"NAV ${now_nav:,.0f} (vs ${anchor_nav:,.0f} last check, {pct:+.1f}%)")
        else:
            lines.append(f"NAV ${now_nav:,.0f}")

    # --- Regime / Weather shift ---
    weather = comps.get("weather")
    regime = comps.get("regime")
    if has_baseline:
        if regime and state.get("last_regime") and regime != state.get("last_regime"):
            lines.append(f"Risk Regime: {state.get('last_regime')} → {regime} (shifted)")
        elif regime:
            lines.append(f"Risk Regime: {regime} (unchanged)")
        if weather and state.get("last_weather") and weather != state.get("last_weather"):
            lines.append(f"Portfolio Weather: {state.get('last_weather')} → {weather} (shifted)")
        elif weather:
            lines.append(f"Portfolio Weather: {weather} (unchanged)")
    else:
        if regime:
            lines.append(f"Risk Regime: {regime}")
        if weather:
            lines.append(f"Portfolio Weather: {weather}")

    # --- Alerts since anchor ---
    if alerts:
        labels = []
        for a in alerts[:5]:
            sym = a.get("symbol")
            labels.append(f"{a.get('title', 'alert')}{' (' + sym + ')' if sym else ''}")
        verb = "New alert" if len(alerts) == 1 else f"{len(alerts)} new alerts"
        lines.append(f"{verb}: " + "; ".join(labels))

    # --- Urgent earnings ---
    if earnings:
        parts = [f"{e['symbol']} ({e['days_until']}d)" for e in earnings[:4]]
        lines.append("Earnings this week: " + ", ".join(parts))

    if not lines:
        return "", state

    if not has_baseline:
        lines.insert(0, "First briefing — current state only (no prior baseline yet).")

    block = "## SINCE YOU LAST CHECKED (Compass, proactive)\n" + "\n".join(f"- {line}" for line in lines)

    next_state = {
        "last_generated_at": time.time(),
        "last_nav": now_nav,
        "last_regime": regime,
        "last_weather": weather,
    }
    return block, next_state
