"""Watchlist CRUD endpoints."""

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(prefix="/api/watchlist", tags=["watchlist"])


class SymbolBody(BaseModel):
    symbol: str


class ReorderBody(BaseModel):
    symbols: list[str]


@router.get("")
def get_watchlist():
    from data.watchlist import get_watchlist
    return get_watchlist()


@router.post("")
def add_to_watchlist(body: SymbolBody):
    from data.watchlist import add_symbol
    return add_symbol(body.symbol)


@router.delete("/{symbol}")
def remove_from_watchlist(symbol: str):
    from data.watchlist import remove_symbol
    return remove_symbol(symbol.upper())


@router.put("/reorder")
def reorder_watchlist(body: ReorderBody):
    from data.watchlist import reorder
    reorder(body.symbols)
    return {"ok": True}
