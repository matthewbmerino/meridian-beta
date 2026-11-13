"""Year-over-year seasonal returns — pure math, no data imports.

Input: dated closes (any span, ascending). Output: one normalized series per
calendar year, so the years overlay on a single chart the TradingView way.

Normalization: cumulative % from the FIRST close of that year present in the
data. The newest year is partial by construction — it is labeled, never padded
to look complete. `day` is the day-of-year (1-366), which is how the overlay
aligns Feb 3 of every year at the same x.
"""

from __future__ import annotations

import datetime

MAX_YEARS = 8


def _date(v):
    if v is None or v == "":
        return None
    s = str(v).strip()
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S+00:00", "%m/%d/%Y"):
        try:
            return datetime.datetime.strptime(s[: len(fmt) + 4] if "T" in s else s, fmt).date()
        except ValueError:
            continue
    try:
        return datetime.date.fromisoformat(s[:10])
    except ValueError:
        return None


def _f(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None  # reject NaN


def seasonal_series(rows: list[dict], max_years: int = MAX_YEARS) -> dict:
    """rows: [{"date": ..., "close": ...}] (any order) -> per-year series."""
    by_year: dict[int, list[tuple[datetime.date, float]]] = {}
    for r in rows:
        d = _date(r.get("date"))
        c = _f(r.get("close"))
        if d is None or c is None or c <= 0:
            continue
        by_year.setdefault(d.year, []).append((d, c))

    years = sorted(by_year)[-max_years:]
    out = []
    for y in years:
        pts = sorted(by_year[y])
        base = pts[0][1]
        series = [{"day": d.timetuple().tm_yday, "pct": round((c / base - 1) * 100, 2)} for d, c in pts]
        vals = [p["pct"] for p in series]
        last_day = series[-1]["day"]
        out.append({
            "year": y,
            "points": series,
            "final": vals[-1],
            "max": max(vals),
            "min": min(vals),
            # the newest year, or any year whose tape stops well short of Dec 31
            "partial": y == years[-1] and last_day < 355,
            "from": pts[0][0].isoformat(),
            "through": pts[-1][0].isoformat(),
        })

    return {
        "years": out,
        "note": (
            "Each line is cumulative % from that year's first close in the tape — "
            "a shape comparison, not a calendar-anchored total."
        ),
    }
