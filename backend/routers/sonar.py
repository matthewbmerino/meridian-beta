"""Sonar universe scan endpoint — reads from scheduler cache.

The scan is expensive (503 symbols × network fetch + signals ≈ 31s sequential),
so it runs in the background scheduler and the endpoint serves the cached
result. The cache is persisted to disk so a backend restart doesn't throw
away a warm scan — the operator never pays the cold-scan cost twice.
"""

import datetime
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import APIRouter, Query

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/sonar", tags=["sonar"])

_SCAN_CACHE_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "sonar_cache.json"

_scan_cache: dict[str, dict] = {}


def _looks_synthetic(results: list | None) -> bool:
    """True when a scan payload is fixture data rather than a real universe.

    The 2026-10-08 defect: the test suite wrote this cache (it was absent from
    the isolation list in tests/conftest.py) and PROD then served `AAA`/`BBB`
    with identical synthetic values to the Sonar page and the Opportunity Feed —
    a fake ticker was being ranked as a live setup. A cache that cannot tell
    fixture data from a real scan is a surface that will confidently lie, so the
    loader refuses the payload and the writer rejects it.
    """
    if not isinstance(results, list) or len(results) < 20:
        return True
    syms = [str(r.get("symbol", "")) for r in results if isinstance(r, dict)]
    if not syms:
        return True
    # Real S&P 500 tickers are 1-5 chars and never the classic test placeholders.
    if any(s in ("AAA", "BBB", "CCC", "DDD", "TEST", "") for s in syms):
        return True
    # Identical metric vectors across distinct symbols cannot happen with real data.
    sig = {(r.get("rsi"), r.get("sma_50"), r.get("current_price"),
            r.get("composite_score")) for r in results if isinstance(r, dict)}
    if len(sig) < max(2, len(syms) // 4):
        return True
    return False


def _load_cache():
    """Load the persisted scan cache at startup, refusing anything synthetic."""
    global _scan_cache
    try:
        if _SCAN_CACHE_PATH.exists():
            data = json.loads(_SCAN_CACHE_PATH.read_text())
            if isinstance(data, dict):
                clean = {}
                for universe, payload in data.items():
                    results = (payload or {}).get("results") if isinstance(payload, dict) else None
                    if _looks_synthetic(results):
                        log.error(
                            "Sonar cache REJECTED for %r: payload does not look like a real "
                            "universe scan (%s rows). Serving nothing beats serving fixtures.",
                            universe, len(results or []),
                        )
                        continue
                    clean[universe] = payload
                _scan_cache = clean
    except (json.JSONDecodeError, OSError) as e:
        log.warning("Sonar cache load failed: %s", e)


def _save_cache():
    """Persist the scan cache atomically (tmp + rename). Never persist fixtures."""
    try:
        clean = {k: v for k, v in _scan_cache.items()
                 if not _looks_synthetic((v or {}).get("results"))}
        if len(clean) != len(_scan_cache):
            log.error("Sonar cache write skipped for %d synthetic payload(s)",
                      len(_scan_cache) - len(clean))
        _SCAN_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _SCAN_CACHE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(clean, default=str))
        tmp.rename(_SCAN_CACHE_PATH)
    except OSError as e:
        log.warning("Sonar cache save failed: %s", e)


def update_scan_cache(universe: str, results: list, timestamp: str):
    _scan_cache[universe] = {"results": results, "timestamp": timestamp}
    _save_cache()


#: Sessions the scan needs. STBR wants 140 bars and SMA-200 wants 200, so a
#: window shorter than ~260 would silently null out half the signal set.
_SCAN_SESSIONS = 400


def _panel_frames():
    """(close, volume) session x symbol frames covering the scan window.

    One local parquet read + pivot, instead of 503 network calls. Returns
    ``(None, None)`` when the panel is unavailable — the caller reports an empty
    scan rather than falling back to hundreds of per-symbol fetches.
    """
    import datetime as dt

    import pandas as pd

    from data import market_panel

    try:
        index = market_panel.session_index()
        if len(index) == 0:
            return None, None
        end = index[-1].date()
        start = (index[-1] - pd.Timedelta(days=_SCAN_SESSIONS * 2)).date()
        if (end - start).days < 0:
            start = dt.date(2021, 10, 15)
        close, volume = market_panel.frames(start=start, end=end)
        if close is None or close.empty:
            return None, None
        return close.tail(_SCAN_SESSIONS), volume.reindex(close.index).fillna(0.0)
    except Exception as e:
        log.warning("Sonar panel frames unavailable: %s", e)
        return None, None


def _scan_one(symbol: str, close_frame, volume_frame) -> dict | None:
    """Score one symbol from the panel frames. Returns None on any failure."""
    import pandas as pd

    from analysis.signals import generate_signals
    from analysis.stbr import calculate_stbr

    try:
        col = symbol
        if col not in close_frame.columns:
            # The panel keys class shares with a dot (BRK.B); the universe list
            # spells them with a dash. Try the other spelling once.
            alt = symbol.replace("-", ".")
            if alt in close_frame.columns:
                col = alt
            else:
                return None

        close = close_frame[col].dropna()
        if len(close) < 20:
            return None
        vol = volume_frame[col].reindex(close.index).fillna(0.0)
        # Signals read close and volume only; OHLC is not invented for them,
        # it is simply not used downstream.
        df = pd.DataFrame({"close": close, "volume": vol})

        sig = generate_signals(symbol, df)
        stbr_df = calculate_stbr(close)
        stbr_val = float(stbr_df["stbr"].iloc[-1]) if not stbr_df.empty else None
        stbr_label = stbr_df["risk_label"].iloc[-1] if not stbr_df.empty else None
        return {**sig, "stbr": stbr_val, "stbr_label": stbr_label}
    except Exception as e:
        log.debug("Sonar scan failed for %s: %s", symbol, e)
        return None


def _run_scan(symbols: list[str]) -> list[dict]:
    """Scan a universe from the local panel.

    CPU-bound now, so the threads buy little — but the work is pandas per symbol
    and the pool keeps a 503-name scan near a second. No network, so it can never
    starve the market-data rate limiter again.
    """
    close_frame, volume_frame = _panel_frames()
    if close_frame is None:
        log.warning("Sonar scan skipped: the market panel is unavailable")
        return []

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda s: _scan_one(s, close_frame, volume_frame), symbols))
    return [r for r in results if r is not None]


@router.get("/scan")
def sonar_scan(universe: str = Query("S&P 500")):
    cached = _scan_cache.get(universe)
    if cached is not None:
        return {**cached, "universe": universe, "cached": True}

    from data.universe import UNIVERSES
    loader = UNIVERSES.get(universe)
    if loader is None:
        return {"error": f"Unknown universe: {universe}", "available": list(UNIVERSES.keys())}

    symbols = loader()
    if not symbols or len(symbols) < 20:
        # An empty or truncated universe means the upstream list failed to load.
        # Scanning it would publish a "no setups" board that is really a data gap.
        return {
            "results": [], "universe": universe, "cached": False,
            "available": False,
            "error": f"Universe {universe!r} resolved to {len(symbols)} symbols — "
                     f"the upstream list is unavailable; not scanning.",
        }

    results = _run_scan(symbols)
    if _looks_synthetic(results):
        # Never cache a scan that cannot be a real universe.
        return {
            "results": [], "universe": universe, "cached": False, "available": False,
            "error": "Scan produced no valid rows — not cached.",
        }
    ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
    update_scan_cache(universe, results, ts)

    return {"results": results, "timestamp": ts, "universe": universe, "cached": False}


@router.get("/universes")
def list_universes():
    from data.universe import UNIVERSES
    return list(UNIVERSES.keys())
