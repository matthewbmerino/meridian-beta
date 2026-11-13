"""Cash position math — pure: balance history, allocation, interest, treasury view.

The balance history is reconstructed BACKWARD from the current total:
    balance(t) = current_total - sum(movements after t)
That needs no invented opening balances — the number anchored is one the
account store already reconciles to, and every earlier point is that anchor
minus what the ledger says moved since.

Interest: the run-rate is stated APY x balance per account, blended in dollars.
It is a FORWARD number. Interest actually earned appears when it is recorded as
a movement with tag "interest" — until then the readout says so rather than
estimating a past that was never logged.

The TREASURY VIEW (``treasury_view``) answers the three questions a cash book
has to answer, and nothing else:
    1. what it earns, after tax, against what a bill pays  -> blended, gap
    2. how fast it can be spent                            -> liquidity ladder
    3. the one move worth making                           -> ranked actions
Every figure degrades to ``None`` when its input is missing. A benchmark that
could not be fetched never becomes a default rate, and an unstated tax rate
never becomes an assumed one.
"""

from __future__ import annotations

import datetime as dt

# Liquidity bands, in ladder order. ``max_days`` is inclusive; ``None`` means the
# band is defined by something other than a day count (a lock with no stated
# maturity, or an illiquid position).
LIQUIDITY_BANDS: tuple[dict, ...] = (
    {"key": "now", "label": "Today", "max_days": 0},
    {"key": "week", "label": "Within 7 days", "max_days": 7},
    {"key": "month", "label": "Within 30 days", "max_days": 30},
    {"key": "year", "label": "Within a year", "max_days": 365},
    {"key": "locked", "label": "Over a year", "max_days": None},
    {"key": "illiquid", "label": "At stake", "max_days": None},
)

_BAND_LABELS = {b["key"]: b["label"] for b in LIQUIDITY_BANDS}

# A position below the benchmark by less than this is noise, not an action — it
# is inside the cost of moving the money.
_ACTION_MIN_BP = 25.0
# Below this balance the dollars do not justify the decision.
_ACTION_MIN_BALANCE = 5000.0
_ACTION_LIMIT = 5


def _f(v) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    return f if f == f else 0.0


def _opt(v) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def running_series(movements: list[dict], current_total: float) -> list[dict]:
    """movements: [{"as_of": ISO date, "amount": signed, "tag": str}].

    Returns [{date, balance, reconstructed?}] ascending, ending at
    current_total. The first point is the balance BEFORE the first recorded
    movement — reconstructed from the anchor, and labeled as such so it is
    never mistaken for a logged balance.
    """
    dated = [m for m in movements if m.get("as_of")]
    if not dated:
        return []
    by_day: dict[str, float] = {}
    for m in dated:
        d = str(m["as_of"])[:10]
        by_day[d] = by_day.get(d, 0.0) + _f(m.get("amount"))
    days = sorted(by_day)
    total = sum(by_day.values())

    out = [{"date": days[0], "balance": round(current_total - total, 2), "reconstructed": True}]
    running = current_total - total
    for d in days:
        running += by_day[d]
        out.append({"date": d, "balance": round(running, 2)})
    return out


def allocation(buckets: list[dict]) -> dict:
    """buckets: [{"label", "kind", "amount", "liquid", "apy", "instrument", "maturity"}].

    Groups by kind; keeps liquid vs illiquid split explicit — the sportsbook
    balance is cash-like but not deployable, and merging them would overstate
    what can be put to work.
    """
    total = sum(_f(b.get("amount")) for b in buckets)
    liquid = sum(_f(b.get("amount")) for b in buckets if b.get("liquid", True))
    by_kind: dict[str, float] = {}
    for b in buckets:
        k = b.get("kind") or "other"
        by_kind[k] = by_kind.get(k, 0.0) + _f(b.get("amount"))
    return {
        "total": round(total, 2),
        "liquid": round(liquid, 2),
        "illiquid": round(total - liquid, 2),
        "by_kind": {k: round(v, 2) for k, v in sorted(by_kind.items(), key=lambda kv: -kv[1])},
        "rows": [
            {
                "label": b.get("label"),
                "kind": b.get("kind"),
                "amount": round(_f(b.get("amount")), 2),
                "liquid": bool(b.get("liquid", True)),
                "apy": b.get("apy"),
                "instrument": b.get("instrument"),
                "maturity": b.get("maturity"),
                "share": round(100 * _f(b.get("amount")) / total, 1) if total else 0.0,
                "annual_interest": round(_f(b.get("amount")) * _f(b.get("apy")) / 100, 2)
                if b.get("apy") is not None else None,
            }
            for b in sorted(buckets, key=lambda b: -_f(b.get("amount")))
        ],
    }


def interest_summary(alloc: dict, earned_to_date: float | None) -> dict:
    """Run-rate from stated APYs; earned-to-date only if the ledger recorded it."""
    rows = [r for r in alloc.get("rows", []) if r.get("annual_interest") is not None]
    total_annual = sum(r["annual_interest"] for r in rows)
    covered = sum(r["amount"] for r in rows)
    blended = round(100 * total_annual / covered, 2) if covered else None
    return {
        "annual_run_rate": round(total_annual, 2),
        "monthly_run_rate": round(total_annual / 12, 2),
        "blended_apy": blended,
        "covered_balance": round(covered, 2),
        "uncovered_balance": round(alloc.get("total", 0) - covered, 2),
        "earned_to_date": round(earned_to_date, 2) if earned_to_date is not None else None,
    }


def days_to_cash(instrument: str | None, catalog: dict, maturity: str | None, as_of: dt.date) -> int | None:
    """Days until the balance can be spent WITHOUT A PENALTY.

    The definition is deliberately about access, not about rate certainty: a
    T-bill sold early is T+1 (a market sale, no penalty), so its maturity date
    is a rate-lock date shown in its own column and never a liquidity
    constraint. A term CD is different — breaking it costs interest — so it has
    no access answer of its own and resolves from its maturity instead. An
    undated term CD returns ``None`` (unknown lock) rather than a flattering
    zero.
    """
    spec = catalog.get(instrument or "") or catalog.get("other") or {}
    base = spec.get("days")
    if base is not None:
        return int(base)
    if not maturity:
        return None
    try:
        mat = dt.date.fromisoformat(str(maturity)[:10])
    except ValueError:
        return None
    return max((mat - as_of).days, 0)


def band_for(days: int | None, liquid: bool) -> str:
    """Ladder band for a position. Illiquid is its own band, not a long lock."""
    if not liquid:
        return "illiquid"
    if days is None:
        return "locked"
    if days <= 0:
        return "now"
    if days <= 7:
        return "week"
    if days <= 30:
        return "month"
    if days <= 365:
        return "year"
    return "locked"


def treasury_view(
    rows: list[dict],
    catalog: dict | None = None,
    benchmark: dict | None = None,
    assumptions: dict | None = None,
    as_of: dt.date | None = None,
) -> dict:
    """Blended yield (pre/post tax), the gap to the risk-free, liquidity, actions.

    Pure: every input is passed in. ``benchmark`` is the ``{rate, as_of, series,
    label}`` contract from ``data.fred.risk_free_rate`` — when it is absent the
    gap degrades to ``None`` and the surface says the benchmark is unavailable
    rather than comparing against a remembered rate.
    """
    catalog = catalog or {}
    assumptions = assumptions or {}
    as_of = as_of or dt.date.today()

    fed = _opt(assumptions.get("federal"))
    state = _opt(assumptions.get("state_local"))
    combined = fed + state if fed is not None and state is not None else None
    bench_rate = _opt((benchmark or {}).get("rate"))

    total = sum(_f(r.get("amount")) for r in rows)
    enriched: list[dict] = []
    covered = 0.0
    covered_dollars = 0.0
    post_dollars = 0.0
    post_complete = combined is not None
    gap_dollars = 0.0
    gap_covered = 0.0

    for r in rows:
        amount = _f(r.get("amount"))
        liquid = bool(r.get("liquid", True))
        instrument = r.get("instrument") or ("savings" if liquid else "other")
        spec = catalog.get(instrument) or catalog.get("other") or {}
        state_exempt = bool(spec.get("state_exempt", False))
        days = days_to_cash(instrument, catalog, r.get("maturity"), as_of) if liquid else None
        band = band_for(days, liquid)
        apy = _opt(r.get("apy"))

        after_tax = None
        if apy is not None and combined is not None:
            # Treasury interest escapes the state and local rate; bank interest
            # does not. This is the whole reason a lower bill can beat a higher CD.
            rate = fed if (state_exempt and fed is not None) else combined
            after_tax = round(apy * (1 - rate / 100), 3)

        gap_bp = None
        gap_annual = None
        if apy is not None and bench_rate is not None:
            # One sign convention on both axes: apy MINUS the bill, so negative
            # always means "earning less than the risk-free".
            gap_bp = round((apy - bench_rate) * 100, 1)
            gap_annual = round(amount * (apy - bench_rate) / 100, 2)

        enriched.append(
            {
                **r,
                "instrument": instrument,
                "instrument_label": spec.get("label", "Other"),
                "state_exempt": state_exempt,
                "days_to_cash": days,
                "band": band,
                "band_label": _BAND_LABELS.get(band, ""),
                "after_tax_apy": after_tax,
                "gap_bp": gap_bp,
                "gap_annual": gap_annual,
            }
        )

        if apy is not None:
            covered += amount
            covered_dollars += amount * apy / 100
            if after_tax is None:
                post_complete = False
            else:
                post_dollars += amount * after_tax / 100
            if gap_annual is not None:
                gap_dollars += gap_annual
                gap_covered += amount

    # A post-tax total is only a total when EVERY covered row could be taxed at
    # the stated rates; one missing leg makes it a partial sum, which is worse
    # than no number at all.
    post_total: float | None = None if not post_complete or covered == 0 else post_dollars

    bands = []
    for band_spec in LIQUIDITY_BANDS:
        members = [r for r in enriched if r["band"] == band_spec["key"]]
        amount = sum(r["amount"] for r in members)
        bands.append(
            {
                "key": band_spec["key"],
                "label": band_spec["label"],
                "amount": round(amount, 2),
                "share": round(100 * amount / total, 1) if total else 0.0,
                "count": len(members),
            }
        )

    # Weighted days excludes the "no defined exit" (999) rows — averaging an
    # undefined horizon into a day count would invent one.
    weighted_num = 0.0
    weighted_den = 0.0
    for r in enriched:
        d = r["days_to_cash"]
        if d is None or d >= 999 or not r["liquid"]:
            continue
        weighted_num += r["amount"] * d
        weighted_den += r["amount"]
    weighted_days = round(weighted_num / weighted_den, 1) if weighted_den else None

    now_band = next(b for b in bands if b["key"] == "now")
    locked = sum(b["amount"] for b in bands if b["key"] in ("locked", "illiquid"))
    blended_pre = round(100 * covered_dollars / covered, 2) if covered else None
    actions = _actions(enriched, bench_rate, as_of)

    return {
        "benchmark": benchmark,
        "assumptions": {
            "federal": fed,
            "state_local": state,
            "combined": combined,
            "is_set": combined is not None,
        },
        "blended": {
            "pre": blended_pre,
            "post": round(100 * post_total / covered, 2) if post_total is not None and covered else None,
            "dollars_pre": round(covered_dollars, 2),
            "dollars_post": round(post_total, 2) if post_total is not None else None,
            "covered": round(covered, 2),
            "uncovered": round(total - covered, 2),
        },
        "gap": {
            # bp, not percentage points: the blended rate and the benchmark are
            # both percents, so the difference is 100x before it is a spread.
            "bp": round((blended_pre - bench_rate) * 100, 1)
            if blended_pre is not None and bench_rate is not None else None,
            "annual": round(gap_dollars, 2) if gap_covered else None,
            "covered": round(gap_covered, 2),
        },
        "liquidity": {
            "bands": bands,
            "weighted_days": weighted_days,
            "now": now_band,
            "locked": round(locked, 2),
        },
        "actions": actions,
        "on_table": round(sum(a["annual"] for a in actions if a.get("annual")), 2),
        "rows": enriched,
    }


def _actions(rows: list[dict], bench_rate: float | None, as_of: dt.date) -> list[dict]:
    """The moves worth making, ranked by dollars — never by rate alone.

    Three kinds, all actionable today: a position paying under the bill, a
    position whose rate was never recorded, and a maturity close enough that
    doing nothing reprices it.
    """
    out: list[dict] = []
    for r in rows:
        if not r.get("liquid"):
            continue
        amount = _f(r.get("amount"))
        if amount < _ACTION_MIN_BALANCE:
            continue
        apy = _opt(r.get("apy"))

        if apy is None:
            out.append(
                {
                    "kind": "unrecorded",
                    "label": r.get("label"),
                    "instrument": r.get("instrument"),
                    "amount": round(amount, 2),
                    "apy": None,
                    "benchmark": bench_rate,
                    "gap_bp": None,
                    "annual": round(amount * bench_rate / 100, 2) if bench_rate is not None else None,
                    "detail": "rate not recorded",
                }
            )
        elif bench_rate is not None and r.get("gap_bp") is not None and r["gap_bp"] <= -_ACTION_MIN_BP:
            out.append(
                {
                    "kind": "below",
                    "label": r.get("label"),
                    "instrument": r.get("instrument"),
                    "amount": round(amount, 2),
                    "apy": apy,
                    "benchmark": bench_rate,
                    "gap_bp": r["gap_bp"],
                    "annual": round(-r["gap_annual"], 2) if r.get("gap_annual") is not None else None,
                    "detail": f"{abs(r['gap_bp']):.0f} bp under the bill",
                }
            )

        maturity = r.get("maturity")
        if maturity:
            try:
                mat = dt.date.fromisoformat(str(maturity)[:10])
            except ValueError:
                mat = None
            if mat is not None:
                days = (mat - as_of).days
                if 0 <= days <= 90:
                    out.append(
                        {
                            "kind": "maturity",
                            "label": r.get("label"),
                            "instrument": r.get("instrument"),
                            "amount": round(amount, 2),
                            "apy": apy,
                            "benchmark": bench_rate,
                            "gap_bp": r.get("gap_bp"),
                            "annual": None,
                            "detail": f"matures in {days} days",
                        }
                    )

    out.sort(key=lambda a: -(a.get("annual") or 0))
    return out[:_ACTION_LIMIT]
