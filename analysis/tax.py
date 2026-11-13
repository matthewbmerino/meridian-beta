"""Tax views over the realized lot tape — pure math, no data imports.

The rawest material this book has: the broker's per-lot record (close date,
term, gain). Two professional reads a taxable account needs, computed from it:

  1. The SHORT/LONG split — what the tax is actually levied on, not just the
     headline realized P&L.
  2. OPEN WASH-SALE WINDOWS — IRS §1091 defers a loss when a substantially
     identical security is bought within 30 calendar days before OR after the
     sale. A re-entry inside the window does not lose the economics, it loses
     the deduction; the desk should know which losses are still protected.

Plus a TIE-OUT: the lots sum against the reported total. If they disagree, the
readout says so — the number is checkable without trusting the UI, the parser,
or whoever built either.
"""

from __future__ import annotations

import datetime

WASH_WINDOW_DAYS = 30


def _parse_date(v) -> datetime.date | None:
    """Broker date → date. Handles MM/DD/YYYY (Schwab) and ISO (Chase).

    Returns None on anything else — a lot with an unreadable date is EXCLUDED
    from window math and counted in `undated_losses`, never guessed.
    """
    if v is None or v == "":
        return None
    s = str(v).strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"):
        try:
            return datetime.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _f(v) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    return f if f == f else 0.0  # reject NaN


def summarize(lots: list[dict], reported_total: float | None,
              as_of: datetime.date) -> dict:
    """Render the lot tape into tax reads. `as_of` is supplied by the caller
    (America/New_York), never read from the server clock here."""
    gains = losses = 0
    gain_sum = loss_sum = 0.0
    st = lt = 0.0
    lots_sum = 0.0
    undated_losses = 0
    windows: list[dict] = []

    for lot in lots:
        pl = _f(lot.get("realized_pl"))
        lots_sum += pl
        st += _f(lot.get("st_gl"))
        lt += _f(lot.get("lt_gl"))
        if pl >= 0:
            gains += 1
            gain_sum += pl
        else:
            losses += 1
            loss_sum += pl
            closed = _parse_date(lot.get("close_date"))
            if closed is None:
                undated_losses += 1
                continue
            window_end = closed + datetime.timedelta(days=WASH_WINDOW_DAYS)
            if window_end >= as_of:
                windows.append({
                    "symbol": lot.get("symbol") or lot.get("underlying"),
                    "closed": closed.isoformat(),
                    "loss": round(pl, 2),
                    "window_end": window_end.isoformat(),
                    "safe_from": (window_end + datetime.timedelta(days=1)).isoformat(),
                })

    windows.sort(key=lambda w: w["window_end"])
    tie_out = None
    if reported_total is not None and lots:
        # No lots -> nothing to compare; a "mismatch" against an empty tape
        # would read as a parser failure when there is simply no tape.
        delta = round(lots_sum - _f(reported_total), 2)
        tie_out = {
            "lots_sum": round(lots_sum, 2),
            "reported_total": round(_f(reported_total), 2),
            "delta": delta,
            "ok": abs(delta) < 0.01,
        }

    return {
        "lots": len(lots),
        "gains": {"count": gains, "sum": round(gain_sum, 2)},
        "losses": {"count": losses, "sum": round(loss_sum, 2)},
        "term": {"short": round(st, 2), "long": round(lt, 2)},
        "open_wash_windows": windows,
        "undated_losses": undated_losses,
        "tie_out": tie_out,
        "note": (
            "Wash windows cover lots the tape dates; a purchase within 30 days "
            "BEFORE a loss sale also defers it, but position imports carry no "
            "open dates — the broker's 1099 is the authority at year end."
        ),
    }


# Federal estimated-tax due dates: Q1-Q3 in-year, Q4 the following January.
QUARTER_DUES = (("Q1", 4, 15), ("Q2", 6, 15), ("Q3", 9, 15), ("Q4", 1, 15))
# Stated marginal-rate assumptions for the coverage read. Labels, not advice:
# short-term gains are ordinary income, so a flat rate is a coverage check.
STATED_RATES = (0.24, 0.32, 0.35, 0.37)


def quarterly_position(payments: list[dict], schedule: dict,
                       realized: dict, as_of: datetime.date) -> dict:
    """Which quarters are paid / due / upcoming, and coverage of the realized
    tax at STATED rates. `realized` is the tax block's own gains/term split."""
    year = int(schedule.get("year") or as_of.year)
    q_amount = _f(schedule.get("quarterly_amount"))
    quarters = []
    paid_ytd = 0.0
    for q, month, day in QUARTER_DUES:
        due_year = year + 1 if q == "Q4" else year
        due = datetime.date(due_year, month, day)
        key = f"{year}-{q}"
        paid = round(sum(_f(p.get("amount")) for p in payments if p.get("quarter") == key), 2)
        if paid > 0 and (q_amount <= 0 or paid >= q_amount):
            status = "paid"
        elif paid > 0:
            status = "partial"
        elif due <= as_of:
            status = "due"
        else:
            status = "upcoming"
        paid_ytd += paid
        quarters.append({
            "quarter": key, "due": due.isoformat(), "paid": paid,
            "status": status, "scheduled": round(q_amount, 2),
        })
    paid_ytd = round(paid_ytd, 2)

    st = _f((realized.get("term") or {}).get("short"))
    lt = _f((realized.get("term") or {}).get("long"))
    estimates = {f"{int(r * 100)}%": round((st + lt) * r, 2) for r in STATED_RATES}
    coverage = {k: (round(100 * paid_ytd / v, 1) if v > 0 else None) for k, v in estimates.items()}
    return {
        "year": year,
        "quarterly_amount": round(q_amount, 2),
        "quarters": quarters,
        "paid_ytd": paid_ytd,
        "realized_total": round(st + lt, 2),
        "estimates": estimates,
        "coverage_pct": coverage,
        "note": (
            "Estimates apply a flat stated rate to this year's realized total"
            + ("" if lt else " (all short-term, so ordinary rates apply)")
            + " \u2014 bracket effects and other income are out of scope; this is "
            "a coverage check on the quarterly payments, not a filing."
        ),
    }
