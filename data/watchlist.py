"""Persistent watchlist storage.

Stores a simple JSON list of ticker symbols on disk.
"""

from __future__ import annotations

from pathlib import Path

from data.crypto import load_json_store, write_json
from data.session import data_root


def _file() -> Path:
    return data_root() / "watchlist.json"


def _read() -> list[str]:
    data = load_json_store(_file(), "watchlist", [])
    return list(dict.fromkeys(data)) if isinstance(data, list) else []


def _write(symbols: list[str]):
    try:
        write_json(_file(), "watchlist", symbols)
    except OSError:
        pass


def _canon(symbol: str) -> str:
    """Normalize to the symbol the app stores.

    Crypto spellings collapse to their canonical form (``X:BTCUSD`` -> ``BTC``)
    so the watchlist holds one row per asset and the link it builds resolves.
    """
    sym = symbol.strip().upper()
    from data.crypto_assets import canonical

    return canonical(sym) or sym


def get_watchlist() -> list[str]:
    return _read()


def add_symbol(symbol: str) -> list[str]:
    symbols = _read()
    sym = _canon(symbol)
    if sym and sym not in symbols:
        symbols.append(sym)
        _write(symbols)
    return symbols


def remove_symbol(symbol: str) -> list[str]:
    symbols = _read()
    sym = _canon(symbol)
    symbols = [s for s in symbols if s != sym]
    _write(symbols)
    return symbols


def reorder(symbols: list[str]):
    _write([s for s in (_canon(s) for s in symbols) if s])
