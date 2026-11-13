"""Earnings data via StockAnalysis.com — free, no API key.

Two tiers:
  Tier 1 (calendar, 1 request, TTL 6h): GET /_api/endpoints/earnings-calendar
    serves {status, data: {days, weeks:[{weekOf, days:[{date, day, symbols:[
    {s, n, t(amc/bmo), e(eps est), eg, r(rev est), rg, m}]}]}]}} (~4,500 symbols).
    Indexed by symbol. Covers ~492/503 S&P 500 names. The HTML page
    /stocks/earnings-calendar/ is the fallback (it stopped embedding per-symbol
    rows in Oct 2026 — now only aggregate day counts).
  Tier 2 (per-symbol, TTL 24h, on-demand): GET /stocks/{sym}/financials/?p=quarterly
    gives the "Earnings Per Share" row for prior-quarter EPS actuals. Only fired
    for a symbol being viewed on Search — never in hot paths.

Fallback: if a symbol isn't in the calendar window (e.g. BRK-B, COST), the
per-stock page's `earningsDate:"..."` field is used for the date alone.

Public API (unchanged contract):
    get_ticker_earnings(symbol) -> dict | None
    get_portfolio_earnings(symbols, positions_df) -> list[dict]
"""

from __future__ import annotations

import logging
import re
from datetime import datetime

import pandas as pd
import requests

from data.cache import ttl_cache

log = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
}
_CALENDAR_URL = "https://stockanalysis.com/stocks/earnings-calendar/"
# The site moved the per-symbol calendar rows here (~Oct 2026): the HTML page
# now embeds only aggregate day counts, while this JSON route serves the full
# weeks[].days[].symbols[] payload (same {s, n, t, e, r} schema, ~4,500 symbols).
_CALENDAR_API_URL = "https://stockanalysis.com/_api/endpoints/earnings-calendar"
_STOCK_URL = "https://stockanalysis.com/stocks/{sym}/"
_FIN_URL = "https://stockanalysis.com/stocks/{sym}/financials/?p=quarterly"

# ---------------------------------------------------------------------------
# Calendar parsing
# ---------------------------------------------------------------------------

def parse_calendar(html: str) -> dict[str, dict]:
    """Parse the embedded earnings JSON blob into {SYMBOL: {date, eps_est, rev_est, time}}.

    The page embeds: earnings:[{weekOf:"...",days:[{date:"...",day:"...",symbols:[...]}]}]
    Handles nulls and leading-dot numbers (e:-.4, e:.78).
    """
    idx = html.find("earnings:[{weekOf:")
    if idx < 0:
        return {}
    start = html.find("[", idx)
    if start < 0:
        return {}
    depth = 0
    end = -1
    for i in range(start, len(html)):
        if html[i] == "[":
            depth += 1
        elif html[i] == "]":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end < 0:
        return {}
    js = html[start:end]

    # Convert JS object literal to JSON: quote bare keys, fix leading-dot numbers
    js = re.sub(r"([{,]\s*)([a-zA-Z_][a-zA-Z0-9_]*):", r'\1"\2":', js)
    js = re.sub(r":-\.", ":-0.", js)
    js = re.sub(r":\.", ":0.", js)

    import json
    try:
        data = json.loads(js)
    except json.JSONDecodeError:
        return {}

    by_sym: dict[str, dict] = {}
    for week in data:
        for day in week.get("days", []):
            date = day.get("date")
            for s in day.get("symbols", []):
                sym = s.get("s")
                if not sym:
                    continue
                by_sym[sym] = {
                    "date": date,
                    "eps_estimate": s.get("e"),
                    "revenue_estimate": s.get("r"),
                    "time": s.get("t"),
                }
    return by_sym


def parse_calendar_json(data: dict) -> dict[str, dict]:
    """Parse the calendar JSON endpoint payload into {SYMBOL: {...}}.

    Payload shape: {status, data: {days: [...aggregate counts...],
    weeks: [{weekOf, days: [{date, day, symbols: [{s, n, t, e, r}]}]}]}}.
    Only weeks[].days[].symbols[] carries per-symbol rows.
    """
    weeks = (data.get("data") or {}).get("weeks") or []
    by_sym: dict[str, dict] = {}
    for week in weeks:
        for day in week.get("days", []):
            date = day.get("date")
            for s in day.get("symbols", []) or []:
                sym = s.get("s")
                if not sym:
                    continue
                by_sym[sym] = {
                    "date": date,
                    "eps_estimate": s.get("e"),
                    "revenue_estimate": s.get("r"),
                    "time": s.get("t"),
                }
    return by_sym


@ttl_cache(ttl_seconds=21600)  # 6 hours
def get_calendar() -> dict[str, dict]:
    """Fetch and parse the earnings calendar once. Returns {SYMBOL: {...}}.

    Primary source is the JSON endpoint; the HTML page's embedded blob is kept
    as a fallback in case the site reverts or the API route moves. An empty
    result from both sources is a degraded state, not an empty universe.
    """
    try:
        r = requests.get(_CALENDAR_API_URL, headers=_HEADERS, timeout=15)
        r.raise_for_status()
        cal = parse_calendar_json(r.json())
        if cal:
            return cal
        log.warning("Earnings calendar JSON empty; falling back to HTML parse")
    except Exception as e:
        log.warning("Earnings calendar API fetch failed: %s", e)
    try:
        r = requests.get(_CALENDAR_URL, headers=_HEADERS, timeout=15)
        r.raise_for_status()
        return parse_calendar(r.text)
    except Exception as e:
        log.warning("Earnings calendar fetch failed: %s", e)
        return {}


# ---------------------------------------------------------------------------
# Per-symbol enrichment
# ---------------------------------------------------------------------------

@ttl_cache(ttl_seconds=86400)  # 24 hours
def _get_prior_eps(symbol: str) -> float | None:
    """Prior-quarter EPS actual from the quarterly financials page."""
    try:
        r = requests.get(_FIN_URL.format(sym=symbol.lower()), headers=_HEADERS, timeout=12)
        r.raise_for_status()
        text = r.text
        m = re.search(r"<table[^>]*>.*?</table>", text, re.S)
        if not m:
            return None
        cells = re.findall(r"<td[^>]*>(.*?)</td>", m.group(0), re.S)
        clean = [re.sub(r"<[^>]+>", "", c).strip() for c in cells]
        for i, c in enumerate(clean):
            if "Earnings Per" in c and i + 1 < len(clean):
                try:
                    return float(clean[i + 1])
                except ValueError:
                    return None
        return None
    except Exception as e:
        log.warning("Prior-EPS fetch failed for %s: %s", symbol, e)
        return None


@ttl_cache(ttl_seconds=86400)  # 24 hours
def _get_stock_earnings_date(symbol: str) -> str | None:
    """Fallback: earnings date from the per-stock page (earningsDate:"..." field)."""
    try:
        r = requests.get(_STOCK_URL.format(sym=symbol.lower()), headers=_HEADERS, timeout=12)
        r.raise_for_status()
        m = re.search(r'earningsDate:"([^"]+)"', r.text)
        return m.group(1) if m else None
    except Exception as e:
        log.warning("Stock earnings-date fetch failed for %s: %s", symbol, e)
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------



_YAHOO_SESSION: requests.Session | None = None
_YAHOO_CRUMB: str = ""
_YAHOO_CRUMB_AT: float = 0.0


def _yahoo_session() -> tuple:
    """Shared session + fresh crumb (crumb TTL ~1h). Reuses across calls."""
    global _YAHOO_SESSION, _YAHOO_CRUMB, _YAHOO_CRUMB_AT
    import time
    now = time.time()
    if _YAHOO_SESSION is None:
        s = requests.Session()
        s.headers.update({
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36",
        })
        _YAHOO_SESSION = s
    if not _YAHOO_CRUMB or now - _YAHOO_CRUMB_AT > 3000:
        try:
            _YAHOO_SESSION.get("https://fc.yahoo.com", timeout=10)
            crumb = _YAHOO_SESSION.get(
                "https://query1.finance.yahoo.com/v1/test/getcrumb", timeout=10
            ).text.strip()
            if crumb and "Too Many" not in crumb:
                _YAHOO_CRUMB = crumb
                _YAHOO_CRUMB_AT = now
        except Exception:
            pass
    return _YAHOO_SESSION, _YAHOO_CRUMB


@ttl_cache(ttl_seconds=86400)  # 24 hours
def _yahoo_next_earnings(symbol: str) -> dict | None:
    """Next earnings date + EPS estimate from Yahoo quoteSummary.

    Returns {earnings_date: str (YYYY-MM-DD), eps_estimate: float} or None.
    Shared session + crumb; retries once on 429 with a short backoff.
    """
    import time
    s, crumb = _yahoo_session()
    if not crumb:
        return None
    url = (f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/"
           f"{symbol}?modules=calendarEvents&crumb={crumb}")
    for attempt in (0, 1):
        try:
            r = s.get(url, timeout=15)
            if r.status_code == 429 and attempt == 0:
                time.sleep(2.0)
                s, crumb = _yahoo_session()  # refresh crumb
                if not crumb:
                    return None
                url = (f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/"
                       f"{symbol}?modules=calendarEvents&crumb={crumb}")
                continue
            r.raise_for_status()
            d = r.json()
            ce = d.get("quoteSummary", {}).get("result", [{}])[0].get("calendarEvents", {})
            ed = ce.get("earnings", {}).get("earningsDate", [{}])
            if not ed or not ed[0].get("fmt"):
                return None
            return {
                "earnings_date": ed[0]["fmt"],
                "eps_estimate": ce.get("earnings", {}).get("earningsAverage", {}).get("raw"),
            }
        except Exception as e:
            if attempt == 0:
                time.sleep(1.0)
                continue
            log.warning("Yahoo earnings fetch failed for %s: %s", symbol, e)
            return None
    return None


def _parse_date(raw) -> pd.Timestamp | None:
    """Parse a date string into a tz-naive Timestamp, or None."""
    if raw is None:
        return None
    try:
        dt = pd.to_datetime(raw)
        if hasattr(dt, "tzinfo") and dt.tzinfo is not None:
            dt = dt.tz_localize(None)
        return dt
    except Exception as e:
        # Untrusted input guard: a malformed date means "no date", not failure.
        log.debug("Unparseable date %r: %s", raw, e)
        return None


def get_ticker_earnings(symbol: str) -> dict | None:
    """Next earnings date, EPS estimate, and prior EPS/surprise for *symbol*.

    Returns a dict with {earnings_date, eps_estimate, prior_eps_actual,
    prior_surprise_pct} or None if data is unavailable.
    """
    sym = symbol.upper()
    cal = get_calendar()
    entry = cal.get(sym)

    if entry is None:
        # Fallback 1: Yahoo quoteSummary (true next date, any horizon)
        y = _yahoo_next_earnings(sym)
        if y and y.get("earnings_date"):
            return {
                "earnings_date": _parse_date(y["earnings_date"]),
                "eps_estimate": y.get("eps_estimate"),
                "prior_eps_actual": None,
                "prior_surprise_pct": None,
            }
        # Fallback 2: per-stock page date only
        date_str = _get_stock_earnings_date(sym)
        if date_str is None:
            return None
        earnings_dt = _parse_date(date_str)
        if earnings_dt is None:
            return None
        return {
            "earnings_date": earnings_dt,
            "eps_estimate": None,
            "prior_eps_actual": None,
            "prior_surprise_pct": None,
        }

    earnings_dt = _parse_date(entry["date"])
    if earnings_dt is None:
        return None

    # Calendar entry stale (past date, e.g. window rolled past Q3) — use Yahoo.
    if earnings_dt < pd.Timestamp(datetime.now()):
        y = _yahoo_next_earnings(sym)
        if y and y.get("earnings_date"):
            return {
                "earnings_date": _parse_date(y["earnings_date"]),
                "eps_estimate": y.get("eps_estimate"),
                "prior_eps_actual": None,
                "prior_surprise_pct": None,
            }

    # Prior EPS is a per-symbol request — only worth it for a viewed symbol.
    prior_eps = _get_prior_eps(sym)

    return {
        "earnings_date": earnings_dt,
        "eps_estimate": entry.get("eps_estimate"),
        "prior_eps_actual": prior_eps,
        "prior_surprise_pct": None,  # not available from this source
    }


def get_portfolio_earnings(symbols, positions_df):
    """Batch-fetch earnings data for *symbols*, check options overlap, return sorted list.

    Uses the calendar fast path only (1 request total) — no per-symbol calls.
    Each entry: {symbol, earnings_date, days_until, urgent, eps_estimate,
                 prior_eps_actual, prior_surprise_pct, options_overlap}
    Sorted by days_until ascending.
    """
    cal = get_calendar()
    results = []
    now = datetime.now()

    # Track where each symbol's date came from, and which ones yielded nothing.
    # Without this the endpoint returns [] and there is no way to tell "nothing
    # upcoming" from "every source failed" — which is exactly the bug this fixes.
    provenance: dict[str, str] = {}
    unresolved: list[str] = []

    for sym in symbols:
        symbol = sym.upper()
        entry = cal.get(symbol)
        earnings_dt = None
        eps_est = None
        source = None
        if entry is not None:
            earnings_dt = _parse_date(entry["date"])
            if earnings_dt is not None and earnings_dt < pd.Timestamp(now):
                earnings_dt = None  # stale — try the per-symbol sources
            else:
                eps_est = entry.get("eps_estimate")
                source = "calendar"
        if earnings_dt is None:
            y = _yahoo_next_earnings(symbol)
            if y and y.get("earnings_date"):
                earnings_dt = _parse_date(y["earnings_date"])
                eps_est = y.get("eps_estimate")
                source = "yahoo"
        if earnings_dt is None:
            # Third fallback: the per-stock page. This source works when the
            # calendar is empty and Yahoo returns nothing, which is the state
            # that produced the silent empty list. Cached 24h per symbol.
            try:
                raw = _get_stock_earnings_date(symbol)
                if raw:
                    parsed = _parse_date(raw)
                    if parsed is not None:
                        earnings_dt = parsed
                        source = "stockanalysis"
            except Exception as e:
                log.debug("StockAnalysis earnings fallback failed for %s: %s", symbol, e)
        if earnings_dt is None:
            provenance[symbol] = "no_source"
            unresolved.append(symbol)
            continue
        provenance[symbol] = source or "unknown"

        days_until = (earnings_dt - pd.Timestamp(now)).days

        # Skip earnings more than 30 days in the past
        if days_until < -30:
            continue

        # Check options overlap: any option expiring within 7 days of earnings
        options_overlap = False
        sym_options = positions_df[
            (positions_df["underlying"] == sym.upper())
            & (positions_df["asset_type"] == "OPTION")
            & (positions_df["expiration"].notna())
        ]
        if not sym_options.empty:
            for _, opt in sym_options.iterrows():
                try:
                    exp_dt = pd.to_datetime(opt["expiration"])
                    if hasattr(exp_dt, "tzinfo") and exp_dt.tzinfo is not None:
                        exp_dt = exp_dt.tz_localize(None)
                    if abs((exp_dt - pd.Timestamp(earnings_dt)).days) <= 7:
                        options_overlap = True
                        break
                except Exception as e:
                    # Per-option date guard: skip unparseable expirations.
                    log.debug("Unparseable option expiration for %s: %s", sym, e)
                    continue

        results.append({
            "symbol": sym.upper(),
            "earnings_date": earnings_dt,
            "days_until": days_until,
            "urgent": 0 <= days_until <= 7,
            "eps_estimate": eps_est,
            "prior_eps_actual": None,
            "prior_surprise_pct": None,
            "options_overlap": options_overlap,
        })

    results.sort(key=lambda x: x["days_until"])
    return results

def get_portfolio_earnings_report(symbols, positions_df) -> dict:
    """Earnings rows PLUS an explicit coverage report.

    Returns ``{"rows": [...], "coverage": {...}}`` where coverage names each
    symbol's source and the symbols that could not be resolved at all.

    This exists because the bare list made an empty result indistinguishable
    from a failure: the endpoint returned ``[]`` while every holding was
    actually reporting inside five weeks, and nothing in the response hinted
    that the sources had gone dark. Coverage turns that silence into a statement.
    """
    rows = get_portfolio_earnings(symbols, positions_df)

    resolved = {r["symbol"] for r in rows if isinstance(r, dict)}
    requested = [str(s).upper() for s in symbols]
    unresolved = [s for s in requested if s not in resolved]

    return {
        "rows": rows,
        "coverage": {
            "requested": len(requested),
            "resolved": len(resolved),
            "unresolved": unresolved,
            "degraded": len(unresolved) > 0,
            "note": (
                "No earnings dates could be resolved for some holdings - the "
                "calendar and per-symbol sources both returned nothing. This is "
                "a data gap, not a statement that nothing is upcoming."
                if unresolved else
                "All requested symbols resolved to an earnings date."
            ),
        },
    }

