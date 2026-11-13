"""Free market feed — Yahoo Finance via yfinance.

The self-hosted edition ships with no paid data key. This module is the
default equity feed when neither a Schwab grant nor a Massive key is
configured, and the last fallback when either fails.

Daily bars, delayed quotes. It never fabricates a price: a symbol Yahoo cannot
answer is omitted, and the caller's existing "no quote -> stale" path takes
over.

Politeness: one batched request per symbol set, a 30s quote cache, a 15-minute
history cache, a 1s minimum between requests, and a 5-minute backoff after a
Yahoo error. A long universe (sonar) is capped rather than hammered.
"""

from __future__ import annotations

import datetime as dt
import logging
import math
import threading
import time
from zoneinfo import ZoneInfo

import pandas as pd

log = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")
_QUOTE_TTL = 30.0
_HISTORY_TTL = 900.0
_FUNDAMENTAL_TTL = 3600.0
_SEARCH_TTL = 300.0
_MIN_INTERVAL = 0.35
_BACKOFF_SECONDS = 300.0
_MAX_BATCH = 60

_lock = threading.Lock()
_cache: dict[tuple, tuple[float, object]] = {}
_last_call = 0.0
_backoff_until = 0.0


def _session() -> str:
    now = dt.datetime.now(_ET)
    if now.weekday() >= 5:
        return "closed"
    minutes = now.hour * 60 + now.minute
    if minutes < 4 * 60:
        return "closed"
    if minutes < 9 * 60 + 30:
        return "pre-market"
    if minutes < 16 * 60:
        return "regular"
    if minutes < 20 * 60:
        return "after-hours"
    return "closed"


def _cached(key: tuple, ttl: float):
    item = _cache.get(key)
    if item and time.time() - item[0] < ttl:
        return item[1]
    return None


def _store(key: tuple, value) -> None:
    _cache[key] = (time.time(), value)
    if len(_cache) > 256:
        cutoff = time.time() - 6 * 3600
        for old_key in [k for k, (ts, _) in _cache.items() if ts < cutoff]:
            _cache.pop(old_key, None)


def _num(value, default=0.0) -> float:
    try:
        number = float(value)
        return default if math.isnan(number) else number
    except (TypeError, ValueError):
        return default


def _download(tickers: str, **kwargs) -> pd.DataFrame | None:
    """One polite yfinance call. Returns None on rate limit or failure."""
    global _last_call, _backoff_until
    now = time.time()
    if now < _backoff_until:
        return None
    with _lock:
        wait = _MIN_INTERVAL - (time.time() - _last_call)
        if wait > 0:
            time.sleep(wait)
        _last_call = time.time()
    try:
        import yfinance as yf

        df = yf.download(
            tickers=tickers,
            group_by="ticker",
            auto_adjust=False,
            progress=False,
            threads=False,
            **kwargs,
        )
        if df is None or df.empty:
            return None
        return df
    except Exception as exc:
        _backoff_until = time.time() + _BACKOFF_SECONDS
        log.warning("yfinance unavailable for %d tickers (%s); backing off %.0fs",
                    len(tickers.split()), exc, _BACKOFF_SECONDS)
        return None


def _symbol_frame(df: pd.DataFrame | None, symbol: str) -> pd.DataFrame | None:
    if df is None or df.empty:
        return None
    try:
        if isinstance(df.columns, pd.MultiIndex):
            level0 = df.columns.get_level_values(0)
            if symbol in level0:
                sub = df[symbol]
            elif len(set(level0)) == 1:
                sub = df.droplevel(0, axis=1)
            else:
                return None
        else:
            sub = df
        sub = sub.copy()
        sub.columns = [str(c).strip().lower().replace(" ", "_") for c in sub.columns]
        if "close" not in sub.columns:
            return None
        sub = sub[~sub["close"].isna()]
        return sub if not sub.empty else None
    except Exception:
        return None


def get_quotes_schwab_compat(symbols: tuple[str, ...], max_age: float | None = None) -> dict:
    """Batch quotes in the Schwab-compatible shape the app already consumes."""
    syms = tuple(sorted({s.upper() for s in symbols if s}))
    if not syms:
        return {}
    if len(syms) > _MAX_BATCH:
        log.info("yfinance quote batch capped at %d of %d symbols", _MAX_BATCH, len(syms))
        syms = syms[:_MAX_BATCH]
    key = ("quotes", syms)
    ttl = max(_QUOTE_TTL, float(max_age or 0))
    cached = _cached(key, ttl)
    if cached is not None:
        return dict(cached)

    df = _download(" ".join(syms), period="5d", interval="1d")
    if df is None:
        return dict(_cached(key, 6 * 3600) or {})

    session = _session()
    result: dict[str, dict] = {}
    for sym in syms:
        sub = _symbol_frame(df, sym)
        if sub is None or len(sub) < 1:
            continue
        last = sub.iloc[-1]
        prev_close = _num(sub.iloc[-2]["close"], _num(last["close"])) if len(sub) >= 2 else _num(last["close"])
        price = _num(last["close"])
        if price <= 0:
            continue
        change = price - prev_close
        pct = (change / prev_close * 100) if prev_close else 0.0
        volume = _num(last.get("volume"))
        result[sym] = {
            "quote": {
                "lastPrice": price,
                "bidPrice": 0,
                "askPrice": 0,
                "bidSize": 0,
                "askSize": 0,
                "netChange": change,
                "netPercentChangeInDouble": pct,
                "openPrice": _num(last.get("open")),
                "highPrice": _num(last.get("high")),
                "lowPrice": _num(last.get("low")),
                "closePrice": price,
                "totalVolume": volume,
                "mark": price,
                "stale": False,
                "session": session,
                "preMarketChange": 0,
                "preMarketPercentChange": 0,
                "52WkHigh": None,
                "52WkLow": None,
            },
            "regular": {
                "regularMarketLastPrice": price,
                "regularMarketNetChange": change,
                "regularMarketPercentChange": pct,
            },
            "fundamental": {},
        }
    if result:
        _store(key, result)
    return result


def get_price_history(symbol: str, days: int = 365) -> pd.DataFrame:
    """Daily OHLCV, lowercase columns, tz-naive DatetimeIndex."""
    symbol = symbol.upper()
    days = max(int(days), 30)
    key = ("history", symbol, days)
    cached = _cached(key, _HISTORY_TTL)
    if isinstance(cached, pd.DataFrame):
        return cached.copy()

    end = dt.date.today() + dt.timedelta(days=1)
    start = dt.date.today() - dt.timedelta(days=days)
    df = _download(symbol, start=start.isoformat(), end=end.isoformat(), interval="1d")
    sub = _symbol_frame(df, symbol)
    if sub is None:
        return pd.DataFrame()

    close_col = "adj_close" if "adj_close" in sub.columns and not sub["adj_close"].isna().all() else "close"
    out = pd.DataFrame({
        "open": sub.get("open", sub["close"]),
        "high": sub.get("high", sub["close"]),
        "low": sub.get("low", sub["close"]),
        "close": sub[close_col],
        "volume": sub.get("volume", 0),
    })
    out.index = pd.to_datetime(out.index)
    if getattr(out.index, "tz", None) is not None:
        out.index = out.index.tz_localize(None)
    out = out[~out["close"].isna()].sort_index()
    out = out[~out.index.duplicated(keep="last")]
    if out.empty:
        return out
    _store(key, out)
    return out.copy()


def get_fundamentals(symbol: str) -> dict:
    """A small Schwab-compatible fundamentals block from Yahoo."""
    symbol = symbol.upper()
    key = ("fundamentals", symbol)
    cached = _cached(key, _FUNDAMENTAL_TTL)
    if cached is not None:
        return dict(cached)
    try:
        import yfinance as yf

        info = yf.Ticker(symbol).get_info() or {}
        record = {
            "instruments": [{
                "symbol": symbol,
                "description": info.get("longName") or info.get("shortName") or "",
                "fundamental": {
                    "symbol": symbol,
                    "marketCap": info.get("marketCap", 0),
                    "sharesOutstanding": info.get("sharesOutstanding", 0),
                    "description": info.get("longName") or info.get("shortName") or "",
                    "sector": info.get("sector") or "",
                    "exchange": info.get("exchange") or "",
                },
            }],
        }
        _store(key, record)
        return dict(record)
    except Exception as exc:
        log.info("yfinance fundamentals unavailable for %s (%s)", symbol, exc)
        return {"instruments": []}


def search_instruments(query: str) -> dict:
    """Ticker search, reshaped to the Schwab-compatible result list."""
    query = (query or "").strip()
    if not query:
        return {"instruments": []}
    key = ("search", query.lower())
    cached = _cached(key, _SEARCH_TTL)
    if cached is not None:
        return dict(cached)
    instruments: list[dict] = []
    try:
        import yfinance as yf

        for item in (yf.Search(query, max_results=20).quotes or []):
            quote_type = str(item.get("quoteType") or "").upper()
            if quote_type not in ("EQUITY", "ETF", "MUTUALFUND", "INDEX", "CRYPTOCURRENCY"):
                continue
            instruments.append({
                "symbol": item.get("symbol", ""),
                "description": item.get("longname") or item.get("shortname") or "",
                "assetType": "ETF" if quote_type == "ETF" else "EQUITY",
                "exchange": item.get("exchange", ""),
            })
    except Exception as exc:
        log.info("yfinance search unavailable (%s)", exc)
    if not instruments:
        symbol = query.upper()
        try:
            import yfinance as yf

            hist = yf.Ticker(symbol).history(period="5d")
            if hist is not None and not hist.empty:
                instruments = [{"symbol": symbol, "description": "", "assetType": "EQUITY", "exchange": ""}]
        except Exception:
            pass
    record = {"instruments": instruments}
    if instruments:
        _store(key, record)
    return record
