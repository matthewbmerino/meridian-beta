"""Outcome library — resolves every logged decision into a measured result.

The Decision Book (data/decisions.py) records *pre-registered forecasts*:
direction, size, conviction, horizon, and a context anchor captured at
decision time. This module arms each decision with an outcome record and
resolves it once its forecast windows close.

Institutional discipline (no lookahead, idempotent, two-sided):
  - A horizon resolves only after its trading-day window has closed (calendar
    built from SPY history), mirroring the no-lookahead rule in
    analysis/backtest.py.
  - Resolution is idempotent: re-running never re-resolves a closed window.
  - **A cell is ``pending``, ``resolved``, ``unclassified``, or ``no_data`` with
    a reason — never a bare null.** "Window has not closed" and "window closed
    but the price series is missing" are different facts and are stored
    differently, so a null can never mean two things.
  - Two-sided win semantics (how a desk books it): a long call wins when forward
    return > 0; a defensive call (trim/sell/close) wins when the forward return
    is below the SPY benchmark. **A call with no classifiable intent is
    ``unclassified`` and is NOT scored** — it is reported separately rather than
    silently counted as a buy.
  - The decision date is the EXCHANGE-LOCAL session (America/New_York), not the
    UTC date, so a decision logged at 20:30 ET anchors to that day's session.

Storage: encrypted JSON at data/outcomes.json (same at-rest discipline as
decisions/theses — data/crypto.py).
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from data.crypto import StoreUnreadable, load_json_store, write_json

log = logging.getLogger(__name__)

_OUTCOMES_PATH = Path(__file__).resolve().parent / "outcomes.json"

MAX_OUTCOMES = 5000

# Horizons resolve in trading days (from the market calendar via SPY history).
DEFAULT_HORIZONS = ("1d", "5d", "20d", "60d")

# The exchange the book trades on. A decision logged after the close belongs to
# that session, which UTC does not express.
EXCHANGE_TZ = ZoneInfo("America/New_York")

CELL_PENDING = "pending"
CELL_RESOLVED = "resolved"
CELL_NO_DATA = "no_data"
CELL_UNCLASSIFIED = "unclassified"


def _path() -> Path:
    return _OUTCOMES_PATH


def _read() -> list[dict]:
    data = load_json_store(_path(), "outcomes", [])
    return data if isinstance(data, list) else []


def _write(outcomes: list[dict]) -> None:
    write_json(_path(), "outcomes", outcomes)


def _suffix_horizon(h: str) -> int:
    """Trading-day count for a horizon key ('20d' -> 20)."""
    return int(h[:-1]) if h.endswith("d") else 0


def horizon_cell(record: dict, horizon: str) -> dict | None:
    """Read one horizon cell, tolerating both stored shapes.

    Older records keep the horizon keys flat on the record; newer ones nest
    them under ``horizons``. Readers must accept both — the record endpoint
    historically read only the nested shape and returned ``null`` for every
    genuinely resolved cell.

    A cell resolved before the ``status`` field existed is normalised to
    ``resolved`` here, so no consumer has to re-derive "absent status means it
    was resolved under the old model".
    """
    nested = record.get("horizons")
    src = nested if isinstance(nested, dict) else record
    cell = src.get(horizon)
    if not isinstance(cell, dict):
        return None
    if "status" not in cell:
        return {**cell, "status": CELL_RESOLVED}
    return cell


def cell_win(record: dict, horizon: str):
    """The win flag for a cell, or None when pending/no_data/unclassified."""
    cell = horizon_cell(record, horizon)
    if not cell:
        return None
    if cell.get("status") in (CELL_PENDING, CELL_NO_DATA, CELL_UNCLASSIFIED):
        return None
    return cell.get("win")


def cell_status(record: dict, horizon: str) -> str:
    """pending | resolved | no_data | unclassified for one horizon."""
    cell = horizon_cell(record, horizon)
    if not cell:
        return CELL_PENDING
    return str(cell.get("status") or CELL_RESOLVED)


# ---------------------------------------------------------------------------
# Anchor primitives (each degrades to None, never fabricates)
# ---------------------------------------------------------------------------

def _current_price(symbol: str) -> float | None:
    try:
        from data.market import get_price_history
        df = get_price_history(symbol, days=30)
        if df is None or df.empty or "close" not in df.columns:
            return None
        p = float(df["close"].iloc[-1])
        return p if p == p else None
    except Exception:
        return None


def _current_score(symbol: str) -> float | None:
    try:
        from analysis.signals import generate_signals
        from data.market import get_price_history
        df = get_price_history(symbol, days=400)
        if df is None or df.empty:
            return None
        sig = generate_signals(symbol, df)
        s = sig.get("composite_score")
        return float(s) if s is not None else None
    except Exception:
        return None


def _current_weight(symbol: str) -> float | None:
    try:
        from data.account import get_combined_positions, get_combined_summary
        positions = get_combined_positions()
        if positions is None or positions.empty:
            return None
        nav = float(get_combined_summary(positions).get("net_liquidation", 0) or 0)
        if nav <= 0:
            return None
        row = positions[positions["symbol"] == symbol]
        if row.empty:
            return None
        mv = float(row.iloc[0].get("market_value", 0) or 0)
        return mv / nav if mv else None
    except Exception:
        return None


def _current_composite(key: str) -> str | None:
    try:
        from data.briefing import _current_composites
        return _current_composites().get(key)
    except Exception:
        return None


def _current_theme(symbol: str) -> str | None:
    try:
        from data.theses import get_thesis
        return (get_thesis(symbol) or {}).get("theme")
    except Exception:
        return None


def _snapshot_weight(symbol: str) -> float | None:
    """Fallback weight from the last portfolio snapshot.

    Arming after hours (NAV unavailable) used to record a null weight, which
    then made the scorecard's by-weight slices under-count. The last snapshot
    is a labelled fallback, never a fabricated live number.
    """
    try:
        from data.snapshots import load_position_history
        df = load_position_history(symbol)
        if df is None or df.empty:
            return None
        if "market_value" not in df.columns:
            return None
        last_ts = df["ts"].max() if "ts" in df.columns else None
        latest = df if last_ts is None else df[df["ts"] == last_ts]
        total = float(latest["market_value"].fillna(0).sum())
        if total <= 0:
            return None
        row = latest[latest["symbol"].astype(str).str.upper() == symbol.upper()]
        if row.empty:
            return None
        value = float(row["market_value"].fillna(0).iloc[0])
        return value / total if value else None
    except Exception:
        return None


def _anchor_for(symbol: str) -> dict:
    """Decision-time context snapshot. Any missing piece degrades to null —
    never fabricated (a wrong anchor is worse than a missing one). ``weight_source``
    records WHICH path produced the weight, so a fallback is visible."""
    weight = _current_weight(symbol)
    weight_source = "live" if weight is not None else None
    if weight is None:
        weight = _snapshot_weight(symbol)
        weight_source = "snapshot" if weight is not None else "unavailable"
    return {
        "score": _current_score(symbol),
        "price": _current_price(symbol),
        "weight": weight,
        "weight_source": weight_source,
        "regime": _current_composite("regime"),
        "weather": _current_composite("weather"),
        "theme": _current_theme(symbol),
    }


# ---------------------------------------------------------------------------
# Arming
# ---------------------------------------------------------------------------

def arm_outcome(decision: dict) -> dict | None:
    """Create an outcome record for a logged decision. Returns the record.

    ``decision`` carries the pre-registration fields (id, ts, symbol, type,
    action, book, direction, size, conviction, horizon, constraint, source)
    plus an optional ``anchor`` snapshot. Re-arming is idempotent on
    decision_id. Best-effort: never raises.
    """
    try:
        if not decision or not decision.get("id"):
            return None
        sym = str(decision.get("symbol", "")).upper()
        if not sym:
            return None
        anchor = decision.get("anchor")
        if not isinstance(anchor, dict):
            anchor = _anchor_for(sym)
        record = {
            "id": uuid.uuid4().hex[:12],
            "decision_id": decision["id"],
            "decision_ts": decision.get("ts", time.time()),
            "symbol": sym,
            "type": decision.get("type", "manual"),
            "action": decision.get("action", ""),
            "book": decision.get("book", "alpha"),
            "direction": decision.get("direction"),
            "size": decision.get("size"),
            "conviction": decision.get("conviction"),
            "horizon": decision.get("horizon"),
            "constraint": decision.get("constraint"),
            "source": decision.get("source", "ui"),
            "actor": decision.get("actor"),
            "anchor": anchor,
            "horizons": {h: None for h in DEFAULT_HORIZONS},
            "resolved": False,
        }
        outcomes = _read()
        outcomes = [o for o in outcomes if o.get("decision_id") != decision["id"]]
        outcomes.insert(0, record)
        _write(outcomes[:MAX_OUTCOMES])
        return record
    except Exception as e:
        log.warning("Outcome arm failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

def _trading_dates(days: int = 800) -> list:
    """Daily market calendar (SPY history), oldest-first, as a date list."""
    try:
        from data.market import get_price_history
        df = get_price_history("SPY", days=days)
        if df is None or df.empty:
            return []
        return [d.date() for d in df.index.tolist()]
    except Exception as e:
        log.debug("Outcome calendar failed: %s", e)
        return []


def _close_series(symbol: str, days: int = 800) -> dict:
    """{date: close} for a symbol, for forward-return math."""
    try:
        from data.market import get_price_history
        df = get_price_history(symbol, days=days)
        if df is None or df.empty or "close" not in df.columns:
            return {}
        return {d.date(): float(c) for d, c in zip(df.index, df["close"]) if c == c}
    except Exception:
        return {}


def _close_at(series: dict, day) -> float | None:
    """Close on *day*, or the most recent prior close (the decision may land
    on a non-trading day for the symbol)."""
    if day in series:
        return series[day]
    prev = None
    for d, c in sorted(series.items()):
        if d > day:
            break
        prev = c
    return prev


def decision_session(decision_ts: float):
    """The exchange-local session date for a decision timestamp."""
    try:
        return datetime.fromtimestamp(decision_ts, tz=EXCHANGE_TZ).date()
    except Exception:
        return None


def _forward_return(decision_ts: float, horizon: int, symbol: str):
    """Return (symbol forward return, SPY benchmark return, reason).

    ``reason`` is set only when the window has closed but the data cannot be
    produced — the caller stores that as ``no_data`` rather than a permanent
    pending. Returns (None, None, None) when the window has not closed.
    """
    decision_date = decision_session(decision_ts)
    if decision_date is None:
        return None, None, "invalid decision timestamp"

    calendar = _trading_dates()
    if not calendar:
        return None, None, "market calendar unavailable"

    start_idx = None
    for i, d in enumerate(calendar):
        if d >= decision_date:
            start_idx = i
            break
    if start_idx is None:
        start_idx = len(calendar) - 1
    end_idx = start_idx + horizon
    if end_idx >= len(calendar):
        return None, None, None  # window not yet closed

    start_day = calendar[start_idx]
    end_day = calendar[end_idx]
    sym_series = _close_series(symbol)
    bench_series = _close_series("SPY")

    s0 = _close_at(sym_series, start_day)
    s1 = _close_at(sym_series, end_day)
    if not s0 or not s1 or s0 <= 0:
        return None, None, f"no price series for {symbol} over {start_day}..{end_day}"
    sym_ret = s1 / s0 - 1.0
    b0 = _close_at(bench_series, start_day)
    b1 = _close_at(bench_series, end_day)
    bench_ret = (b1 / b0 - 1.0) if (b0 and b1 and b0 > 0) else None
    return sym_ret, bench_ret, None


# Directions that describe an exit rather than an entry. Kept explicit: the
# journal's `direction` field is free text, so anything unrecognised is
# unclassified rather than assumed long.
DEFENSIVE_DIRECTIONS = ("short", "sell", "trim", "reduce", "exit", "close", "avoid")
LONG_DIRECTIONS = ("long", "buy", "add", "hold")
DEFENSIVE_ACTIONS = ("trim", "sell", "close", "reduce", "exit", "avoid")


def classify_intent(action: str, direction: str | None) -> str:
    """'long' | 'defensive' | 'unclassified' for a decision.

    ``direction`` is the explicit side of the trade and wins over ``action``
    prose — EXCEPT when the two visibly disagree, which is a data-quality
    conflict rather than a classification, so it returns ``unclassified``
    instead of picking a side. The journal's staging vocabulary
    (``STAGE_ENTRY``) describes process, not side, so it does not classify on
    its own.
    """
    d = (direction or "").strip().lower()
    by_direction = None
    if d in LONG_DIRECTIONS:
        by_direction = "long"
    elif d in DEFENSIVE_DIRECTIONS:
        by_direction = "defensive"

    a = (action or "").strip().lower()
    by_action = None
    if any(k in a for k in DEFENSIVE_ACTIONS):
        by_action = "defensive"
    elif any(k in a for k in ("buy", "add", "open")):
        by_action = "long"

    if by_direction and by_action and by_direction != by_action:
        # Conflicting record: scoring it either way would be inventing a side.
        return "unclassified"
    return by_direction or by_action or "unclassified"


def _is_win(action: str, direction: str | None, ret: float, bench_ret: float | None):
    """Two-sided win classification. Returns True/False, or None when the
    decision's intent cannot be classified (never silently scored as long).

    A long call wins when forward return > 0. A defensive call wins when the
    forward return is below the SPY benchmark — value booked by dodging the
    decline (benchmark fallback when SPY is unavailable: return < 0).
    """
    intent = classify_intent(action, direction)
    if intent == "unclassified":
        return None
    if intent == "defensive":
        if bench_ret is not None:
            return ret < bench_ret
        return ret < 0.0
    return ret > 0.0


def resolve_outcomes() -> dict:
    """Resolve every outcome whose window has closed. Idempotent.

    Returns counts, never raises. A cell that cannot be resolved is stored as
    ``no_data`` with its reason, so an unresolvable forecast is visible instead
    of accumulating as a permanent null.
    """
    outcomes = _read()
    resolved = 0
    pending = 0
    no_data = 0
    unclassified = 0
    changed = False
    for o in outcomes:
        horizons = o.get("horizons")
        if not isinstance(horizons, dict):
            continue
        for h, entry in horizons.items():
            if isinstance(entry, dict) and entry.get("status") in (
                CELL_RESOLVED, CELL_NO_DATA, CELL_UNCLASSIFIED
            ):
                continue
            if h not in DEFAULT_HORIZONS:
                continue
            ret, bench, reason = _forward_return(
                float(o.get("decision_ts", 0)), _suffix_horizon(h), str(o.get("symbol", ""))
            )
            if ret is None and reason is None:
                # Window still open: leave the slot absent so "not due" stays
                # distinguishable from "due but unproducible".
                pending += 1
                continue
            if ret is None:
                horizons[h] = {
                    "status": CELL_NO_DATA,
                    "ret": None,
                    "bench_ret": None,
                    "win": None,
                    "reason": reason,
                    "resolved_at": time.time(),
                }
                no_data += 1
                changed = True
                continue
            win = _is_win(o.get("action", ""), o.get("direction"), ret, bench)
            horizons[h] = {
                "status": CELL_UNCLASSIFIED if win is None else CELL_RESOLVED,
                "ret": round(ret, 6),
                "bench_ret": round(bench, 6) if bench is not None else None,
                "win": win,
                "reason": (None if win is not None else
                           "intent could not be classified (direction/action)"),
                "resolved_at": time.time(),
            }
            if win is None:
                unclassified += 1
            else:
                resolved += 1
            changed = True
        cells = [horizons.get(h) for h in DEFAULT_HORIZONS]
        if all(isinstance(c, dict) for c in cells):
            o["resolved"] = True
    if changed:
        _write(outcomes)
    return {
        "resolved": resolved,
        "pending": pending,
        "no_data": no_data,
        "unclassified": unclassified,
    }


def rescore_cells(dry_run: bool = True) -> dict:
    """Re-derive every stored cell's verdict from its raw returns.

    Win/loss is a DERIVED layer — the raw `ret`/`bench_ret` are what the store
    guarantees. A cell resolved under an older classifier keeps its old verdict
    forever otherwise, so a rule correction (the defensive/`unclassified` fix)
    would only ever apply to future decisions while the historical record kept
    reporting the old rule's numbers as fact.

    Rewrites only the derived fields (`status`, `win`, `reason`); the returns and
    the resolution timestamp are untouched. ``dry_run`` reports the diff without
    writing, so the change can be reviewed before it lands.
    """
    outcomes = _read()
    changes: list[dict] = []
    for o in outcomes:
        horizons = o.get("horizons")
        if not isinstance(horizons, dict):
            continue
        for h in DEFAULT_HORIZONS:
            cell = horizons.get(h)
            if not isinstance(cell, dict) or cell.get("ret") is None:
                continue
            ret = float(cell["ret"])
            bench = cell.get("bench_ret")
            bench = float(bench) if bench is not None else None
            new_win = _is_win(o.get("action", ""), o.get("direction"), ret, bench)
            new_status = CELL_UNCLASSIFIED if new_win is None else CELL_RESOLVED
            old_status = str(cell.get("status") or CELL_RESOLVED)
            old_win = cell.get("win")
            if new_status == old_status and new_win == old_win:
                continue
            changes.append({
                "decision_id": o.get("decision_id"),
                "symbol": o.get("symbol"),
                "action": o.get("action"),
                "direction": o.get("direction"),
                "horizon": h,
                "ret": ret,
                "bench_ret": bench,
                "old_status": old_status,
                "old_win": old_win,
                "new_status": new_status,
                "new_win": new_win,
            })
            if not dry_run:
                cell["status"] = new_status
                cell["win"] = new_win
                cell["reason"] = (None if new_win is not None else
                                  "intent could not be classified (direction/action)")
                cell["rescored_at"] = time.time()
    if changes and not dry_run:
        _write(outcomes)
    return {"dry_run": dry_run, "changed_cells": len(changes), "changes": changes}


def list_outcomes(limit: int = 200) -> list[dict]:
    return _read()[:limit]


def outcomes_by_decision() -> dict[str, dict]:
    """{decision_id: horizons} — the join the journal surface needs.

    A decision row can only show its own forecast if the two stores are joined;
    the decisions endpoint holds no outcome data of its own. Built once per
    request rather than per row.
    """
    out: dict[str, dict] = {}
    for o in _read():
        did = o.get("decision_id")
        if did:
            out[did] = {
                h: horizon_cell(o, h) for h in DEFAULT_HORIZONS
            }
    return out


def outcome_for(decision_id: str) -> dict | None:
    for o in _read():
        if o.get("decision_id") == decision_id:
            return {h: horizon_cell(o, h) for h in DEFAULT_HORIZONS}
    return None


def outcome_count() -> int:
    return len(_read())


def store_health() -> dict:
    try:
        outcomes = _read()
    except StoreUnreadable as e:
        return {"readable": False, "error": str(e), "count": None}
    cells = {h: 0 for h in DEFAULT_HORIZONS}
    resolved_cells = 0
    for o in outcomes:
        for h in DEFAULT_HORIZONS:
            if cell_status(o, h) == CELL_RESOLVED:
                cells[h] += 1
                resolved_cells += 1
    return {
        "readable": True,
        "error": None,
        "count": len(outcomes),
        "resolved_cells": resolved_cells,
        "cells_by_horizon": cells,
    }
