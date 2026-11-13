"""Coinbase Exchange public market data — crypto quotes and candles, no key.

Why here: Schwab has no crypto product, and yfinance served crypto at ~176ms
with a 15-minute-delayed feel and a five-minute cache. Coinbase's public
endpoints answer in ~30ms, are the venue FRED's CBBTCUSD series is derived from,
and need no credentials — so crypto can tick at the same 2-second freshness as
equities instead of freezing for five minutes.

THE TRAP THIS MODULE EXISTS TO AVOID
------------------------------------
Coinbase candles are NOT OHLC order. The row is:

    [time, LOW, HIGH, OPEN, CLOSE, volume]

Reading it as (open, high, low, close) silently swaps open with low and high
with open: every candle stays plausible — high still exceeds low — while the
open, the high and the low are all wrong. ``_candle_frame`` maps by index and a
test pins it.

Public endpoints are rate-limited per IP (10 req/s, burst 15), so a 350-row cap
per candle request and a short per-symbol cache keep us far inside it.
"""

from __future__ import annotations

import logging
import threading
import time
from zoneinfo import ZoneInfo

import pandas as pd
import requests

log = logging.getLogger(__name__)

_API = "https://api.exchange.coinbase.com"
_UA = {"User-Agent": "meridian/1.0"}
_ET = ZoneInfo("America/New_York")

#: Canonical symbol -> Coinbase product id.
PRODUCTS: dict[str, str] = {
    "BTC": "BTC-USD",
}

#: How long a quote may be reused. The live stream passes max_age=2.
_QUOTE_TTL = 2.0

_CANDLE_MAX = 350
_GRANULARITY_DAILY = 86400

_quote_stamp: dict[str, tuple[float, dict]] = {}
_quote_lock = threading.Lock()


def product(canon: str) -> str | None:
    return PRODUCTS.get(str(canon).upper())


def _get(path: str, params: dict | None = None, timeout: int = 10):
    resp = requests.get(f"{_API}{path}", params=params or {}, headers=_UA, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def quote(canon: str, max_age: float | None = None) -> dict | None:
    """24h stats for a canonical symbol: last, open, high, low, volume.

    Cached per symbol so several stream subscribers share one request. Returns
    None on any failure — the caller falls back rather than inventing a price.
    """
    prod = product(canon)
    if not prod:
        return None

    ttl = _QUOTE_TTL if max_age is None else max(0.0, float(max_age))
    now = time.time()
    with _quote_lock:
        hit = _quote_stamp.get(canon)
        if hit is not None and now - hit[0] < ttl:
            return hit[1]

    try:
        raw = _get(f"/products/{prod}/stats")
        out = {
            "last": float(raw["last"]),
            "open": float(raw["open"]),
            "high": float(raw["high"]),
            "low": float(raw["low"]),
            "volume": float(raw.get("volume") or 0.0),
        }
    except Exception as exc:
        log.warning("Coinbase quote failed for %s: %s", prod, exc)
        return None

    with _quote_lock:
        _quote_stamp[canon] = (time.time(), out)
    return out


def _candle_frame(rows: list[list]) -> pd.DataFrame:
    """Coinbase candle rows -> OHLCV frame indexed by date.

    Row order is [time, low, high, open, close, volume] — mapped by index, never
    by position-in-a-tuple-that-looks-like-OHLC.
    """
    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows, columns=["t", "low", "high", "open", "close", "volume"])
    df = df.astype({"t": "int64", "low": float, "high": float, "open": float, "close": float, "volume": float})
    dates = pd.to_datetime(df["t"], unit="s", utc=True).dt.tz_convert(_ET).dt.normalize().dt.tz_localize(None)
    df.index = pd.DatetimeIndex(dates, name="date")
    return df[["open", "high", "low", "close", "volume"]].sort_index()


def candles(canon: str, days: int = 365) -> pd.DataFrame:
    """Daily OHLCV for *canon*, or an empty frame.

    Coinbase returns at most 350 candles per request, so a longer window is
    walked backwards in pages. A page that fails ends the walk rather than
    fabricating continuity — the caller gets what was actually served.
    """
    prod = product(canon)
    if not prod or days <= 0:
        return pd.DataFrame()

    remaining = int(days)
    end = int(time.time())
    frames: list[pd.DataFrame] = []

    while remaining > 0:
        span = min(remaining, _CANDLE_MAX)
        start = end - span * _GRANULARITY_DAILY
        try:
            rows = _get(
                f"/products/{prod}/candles",
                {
                    "granularity": _GRANULARITY_DAILY,
                    "start": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(start)),
                    "end": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(end)),
                },
            )
        except Exception as exc:
            log.warning("Coinbase candles failed for %s: %s", prod, exc)
            break
        frame = _candle_frame(rows if isinstance(rows, list) else [])
        if frame.empty:
            break
        frames.append(frame)
        remaining -= span
        end = int(frame.index[0].timestamp()) if not frame.empty else end - span * _GRANULARITY_DAILY

    if not frames:
        return pd.DataFrame()

    merged = pd.concat(frames)
    merged = merged[~merged.index.duplicated(keep="last")].sort_index()
    return merged.tail(days)
