"""Massive (formerly Polygon.io) market data: quotes, price history, options, search."""

import datetime
import logging
import time
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from config import MASSIVE_API_KEY
from data.cache import ttl_cache

log = logging.getLogger(__name__)

_BASE = "https://api.massive.com"

_ET = ZoneInfo("America/New_York")

# US equity session boundaries (ET). Pre-market 4:00-9:30, regular 9:30-16:00,
# after-hours 16:00-20:00, closed otherwise (weekends + overnight).
_PRE_MARKET_OPEN = datetime.time(4, 0)
_REGULAR_OPEN = datetime.time(9, 30)
_REGULAR_CLOSE = datetime.time(16, 0)
_AFTER_HOURS_CLOSE = datetime.time(20, 0)


def market_session(now: datetime.datetime | None = None) -> str:
    """Classify the current US equity market session.

    Returns one of ``"pre-market"``, ``"regular"``, ``"after-hours"``,
    ``"closed"``. DST-safe: boundaries are evaluated in America/New_York, not
    server-local time. ``now`` is injectable for tests.
    """
    if now is None:
        now = datetime.datetime.now(datetime.timezone.utc)
    et = now.astimezone(_ET)
    if et.weekday() >= 5:
        return "closed"
    t = et.time()
    if t < _PRE_MARKET_OPEN:
        return "closed"
    if t < _REGULAR_OPEN:
        return "pre-market"
    if t < _REGULAR_CLOSE:
        return "regular"
    if t < _AFTER_HOURS_CLOSE:
        return "after-hours"
    return "closed"


def _redact_key(text: str) -> str:
    """A 429 from Massive must not write the API key into the logs.

    requests puts the full request URL — including ``apiKey=...`` — into the
    HTTPError string. Redact it before anything logs or re-raises it.
    """
    key = MASSIVE_API_KEY or ""
    return text.replace(key, "[redacted]") if key else text


def _redact_url(value) -> str:
    text = value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value or "")
    return _redact_key(text)


def _get(path: str, params: dict | None = None, timeout: int = 15) -> dict:
    params = params or {}
    params["apiKey"] = MASSIVE_API_KEY
    try:
        resp = requests.get(f"{_BASE}{path}", params=params, timeout=timeout)
        resp.raise_for_status()
    except requests.RequestException as exc:
        if MASSIVE_API_KEY:
            request = getattr(exc, "request", None)
            if request is not None:
                try:
                    request.url = _redact_url(request.url)
                except Exception:
                    pass
            response = getattr(exc, "response", None)
            if response is not None:
                try:
                    response.url = _redact_url(response.url)
                except Exception:
                    pass
            exc.args = tuple(_redact_key(str(arg)) for arg in exc.args)
        raise
    return resp.json()


@ttl_cache(ttl_seconds=30)
def get_snapshot(symbol: str) -> dict:
    """Single ticker snapshot: day bar, prevDay, change, lastQuote, lastTrade."""
    data = _get(f"/v2/snapshot/locale/us/markets/stocks/tickers/{symbol.upper()}")
    return data.get("ticker", {})


#: When the plans stops including snapshots, every quote miss would fire one
#: 403 for the batch and then N more in the per-symbol fallback. Once the feed
#: has said "not entitled", believe it for an hour instead of logging the same
#: refusal on every call.
_snapshots_blocked_until = 0.0


def _is_not_entitled(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    if getattr(response, "status_code", None) in (401, 403):
        return True
    text = str(exc)
    return "403" in text or "NOT_AUTHORIZED" in text or "not entitled" in text.lower()


@ttl_cache(ttl_seconds=30)
def get_snapshots(symbols: tuple[str, ...]) -> dict[str, dict]:
    """Batch snapshots via the all-tickers endpoint filtered to requested symbols.

    Uses the ``tickers`` filter so a cold cache on a large universe is a single
    HTTP request instead of N.
    """
    if not symbols:
        return {}
    syms = [s.upper() for s in symbols]

    global _snapshots_blocked_until
    if time.time() < _snapshots_blocked_until:
        return {}

    try:
        data = _get(
            "/v2/snapshot/locale/us/markets/stocks/tickers",
            params={"tickers": ",".join(syms)},
        )
        results = {}
        for t in data.get("tickers", []):
            sym = t.get("ticker")
            if sym:
                results[sym.upper()] = t
        return results
    except Exception as e:
        if _is_not_entitled(e):
            _snapshots_blocked_until = time.time() + 3600
            log.info(
                "Massive snapshots are not entitled on this plan (%s) — quote fallback disabled for 1h", e
            )
            return {}
        # Batch endpoint down or filtered request rejected — fall back to
        # per-symbol. This is a degradation path, so it must be audible.
        log.warning("Batch snapshot fetch failed (%s), falling back per-symbol", e)
        results = {}
        for sym in syms:
            try:
                results[sym] = get_snapshot(sym)
            except Exception as e:
                log.warning("Per-symbol snapshot fallback failed for %s: %s", sym, e)
        return results


@ttl_cache(ttl_seconds=3600)
def get_price_history(symbol: str, days: int = 365) -> pd.DataFrame:
    """Fetch daily OHLCV as a DataFrame from Massive aggregates."""
    end = datetime.datetime.now(datetime.timezone.utc)
    start = end - datetime.timedelta(days=days)
    start_str = start.strftime("%Y-%m-%d")
    end_str = end.strftime("%Y-%m-%d")

    data = _get(
        f"/v2/aggs/ticker/{symbol.upper()}/range/1/day/{start_str}/{end_str}",
        params={"adjusted": "true", "sort": "asc", "limit": "50000"},
    )
    results = data.get("results", [])
    if not results:
        return pd.DataFrame()

    df = pd.DataFrame(results)
    df["date"] = pd.to_datetime(df["t"], unit="ms")
    df = df.rename(columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"})
    df = df.set_index("date").sort_index()
    return df[["open", "high", "low", "close", "volume"]]


def get_quotes_schwab_compat(symbols: tuple[str, ...]) -> dict:
    """Fetch snapshots and reshape into Schwab-compatible quote format.

    The rest of the codebase expects the Schwab quote shape:
      {SYMBOL: {quote: {lastPrice, bidPrice, askPrice, ...}, regular: {...}}}
    This function bridges the Massive snapshot format to that shape.
    """
    snapshots = get_snapshots(symbols)
    result = {}
    session = market_session()
    for sym, snap in snapshots.items():
        day = snap.get("day", {})
        lq = snap.get("lastQuote", {})
        lt = snap.get("lastTrade", {})
        change = snap.get("todaysChange", 0)
        change_pct = snap.get("todaysChangePerc", 0)
        last_price = day.get("c") or (lt.get("p") if lt else 0)

        # Degrade, don't fabricate: Massive returns a zeroed day bar (c:0, no
        # lastTrade) pre-market/early-session. Serving 0 as a "price" silently
        # freezes downstream NAV at stale import values. Fall back to the
        # intraday minute bar, then prevDay close + todaysChange.
        #
        # Staleness is data-driven, not clock-guessed: a zeroed day bar with a
        # live minute bar is a *current extended-hours print* (pre-market /
        # after-hours / closed) — the price is real, just not a regular-session
        # print, so it is NOT stale. Only a zeroed day bar with no minute bar
        # during the regular session is genuine degradation. The clock only
        # picks the session label; the data decides staleness.
        stale = False
        if not last_price:
            min_bar = snap.get("min", {})
            prev_close = snap.get("prevDay", {}).get("c")
            if min_bar.get("c"):
                last_price = min_bar["c"]
            elif prev_close and change:
                last_price = prev_close + change
            stale = session == "regular"

        result[sym] = {
            "quote": {
                "lastPrice": last_price,
                "bidPrice": lq.get("p", 0),
                "askPrice": lq.get("P", 0),
                "bidSize": lq.get("s", 0),
                "askSize": lq.get("S", 0),
                "netChange": change,
                "netPercentChangeInDouble": change_pct,
                "openPrice": day.get("o", 0),
                "highPrice": day.get("h", 0),
                "lowPrice": day.get("l", 0),
                "closePrice": day.get("c", 0),
                "totalVolume": day.get("v", 0),
                "mark": last_price,
                "stale": stale,
                "session": session,
                # Extended-hours move vs prior close. Massive reports the
                # overnight/pre-market move as todaysChange even when the day
                # bar is zeroed; surface it so consumers can show "PM -2.5%"
                # instead of greying the row.
                "preMarketChange": change,
                "preMarketPercentChange": change_pct,
                # 52-week high/low isn't in the snapshot feed. Never fabricate
                # a plausible-looking default — consumers derive it from price
                # history (see agent/tools.py tool_get_quote) or degrade.
                "52WkHigh": None,
                "52WkLow": None,
            },
            "regular": {
                "regularMarketLastPrice": last_price,
                "regularMarketNetChange": change,
                "regularMarketPercentChange": change_pct,
            },
            "fundamental": {},
        }
    return result


@ttl_cache(ttl_seconds=300)
def get_option_chain(symbol: str) -> dict:
    """Fetch option chain snapshot and reshape to Schwab-compatible format."""
    data = _get(
        f"/v3/snapshot/options/{symbol.upper()}",
        params={"limit": "250"},
        timeout=30,
    )
    results = data.get("results", [])

    call_map: dict[str, dict[str, list]] = {}
    put_map: dict[str, dict[str, list]] = {}
    underlying_price = 0

    for opt in results:
        details = opt.get("details", {})
        greeks = opt.get("greeks", {})
        day = opt.get("day", {})
        uq = opt.get("underlying_asset", {})
        if uq and uq.get("price"):
            underlying_price = uq["price"]

        exp_date = details.get("expiration_date", "")
        strike = details.get("strike_price", 0)
        contract_type = details.get("contract_type", "").lower()

        contract = {
            "symbol": details.get("ticker", ""),
            "description": f"{symbol.upper()} {exp_date} {strike} {contract_type.upper()}",
            "putCall": "CALL" if contract_type == "call" else "PUT",
            "strikePrice": strike,
            "bid": opt.get("last_quote", {}).get("bid", 0),
            "ask": opt.get("last_quote", {}).get("ask", 0),
            # "last_updated" is a Unix timestamp, not a price — falling back to
            # it when close is 0 would report a ~1.7-billion-dollar last price.
            "last": day.get("close", 0),
            "totalVolume": day.get("volume", 0),
            "openInterest": opt.get("open_interest", 0),
            "delta": greeks.get("delta", 0),
            "gamma": greeks.get("gamma", 0),
            "theta": greeks.get("theta", 0),
            "vega": greeks.get("vega", 0),
            "volatility": opt.get("implied_volatility", 0),
            "daysToExpiration": 0,
            "inTheMoney": False,
            "expirationDate": exp_date,
        }

        if underlying_price and strike:
            if contract_type == "call":
                contract["inTheMoney"] = underlying_price > strike
            else:
                contract["inTheMoney"] = underlying_price < strike

        if exp_date:
            try:
                exp_dt = datetime.datetime.strptime(exp_date, "%Y-%m-%d")
                contract["daysToExpiration"] = max(
                    0, (exp_dt - datetime.datetime.now()).days
                )
            except ValueError:
                pass

        target_map = call_map if contract_type == "call" else put_map
        exp_key = f"{exp_date}:0"
        strike_key = str(strike)
        if exp_key not in target_map:
            target_map[exp_key] = {}
        if strike_key not in target_map[exp_key]:
            target_map[exp_key][strike_key] = []
        target_map[exp_key][strike_key].append(contract)

    return {
        "callExpDateMap": call_map,
        "putExpDateMap": put_map,
        "underlyingPrice": underlying_price,
    }


@ttl_cache(ttl_seconds=300)
def search_instruments(query: str) -> dict:
    """Search for tickers and reshape to Schwab-compatible format."""
    data = _get(
        "/v3/reference/tickers",
        params={"search": query, "active": "true", "limit": "20"},
    )
    instruments = []
    for r in data.get("results", []):
        asset_type = "EQUITY"
        if r.get("type") == "ETF":
            asset_type = "ETF"
        elif r.get("market") == "otc":
            asset_type = "OTC"
        instruments.append({
            "symbol": r.get("ticker", ""),
            "description": r.get("name", ""),
            "assetType": asset_type,
            "exchange": r.get("primary_exchange", ""),
        })
    return {"instruments": instruments}


@ttl_cache(ttl_seconds=300)
def get_fundamentals(symbol: str) -> dict:
    """Fetch ticker details and reshape to Schwab-compatible fundamentals."""
    data = _get(f"/v3/reference/tickers/{symbol.upper()}")
    r = data.get("results", {})
    return {
        "instruments": [{
            "symbol": r.get("ticker", symbol),
            "description": r.get("description", ""),
            "fundamental": {
                "symbol": r.get("ticker", symbol),
                "marketCap": r.get("market_cap", 0),
                "sharesOutstanding": r.get("share_class_shares_outstanding", 0),
                "description": r.get("description", ""),
                "sector": r.get("sic_description", ""),
                "exchange": r.get("primary_exchange", ""),
            },
        }],
    }
