"""Market data: quotes, price history, option chains.

Two feeds, routed by asset class:

  * **Equities/ETFs/options** -> Massive API (``data/massive.py``).
  * **Crypto** -> yfinance (``data/crypto_assets.py``). Massive's plan does NOT
    license crypto snapshots (403 on the global crypto snapshot endpoint,
    verified 2026-10-08), and its aggregates cap at 730 daily rows — too short
    for a multi-cycle risk read. See ``data/crypto_assets.py`` for the evidence.

Routing lives HERE, at the boundary, so every existing consumer (STBR, signals,
charts, iv-rank, the quote stream) works on a crypto symbol with no special
casing of its own.
"""

import logging
import os

import pandas as pd

from data import massive

log = logging.getLogger(__name__)


def get_quotes(symbols, max_age: float | None = None):
    """Batch-fetch quotes for a list of symbols. Returns dict keyed by symbol.

    ``max_age`` is for the live stream: a page load is content with a 30s-old
    quote, a ticking table is not.
    """
    if not symbols:
        return {}
    if isinstance(symbols, str):
        symbols = (symbols,)

    from data import crypto_assets

    syms = [s.upper() for s in symbols]
    crypto_syms = [s for s in syms if crypto_assets.is_crypto(s)]
    equity_syms = [s for s in syms if not crypto_assets.is_crypto(s)]

    result = {}
    # Crypto FIRST so a canonical symbol is not overwritten by a failed equity
    # lookup of the same name (and vice versa).
    for sym in crypto_syms:
        result.update(crypto_assets.get_crypto_quote(sym, max_age=max_age))
    if equity_syms:
        result.update(_equity_quotes(tuple(sorted(equity_syms)), max_age=max_age))
    return result


def _equity_quotes(symbols: tuple[str, ...], max_age: float | None = None) -> dict:
    """Equity quotes: Schwab if configured, Massive if keyed, else Yahoo.

    The self-hosted edition has neither credential and uses the free feed. A
    failed paid feed degrades to Yahoo rather than to an empty dict, so the
    book still refreshes. Never a fabricated price.
    """
    from data import schwab

    if schwab.configured():
        try:
            quotes = schwab.get_quotes_schwab_compat(symbols, max_age=max_age)
            if quotes:
                return quotes
            log.warning("Schwab returned no quotes for %d symbols; trying the next feed", len(symbols))
        except Exception as exc:
            log.warning("Schwab quote feed failed (%s); trying the next feed", exc)

    if os.getenv("MASSIVE_API_KEY"):
        try:
            quotes = massive.get_quotes_schwab_compat(symbols)
            if quotes:
                return quotes
            log.warning("Massive returned no quotes for %d symbols; using Yahoo", len(symbols))
        except Exception as exc:
            log.warning("Massive quote feed failed (%s); using Yahoo", exc)

    from data import yfinance_feed

    return yfinance_feed.get_quotes_schwab_compat(symbols, max_age=max_age)


def _equity_history(symbol: str, days: int):
    """Daily OHLCV: Schwab if configured, Massive if keyed, else Yahoo."""
    from data import schwab

    if schwab.configured():
        try:
            df = schwab.get_price_history(symbol, days=days)
            if df is not None and not df.empty:
                return df
            log.warning("Schwab history empty for %s; trying the next feed", symbol)
        except Exception as exc:
            log.warning("Schwab history failed for %s (%s); trying the next feed", symbol, exc)

    if os.getenv("MASSIVE_API_KEY"):
        try:
            df = massive.get_price_history(symbol, days=days)
            if df is not None and not df.empty:
                return df
            log.warning("Massive history empty for %s; using Yahoo", symbol)
        except Exception as exc:
            log.warning("Massive history failed for %s (%s); using Yahoo", symbol, exc)

    from data import yfinance_feed

    return yfinance_feed.get_price_history(symbol, days=days)


def quote_view(qdata: dict) -> dict:
    """The wire shape for one symbol's live quote.

    This is the exact payload the /ws/quotes stream sends, and the HTTP
    snapshot endpoint serves the identical dict -- one builder, so the two
    transports can never drift. Prices, session, and the genuine-staleness
    flag all travel together; consumers decide what to render.
    """
    from data.account import extract_live_price

    price, net_change, net_pct, stale, session = extract_live_price(qdata)
    q = qdata.get("quote", {})
    return {
        "price": price,
        "change": net_change,
        "pct": net_pct,
        "mark": q.get("mark"),
        "postMarketChange": q.get("postMarketChange"),
        "postMarketPct": q.get("postMarketPercentChange"),
        "preMarketChange": q.get("preMarketChange"),
        "preMarketPct": q.get("preMarketPercentChange"),
        "session": session,
        "stale": stale,
    }


def get_price_history(symbol, days=365):
    """Fetch daily OHLCV as a DataFrame, from the feed that serves the asset."""
    from data import crypto_assets

    if crypto_assets.is_crypto(symbol):
        return crypto_assets.get_crypto_history(symbol, days=days)
    return _equity_history(symbol.upper(), days)


def get_option_chain(symbol):
    """Fetch the full option chain with greeks for a symbol.

    Primary: Massive snapshot. Fallback: yfinance (free, 15-min delayed) —
    Massive's plan returns NOT_AUTHORIZED for options (verified 2026-10-07),
    so without the fallback every chain request degraded to an empty chain.
    Both return the SAME Schwab-compatible dict shape (data/options_yf.py).
    """
    from data.options_yf import yf_option_chain

    if os.getenv("MASSIVE_API_KEY"):
        try:
            return massive.get_option_chain(symbol.upper())
        except Exception as exc:
            log.warning("Massive option chain failed for %s (%s); using Yahoo", symbol, exc)
    chain = yf_option_chain(symbol.upper())
    if chain is not None:
        return chain
    raise RuntimeError(f"No option chain available for {symbol}")


def search_instruments(query, projection=None):
    """Search for instruments by symbol or description."""
    if os.getenv("MASSIVE_API_KEY"):
        try:
            return massive.search_instruments(query)
        except Exception as exc:
            log.warning("Massive search failed (%s); using Yahoo", exc)
    from data.yfinance_feed import search_instruments as yf_search

    return yf_search(query)


def get_fundamentals(symbol):
    """Fetch fundamental data for a symbol."""
    if os.getenv("MASSIVE_API_KEY"):
        try:
            return massive.get_fundamentals(symbol.upper())
        except Exception as exc:
            log.warning("Massive fundamentals failed for %s (%s); using Yahoo", symbol, exc)
    from data.yfinance_feed import get_fundamentals as yf_fundamentals

    return yf_fundamentals(symbol.upper())


def flatten_option_chain(chain_json):
    """Flatten option chain JSON into a DataFrame of individual contracts."""
    rows = []
    for map_key in ("callExpDateMap", "putExpDateMap"):
        exp_map = chain_json.get(map_key, {})
        for exp_date_str, strikes in exp_map.items():
            exp_label = exp_date_str.split(":")[0] if ":" in exp_date_str else exp_date_str
            for strike_str, contracts in strikes.items():
                for c in contracts:
                    rows.append({
                        "symbol": c.get("symbol", ""),
                        "description": c.get("description", ""),
                        "put_call": c.get("putCall", ""),
                        "strike": c.get("strikePrice", 0),
                        "bid": c.get("bid", 0),
                        "ask": c.get("ask", 0),
                        "last": c.get("last", 0),
                        "volume": c.get("totalVolume", 0),
                        "open_interest": c.get("openInterest", 0),
                        "delta": c.get("delta", 0),
                        "gamma": c.get("gamma", 0),
                        "theta": c.get("theta", 0),
                        "vega": c.get("vega", 0),
                        "iv": c.get("volatility", 0),
                        "dte": c.get("daysToExpiration", 0),
                        "in_the_money": c.get("inTheMoney", False),
                        "expiration_date": c.get("expirationDate", ""),
                        "expiration": exp_label,
                    })
    return pd.DataFrame(rows)
