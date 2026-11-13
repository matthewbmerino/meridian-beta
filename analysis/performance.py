"""Daily performance: the market's P&L, with contributions taken out.

The snapshot store records **net liquidation**, which is a BALANCE: it rises
when money arrives. Diffing that balance books a deposit as a trading gain —
a $620,000 house-proceeds transfer in, and the withdrawal six weeks later,
became the "best day" and the "worst day" on the Performance page. They were
neither; they were the operator moving his own money.

The series is therefore built by **decomposition**, not by filtering a
balance. Between two closes every position contributes two separate things:

    market = qty_prev * (price_now - price_prev)      what the market did
    traded = (qty_now  - qty_prev) * price_now        what capital did

and cash moves against the trade. Summed across the book,

    ΔNAV = Σ market + Σ traded + Δcash

so ``flow = Δcash + Σ traded`` is the money that arrived or left without
being earned, and ``Σ market`` is the day's P&L. The identity holds exactly,
which is the point: every day reconciles, and no day has to be thrown away.

Position edges, chosen so nothing is invented:

* A position that APPEARS is opened at today's price — it has no P&L yet, its
  value is capital that arrived.
* A position that DISAPPEARS is closed at yesterday's price — its exit is
  capital leaving, not a loss of its whole market value.
* A SPLIT keeps its market value: the quantity and price move in opposite
  directions by the same factor, so both sides are booked as zero. Without
  this guard a 2:1 split reads as a −50% day and a corresponding "deposit".

The base is the **brokerage** book (buying power + market value), never the
combined net liquidation: hand-entered external balances live in the latter,
and every edit of one of those would otherwise read as a return. Cash that the
operator moves into or out of the brokerage is still measured — it shows up as
``flow`` and is excluded from P&L by the same identity.

Rows are **calendar closes** in the operator's timezone (the last observation
of each date), because the snapshot feed samples intraday: consecutive samples
are minutes apart, not days. A close-to-close move that spans more than
``MAX_SPAN_DAYS`` calendar days is an observation gap (the app was closed), so
it still moves the return index but is kept out of the best/worst-day
statistic — a five-day gain is not a "best day".
"""

from __future__ import annotations

import datetime as _dt
from typing import Any

import pandas as pd

TZ = "America/New_York"

# A close-to-close move spanning more than this many calendar days is an
# observation gap (weekend is 1-2, a closed app is more), not a day.
MAX_SPAN_DAYS = 2

# Split guard: quantity and price both moved by more than this factor while
# their product stayed within tolerance of 1 — a split, not a trade.
_SPLIT_FACTOR = 1.5
_SPLIT_VALUE_TOL = 0.2

_ZERO = 0.0


def _num(v: Any) -> float | None:
    """Coerce to float or None. NaN/inf/garbage become None, never 0."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def _book(positions: pd.DataFrame) -> dict[pd.Timestamp, dict[tuple[str, str], tuple[float, float]]]:
    """Index the position snapshots by timestamp: (symbol, source) -> (qty, price).

    A timestamp with no rows at all is ABSENT from the map (unknown book); a
    timestamp whose rows are all zero-quantity still appears with entries.
    The caller distinguishes the two — see ``build_days``.
    """
    out: dict[pd.Timestamp, dict[tuple[str, str], tuple[float, float]]] = {}
    if positions is None or positions.empty:
        return out
    cols = ["ts", "symbol", "source", "quantity", "current_price"]
    for rec in positions.loc[:, cols].to_dict("records"):
        qty = _num(rec.get("quantity"))
        px = _num(rec.get("current_price"))
        key = (str(rec.get("symbol") or ""), str(rec.get("source") or ""))
        out.setdefault(rec["ts"], {})[key] = (qty if qty is not None else 0.0,
                                             px if px is not None else 0.0)
    return out


def _is_split(q0: float, p0: float, q1: float, p1: float) -> bool:
    """True when quantity and price moved in opposite directions by one factor."""
    if not q0 or not p0 or not p1:
        return False
    ratio_q = q1 / q0
    ratio_p = p1 / p0
    return (max(ratio_q, ratio_p) >= _SPLIT_FACTOR
            and min(ratio_q, ratio_p) <= 1 / _SPLIT_FACTOR
            and abs(ratio_q * ratio_p - 1.0) <= _SPLIT_VALUE_TOL)


def decompose(
    prev_book: dict[tuple[str, str], tuple[float, float]],
    cur_book: dict[tuple[str, str], tuple[float, float]],
) -> tuple[float, float]:
    """Split the book's change into ``(market_pnl, traded_value)``.

    ``traded_value`` is capital entering (positive) or leaving (negative) the
    brokerage through trades; ``market_pnl`` is what holding the book earned.
    Cash moves against ``traded_value``, so ``Δbook_nav = market + traded``.
    """
    market = _ZERO
    traded = _ZERO
    for key in set(prev_book) | set(cur_book):
        before = prev_book.get(key)
        after = cur_book.get(key)
        if before is None:
            if after is None:
                continue
            q1, p1 = after
            traded += q1 * p1          # opened today: capital in, no P&L yet
            continue
        if after is None:
            q0, p0 = before
            traded -= q0 * p0          # closed today: capital out at yesterday's mark
            continue
        q0, p0 = before
        q1, p1 = after
        if _is_split(q0, p0, q1, p1):
            continue                   # value preserved: neither P&L nor flow
        market += q0 * (p1 - p0)
        traded += (q1 - q0) * p1
    return market, traded


def build_days(
    closes: pd.DataFrame,
    positions: pd.DataFrame,
    tz: str = TZ,
) -> list[dict]:
    """One row per calendar close with its flow and market P&L.

    ``closes`` is ``portfolio_snapshots`` (ts, net_liquidation, cash_balance,
    buying_power, long_market_value, short_market_value); ``positions`` is
    ``position_snapshots`` (ts, symbol, source, quantity, current_price).
    """
    if closes is None or closes.empty:
        return []
    df = closes.copy()
    df["ts"] = pd.to_datetime(df["ts"], utc=True, errors="coerce")
    df = df.dropna(subset=["ts"]).sort_values("ts")
    if df.empty:
        return []
    df["date"] = df["ts"].dt.tz_convert(tz).dt.date
    last = df.groupby("date", sort=True).tail(1).reset_index(drop=True)

    books = _book(positions)

    days: list[dict] = []
    level = 100.0
    prev_date: _dt.date | None = None
    prev_cash = 0.0
    prev_book_nav = 0.0
    prev_count = 0.0
    prev_ts: pd.Timestamp | None = None

    for rec in last.to_dict("records"):
        ts = rec["ts"]
        cash = _num(rec.get("buying_power")) or _ZERO
        long_mv = _num(rec.get("long_market_value")) or _ZERO
        short_mv = _num(rec.get("short_market_value")) or _ZERO
        nav = _num(rec.get("net_liquidation")) or _ZERO
        count = _num(rec.get("position_count")) or _ZERO
        book_nav = cash + long_mv + short_mv

        if prev_ts is None:
            days.append({
                "date": str(rec["date"]),
                "ts": ts.isoformat(),
                "nav": round(nav, 2),
                "book_nav": round(book_nav, 2),
                "flow": 0.0,
                "pnl": None,
                "ret": None,
                "index": level,
                "drawdown": 0.0,
                "span_days": 0,
                "rankable": False,
            })
            prev_date, prev_cash, prev_book_nav = rec["date"], cash, book_nav
            prev_count, prev_ts = count, ts
            continue

        # A close that reports zero positions genuinely holds an empty book; a
        # close with a nonzero count but no position rows is a failed read, and
        # its book is unknown rather than empty.
        prev_book = books.get(prev_ts)
        if prev_book is None and prev_count == 0:
            prev_book = {}
        cur_book = books.get(ts)
        if cur_book is None and count == 0:
            cur_book = {}
        one_sided = (prev_book is None) != (cur_book is None)

        pnl: float | None
        flow: float | None
        if one_sided:
            # A close with no position rows beside one that has them: the book
            # is unknowable, so neither number is reported for the day.
            pnl = None
            flow = None
        else:
            market, traded = decompose(prev_book or {}, cur_book or {})
            flow = (cash - prev_cash) + traded
            pnl = market

        ret: float | None = None
        if pnl is not None and prev_book_nav > 0:
            ret = pnl / prev_book_nav
            level *= 1.0 + ret

        span = (rec["date"] - prev_date).days if prev_date else 0
        days.append({
            "date": str(rec["date"]),
            "ts": ts.isoformat(),
            "nav": round(nav, 2),
            "book_nav": round(book_nav, 2),
            "flow": None if flow is None else round(flow, 2),
            "pnl": None if pnl is None else round(pnl, 2),
            "ret": None if ret is None else round(ret, 8),
            "index": round(level, 4),
            "drawdown": 0.0,
            "span_days": span,
            # Rankable = a single day's P&L: measured, and not an observation
            # gap. A five-day gain still compounds the index, but it is not a
            # "best day" and it gets no bar in the histogram.
            "rankable": pnl is not None and span <= MAX_SPAN_DAYS,
        })
        prev_date, prev_cash, prev_book_nav = rec["date"], cash, book_nav
        prev_count, prev_ts = count, ts

    return _with_drawdown(days)


def _with_drawdown(days: list[dict]) -> list[dict]:
    """Fill ``drawdown`` (% from the running peak of the return index)."""
    peak = -float("inf")
    for d in days:
        level = d.get("index") or 100.0
        if level > peak:
            peak = level
        d["drawdown"] = round(((level - peak) / peak) * 100.0, 4) if peak else 0.0
    return days


def slice_range(days: list[dict], span_days: int | None) -> list[dict]:
    """Last ``span_days`` calendar days, rebased to 100 at the first kept close."""
    if not days:
        return []
    if span_days is None or span_days <= 0:
        return _rebase(days)
    last = _dt.date.fromisoformat(str(days[-1]["date"]))
    cutoff = last - _dt.timedelta(days=int(span_days))
    kept = [d for d in days if _dt.date.fromisoformat(str(d["date"])) >= cutoff]
    return _rebase(kept or days[-1:])


def _rebase(days: list[dict]) -> list[dict]:
    """Rescale the index so the window starts at 100 and recompute drawdown.

    The first close of a window carries no P&L: the move INTO it was earned
    before the window opened, and counting it would make ``total_pnl`` disagree
    with the index that starts at 100 on that same close.
    """
    if not days:
        return []
    base = days[0].get("index") or 100.0
    out = []
    for i, d in enumerate(days):
        row = dict(d)
        level = (d.get("index") or base) / base * 100.0
        row["index"] = round(level, 4)
        if i == 0:
            row["pnl"] = None
            row["ret"] = None
            row["rankable"] = False
        out.append(row)
    return _with_drawdown(out)


def summarize(days: list[dict], max_span_days: int = MAX_SPAN_DAYS) -> dict:
    """Best/worst day, total P&L, flow-adjusted return and drawdown.

    ``pnl``/``flow`` are None on the first close and on any close whose book
    could not be read; those rows are counted but never ranked.
    """
    empty: dict[str, Any] = {
        "days": 0,
        "measured_days": 0,
        "first_date": None,
        "last_date": None,
        "best_day": 0.0,
        "best_day_date": None,
        "worst_day": 0.0,
        "worst_day_date": None,
        "total_pnl": 0.0,
        "total_return_pct": 0.0,
        "max_drawdown_pct": 0.0,
        "peak_date": None,
        "trough_date": None,
        "peak_level": 0.0,
        "trough_level": 0.0,
        "current_drawdown_pct": 0.0,
        "recovery_pct": 0.0,
        "duration_days": 0,
        "flow_total": 0.0,
        "flow_days": [],
        "long_span_days": 0,
    }
    if not days:
        return empty

    measured = [d for d in days if d.get("pnl") is not None]
    if not measured:
        return empty

    comparable = [d for d in measured if d.get("rankable", (d.get("span_days") or 0) <= max_span_days)]
    if not comparable:
        comparable = measured

    best = max(comparable, key=lambda d: float(d["pnl"]))
    worst = min(comparable, key=lambda d: float(d["pnl"]))

    peak_level = -float("inf")
    trough_level = float(measured[0].get("index") or 100.0)
    peak_date = measured[0]["date"]
    trough_date = measured[0]["date"]
    max_dd = 0.0
    last_level = float(measured[0].get("index") or 100.0)
    for d in measured:
        level = float(d.get("index") or 100.0)
        last_level = level
        if level > peak_level:
            peak_level = level
            peak_date = d["date"]
        dd = ((level - peak_level) / peak_level) * 100.0 if peak_level else 0.0
        if dd < max_dd:
            max_dd = dd
            trough_date = d["date"]
            trough_level = level

    # How much of the drawdown has been retraced, and how long it took to make.
    span = peak_level - trough_level
    recovery = ((last_level - trough_level) / span * 100.0) if span > 0 else 0.0
    recovery = max(0.0, min(100.0, recovery))
    duration = (_dt.date.fromisoformat(str(trough_date))
                - _dt.date.fromisoformat(str(peak_date))).days

    flows = [
        {"date": d["date"], "amount": float(d["flow"])}
        for d in days
        if d.get("flow") is not None and abs(float(d["flow"])) >= 0.5
    ]

    return {
        "days": len(days),
        "measured_days": len(measured),
        "first_date": days[0]["date"],
        "last_date": days[-1]["date"],
        "best_day": float(best["pnl"]),
        "best_day_date": best["date"],
        "worst_day": float(worst["pnl"]),
        "worst_day_date": worst["date"],
        "total_pnl": round(sum(float(d["pnl"]) for d in measured), 2),
        "total_return_pct": round(float(measured[-1].get("index") or 100.0) - 100.0, 4),
        "max_drawdown_pct": round(max_dd, 4),
        "peak_date": peak_date,
        "trough_date": trough_date,
        "peak_level": round(peak_level, 4),
        "trough_level": round(trough_level, 4),
        "current_drawdown_pct": round(float(measured[-1].get("drawdown") or 0.0), 4),
        "recovery_pct": round(recovery, 2),
        "duration_days": duration,
        "flow_total": round(sum(f["amount"] for f in flows), 2),
        "flow_days": flows,
        "long_span_days": len(measured) - len(comparable),
    }
