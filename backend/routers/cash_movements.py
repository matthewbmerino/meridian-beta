"""Cash movements — the append-only ledger beside the balance stores.

PRIVATE — movement history names institutions and amounts (operator PII).
Gated by require_auth (fail-closed).

There is deliberately NO update and NO delete endpoint: a movement is a fact
about the past, and the store's own docs say how to correct one (append a
reversing row). Validation problems come back as ``{"error": ...}`` in the BODY
with HTTP 200, the convention the rest of this backend uses.
"""


import datetime as dt
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from backend.auth import require_auth

router = APIRouter(
    prefix="/api/cash-movements",
    tags=["cash"],
    dependencies=[Depends(require_auth)],
)


class MovementBody(BaseModel):
    as_of: str
    institution: str
    amount: float
    description: str
    tag: str = "other"
    source: str = "operator"
    note: str | None = None


@router.get("")
def list_cash_movements(
    institution: str | None = None,
    tag: str | None = None,
    since: str | None = None,
    limit: int | None = None,
):
    """Movements (newest first) plus the rollup for whatever was filtered."""
    from analysis.cash_movements import summarize
    from data.cash_movements import list_movements

    rows = list_movements(institution=institution, tag=tag, since=since, limit=limit)
    return {"movements": rows, "summary": summarize(rows)}


@router.post("")
def add_cash_movement(body: MovementBody):
    from data.cash_movements import add_movement

    try:
        row = add_movement(
            as_of=body.as_of,
            institution=body.institution,
            amount=body.amount,
            description=body.description,
            tag=body.tag,
            source=body.source,
            note=body.note,
        )
    except ValueError as e:
        return {"error": str(e)}
    return {"movement": row}


@router.get("/reconcile")
def reconcile_cash_movements():
    """Derived (opening + movements) vs the asserted balances in external_cash.

    Reports drift, unopened institutions and orphans; it corrects nothing.
    """
    from analysis.cash_movements import reconcile
    from data.cash_movements import list_movements
    from data.external_cash import list_external_cash

    balances = {
        str(e.get("institution")): float(e.get("amount") or 0)
        for e in list_external_cash()
        if e.get("institution")
    }
    return reconcile(list_movements(), balances)


@router.get("/deployed")
def deployed_capital():
    """Capital deployed into renovation and sale costs, by month."""
    from analysis.cash_movements import deployed
    from data.cash_movements import list_movements

    return deployed(list_movements())


# ---------------------------------------------------------------- cash position + tax quarters
# A second router (paths independent of this module's prefix), registered in
# backend/main.py alongside the first.
position_router = APIRouter(prefix="/api", tags=["cash", "tax"])


@position_router.get("/cash/position")
def cash_position():
    """Cash as a position: buckets, terms, interest, liquidity ladder, actions.

    History is reconstructed BACKWARD from the summed buckets — the totals the
    account store already reconciles to — so no opening balance is invented.
    A reconstructed series starts where the ledger starts; that is stated, not
    painted over.

    The benchmark is fetched, never assumed: absent, every spread degrades to
    null and the surface says the benchmark is unavailable.
    """
    from analysis.cash_position import allocation, interest_summary, running_series, treasury_view
    from data.account import get_combined_positions, get_combined_summary
    from data.cash_movements import list_movements
    from data.cash_yields import INSTRUMENTS, get_assumptions, instrument_catalog, list_positions
    from data.external_cash import LIQUID_KINDS, list_external_cash, normalize_kind
    from data.fred import cash_curve, risk_free_rate

    movements = list_movements()
    terms = list_positions()
    buckets = []
    for e in list_external_cash():
        label = e.get("institution", "?")
        kind = normalize_kind(e.get("kind"))
        stated = terms.get(label) or {}
        buckets.append({
            "label": label,
            "kind": kind,
            "liquid": kind in LIQUID_KINDS,
            "amount": e.get("amount", 0),
            "apy": stated.get("apy"),
            "instrument": stated.get("instrument"),
            "maturity": stated.get("maturity"),
        })
    positions = get_combined_positions()
    summary = get_combined_summary(positions)
    b_cash = round(
        float(summary.get("cash_balance") or 0) - float(summary.get("external_cash_liquid") or 0), 2
    )
    if b_cash:
        stated = terms.get("Brokerage cash") or {}
        buckets.append({"label": "Brokerage cash", "kind": "brokerage", "liquid": True,
                        "amount": b_cash, "apy": stated.get("apy"),
                        "instrument": stated.get("instrument") or "brokerage",
                        "maturity": stated.get("maturity")})
    total = round(sum(float(b.get("amount") or 0) for b in buckets), 2)
    alloc = allocation(buckets)

    benchmark = None
    try:
        base = risk_free_rate()
        if base:
            benchmark = {**base, "curve": cash_curve()}
    except Exception:  # a dead FRED greys the spread; it never blocks the page
        benchmark = None

    today = dt.datetime.now(ZoneInfo("America/New_York")).date()
    treasury = treasury_view(alloc["rows"], INSTRUMENTS, benchmark, get_assumptions(), today)

    year_prefix = str(dt.datetime.now(dt.timezone.utc).year)
    earned = round(sum(
        float(m.get("amount") or 0) for m in movements
        if m.get("tag") == "interest" and str(m.get("as_of", ""))[:4] == year_prefix
    ), 2)
    return {
        "allocation": alloc,
        # The denominator beside the number: NAV and the desk's own tracked
        # constraint (buying power) travel with the deployable total.
        "nav": round(float(summary.get("net_liquidation") or 0), 2) or None,
        "buying_power": round(float(summary.get("buying_power") or 0), 2) or None,
        "series": running_series(movements, total),
        "movements": [
            {"as_of": m.get("as_of"), "amount": m.get("amount"), "tag": m.get("tag"),
             "source": m.get("source"), "note": m.get("note")}
            for m in movements
        ],
        "interest": interest_summary(alloc, earned if earned else None),
        "treasury": treasury,
        "instruments": instrument_catalog(),
        "yields": {k: v["apy"] for k, v in terms.items() if v.get("apy") is not None},
        "ledger_starts": min((str(m.get("as_of")) for m in movements), default=None),
    }


@position_router.post("/cash/yields")
def set_cash_yield(body: dict):
    """Set any subset of one position's terms: rate, instrument, maturity.

    Omitted keys are PRESERVED — the body's key set is what distinguishes
    "leave it" from "clear it", so a caller sending only ``apy`` cannot wipe a
    maturity date it never read.
    """
    from data.cash_yields import list_positions, upsert_position

    kwargs = {k: body[k] for k in ("apy", "instrument", "maturity") if k in body}
    try:
        upsert_position(body.get("label", ""), **kwargs)
    except ValueError as e:
        return {"error": str(e)}
    return {"positions": list_positions()}


@position_router.post("/cash/assumptions")
def set_cash_assumptions(body: dict):
    """State the marginal tax rates the after-tax view is computed at."""
    from data.cash_yields import set_assumptions

    kwargs = {k: body[k] for k in ("federal", "state_local") if k in body}
    try:
        return {"assumptions": set_assumptions(**kwargs)}
    except ValueError as e:
        return {"error": str(e)}


@position_router.get("/tax/quarters")
def tax_quarters():
    """Quarterly estimated-tax position: paid / due / upcoming + coverage."""
    import datetime as _dt

    from analysis.tax import quarterly_position, summarize
    from data.imports import get_all_activity_lots
    from data.realized import get_combined_realized_pl
    from data.tax_payments import get_schedule, list_payments

    _, sec_total = get_combined_realized_pl()
    lots = get_all_activity_lots()
    lots = lots.to_dict(orient="records") if not lots.empty else []
    as_of = _dt.datetime.now(_dt.timezone.utc).date()
    tax_block = summarize(lots, round(sec_total, 2), as_of)
    return quarterly_position(list_payments(), get_schedule(), tax_block, as_of)


@position_router.post("/tax/payments")
def add_tax_payment(body: dict):
    """Record a quarterly federal estimated payment (append-only)."""
    from data.tax_payments import add_payment

    try:
        entry = add_payment(
            paid_date=body.get("paid_date", ""),
            amount=body.get("amount"),
            quarter=body.get("quarter"),
            note=body.get("note", ""),
        )
    except ValueError as e:
        return {"error": str(e)}
    return {"payment": entry}


@position_router.post("/tax/schedule")
def set_tax_schedule(body: dict):
    """Update the stated quarterly plan (amount + year)."""
    from data.tax_payments import set_schedule

    try:
        return {"schedule": set_schedule(body.get("quarterly_amount"), body.get("year"))}
    except ValueError as e:
        return {"error": str(e)}
