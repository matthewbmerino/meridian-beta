"""Deep history — the multi-decade price/macro store behind the regime study.

**Why this store exists.** The Massive (Polygon) quote feed caps daily
aggregates at exactly five years (verified 2026-10-15: SPY returns 1254 daily
rows from 2021-10-15 however early the request starts; the index endpoints are
NOT_AUTHORIZED on this plan). Five years is one cycle, so the market panel can
answer "what has happened since 2021" and cannot answer "what happened after a
1999-style state". A question about SECULAR market structure therefore needs a
second, longer feed, and this module reads its store.

**What it is.** ``data/deep_history/{prices,macro}/<name>.parquet``, built by
``scripts/collect_deep_history.py`` from two providers: yfinance (equities and
indices, back to 1927 for the S&P 500) and FRED (macro, back to 1945 for the
Buffett-indicator denominator). One file per series, plus a ``manifest.json``
recording row counts, first/last observation and the build timestamp.

**The store is OPTIONAL and every reader degrades to empty.** A missing store is
not an error — it means the collector has not run — and callers must render
"run the collector" rather than an empty chart that reads as "no history".

Reads are cheap: each series is one small parquet under 500 KB, so a caller may
ask for a dozen symbols per request without a cache. Nothing here fetches.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pandas as pd

from data.session import data_root

log = logging.getLogger(__name__)

_DIRNAME = "deep_history"
_PRICES = "prices"
_MACRO = "macro"
_MANIFEST = "manifest.json"


def store_dir() -> Path:
    """The deep-history store directory (not created — it is optional data)."""
    return data_root() / _DIRNAME


def store_path() -> Path:
    """Public wrapper for the store path.

    ``data/AGENTS.md`` forbids importing ``_file``-style privates across layers,
    so a router that wants to report a missing-store condition takes the path
    from here.
    """
    return store_dir()


def _filename(name: str) -> str:
    """Map a ticker/series id to its file name.

    ``^GSPC`` and ``BTC-USD`` are not legal file names in a shared store (the
    caret sorts oddly and the dash is easy to mistype), so both become ``_``.
    The mapping is total and reversible-looking but NOT reversed anywhere: the
    manifest is the only place a name is resolved back, and callers pass the
    provider's own symbol.
    """
    return name.replace("^", "_").replace("-", "_") + ".parquet"


def _read(kind: str, name: str) -> pd.Series:
    path = store_dir() / kind / _filename(name)
    if not path.exists():
        return pd.Series(dtype=float)
    try:
        df = pd.read_parquet(path)
    except Exception as e:  # a corrupt series must not take the page down
        log.warning("Deep history read failed for %s/%s: %s", kind, name, e)
        return pd.Series(dtype=float)
    if df.empty or "date" not in df.columns or "close" not in df.columns:
        return pd.Series(dtype=float)
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"])
    try:
        df["date"] = df["date"].dt.tz_localize(None)
    except TypeError:
        pass
    return df.dropna(subset=["close"]).set_index("date")["close"].astype(float).sort_index()


def prices(symbol: str) -> pd.Series:
    """Daily close for ``symbol``, ascending, or an EMPTY Series when absent."""
    return _read(_PRICES, symbol)


def price_frame(symbols: list[str]) -> pd.DataFrame:
    """Sessions x symbols close frame. Missing symbols are OMITTED columns.

    An inner join would silently shrink to the shortest series; callers that
    need alignment should reindex explicitly.
    """
    cols = {}
    for s in symbols:
        ser = prices(s)
        if len(ser):
            cols[s] = ser
    return pd.DataFrame(cols).sort_index() if cols else pd.DataFrame()


def macro(series_id: str) -> pd.Series:
    """A macro series (FRED id, or ``SHILLER_CAPE``), ascending, or EMPTY."""
    path = store_dir() / _MACRO / f"{series_id}.parquet"
    if not path.exists():
        return pd.Series(dtype=float)
    try:
        df = pd.read_parquet(path)
    except Exception as e:
        log.warning("Deep history macro read failed for %s: %s", series_id, e)
        return pd.Series(dtype=float)
    if df.empty or "date" not in df.columns or "value" not in df.columns:
        return pd.Series(dtype=float)
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"])
    return df.dropna(subset=["value"]).set_index("date")["value"].astype(float).sort_index()


def manifest() -> dict:
    """The collector's own record of what it wrote, or ``{}`` if absent."""
    p = store_dir() / _MANIFEST
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError) as e:
        log.warning("Deep history manifest unreadable: %s", e)
        return {}


def available() -> bool:
    """True when at least one price series is on disk."""
    d = store_dir() / _PRICES
    return d.is_dir() and any(d.glob("*.parquet"))


def status() -> dict:
    """Describe the store for the surface — never raises."""
    man = manifest()
    built = man.get("generated_at")
    if not available():
        return {
            "available": False,
            "built_at": built,
            "n_prices": 0,
            "n_macro": 0,
            "price_symbols": [],
            "macro_series": [],
            "note": (
                "No deep-history store yet. The quote feed (Massive) caps daily "
                "aggregates at 5 years, so the multi-cycle study needs "
                "scripts/collect_deep_history.py to have run."
            ),
        }
    pr = man.get("prices") or []
    mc = man.get("macro") or []
    return {
        "available": True,
        "built_at": built,
        "n_prices": len(pr),
        "n_macro": len(mc),
        "price_symbols": [r.get("symbol") for r in pr],
        "macro_series": [r.get("series") for r in mc],
        "earliest": min((r.get("first") for r in pr + mc if r.get("first")), default=None),
        "latest": max((r.get("last") for r in pr + mc if r.get("last")), default=None),
        "note": None,
    }
