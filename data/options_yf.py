"""yfinance option-chain fallback — free delayed data when Massive 403s.

Massive's current plan is NOT_AUTHORIZED for options snapshots (verified live
2026-10-07), so every chain request died to the plan wall. This module
reshapes a yfinance chain into the SAME Schwab-compatible dict shape
get_option_chain already returns, so every downstream consumer (flatten,
router, agent tool) needs zero changes.

yfinance rows carry NaN in volume/openInterest/greeks routinely — every
numeric cast here is NaN-safe. Transient Yahoo rate limits get ONE spaced
retry before giving up (logged, never silent).
"""

from __future__ import annotations

import datetime
import logging
import math
import time

log = logging.getLogger(__name__)


def _num(value, default=0.0) -> float:
    """NaN-safe float coercion."""
    try:
        f = float(value)
        return default if math.isnan(f) else f
    except (TypeError, ValueError):
        return default


def _opt(value) -> float | None:
    """Optional float: NaN/None/absent -> None (unavailable), never 0."""
    try:
        f = float(value)
        return None if math.isnan(f) else f
    except (TypeError, ValueError):
        return None


def _int(value, default=0) -> int:
    return int(_num(value, default))


def _bool_flag(value) -> bool | None:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    return bool(value)


def _spot(symbol: str) -> float:
    """Best-effort underlying spot for ITM flags; 0 when unavailable (acceptable)."""
    try:
        import yfinance as yf

        fi = yf.Ticker(symbol).fast_info
        for attr in ("last_price", "lastPrice", "previous_close_price", "previousClosePrice"):
            price = getattr(fi, attr, None)
            if price:
                return float(price)
        return 0.0
    except Exception:
        return 0.0


def yf_option_chain(symbol: str, retried: bool = False) -> dict | None:
    """Return a Schwab-compatible chain dict, or None when yfinance fails."""
    try:
        import yfinance as yf

        t = yf.Ticker(symbol.upper())
        expiries = t.options
        if not expiries:
            log.warning("yfinance returned no expiries for %s", symbol)
            return None
        spot = _spot(symbol)

        call_map: dict[str, dict[str, list]] = {}
        put_map: dict[str, dict[str, list]] = {}
        now = datetime.datetime.now()

        for exp_date in expiries[:6]:  # 6 nearest expiries is plenty for analysis
            ch = t.option_chain(exp_date)
            try:
                exp_dt = datetime.datetime.strptime(exp_date, "%Y-%m-%d")
                dte = max(0, (exp_dt - now).days)
            except ValueError:
                dte = 0
            for kind, frame in (("CALL", ch.calls), ("PUT", ch.puts)):
                if frame is None or frame.empty:
                    continue
                target_map = call_map if kind == "CALL" else put_map
                exp_key = f"{exp_date}:0"
                bucket = target_map.setdefault(exp_key, {})
                for _, r in frame.iterrows():
                    strike = _num(r.get("strike"), 0.0)
                    itm = (
                        (spot > strike) if kind == "CALL" else (spot < strike)
                    ) if spot else _bool_flag(r.get("inTheMoney"))
                    contract = {
                        "symbol": str(r.get("contractSymbol", "")),
                        "description": f"{symbol.upper()} {exp_date} {strike:g} {kind}",
                        "putCall": kind,
                        "strikePrice": strike,
                        "bid": _num(r.get("bid")),
                        "ask": _num(r.get("ask")),
                        "last": _num(r.get("lastPrice")),
                        "totalVolume": _int(r.get("volume")),
                        "openInterest": _int(r.get("openInterest")),
                        # yfinance chains carry NO greek columns at all — never
                        # coerce a missing greek to 0.0 (that fabricates a
                        # delta-0.0 ATM call). None means unavailable.
                        "delta": _opt(r.get("delta")),
                        "gamma": _opt(r.get("gamma")),
                        "theta": _opt(r.get("theta")),
                        "vega": _opt(r.get("vega")),
                        "volatility": _opt(r.get("impliedVolatility")),
                        "daysToExpiration": dte,
                        "inTheMoney": bool(itm) if itm is not None else False,
                        "expirationDate": exp_date,
                    }
                    bucket.setdefault(str(strike), []).append(contract)

        return {
            "callExpDateMap": call_map,
            "putExpDateMap": put_map,
            "underlyingPrice": spot,
            "source": "yfinance",  # provenance tag; the Massive path carries no such key
        }
    except Exception as exc:
        # Transient rate limits are the common failure (Yahoo throttles rapid
        # successive calls). One spaced retry before giving up.
        if not retried:
            log.warning(
                "yfinance chain attempt for %s failed (%s: %.150s) - retrying once",
                symbol, type(exc).__name__, exc,
            )
            time.sleep(2)
            return yf_option_chain(symbol, retried=True)
        log.error("yfinance chain fallback failed twice for %s: %s", symbol, exc)
        return None
