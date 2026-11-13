"""External cash endpoints — balances held at other institutions.

PRIVATE — external cash is GLOBAL (the operator's balances are their own).
Gated by require_auth (fail-closed).

Entries carry a ``kind``: ``bank`` (liquid cash — rolls into cash_balance)
or ``sportsbook`` (illiquid stake: withdrawable balance + open bets at
stake — NAV only). Omitted kind is ``bank``, matching legacy entries.
"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from backend.auth import require_auth

router = APIRouter(
    prefix="/api/external-cash",
    tags=["external-cash"],
    dependencies=[Depends(require_auth)],
)


class ExternalCashBody(BaseModel):
    institution: str
    amount: float
    kind: str = "bank"


class ExternalCashUpdateBody(BaseModel):
    institution: str
    amount: float
    # Omitted on a plain amount-edit → the entry keeps its existing kind.
    # An explicit kind reclassifies (e.g. bet settled → withdrawable cash).
    kind: str | None = None


@router.get("")
def list_external_cash():
    from data.external_cash import get_cash_totals, normalize_kind
    from data.external_cash import list_external_cash as _list_entries
    totals = get_cash_totals()
    return {
        "entries": [
            {**e, "kind": normalize_kind(e.get("kind"))}
            for e in _list_entries()
        ],
        "total": totals["total"],
        "liquid": totals["liquid"],
        "illiquid": totals["illiquid"],
    }


@router.post("")
def add_external_cash(body: ExternalCashBody):
    from data.external_cash import add_external_cash
    try:
        entry = add_external_cash(body.institution, body.amount, body.kind)
    except ValueError as e:
        return {"error": str(e)}
    return {"entry": entry}


@router.patch("/{doc_id}")
def update_external_cash(doc_id: str, body: ExternalCashUpdateBody):
    from data.external_cash import update_external_cash
    try:
        entry = update_external_cash(
            doc_id, body.institution, body.amount, body.kind
        )
    except ValueError as e:
        return {"error": str(e)}
    if entry is None:
        return {"error": "entry not found"}
    return {"entry": entry}


@router.delete("/{doc_id}")
def remove_external_cash(doc_id: str):
    from data.external_cash import remove_external_cash
    return {"ok": remove_external_cash(doc_id)}
