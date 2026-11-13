"""Liability endpoints — credit cards and installment debt.

PRIVATE — liabilities are GLOBAL (the operator's debts are their own). Gated by
require_auth (fail-closed).

This is the ONE place assets and debt meet. ``net_liquidation`` (from
``data/account.py``) is never reduced by debt anywhere else; the combined
``net_worth`` figure is computed here so there is exactly one definition of it.

An unreadable store returns an explicit ``error`` alongside an empty entry list
— a corrupt file must never surface as "$0 owed".
"""

from datetime import date

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from backend.auth import require_auth
from data.cache import ttl_cache

router = APIRouter(
    prefix="/api/liabilities",
    tags=["liabilities"],
    dependencies=[Depends(require_auth)],
)


class LiabilityBody(BaseModel):
    kind: str = "revolving"
    name: str
    balance: float
    apr: float = 0.0
    payment: float | None = None
    due_day: int | None = None
    autopay_full: bool = False
    term_months: int | None = None
    originated_at: str | None = None
    note: str | None = None
    # Revolving rows only: the line the balance is drawn against. Refused on a
    # loan or a commitment by the store (a contradictory row otherwise).
    credit_limit: float | None = None
    # Manual-order slot. Omitted/None on a PATCH keeps the existing position —
    # order is changed through /reorder, never by an edit form.
    position: int | None = None


class ReorderBody(BaseModel):
    """The full desired order, as a list of liability ids."""

    ids: list[str]


def _fields(body: LiabilityBody) -> dict:
    return {
        "kind": body.kind,
        "name": body.name,
        "balance": body.balance,
        "apr": body.apr,
        "payment": body.payment,
        "due_day": body.due_day,
        "autopay_full": body.autopay_full,
        "term_months": body.term_months,
        "originated_at": body.originated_at,
        "note": body.note,
        "position": body.position,
        "credit_limit": body.credit_limit,
    }


def _liquid_cash() -> float:
    """Liquid cash only (kind=bank). Illiquid balances are not spendable.

    Fallback only — the primary source is ``get_combined_summary()["cash_balance"]``
    (see ``_assets_and_cash``), because that is the figure the Overview shows and
    a coverage number that disagrees with the cash it covers is worse than none.
    """
    from data.external_cash import get_cash_totals

    return float(get_cash_totals()["liquid"])


def _assets_and_cash() -> tuple[float | None, float | None]:
    """The operator's asset total and SPENDABLE cash, or (None, ...) on failure.

    ``cash_balance`` (brokerage sweep + liquid external) is used for debt
    coverage rather than external balances alone, so the coverage line and the
    Overview's cash figure are the same money. When the summary cannot be
    computed, cash falls back to external liquid cash and NAV degrades to None —
    a feed failure must not become an assumed number.
    """
    try:
        from data.account import get_combined_summary

        agg = get_combined_summary()
        return float(agg["net_liquidation"]), float(agg["cash_balance"])
    except Exception:
        return None, _liquid_cash()


def _other_assets() -> float | None:
    """Marked value of the alternatives ledger (property, vehicles, collectibles).

    A SEPARATE book, deliberately outside ``net_liquidation``: injecting a house
    into NAV would paint the performance history with a fake best day. It is
    read only to publish the separately-labelled inclusive figure. Unreadable or
    empty returns None (absent), never 0 — "no marks" and "zero value" are
    different statements.
    """
    try:
        from data.alternatives import rollup as _alternatives_rollup

        roll = _alternatives_rollup()
        if roll.get("marked_count"):
            return roll.get("total_mark")
    except Exception:
        return None
    return None


@router.get("")
def list_liabilities():
    """Every liability with its schedule, the rollup, and net worth."""
    from analysis.liabilities import net_worth, rollup
    from data.crypto import StoreUnreadable
    from data.crypto import store_error as _store_error
    from data.liabilities import list_liabilities as _list_entries
    from data.liabilities import store_path

    try:
        entries = _list_entries()
    except StoreUnreadable as exc:
        return {
            "entries": [],
            "rollup": None,
            "net_liquidation": None,
            "net_worth": None,
            "other_assets": None,
            "net_worth_incl_other": None,
            "debt_to_assets": None,
            "error": f"liability store unreadable: {exc}",
        }

    cash = _liquid_cash()
    net_liquidation = None
    try:
        from data.account import get_combined_summary

        agg = get_combined_summary()
        cash = float(agg["cash_balance"])
        net_liquidation = float(agg["net_liquidation"])
    except Exception:
        net_liquidation = None

    summary = rollup(entries, date.today(), liquid_cash=cash)

    other = _other_assets()
    nw = net_worth(net_liquidation, summary["total"]) if net_liquidation is not None else None
    nw_incl = (
        net_worth(net_liquidation, summary["total"], other)
        if net_liquidation is not None
        else None
    )
    debt_to_assets = (
        round(summary["total"] / net_liquidation, 4)
        if net_liquidation and net_liquidation > 0
        else None
    )

    return {
        "entries": entries,
        "rollup": summary,
        "net_liquidation": net_liquidation,
        "net_worth": nw,
        "other_assets": other,
        "net_worth_incl_other": nw_incl,
        "debt_to_assets": debt_to_assets,
        "error": _store_error(store_path()),
    }


@router.post("")
def add_liability(body: LiabilityBody):
    from data.liabilities import add_liability as _add
    from data.liabilities import public_entry

    try:
        entry = _add(**_fields(body))
    except ValueError as e:
        return {"error": str(e)}
    return {"entry": public_entry(entry)}


@router.get("/analysis")
def liability_analysis(include_projection: bool = True):
    """Debt analysis: paydown projection, payoff order, cost of carry, flags.

    ``include_projection=False`` drops the month-by-month series (chart data)
    for consumers that only need the conclusions — the Compass tool uses this so
    an agent isn't handed 60 rows of chart points.

    Every piece degrades independently: a store that cannot be read returns an
    explicit error, a missing risk-free benchmark leaves the carry verdict
    ``"unknown"``, and an unreadable NAV leaves net worth as None. None of those
    becomes an assumed number.
    """
    return _cached_liability_analysis(include_projection)


def _liability_analysis_compute(include_projection: bool = True):
    from analysis.liabilities import debt_analysis
    from data.crypto import StoreUnreadable
    from data.crypto import store_error as _store_error
    from data.liabilities import list_liabilities as _list_entries
    from data.liabilities import store_path

    blank = {
        "as_of": None,
        "summary": None,
        "carry": None,
        "projection": None,
        "timeline": [],
        "priority": [],
        "flags": [],
    }

    try:
        entries = _list_entries()
    except StoreUnreadable as exc:
        return {**blank, "error": f"liability store unreadable: {exc}"}

    # The risk-free benchmark is an INPUT, not an assumption: no FRED, no rate.
    benchmark = None
    try:
        from data.fred import risk_free_rate

        benchmark = risk_free_rate()
    except Exception:
        benchmark = None

    net_liquidation, cash = _assets_and_cash()

    # The alternatives book is a SEPARATE ledger (property, vehicles,
    # collectibles) and must never enter net_liquidation. Read once, through the
    # shared helper, so this endpoint and the list endpoint can never disagree.
    other_assets = _other_assets()

    result = debt_analysis(
        entries,
        date.today(),
        liquid_cash=cash,
        net_liquidation=net_liquidation,
        other_assets=other_assets,
        benchmark=benchmark,
    )
    if not include_projection:
        result["projection"] = None
    return {**result, "error": _store_error(store_path())}


# TTL-cached: debt analysis over slowly-changing balances.
_cached_liability_analysis = ttl_cache(ttl_seconds=300)(_liability_analysis_compute)




@router.post("/reorder")
def reorder_liabilities(body: ReorderBody):
    """Persist the operator's manual order for the liability list.

    Declared before the ``/{doc_id}`` routes so the literal path can never be
    read as an id.
    """
    from data.liabilities import public_entry
    from data.liabilities import reorder_liabilities as _reorder

    try:
        entries = _reorder(body.ids)
    except ValueError as e:
        return {"error": str(e)}
    return {"entries": [public_entry(e) for e in entries]}


@router.patch("/{doc_id}")
def update_liability(doc_id: str, body: LiabilityBody):
    from data.liabilities import list_liabilities as _list
    from data.liabilities import public_entry
    from data.liabilities import update_liability as _update

    try:
        fields = _fields(body)
        # A field the caller did NOT send is KEPT — never reset to the model's
        # default. PATCH replaces the whole record, so any client that does not
        # know about a field erases it by saving: the desktop edit form carries
        # a row's numbers, not its prose or its credit line, and correcting a
        # balance that way silently wiped a card's note (a deferred-interest
        # expiry) and its $11,700 limit, which also dropped the card out of the
        # utilization denominator. An explicitly-sent null STILL clears — that
        # is how a note is removed.
        existing = next((e for e in _list() if e.get("id") == doc_id), None)
        if existing is None:
            return {"error": "liability not found"}
        sent = body.model_fields_set
        for key in fields:
            if key not in sent:
                fields[key] = existing.get(key)
        entry = _update(doc_id, **fields)
    except ValueError as e:
        return {"error": str(e)}
    if entry is None:
        return {"error": "liability not found"}
    return {"entry": public_entry(entry)}


@router.delete("/{doc_id}")
def remove_liability(doc_id: str):
    from data.liabilities import remove_liability as _remove

    return {"ok": _remove(doc_id)}
