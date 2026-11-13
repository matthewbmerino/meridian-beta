"""Ticker universes for broad R×M scans.

The constituent lists move (quarterly for the S&P, less often for NDX/DJIA), so
they are pulled LIVE from published index tables rather than shipped as stale
constants: Wikipedia's "List of S&P 500 companies" for the S&P 500, and
stockanalysis.com's per-index list pages for the Nasdaq-100 and the Dow 30.

Both of the latter were ALSO Wikipedia-sourced until 2026-10-16, when the
articles stopped carrying a constituents table (the Nasdaq-100 article's
sections are History/Methodology/Performance/..., the DJIA article has no
Components section, and `List of Nasdaq-100 companies` is a 404). A scrape with
no matching table returns an EMPTY list, so both universes degraded to "no data"
on every surface that reads them — the leaderboard board, the sonar scan, and
the intelligence sector endpoints all lost two of their three index universes
without an error anywhere. Hence the sanity RANGE on every list parse: a page
that reflows or drops its table must fail as EMPTY (an explicit, reported
degradation) rather than resolve the universe to whatever stray tickers a loose
parse happened to pick up.

The Nasdaq-100 / Dow-30 sector columns died with those tables. Their sector
maps are therefore derived from the S&P 500's published GICS sectors: every Dow
member and the large majority of NDX members are S&P 500 names, so the
vocabulary stays GICS and comparable across universes, and a name the map does
not reach stays blank rather than guessed from a different classification.

Results are cached for a day; failures ride the cache's 60-second negative TTL,
so a transient blip retries within the minute.
"""

from __future__ import annotations

from io import StringIO

import pandas as pd
import requests

from data.cache import ttl_cache

_SP500_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
_NDX_URL   = "https://stockanalysis.com/list/nasdaq-100-stocks/"
_DJIA_URL  = "https://stockanalysis.com/list/dow-jones-stocks/"

_UA = "Mozilla/5.0 (compatible; MeridianSonar/1.0; +https://local)"


def _read_tables(url: str) -> list[pd.DataFrame]:
    resp = requests.get(url, headers={"User-Agent": _UA}, timeout=15)
    resp.raise_for_status()
    return pd.read_html(StringIO(resp.text))


def _clean_symbols(symbols) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for s in symbols:
        if not isinstance(s, str):
            continue
        t = s.strip().upper().replace(".", "-")  # BRK.B -> BRK-B (Schwab style)
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _norm(s):
    return str(s).strip().upper().replace(".", "-")


def _list_page_symbols(tables: list[pd.DataFrame], lo: int, hi: int) -> list[str]:
    """Symbol column of a published index table, inside a sanity range.

    The range is the guard that matters: NDX is ~101 securities and the DJIA 30,
    so a parse landing far outside that is a reflowed page, not a changed index
    — return EMPTY and let the caller degrade explicitly (the leaderboard's
    ``_universe`` refuses to rank a partial board and says why).
    """
    for t in tables:
        col = next((c for c in t.columns if str(c).strip() in ("Symbol", "Ticker")), None)
        if col is None:
            continue
        syms = _clean_symbols(t[col].tolist())
        if lo <= len(syms) <= hi:
            return syms
    return []


@ttl_cache(ttl_seconds=86400)
def sp500() -> list[str]:
    tables = _read_tables(_SP500_URL)
    df = tables[0]
    col = "Symbol" if "Symbol" in df.columns else df.columns[0]
    return _clean_symbols(df[col].tolist())


@ttl_cache(ttl_seconds=86400)
def sp500_sectors() -> dict[str, str]:
    """Return {ticker: GICS Sector} from the S&P 500 Wikipedia table."""
    tables = _read_tables(_SP500_URL)
    df = tables[0]
    sym_col = "Symbol" if "Symbol" in df.columns else df.columns[0]
    sec_col = next((c for c in df.columns if "Sector" in str(c)), None)
    if sec_col is None:
        return {}
    return {_norm(r[sym_col]): str(r[sec_col]) for _, r in df.iterrows()}


@ttl_cache(ttl_seconds=86400)
def nasdaq100() -> list[str]:
    """Nasdaq-100 constituents from the published index list page."""
    return _list_page_symbols(_read_tables(_NDX_URL), 90, 110)


@ttl_cache(ttl_seconds=86400)
def djia() -> list[str]:
    """Dow 30 constituents from the published index list page."""
    return _list_page_symbols(_read_tables(_DJIA_URL), 20, 40)


def _sp500_sector_subset(symbols: list[str]) -> dict[str, str]:
    """GICS sectors for *symbols*, from the one live sector table left.

    The S&P 500 page still publishes a Sector column and the memberships
    overlap heavily (DJIA 30/30 and NDX 86/101 when this was written), so this
    keeps ONE sector vocabulary across universes. Names the map does not reach
    are simply absent — surfaces render them blank rather than guessed from a
    different classification (SIC/ICB titles are not GICS sectors).
    """
    sectors = sp500_sectors()
    return {s: sectors[s] for s in symbols if s in sectors}


@ttl_cache(ttl_seconds=86400)
def nasdaq100_sectors() -> dict[str, str]:
    return _sp500_sector_subset(nasdaq100())


@ttl_cache(ttl_seconds=86400)
def djia_sectors() -> dict[str, str]:
    return _sp500_sector_subset(djia())


UNIVERSES = {
    "S&P 500":     sp500,
    "Nasdaq 100":  nasdaq100,
    "Dow 30":      djia,
}

UNIVERSE_SECTORS = {
    "S&P 500":     sp500_sectors,
    "Nasdaq 100":  nasdaq100_sectors,
    "Dow 30":      djia_sectors,
}
