"""Credit utilization and the recovery clocks.

Two derived things, and neither of them is a credit score.

**Utilization** is arithmetic the operator owns: revolving balances over recorded
credit limits. It can be computed forward from the schedules the liabilities store
already holds, which is why this module exists at all.

**The clocks** are the part a score cannot give you. Three dated factors this book
can observe — utilization falling on the recorded schedules, hard inquiries ageing
off, and new accounts reaching their first birthday — produce a WINDOW with named
falsifiers. They never produce a projected score: no bureau publishes the weights,
so any number would be invented, and an invented number beside real money is worse
than no number.

Pure functions: no data imports (repo invariant #1). Consumers pass rows in.
"""

from __future__ import annotations

import math
from datetime import date

# Conventional card-utilization bands. These are PRESENTATION thresholds, not
# scoring-model outputs — the weights are proprietary, so wherever they are shown
# they are labelled and never used to predict a score.
CONVENTIONAL_BANDS: tuple[float, ...] = (0.30, 0.10)
BANDS_SOURCE = "commonly cited card-utilization bands; not from a scoring model"

# Factor clocks, in months. FICO-style weights put utilization (~30%) and new
# credit (~10%) behind payment history; the first two are what this book can move
# or wait out.
NEW_ACCOUNT_MONTHS = 12
INQUIRY_MONTHS = 12
REACTION_MONTHS = 6


def _num(v) -> float | None:
    """Coerce to a finite float, or None. Never NaN."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _kind(row: dict) -> str:
    return str(row.get("kind") or "").strip().lower()


def _month_label(start: date, offset: int) -> str:
    total = start.year * 12 + (start.month - 1) + offset
    year, month = divmod(total, 12)
    return f"{year}-{month + 1:02d}"


def _add_months(d: date, months: int) -> date:
    total = d.year * 12 + (d.month - 1) + months
    year, month = divmod(total, 12)
    return date(year, month + 1, 1)


# ---------------------------------------------------------------------------
# Utilization
# ---------------------------------------------------------------------------


def utilization_now(rows: list[dict]) -> dict:
    """Per-card and aggregate REVOLVING utilization, with every exclusion named.

    Three buckets, and the middle one is the point:

      - **counted**: a revolving balance that carries a recorded limit;
      - **unmeasurable**: a revolving balance with NO limit — named, never dropped,
        because a balance that vanishes from the denominator flatters the ratio;
      - **excluded**: everything that is not a revolving balance (a loan, a
        commitment, a charge card) — named with its reason.

    ``state`` is ``unknown`` when nothing is measurable: a ratio over an empty
    denominator is not 0%.
    """
    counted: list[dict] = []
    unmeasurable: list[dict] = []
    excluded: list[dict] = []

    for row in rows:
        kind = _kind(row)
        balance = _num(row.get("balance")) or 0.0
        limit = _num(row.get("credit_limit"))
        entry = {"id": row.get("id"), "name": row.get("name"), "kind": kind}
        if kind != "revolving":
            excluded.append({**entry, "balance": balance, "reason": "not a revolving balance"})
            continue
        if not limit or limit <= 0:
            unmeasurable.append({**entry, "balance": balance, "reason": "no credit limit recorded"})
            continue
        counted.append({
            **entry,
            "balance": round(balance, 2),
            "limit": round(limit, 2),
            "utilization": round(balance / limit, 4),
            "autopay_full": bool(row.get("autopay_full")),
        })

    used = sum(c["balance"] for c in counted)
    capacity = sum(c["limit"] for c in counted)
    return {
        "per_card": sorted(counted, key=lambda c: -c["utilization"]),
        "aggregate": {
            "balance": round(used, 2),
            "limit": round(capacity, 2),
            "utilization": round(used / capacity, 4) if capacity > 0 else None,
        },
        "unmeasurable": unmeasurable,
        "excluded": excluded,
        "bands": list(CONVENTIONAL_BANDS),
        "bands_source": BANDS_SOURCE,
        "inputs": [
            f"{len(counted)} revolving balances with a recorded limit",
            f"excluded: {len(excluded)} non-revolving rows (installments, commitments, charge cards)",
            f"unmeasurable: {len(unmeasurable)} revolving rows carry no limit",
            f"bands are conventional, not model-derived: {BANDS_SOURCE}",
        ],
        "state": "ok" if capacity > 0 else "unknown",
    }


def project_utilization(rows: list[dict], months: int = 60, as_of: date | None = None) -> dict:
    """Month-by-month revolving utilization on the RECORDED schedules.

    Two modelling rules, both published in ``inputs`` because either one can flip
    the conclusion:

      - a row that clears every cycle (``autopay_full``) is a spending FLOAT, not a
        paydown: it is held FLAT. Modelling it as amortizing would predict a
        recovery the operator's own spending immediately undoes — the floor here
        is set by monthly spending, not by the promo balances.
      - any other revolving row amortizes at its recorded payment. A row with no
        payment is held flat and NAMED rather than projected as if it were free.

    Installment rows are excluded: their balances do not enter revolving
    utilization, and mixing them in would double-count the same dollar.
    """
    start = as_of or date.today()
    walk: list[dict] = []
    held: list[dict] = []
    unmeasurable: list[dict] = []

    for row in rows:
        if _kind(row) != "revolving":
            continue
        balance = _num(row.get("balance")) or 0.0
        limit = _num(row.get("credit_limit"))
        entry = {"id": row.get("id"), "name": row.get("name"), "balance": round(balance, 2)}
        if not limit or limit <= 0:
            unmeasurable.append({**entry, "reason": "no credit limit recorded"})
            continue
        payment = _num(row.get("payment"))
        if bool(row.get("autopay_full")) or not payment or payment <= 0:
            held.append({**entry, "limit": round(limit, 2),
                         "reason": "clears each cycle (float)" if row.get("autopay_full")
                         else "no payment recorded"})
            continue
        walk.append({**entry, "limit": round(limit, 2), "payment": round(payment, 2)})

    capacity = sum(r["limit"] for r in walk) + sum(r["limit"] for r in held)
    if capacity <= 0:
        return {
            "points": [], "crossings": {}, "unmeasurable": unmeasurable,
            "held_flat": held, "state": "unknown",
            "inputs": ["no revolving balance carries a recorded limit — nothing to project"],
        }

    points = []
    for m in range(months + 1):
        scheduled = sum(max(0.0, r["balance"] - r["payment"] * m) for r in walk)
        floating = sum(r["balance"] for r in held)
        total = scheduled + floating
        points.append({
            "month": _month_label(start, m),
            "scheduled": round(scheduled, 2),
            "float": round(floating, 2),
            "total": round(total, 2),
            "utilization": round(total / capacity, 4),
        })

    crossings: dict[str, dict] = {}
    for band in CONVENTIONAL_BANDS:
        for point in points:
            if point["utilization"] is not None and point["utilization"] <= band:
                crossings[f"{band:.2f}"] = {"month": point["month"], "months_out": points.index(point)}
                break

    return {
        "points": points,
        "crossings": crossings,
        "capacity": round(capacity, 2),
        "held_flat": held,
        "unmeasurable": unmeasurable,
        "bands_source": BANDS_SOURCE,
        "inputs": [
            f"capacity {capacity:,.2f} across {len(walk) + len(held)} revolving rows",
            f"{len(walk)} row(s) amortize at the recorded payment",
            f"{len(held)} row(s) held FLAT: a balance that clears each cycle is a spending float, "
            "so the floor is set by spending, not by the promos",
            "installment balances are excluded (they are not revolving utilization)",
        ],
        "state": "ok",
    }


# ---------------------------------------------------------------------------
# The clocks
# ---------------------------------------------------------------------------


def _months_between(a: date, b: date) -> int:
    return (b.year - a.year) * 12 + (b.month - a.month)


def recovery_windows(
    rows: list[dict],
    projection: dict | None = None,
    as_of: date | None = None,
) -> dict:
    """The dated clocks, and the WINDOW they imply. Never a projected score.

    Each clock is a date the operator can hold in a calendar:

      - **utilization** — the month the projection crosses the conventional bands
        (from ``project_utilization``, so it moves when the payment schedule does);
      - **inquiries** — a hard inquiry stops counting on most models 12 months on;
      - **new accounts** — the drag is largest in a new account's first year, so
        every account opened in the last two years carries an anniversary.

    The window is the LATEST of those dates plus a reaction period, because a score
    is a lagging report of them. ``falsifiers`` states what would prove the
    "it's just the new accounts" reading wrong — and that is the whole value: a
    dated claim that can fail.
    """
    today = as_of or date.today()
    accounts: list[dict] = []
    for row in rows:
        opened_raw = row.get("originated_at")
        if not opened_raw:
            continue
        try:
            opened = date.fromisoformat(str(opened_raw))
        except (TypeError, ValueError):
            continue
        age = _months_between(opened, today)
        accounts.append({
            "id": row.get("id"),
            "name": row.get("name"),
            "opened_at": opened.isoformat(),
            "age_months": age,
            "inquiry_clears": _add_months(opened, INQUIRY_MONTHS).isoformat(),
            "first_birthday": _add_months(opened, NEW_ACCOUNT_MONTHS).isoformat(),
        })
    recent = sorted([a for a in accounts if a["age_months"] <= 24], key=lambda a: a["opened_at"])

    utilization_under_30 = (projection or {}).get("crossings", {}).get("0.30")
    candidates = [a["first_birthday"] for a in recent] + (
        [_month_label(today, utilization_under_30["months_out"]) + "-01"] if utilization_under_30 else []
    )
    window = None
    if candidates:
        latest = max(candidates)
        start = date.fromisoformat(latest)
        newest_birthday = max([a["first_birthday"] for a in recent]) if recent else None
        util_part = (
            f"util < 30% ({utilization_under_30['month']})"
            if utilization_under_30
            else "util < 30% not projected inside the horizon"
        )
        age_part = (
            f"newest account's first birthday ({newest_birthday})"
            if newest_birthday
            else "no account clock recorded"
        )
        window = {
            "start": start.isoformat(),
            "end": _add_months(start, REACTION_MONTHS).isoformat(),
            "basis": f"latest of: {util_part}; {age_part}",
            "reaction_months": REACTION_MONTHS,
        }

    falsifiers = []
    if window:
        falsifiers.append(
            f"If utilization is back under 30% and no score reading has improved from its "
            f"{today.isoformat()} level by {window['end']}, the new-accounts reading is wrong — "
            "the next step is pulling all three bureau reports to hunt for a derogatory mark or an error."
        )
    if not recent:
        falsifiers.append(
            "No account carries a recorded opening date, so the new-account clock cannot be "
            "placed in time — record `originated_at` on the cards opened for financing."
        )
    if not utilization_under_30:
        falsifiers.append(
            "Utilization is not projected to fall under the conventional 30% band inside the "
            "horizon, so the window is set by account age alone."
        )

    return {
        "as_of": today.isoformat(),
        "new_accounts": recent,
        "all_accounts": accounts,
        "utilization_under_30": utilization_under_30,
        "window": window,
        "falsifiers": falsifiers,
        "inputs": [
            f"{len(recent)} account(s) opened within 24 months",
            f"factor clocks: inquiries {INQUIRY_MONTHS}mo, new-account age {NEW_ACCOUNT_MONTHS}mo, "
            f"reaction {REACTION_MONTHS}mo",
            "a projected SCORE is deliberately absent: the weights are proprietary",
        ],
        "state": "ok" if window else "unknown",
    }
