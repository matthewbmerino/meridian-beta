"""Liability analytics — amortization schedules and the obligation rollup.

Pure functions, no side effects, no data fetching (see analysis/AGENTS.md).
Entries arrive as plain dicts (the shape ``data/liabilities.py`` persists) and
every result is a plain dict with ``None`` — never NaN — for an unknown value.

The central question this module answers: of what I owe, how much falls due
soon enough to matter for the cash I have? That is the current/long-term split,
and it comes from a real amortization schedule, never from a rule of thumb.

Two families of entry share the ledger and must never be conflated:

  - DEBT (``revolving``, ``installment``) — a balance owed, repaid over time.
    It amortizes, carries a rate, and is netted out of ``net_worth``.
  - COMMITMENTS (``recurring``, ``oneoff``) — a cash claim, not a balance.
    It never amortizes and is never netted out of ``net_worth``, but it does
    reserve cash: the question "how much of my cash is actually available?"
    has the same answer for an HOA bill as for a card statement.

Debt totals, the current/long-term split, APR, carry and paydown ordering are
computed over DEBT rows ONLY. Commitments are surfaced separately and folded
into the cash reserve.
"""

from __future__ import annotations

import math
from datetime import date

# Mirrors data/liabilities.KINDS/DEBT_KINDS. Duplicated deliberately: the
# analysis layer must not import data.* (purity invariant), and this module must
# stay usable on a bare list of dicts in a unit test.
DEBT_KINDS: tuple[str, ...] = ("revolving", "installment")
COMMITMENT_KINDS: tuple[str, ...] = ("recurring", "oneoff")
_KINDS: tuple[str, ...] = DEBT_KINDS + COMMITMENT_KINDS
_DEFAULT_KIND = "installment"

# The current/non-current boundary. A liability due within this many months is
# short-term (current); the remainder is long-term.
CURRENT_WINDOW_MONTHS = 12

# A balance under half a cent is settled — float dust from the final payment.
_ZERO_EPS = 0.005


def _kind_of(entry: dict) -> str:
    """Read-time kind normalization — mirrors data/liabilities.normalize_kind.

    Unknown/missing reads as ``installment``: the conservative classification.
    """
    k = str(entry.get("kind") or "").strip().lower()
    return k if k in _KINDS else _DEFAULT_KIND


def _num(v) -> float | None:
    """Coerce to a finite float, or None. Never returns NaN."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def monthly_rate(apr_pct: float) -> float:
    """Convert an annual percentage rate (e.g. 4.9) to a monthly fraction."""
    return max(0.0, float(apr_pct)) / 100.0 / 12.0


def next_due_date(as_of: date, due_day: int) -> date:
    """The next occurrence of *due_day* on or after *as_of*.

    ``due_day`` is capped at 28 by the store, so the date is always valid and
    never needs clamping.
    """
    day = max(1, min(28, int(due_day)))
    if as_of.day <= day:
        return date(as_of.year, as_of.month, day)
    year, month = (as_of.year + 1, 1) if as_of.month == 12 else (as_of.year, as_of.month + 1)
    return date(year, month, day)


def amortize(balance: float, apr_pct: float, payment: float) -> dict:
    """Walk a loan's payment schedule from *balance* at *payment* per month.

    Returns ``{months, total_interest, interest_next_12, principal_next_12,
    final_payment, never_amortizes}``.

    ``months`` is the number of payments until the balance is settled, or None
    when the payment never covers the monthly interest — a loan that never
    amortizes is reported as such (``never_amortizes=True``) rather than given
    an invented payoff date. In that case ``interest_next_12`` is the interest
    accrued over a year with no principal retired: the real cost of standing
    still, not a placeholder zero.
    """
    bal = _num(balance) or 0.0
    pay = _num(payment) or 0.0
    rate = monthly_rate(_num(apr_pct) or 0.0)

    if bal <= _ZERO_EPS or pay <= 0:
        return {
            "months": 0 if bal <= _ZERO_EPS else None,
            "total_interest": 0.0 if bal <= _ZERO_EPS else None,
            "interest_next_12": 0.0,
            "principal_next_12": 0.0,
            "final_payment": 0.0,
            "never_amortizes": bal > _ZERO_EPS,
        }

    remaining = bal
    months = 0
    total_interest = 0.0
    interest_next_12 = 0.0
    principal_next_12 = 0.0
    final_payment = 0.0
    current_pay = pay
    never_amortizes = False

    # A hard cap guards against a pathological row spinning the loop; at 600
    # months (50 years) the store already rejects longer terms on write.
    while remaining > _ZERO_EPS and months < 1200:
        interest = remaining * rate
        if pay <= interest:
            # Payment does not even cover the interest — the balance grows.
            never_amortizes = True
            break
        principal = pay - interest
        if principal >= remaining:
            # Final payment: only the balance plus its interest is owed.
            principal = remaining
            final_payment = round(principal + interest, 2)
            current_pay = final_payment
        else:
            current_pay = pay
        remaining -= principal
        total_interest += interest
        if months < CURRENT_WINDOW_MONTHS:
            interest_next_12 += interest
            principal_next_12 += principal
        months += 1

    if never_amortizes:
        return {
            "months": None,
            "total_interest": None,
            "interest_next_12": round(bal * rate * CURRENT_WINDOW_MONTHS, 2),
            "principal_next_12": 0.0,
            "final_payment": None,
            "never_amortizes": True,
        }

    if final_payment == 0.0:
        final_payment = round(current_pay, 2)

    return {
        "months": months,
        "total_interest": round(total_interest, 2),
        "interest_next_12": round(interest_next_12, 2),
        "principal_next_12": round(min(principal_next_12, bal), 2),
        "final_payment": final_payment,
        "never_amortizes": False,
    }


def _add_months(as_of: date, months: int) -> date:
    """*as_of* advanced by *months*, clamped to a valid day of month."""
    total = as_of.month - 1 + months
    year = as_of.year + total // 12
    month = total % 12 + 1
    day = min(as_of.day, [31, 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28,
                          31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1])
    return date(year, month, day)


def _monthly_service(entry: dict, kind: str, balance: float) -> tuple[float | None, bool]:
    """The amount that leaves cash each month, plus whether it is known.

    Revolving: the stored ``payment``, or ``balance`` when ``autopay_full`` —
    paying the statement in full IS the monthly service, by definition, not an
    estimate. Neither present means the service is unknown.
    Installment: the stored ``payment`` (required by the store on write).
    Recurring: ``balance`` IS the monthly claim.
    Oneoff: None — a single payment is not a monthly rate, and reporting it as
    one would inflate the burn rate by its full amount every month. Its cash
    claim is counted once, in ``cash_claim_next_12``.

    Always returns a tuple so a caller can distinguish "zero" from "unknown".
    """
    if kind == "recurring":
        return round(balance, 2), True
    if kind == "oneoff":
        # There is deliberately no monthly amount here, and that is known.
        return None, True
    pay = _num(entry.get("payment"))
    if kind == "revolving" and bool(entry.get("autopay_full")):
        return round(balance, 2), True
    if pay is not None and pay > 0:
        return round(pay, 2), True
    return None, False


def liability_row(entry: dict, as_of: date) -> dict:
    """One liability's schedule, split into current vs long-term principal.

    Revolving balances are short-term in their entirety — a card is payable on
    demand, so there is nothing long-term about it regardless of the payment.
    Installment principal splits at the ``CURRENT_WINDOW_MONTHS`` boundary by
    the actual amortization schedule.

    An installment with no usable payment (only reachable on a hand-edited
    store — writes require one) is treated as due NOW: the conservative
    direction for an obligation, since assuming it is safely far out is the
    error that hurts.
    """
    kind = _kind_of(entry)
    balance = round(_num(entry.get("balance")) or 0.0, 2)
    apr = _num(entry.get("apr")) or 0.0
    due_day = entry.get("due_day")
    service, service_known = _monthly_service(entry, kind, balance)

    row = {
        "id": entry.get("id"),
        "kind": kind,
        "is_debt": kind in DEBT_KINDS,
        "name": entry.get("name") or "",
        "balance": balance,
        "apr": round(apr, 4),
        "payment": _num(entry.get("payment")),
        "monthly_service": service,
        "service_known": service_known,
        "due_day": due_day if isinstance(due_day, int) else None,
        "autopay_full": bool(entry.get("autopay_full")),
        "term_months": entry.get("term_months"),
        "originated_at": entry.get("originated_at"),
        "note": entry.get("note"),
        "never_amortizes": False,
        "schedule_unavailable": False,
        "months_remaining": None,
        "payoff_date": None,
        "current_portion": balance,
        "long_term_portion": 0.0,
        "interest_next_12": 0.0,
        "total_interest_remaining": None,
        "final_payment": None,
        # What a single payment claims from cash — the monthly service where one
        # exists, otherwise the one-off amount. Used by the next-payment line.
        "next_claim": service,
        # The commitment's claim on cash over the current window. For debt this
        # is the current portion; for a commitment it is the amount itself.
        "cash_claim_next_12": 0.0,
    }

    if kind not in DEBT_KINDS:
        # A commitment claims cash without ever being repaid: no schedule, no
        # payoff, no balance. It therefore contributes NOTHING to the debt split
        # or to net worth — recording an HOA bill as debt would understate net
        # worth by its amount forever.
        row["next_claim"] = round(balance, 2)
        row["cash_claim_next_12"] = (
            round(balance * CURRENT_WINDOW_MONTHS, 2)
            if kind == "recurring"   # claims this amount EVERY month
            else round(balance, 2)   # claims it once
        )
        row["current_portion"] = 0.0
        row["long_term_portion"] = 0.0
        return row

    if kind == "revolving":
        if row["autopay_full"]:
            # Clears on the next statement — one cycle, no interest carried.
            row["months_remaining"] = 1
            row["payoff_date"] = next_due_date(as_of, due_day or as_of.day).isoformat()
            row["interest_next_12"] = 0.0
            row["total_interest_remaining"] = 0.0
            row["final_payment"] = balance
        elif service is not None:
            sched = amortize(balance, apr, service)
            row.update({
                "months_remaining": sched["months"],
                "never_amortizes": sched["never_amortizes"],
                "interest_next_12": sched["interest_next_12"],
                "total_interest_remaining": sched["total_interest"],
                "final_payment": sched["final_payment"],
            })
            if sched["months"]:
                row["payoff_date"] = _add_months(as_of, sched["months"]).isoformat()
        else:
            # No payment recorded and not marked full-pay: the carrying cost is
            # unknown, but a balance at a positive APR is still accruing. Report
            # a year carried flat rather than pretending it is free.
            row["schedule_unavailable"] = True
            row["interest_next_12"] = round(balance * monthly_rate(apr) * CURRENT_WINDOW_MONTHS, 2)
        # A card is payable on demand: short-term in full.
        row["current_portion"] = balance
        row["long_term_portion"] = 0.0
        row["cash_claim_next_12"] = round(balance, 2)
        return row

    # --- installment ---
    if service is None:
        row["schedule_unavailable"] = True
        row["current_portion"] = balance
        row["long_term_portion"] = 0.0
        row["cash_claim_next_12"] = round(balance, 2)
        return row

    sched = amortize(balance, apr, service)
    row.update({
        "months_remaining": sched["months"],
        "never_amortizes": sched["never_amortizes"],
        "interest_next_12": sched["interest_next_12"],
        "total_interest_remaining": sched["total_interest"],
        "final_payment": sched["final_payment"],
    })
    if sched["months"]:
        row["payoff_date"] = _add_months(as_of, sched["months"]).isoformat()

    current = min(sched["principal_next_12"], balance)
    row["current_portion"] = round(current, 2)
    row["long_term_portion"] = round(balance - current, 2)
    row["cash_claim_next_12"] = row["current_portion"]
    return row


def liability_rows(entries: list[dict], as_of: date) -> list[dict]:
    """Schedule every liability, preserving the CALLER's order.

    Order is presentation and belongs to the caller: the store supplies the
    operator's manual order, and the UI decides when a column sort overrides it.
    An analytics function silently re-sorting would fight both.
    """
    return [liability_row(e, as_of) for e in entries]


def _bucket(rows: list[dict], is_debt: bool) -> dict:
    total = round(sum(r["balance"] for r in rows), 2)
    current = round(sum(r["current_portion"] for r in rows), 2)
    service = round(sum(r["monthly_service"] for r in rows if r["monthly_service"]), 2)
    return {
        "count": len(rows),
        # For a commitment kind ``total`` is the CLAIM (a monthly amount, or a
        # single payment), not a balance owed. ``is_debt`` says which, so a
        # consumer never has to know the kind list to read ``total`` correctly.
        "is_debt": is_debt,
        "total": total,
        "current": current,
        "long_term": round(total - current, 2),
        "monthly_service": service,
    }


def rollup(entries: list[dict], as_of: date, liquid_cash: float | None = None) -> dict:
    """The obligation picture: debt totals, the current/long-term split, coverage.

    Debt and commitments are reported SEPARATELY and never summed into one
    figure. ``total``/``current``/``long_term``/``monthly_debt_service``/
    ``annual_interest``/``weighted_apr`` cover DEBT rows only, so a monthly HOA
    bill can never inflate the balance owed or shrink net worth. Commitments
    report their own ``monthly_commitments`` and their window claim,
    ``commitments_next_12``.

    ``liquid_cash`` is optional. When supplied, the result carries
    ``cash_coverage`` — what is left of that cash once near-term claims are set
    aside. Both families reserve cash, because "how much of this is actually
    available to me?" has the same answer whether the money is owed or merely
    promised. Coverage is a relationship between verified numbers, not a
    projection.

    Callers should pass the operator's SPENDABLE cash (``cash_balance`` from
    ``data/account.get_combined_summary`` — brokerage sweep + liquid external),
    not external balances alone, so the coverage figure and the cash figure the
    UI shows are the same money.
    """
    rows = liability_rows(entries, as_of)
    debt_rows = [r for r in rows if r["is_debt"]]
    commitment_rows = [r for r in rows if not r["is_debt"]]

    total = round(sum(r["balance"] for r in debt_rows), 2)
    current = round(sum(r["current_portion"] for r in debt_rows), 2)
    long_term = round(total - current, 2)
    monthly_service = round(
        sum(r["monthly_service"] for r in debt_rows if r["monthly_service"]), 2
    )
    annual_interest = round(sum(r["interest_next_12"] for r in debt_rows), 2)
    monthly_commitments = round(
        sum(r["monthly_service"] for r in commitment_rows if r["monthly_service"]), 2
    )
    commitments_next_12 = round(sum(r["cash_claim_next_12"] for r in commitment_rows), 2)
    # The real monthly drain: debt service and repeating commitments together.
    monthly_outflow = round(monthly_service + monthly_commitments, 2)

    weighted_apr = 0.0
    if total > 0:
        weighted_apr = round(sum(r["balance"] * r["apr"] for r in debt_rows) / total, 4)

    by_kind = {
        k: _bucket([r for r in rows if r["kind"] == k], k in DEBT_KINDS) for k in _KINDS
    }

    # The soonest scheduled payment, among entries that carry both a due day and
    # a known claim. A commitment's next payment is a real upcoming payment and
    # belongs here; no due day recorded means no claim.
    next_payment = None
    for r in rows:
        if r["due_day"] is None or not r["next_claim"]:
            continue
        due = next_due_date(as_of, r["due_day"])
        candidate = {
            "name": r["name"],
            "amount": float(r["next_claim"]),
            "due_date": due.isoformat(),
            "days_until": (due - as_of).days,
        }
        if next_payment is None or candidate["days_until"] < next_payment["days_until"]:
            next_payment = candidate

    result = {
        "as_of": as_of.isoformat(),
        "count": len(rows),
        "debt_count": len(debt_rows),
        "commitment_count": len(commitment_rows),
        "total": total,
        "current": current,
        "long_term": long_term,
        "monthly_debt_service": monthly_service,
        "monthly_commitments": monthly_commitments,
        "monthly_outflow": monthly_outflow,
        "commitments_next_12": commitments_next_12,
        "annual_interest": annual_interest,
        "weighted_apr": weighted_apr,
        "high_rate_balance": round(
            sum(r["balance"] for r in debt_rows if r["apr"] >= 5.0), 2
        ),
        "next_payment": next_payment,
        "by_kind": by_kind,
        "items": rows,
        "cash_coverage": None,
    }

    if liquid_cash is not None and math.isfinite(float(liquid_cash)):
        cash = round(float(liquid_cash), 2)
        # Both families claim cash within the same window: debt by its current
        # portion, commitments by what they take over the coming 12 months
        # (a repeating bill twelve times, a one-off once).
        reserved = round(current + commitments_next_12, 2)
        result["cash_coverage"] = {
            "liquid_cash": cash,
            "current_liabilities": current,
            "commitments_next_12": commitments_next_12,
            "reserved": reserved,
            "cash_after_reserve": round(cash - reserved, 2),
            "coverage_ratio": round(cash / reserved, 4) if reserved > 0 else None,
            "months_of_service": (
                round(cash / monthly_outflow, 2) if monthly_outflow > 0 else None
            ),
        }
    return result


def net_worth(
    net_liquidation: float,
    total_liabilities: float,
    other_assets: float | None = None,
) -> float:
    """Assets minus debt. One definition, used everywhere it is displayed.

    ``net_liquidation`` stays untouched as the asset figure — debt never
    silently reduces it (see data/liabilities.py). This is the only place the
    two are combined.

    ``other_assets`` adds a parallel-ledger figure (the alternatives book's
    marked value: property, vehicles, collectibles) that is deliberately NOT in
    ``net_liquidation``. Callers that omit it get the liquid-only number, so
    the two figures come from ONE formula with one definition of "assets" —
    never a second net-worth calculation that can drift from this one.
    """
    assets = _num(net_liquidation) or 0.0
    if other_assets is not None:
        assets += _num(other_assets) or 0.0
    debt = _num(total_liabilities) or 0.0
    return round(assets - debt, 2)


# ---------------------------------------------------------------------------
# Debt analysis — projection, payoff order, cost of carry, data quality
#
# These take the OUTPUT of ``liability_rows`` (already-scheduled rows) rather
# than raw entries, so a caller composes them without re-amortizing the book
# once per question. ``debt_analysis`` is the orchestrator that does the whole
# page from raw entries.
# ---------------------------------------------------------------------------

DEFAULT_HORIZON_MONTHS = 60

# An APR at or above this is the most expensive money in a retail book and is
# where the next dollar of repayment belongs.
HIGH_APR_PCT = 15.0


def _walk(balance: float, rate: float, payment: float, months: int) -> tuple[list[float], list[float]]:
    """Walk *months* payments of *payment*: remaining balance + interest each month.

    Returns UNROUNDED values. Rounding is applied when a figure is emitted, never
    while accumulating — summing pre-rounded monthly interest drifts cents away
    from the closed-form remaining-interest figure the row itself reports, and
    the same loan must not appear to cost two different amounts on two surfaces.

    The final payment is clamped to what is actually owed (balance + that
    month's interest), so the schedule lands exactly on zero rather than
    overshooting.
    """
    balances: list[float] = []
    interest_paid: list[float] = []
    remaining = float(balance)
    for _ in range(months):
        if remaining <= _ZERO_EPS:
            balances.append(0.0)
            interest_paid.append(0.0)
            continue
        interest = remaining * rate
        principal = payment - interest
        if principal <= 0:
            # Payment does not clear the interest — the balance holds.
            balances.append(remaining)
            interest_paid.append(interest)
            continue
        if principal >= remaining:
            principal = remaining
        remaining -= principal
        balances.append(max(remaining, 0.0))
        interest_paid.append(interest)
    return balances, interest_paid


def projection(rows: list[dict], as_of: date, months: int = DEFAULT_HORIZON_MONTHS) -> dict:
    """Month-by-month debt paydown: total balance, principal, and interest.

    This is a PROJECTION of recorded schedules, not a forecast: each row is
    walked at its own stored payment and rate, and a row with no usable schedule
    holds its balance flat (``unscheduled_ids``) rather than being given an
    invented paydown.

    ``net_carry``-style dollar claims live in ``carry_analysis``; nothing here
    depends on a market rate.
    """
    horizon = max(1, int(months))
    total_now = round(sum(r["balance"] for r in rows), 2)

    if not rows:
        # Nothing to project: an empty book is not 60 months of zeros.
        return {
            "as_of": as_of.isoformat(),
            "horizon_months": horizon,
            "points": [],
            "debt_free_month": None,
            "interest_over_horizon": 0.0,
            "principal_over_horizon": 0.0,
            "unscheduled_ids": [],
            "outstanding_ids": [],
        }

    per_item: list[tuple[dict, list[float], list[float]]] = []
    unscheduled: list[str] = []
    for r in rows:
        rate = monthly_rate(r["apr"])
        service = r["monthly_service"]
        if r["autopay_full"]:
            # Clears on the next statement.
            per_item.append((r, [0.0] * horizon, [0.0] * horizon))
        elif service is None or service <= 0 or r["never_amortizes"]:
            # No usable schedule: the balance and its accrual hold flat, and the
            # row is reported as unscheduled rather than given an invented paydown.
            per_item.append((r, [r["balance"]] * horizon, [r["balance"] * rate] * horizon))
            unscheduled.append(r["id"])
        else:
            per_item.append((r, *_walk(r["balance"], rate, service, horizon)))

    points: list[dict] = []
    cumulative_raw = 0.0
    prev_total = total_now
    for m in range(horizon):
        total = round(sum(b[m] for _, b, _ in per_item), 2)
        raw_interest = sum(i[m] for _, _, i in per_item)
        # Accumulate UNROUNDED and round only when emitting: re-rounding the
        # running total every month drifts the final figure cents away from the
        # closed-form remaining interest the same row reports elsewhere.
        cumulative_raw += raw_interest
        points.append({
            "month": _add_months(as_of, m + 1).strftime("%Y-%m"),
            "total": total,
            "principal": round(prev_total - total, 2),
            "interest": round(raw_interest, 2),
            "cumulative_interest": round(cumulative_raw, 2),
            "by_id": {r["id"]: round(b[m], 2) for r, b, _ in per_item},
        })
        prev_total = total
    cumulative_interest = round(cumulative_raw, 2)

    debt_free_month = None
    for p in points:
        if p["total"] <= _ZERO_EPS:
            debt_free_month = p["month"]
            break

    return {
        "as_of": as_of.isoformat(),
        "horizon_months": horizon,
        "points": points,
        "debt_free_month": debt_free_month,
        "interest_over_horizon": cumulative_interest,
        "principal_over_horizon": round(total_now - (points[-1]["total"] if points else total_now), 2),
        # No usable schedule — the balance is held flat, not assumed away.
        "unscheduled_ids": unscheduled,
        # Still outstanding when the horizon ends.
        "outstanding_ids": [r["id"] for r, b, _ in per_item if b and b[-1] > _ZERO_EPS],
    }


def payoff_timeline(rows: list[dict]) -> list[dict]:
    """Every liability ordered by when it retires, soonest first.

    Unscheduled rows (no payoff date) sort last — they have no place on a
    timeline and pretending otherwise would hide the data gap.
    """
    ordered = sorted(
        rows,
        key=lambda r: (r["payoff_date"] is None, r["payoff_date"] or "", -r["balance"]),
    )
    return [
        {
            "id": r["id"],
            "name": r["name"],
            "kind": r["kind"],
            "balance": r["balance"],
            "apr": r["apr"],
            "monthly_service": r["monthly_service"],
            "payoff_date": r["payoff_date"],
            "months_remaining": r["months_remaining"],
            "current_portion": r["current_portion"],
            "long_term_portion": r["long_term_portion"],
            "interest_next_12": r["interest_next_12"],
            "never_amortizes": r["never_amortizes"],
            "schedule_unavailable": r["schedule_unavailable"],
        }
        for r in ordered
    ]


def _action(row: dict, opportunity_rate: float | None) -> str:
    """What the data says to do with this balance. Never a guess — each branch
    is a direct consequence of a recorded field."""
    if row["never_amortizes"]:
        return "Payment does not cover interest — increase it"
    if row["schedule_unavailable"] or (row["monthly_service"] is None and not row["autopay_full"]):
        return "No payment recorded — this cannot be scheduled"
    if row["autopay_full"]:
        return "Float — clears each statement, carries no interest"
    if row["apr"] <= 0:
        return "Keep — free carry"
    if opportunity_rate is not None and row["apr"] < opportunity_rate:
        return f"Keep — below the {opportunity_rate:.2f}% risk-free rate"
    if row["apr"] >= HIGH_APR_PCT:
        return "Highest-cost balance — clear this first"
    return "Pay down ahead of the rest"


def priority_ranking(rows: list[dict], opportunity_rate: float | None = None) -> list[dict]:
    """Where the next dollar of repayment does the most good.

    Ordered by ACTUAL next-12-month INTEREST COST, highest first — then APR, then
    balance. Ranking on APR alone puts a full-pay card that costs nothing at the
    top of a paydown list, which is advice that wastes money; the cost is what
    the money is doing, and the APR is shown alongside it. ``share_of_interest``
    is the fraction of the book's next-12-month interest this row is responsible
    for: the honest measure of what clearing it buys.
    """
    total_interest = sum(r["interest_next_12"] for r in rows)
    ranked = sorted(rows, key=lambda r: (-r["interest_next_12"], -r["apr"], -r["balance"]))
    out = []
    for i, r in enumerate(ranked, 1):
        out.append({
            "rank": i,
            "id": r["id"],
            "name": r["name"],
            "kind": r["kind"],
            "balance": r["balance"],
            "apr": r["apr"],
            "monthly_service": r["monthly_service"],
            "interest_next_12": r["interest_next_12"],
            "share_of_interest": (
                round(r["interest_next_12"] / total_interest, 4) if total_interest > 0 else None
            ),
            "spread": (
                round(opportunity_rate - r["apr"], 4) if opportunity_rate is not None else None
            ),
            "action": _action(r, opportunity_rate),
        })
    return out


def carry_analysis(
    rows: list[dict],
    opportunity_rate: float | None,
    benchmark: dict | None = None,
) -> dict | None:
    """Is this debt expensive relative to the risk-free rate?

    The hurdle is deliberately the RISK-FREE rate, not an expected portfolio
    return: borrowing is only accretive if the money is worth more than the
    safest alternative. ``net_carry`` is the annual dollar value of that spread
    — the amount the book gains (positive) or bleeds (negative) per year by
    holding this debt at these rates.

    Unstated costs are NOT modelled (taxes, origination fees, promo-rate step-ups
    when a 0% window ends). ``inputs`` lists exactly what was used, and the
    verdict is ``unknown`` rather than guessed when the benchmark is missing.
    """
    total = round(sum(r["balance"] for r in rows), 2)
    if total <= 0:
        return None

    weighted_apr = round(sum(r["balance"] * r["apr"] for r in rows) / total, 4)
    annual_carry = round(sum(r["balance"] * r["apr"] / 100 for r in rows), 2)
    bench = benchmark or {}

    out = {
        "weighted_apr": weighted_apr,
        "annual_carry": annual_carry,
        "opportunity_rate": opportunity_rate,
        "opportunity_label": bench.get("label"),
        "opportunity_as_of": bench.get("as_of"),
        "opportunity_series": bench.get("series"),
        "spread": None,
        "net_carry": None,
        "verdict": "unknown",
        "accretive_balance": None,
        "dilutive_balance": None,
        "inputs": [
            f"weighted APR {weighted_apr:.2f}% over {len(rows)} liabilities",
            (
                f"risk-free {opportunity_rate:.2f}% ({bench.get('series', 'FRED')}, {bench.get('as_of')})"
                if opportunity_rate is not None
                else "risk-free benchmark unavailable"
            ),
            "excludes taxes, fees and promo-rate step-ups",
        ],
    }

    if opportunity_rate is None:
        return out

    spread = round(opportunity_rate - weighted_apr, 4)
    out.update({
        "spread": spread,
        "net_carry": round(sum(r["balance"] * (opportunity_rate - r["apr"]) / 100 for r in rows), 2),
        "accretive_balance": round(
            sum(r["balance"] for r in rows if r["apr"] < opportunity_rate), 2
        ),
        "dilutive_balance": round(
            sum(r["balance"] for r in rows if r["apr"] > opportunity_rate), 2
        ),
        "verdict": "accretive" if spread > 0 else ("dilutive" if spread < 0 else "neutral"),
    })
    return out


def data_quality_flags(rows: list[dict]) -> list[dict]:
    """Qualitative read: what the recorded debt cannot tell us, or contradicts.

    A row recorded as a CARD with a term, or one with no payment, is not a
    modelling failure — it is a recording gap, and it is exactly the kind of
    thing that makes every downstream number quietly wrong. Ordered most severe
    first.
    """
    severity_rank = {"critical": 0, "warning": 1, "info": 2}
    flags: list[dict] = []

    for r in rows:
        if r["never_amortizes"]:
            flags.append({
                "severity": "critical",
                "code": "never_amortizes",
                "id": r["id"],
                "name": r["name"],
                "message": "The payment does not cover the monthly interest — this balance never falls",
            })
        if r["kind"] == "revolving" and r["term_months"]:
            flags.append({
                "severity": "warning",
                "code": "card_with_term",
                "id": r["id"],
                "name": r["name"],
                "message": (
                    f"Recorded as a card with a {r['term_months']}-month term, which reads as a loan — "
                    "switch it to Loan to get a payoff date and the short/long-term split"
                ),
            })
        elif r["kind"] == "revolving" and not r["autopay_full"] and r["payment"] is None:
            flags.append({
                "severity": "warning",
                "code": "carrying_unknown",
                "id": r["id"],
                "name": r["name"],
                "message": "Card with no payment and not marked paid-in-full — its carrying cost is unknown",
            })
        if r["schedule_unavailable"] and r["kind"] == "installment":
            flags.append({
                "severity": "warning",
                "code": "unscheduled",
                "id": r["id"],
                "name": r["name"],
                "message": "No schedule — no payoff date or long-term split is possible for this row",
            })
        if r["apr"] >= HIGH_APR_PCT:
            flags.append({
                "severity": "warning",
                "code": "high_apr",
                "id": r["id"],
                "name": r["name"],
                "message": f"{r['apr']:.2f}% APR — the most expensive money in the book",
            })
        if r["due_day"] is None:
            if r["kind"] == "oneoff":
                # A one-off with no date claims cash but cannot be placed in
                # time — reserved anyway (it is a known claim), so say why.
                flags.append({
                    "severity": "warning",
                    "code": "undated_oneoff",
                    "id": r["id"],
                    "name": r["name"],
                    "message": (
                        "One-off payment with no due day — it reserves cash but cannot be "
                        "placed in time. Set a due day, or delete the row once it is paid"
                    ),
                })
            else:
                flags.append({
                    "severity": "info",
                    "code": "missing_due_day",
                    "id": r["id"],
                    "name": r["name"],
                    "message": "No due day — this row never appears in the next-payment line",
                })

    return sorted(flags, key=lambda f: severity_rank.get(f["severity"], 9))


def debt_analysis(
    entries: list[dict],
    as_of: date,
    *,
    liquid_cash: float | None = None,
    net_liquidation: float | None = None,
    other_assets: float | None = None,
    benchmark: dict | None = None,
    months: int = DEFAULT_HORIZON_MONTHS,
) -> dict:
    """The whole debt page in one pass: rows amortized ONCE, then composed.

    ``benchmark`` is the risk-free reference (see ``data.fred.risk_free_rate``).
    When it is missing the carry block reports ``verdict: "unknown"`` and the
    page greys the comparison — a missing rate never becomes an assumed one.

    ``other_assets`` is the parallel-ledger mark (alternatives: property,
    vehicles, collectibles) used ONLY for the separately-labelled
    ``net_worth_incl_other``. ``net_worth`` and ``net_liquidation`` are never
    touched by it — a house entering NAV retroactively would read as the best
    day in the performance history.
    """
    rows = liability_rows(entries, as_of)
    # Debt analytics run over DEBT rows only. A commitment has no rate, no
    # schedule and nothing to pay down, so letting one into the carry, priority
    # or projection maths would invent a paydown target that does not exist.
    debt_rows = [r for r in rows if r["is_debt"]]
    summary = rollup(entries, as_of, liquid_cash=liquid_cash)
    opportunity_rate = _num((benchmark or {}).get("rate"))
    total = summary["total"]
    assets = _num(net_liquidation) if net_liquidation is not None else None
    other = _num(other_assets) if other_assets is not None else None

    return {
        "as_of": as_of.isoformat(),
        "summary": {
            **summary,
            "net_liquidation": assets,
            "net_worth": net_worth(assets, total) if assets is not None else None,
            "other_assets": other,
            "net_worth_incl_other": (
                net_worth(assets, total, other) if assets is not None else None
            ),
            "debt_to_assets": (
                round(total / assets, 4) if assets is not None and assets > 0 else None
            ),
        },
        "carry": carry_analysis(debt_rows, opportunity_rate, benchmark),
        "projection": projection(debt_rows, as_of, months),
        "timeline": payoff_timeline(debt_rows),
        "priority": priority_ranking(debt_rows, opportunity_rate),
        "flags": data_quality_flags(rows),
    }
