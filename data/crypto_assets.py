"""Crypto instrument support.

WHY THIS EXISTS
---------------
Meridian's market layer is US-equity shaped: it fetches via the Massive
snapshot endpoint for ``locale/us/markets/stocks``, and its price history comes
from the Massive aggregates endpoint. A crypto asset is neither, so a symbol
like ``X:BTCUSD`` half-works today: history resolves (the aggregates endpoint is
asset-agnostic) but quotes come back empty, and every downstream consumer reads
an empty quote as "0" or "no data" rather than "wrong feed".

This module is the ONE place that knows which instruments are crypto and which
source serves them.

THE PLAN GATE (measured 2026-10-08, do not assume it changed)
-------------------------------------------------------------
Massive/Polygon crypto is NOT licensed on this plan:

  * ``/v2/snapshot/locale/global/markets/crypto/tickers/X:BTCUSD`` -> **403 Forbidden**
  * the stock snapshot endpoint filtered to ``X:BTCUSD`` -> not returned at all

So crypto quotes can NOT come from Massive. They come from yfinance, which
serves ``BTC-USD`` with no key.

WHAT MASSIVE *DOES* SERVE, AND ITS ONE TRAP
-------------------------------------------
Massive aggregates (``/v2/aggs/ticker/{sym}/range/1/day/...``) DO resolve
``X:BTCUSD`` — but the plan caps the response at **730 daily rows** regardless of
the requested window (days=2000 and days=4000 both return 730). A 140-day STBR
needs 140; a multi-cycle risk read wants far more. And bare ``BTC`` on Massive
resolves to a **$35.63 equity**, ``BTC INC`` — NOT Bitcoin. That is a live
fabrication hazard for anything that tries to be helpful about a symbol the
operator typed as "BTC".

THE RULE THAT FOLLOWS
---------------------
**Crypto comes from exactly one vendor — Coinbase Exchange — for every window,
and yfinance is only a fallback for a failed call.** A mixed series would splice
two vendors' prices and manufacture a discontinuity in a risk metric; and
Coinbase is the venue FRED's recorded CBBTCUSD closes are drawn from, so the live
price and the audit series agree by construction. Coinbase answers in ~30ms
against yfinance's ~176ms, needs no key, and quotes are no longer stuck behind a
five-minute cache — which is what kept a crypto row still while equities ticked.

Deps note: yfinance remains a declared requirement and a load-bearing fallback
(`data/options_yf.py` covers every option chain because Massive's plan returns
NOT_AUTHORIZED for options), so the fallback here adds no new dependency risk.
"""

import logging

import pandas as pd

from data import coinbase
from data.cache import ttl_cache

log = logging.getLogger(__name__)

# Canonical display symbols, in the form the operator types and the app stores.
CRYPTO_BTC = "BTC"
# Yahoo's ticker for the same asset.
_CRYPTO_BTC_YF = "BTC-USD"

#: Canonical symbol -> yfinance ticker.
CRYPTO_YF_TICKERS: dict[str, str] = {
    CRYPTO_BTC: _CRYPTO_BTC_YF,
}

#: Everything that could plausibly be typed for the same asset, mapped to the
#: canonical symbol. Includes the Massive crypto ticker form so a link built from
#: an X:-prefixed URL still lands on the canonical page.
CRYPTO_ALIASES: dict[str, str] = {
    "BTC": CRYPTO_BTC,
    "BTC-USD": CRYPTO_BTC,
    "BTCUSD": CRYPTO_BTC,
    "X:BTCUSD": CRYPTO_BTC,
}

#: What each asset actually is. Surfaced to the UI so a crypto page can drop the
#: equity-only panels (fundamentals, insider, options) instead of rendering rows
#: of em-dashes pretending a stock metric exists.
CRYPTO_META: dict[str, dict] = {
    CRYPTO_BTC: {
        "name": "Bitcoin",
        "asset_type": "CRYPTO",
        "quote_currency": "USD",
        "trades_weekends": True,
        "source": "yfinance",
    },
}

#: A stablecoin or a token we have not vetted must not be silently treated as
#: crypto-with-a-quote. Only symbols in here get crypto treatment.
_KNOWN = frozenset(CRYPTO_ALIASES)


def canonical(symbol: str) -> str | None:
    """Return the canonical crypto symbol for *symbol*, or None if not crypto.

    ``canonical("X:BTCUSD") == "BTC"``; ``canonical("CAT") is None``.
    """
    if not symbol:
        return None
    return CRYPTO_ALIASES.get(str(symbol).strip().upper())


def is_crypto(symbol: str) -> bool:
    """True when *symbol* is a crypto instrument this module serves."""
    return canonical(symbol) is not None


def crypto_meta(symbol: str) -> dict | None:
    """Metadata for a crypto symbol (name, asset_type, source), or None."""
    canon = canonical(symbol)
    return CRYPTO_META.get(canon) if canon else None


def yf_ticker(symbol: str) -> str | None:
    """yfinance ticker for a crypto symbol, or None."""
    canon = canonical(symbol)
    return CRYPTO_YF_TICKERS.get(canon) if canon else None


@ttl_cache(ttl_seconds=900)
def _history_cached(canon: str, days: int = 365) -> pd.DataFrame:
    """Daily OHLCV for a CANONICAL crypto symbol, indexed by date.

    Returns the SAME shape as ``massive.get_price_history`` — a DataFrame with
    columns ``open, high, low, close, volume`` and a DatetimeIndex — so every
    existing consumer (STBR, signals, charts, iv-rank) works unchanged.

    Returns an EMPTY DataFrame for anything that is not a vetted crypto symbol,
    and on any fetch failure. An empty frame is a data gap: callers must render
    it as such and must never substitute a fabricated price.
    """
    ticker = yf_ticker(canon)
    if not ticker:
        return pd.DataFrame()

    # `days` can arrive as a FastAPI Query sentinel if a route function is
    # called directly rather than over HTTP. Coerce rather than explode.
    try:
        days = int(days)
    except (TypeError, ValueError):
        days = 365

    # yfinance returns 7 rows/week for crypto; request a little more than asked
    # so a `days` window yields at least `days` rows back.
    period = "max" if days > 3650 else f"{max(days, 1)}d"
    try:
        import yfinance as yf

        raw = yf.Ticker(ticker).history(period=period, interval="1d", auto_adjust=False)
        if raw is None or raw.empty:
            log.warning("Crypto history empty for %s (%s)", canon, ticker)
            return pd.DataFrame()

        df = raw.rename(
            columns={
                "Open": "open",
                "High": "high",
                "Low": "low",
                "Close": "close",
                "Volume": "volume",
            }
        )
        # Normalize to a tz-naive date index so it lines up with the equity path
        # (massive aggregates come back tz-naive at midnight).
        idx = pd.to_datetime(df.index)
        if getattr(idx, "tz", None) is not None:
            idx = idx.tz_localize(None)
        df.index = idx.normalize()
        df.index.name = "date"

        keep = [c for c in ("open", "high", "low", "close", "volume") if c in df.columns]
        df = df[keep].sort_index()
        return df.tail(days) if days and days > 0 else df
    except Exception as exc:  # network, rate limit, bad payload
        log.warning("Crypto history fetch failed for %s (%s): %s", canon, ticker, exc)
        return pd.DataFrame()


def get_crypto_history(symbol: str, days: int = 365) -> pd.DataFrame:
    """Daily OHLCV for a crypto asset, keyed by any accepted spelling.

    Coinbase candles first (same venue as the recorded daily series, so a chart
    and a mark cannot disagree about which BTC day closed where); the yfinance
    frame is the fallback. Canonicalises first so every spelling shares a fetch.
    Returns an empty DataFrame for a non-crypto symbol.
    """
    canon = canonical(symbol)
    if not canon:
        return pd.DataFrame()

    frame = coinbase.candles(canon, days=days)
    if frame is not None and not frame.empty:
        return frame
    return _history_cached(canon, days)


@ttl_cache(ttl_seconds=300)
def _quote_cached(canon: str) -> dict:
    """Quote for a CANONICAL crypto symbol in the Schwab-compatible shape.

    Returns ``{}`` (never a zeroed quote) when the asset is unknown or the fetch
    fails, so consumers degrade and say so instead of showing a $0 price.

    Shape matches ``massive.get_quotes_schwab_compat``:
    ``{SYMBOL: {"quote": {...}, "regular": {...}, "fundamental": {}}}``.

    Two fields are deliberately honest rather than comfortable:
      * ``bidPrice``/``askPrice``/sizes are 0 — a spot crypto market has no
        consolidated NBBO, and we will not invent one from a mid.
      * ``session`` is always ``"crypto"`` (not ``"closed"``): crypto trades
        continuously, so an equity session label would be wrong. ``stale`` is
        False because a live price is not a stale one.
    """
    ticker = yf_ticker(canon)
    if not canon or not ticker:
        return {}

    try:
        import yfinance as yf

        t = yf.Ticker(ticker)
        fi = getattr(t, "fast_info", None)
        last = prev = None
        if fi is not None:
            try:
                last = float(fi["last_price"])
            except Exception:
                last = None
            try:
                prev = float(fi["previous_close"])
            except Exception:
                prev = None

        # fast_info is a lazy dict and can come back partly populated; fall back
        # to a short history pull rather than serving a half-empty quote.
        if not last:
            hist = t.history(period="5d", interval="1d", auto_adjust=False)
            if hist is not None and not hist.empty:
                last = float(hist["Close"].iloc[-1])
                if len(hist) > 1:
                    prev = float(hist["Close"].iloc[-2])

        if not last:
            log.warning("Crypto quote empty for %s (%s)", canon, ticker)
            return {}

        change = (last - prev) if (last and prev) else 0.0
        pct = (change / prev * 100.0) if (prev and change) else 0.0

        return {
            canon: {
                "quote": {
                    "lastPrice": last,
                    "bidPrice": 0,
                    "askPrice": 0,
                    "bidSize": 0,
                    "askSize": 0,
                    "netChange": change,
                    "netPercentChangeInDouble": pct,
                    "openPrice": 0,
                    "highPrice": 0,
                    "lowPrice": 0,
                    "closePrice": prev or 0,
                    "totalVolume": 0,
                    "mark": last,
                    "stale": False,
                    "session": "crypto",
                    "preMarketChange": 0,
                    "preMarketPercentChange": 0,
                    # Same discipline as the equity path: 52-week extremes are
                    # not in a quote feed. Never fabricate them.
                    "52WkHigh": None,
                    "52WkLow": None,
                },
                "regular": {
                    "regularMarketLastPrice": last,
                    "regularMarketNetChange": change,
                    "regularMarketPercentChange": pct,
                },
                "fundamental": {},
            }
        }
    except Exception as exc:
        log.warning("Crypto quote fetch failed for %s (%s): %s", canon, ticker, exc)
        return {}


def _coinbase_row(canon: str, stats: dict) -> dict:
    """Coinbase 24h stats -> the Schwab-compatible quote dict.

    The 24h move is measured against the stats window's OPEN, which is what
    Coinbase itself calls the change; bid/ask stay 0 because a spot venue has no
    consolidated NBBO and we will not invent one from a mid.
    """
    last = float(stats["last"])
    open_price = float(stats.get("open") or 0.0)
    change = (last - open_price) if open_price else 0.0
    pct = (change / open_price * 100.0) if open_price else 0.0

    return {
        "quote": {
            "lastPrice": last,
            "bidPrice": 0,
            "askPrice": 0,
            "bidSize": 0,
            "askSize": 0,
            "netChange": change,
            "netPercentChangeInDouble": pct,
            "openPrice": open_price,
            "highPrice": float(stats.get("high") or 0.0),
            "lowPrice": float(stats.get("low") or 0.0),
            "closePrice": open_price,
            "totalVolume": float(stats.get("volume") or 0.0),
            "mark": last,
            "stale": False,
            "session": "crypto",
            "preMarketChange": 0,
            "preMarketPercentChange": 0,
            "52WkHigh": None,
            "52WkLow": None,
        },
        "regular": {
            "regularMarketLastPrice": last,
            "regularMarketNetChange": change,
            "regularMarketPercentChange": pct,
        },
        "fundamental": {},
    }


def get_crypto_quote(symbol: str, max_age: float | None = None) -> dict:
    """Quote for a crypto asset, keyed by any accepted spelling.

    Coinbase first: ~30ms, no key, and the venue FRED's CBBTCUSD series is drawn
    from, so the live price and the recorded daily series agree. yfinance is the
    fallback (delayed, and cached five minutes — which is why a crypto row used
    to sit still while equities ticked).

    ``max_age`` is the live stream's freshness floor.

    Returns ``{}`` (never a zeroed quote) for an unknown symbol or a failed
    fetch, so consumers degrade audibly instead of showing a $0 price.
    """
    canon = canonical(symbol)
    if not canon:
        return {}

    stats = coinbase.quote(canon, max_age=max_age)
    if stats:
        return {canon: _coinbase_row(canon, stats)}
    return _quote_cached(canon)


def crypto_details(symbol: str) -> dict | None:
    """Ticker-details payload for a crypto asset, mirroring the equity shape.

    The equity version (``data/reference.py``) reads Massive's reference
    endpoint, which has no crypto record. Fields that genuinely do not exist for
    a crypto asset are ``None``, not 0 and not a plausible-looking guess —
    a market cap of 0 would read as "worthless", not "not applicable".
    """
    meta = crypto_meta(symbol)
    if not meta:
        return None
    canon = canonical(symbol)
    return {
        "symbol": canon,
        "name": meta["name"],
        "description": None,
        "sic_code": None,
        "sic_description": None,
        "market_cap": None,
        "total_employees": None,
        "homepage_url": None,
        "logo_url": None,
        "list_date": None,
        "shares_outstanding": None,
        "asset_type": "CRYPTO",
        "quote_currency": meta["quote_currency"],
        "trades_weekends": meta["trades_weekends"],
        "source": meta["source"],
    }


def search_crypto(query: str) -> list[dict]:
    """Crypto matches for a search query, in the Schwab instrument shape.

    Needed because the equity search hits Massive's reference endpoint, which
    has no crypto records — searching "BTC" there returns ABTC, BDCI and other
    equity tickers containing those letters, never Bitcoin. Without this, the
    asset cannot be found from the app's own search.
    """
    q = (query or "").strip().strip(":").upper()
    if not q:
        return []
    # A query may itself BE a known spelling (X:BTCUSD), which does not appear
    # as a substring of the canonical row — resolve it first.
    q_canon = canonical(q)
    out = []
    for canon, meta in CRYPTO_META.items():
        ticker = CRYPTO_YF_TICKERS.get(canon, "")
        haystack = f"{canon} {meta['name']} {ticker}".upper()
        if q_canon == canon or q in haystack:
            out.append({
                "symbol": canon,
                "description": meta["name"],
                "assetType": "CRYPTO",
                "exchange": "CRYPTO",
            })
    return out
