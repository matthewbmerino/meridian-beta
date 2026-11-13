"""History — the multi-cycle read the quote feed cannot give.

The Massive (Polygon) plan caps daily aggregates at exactly five years, so the
market panel spans one cycle and every question with a two-cycle prerequisite
("is this 1999?") is unanswerable from it. This router reads the optional
deep-history store (``data/deep_history``, built by
``scripts/collect_deep_history.py`` from yfinance + FRED) and exposes two reads:

* ``/api/history/regime`` — today's state vector, the nearest DISTINCT historical
  episodes, what happened from them, and the unconditional base rate the
  analogues have to beat. The base rate is not decoration: on this data the
  analogue set frequently cannot beat it, and the surface says so.
* ``/api/history/breadth`` — how much of the index is being left behind while the
  headline sits at a record, placed against the same reading from every other
  month the index was at a record.

Both are read-only, cached for an hour (the inputs change once a day), and
degrade to ``available: False`` with a reason — never to an empty chart that
reads as "no history".
"""

import logging

from fastapi import APIRouter, Depends

from analysis import regime
from backend.auth import require_auth
from data.cache import ttl_cache

log = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/history", tags=["history"], dependencies=[Depends(require_auth)]
)

# Deep-history series this router needs. Names are the provider's own ids, as the
# collector writes them.
_BROAD = "^GSPC"
_GROWTH = "^IXIC"
_MACRO = ("DGS10", "CPIAUCSL", "BAA10Y", "T10Y2Y")
_CAPE = "SHILLER_CAPE"

# The breadth read walks the panel a year at a time and needs a full 252-session
# warm-up before its first usable month, so it reads the whole panel: two years
# left only ~13 usable months and too few at-record months for a percentile.
# Bounded by the panel's own five-year floor, and read per year so memory stays
# flat.
_BREADTH_SESSIONS_DAYS = 2200


def _load_state() -> dict:
    """Assemble the inputs the study needs, naming anything that is missing."""
    from data import deep_history

    if not deep_history.available():
        return {"available": False, "note": deep_history.status()["note"], "inputs": {}}
    broad = deep_history.prices(_BROAD)
    if broad.empty:
        return {
            "available": False,
            "note": f"{_BROAD} is missing from the deep-history store.",
            "inputs": {},
        }
    growth = deep_history.prices(_GROWTH)
    macro = {sid: deep_history.macro(sid) for sid in _MACRO}
    cape = deep_history.macro(_CAPE)
    missing = [sid for sid, s in macro.items() if s.empty]
    if cape.empty:
        missing.append(_CAPE)
    return {
        "available": True,
        # Named for the STUDY's arguments, not for the provider's series ids:
        # the caller below passes them positionally, and a raw **macro splat
        # would hand `dgs10=` a missing key instead of an empty Series.
        "inputs": {
            "broad": broad,
            "growth": growth,
            "cape": cape,
            "dgs10": macro["DGS10"],
            "cpi": macro["CPIAUCSL"],
            "baa": macro["BAA10Y"],
            "curve": macro["T10Y2Y"],
        },
        "missing_inputs": missing,
    }


@ttl_cache(ttl_seconds=3600)
def _regime_payload() -> dict:
    from data import deep_history

    loaded = _load_state()
    if not loaded["available"]:
        return {"available": False, "note": loaded["note"], "store": deep_history.status()}
    ins = loaded["inputs"]
    try:
        study = regime.study(
            ins["broad"],
            growth=ins["growth"] if not ins["growth"].empty else None,
            dgs10=ins["dgs10"] if not ins["dgs10"].empty else None,
            cpi=ins["cpi"] if not ins["cpi"].empty else None,
            baa=ins["baa"] if not ins["baa"].empty else None,
            curve=ins["curve"] if not ins["curve"].empty else None,
            cape=ins["cape"] if not ins["cape"].empty else None,
        )
    except Exception as e:  # a study that cannot run must say so, not 500
        log.warning("Regime study failed: %s", e)
        return {"available": False, "note": f"Regime study failed: {type(e).__name__}: {e}"}
    study["store"] = deep_history.status()
    study["missing_inputs"] = loaded.get("missing_inputs", [])
    return study


@router.get("/regime")
def get_regime() -> dict:
    """Today's state, its nearest distinct historical analogues, and the base rate."""
    return _cached_get_regime()


def _get_regime_compute():
    return _regime_payload()


# TTL-cached: regime read (daily data).
_cached_get_regime = ttl_cache(ttl_seconds=600)(_get_regime_compute)


def _spx_close_frame() -> tuple:
    """(close frame, index series) for the S&P 500 universe, or (None, None)."""
    import datetime

    import pandas as pd

    from data import market_panel
    from data.universe import UNIVERSES

    try:
        symbols = list(UNIVERSES["S&P 500"]())
    except Exception as e:
        log.warning("Universe load failed for breadth: %s", e)
        return None, None
    if len(symbols) < 200:
        return None, None
    wanted = set(symbols) | {"SPY"}

    start = datetime.date.today() - datetime.timedelta(days=_BREADTH_SESSIONS_DAYS)
    yearly = []
    year = start.year
    while year <= datetime.date.today().year:
        chunk = market_panel.load_panel(
            start=max(start, datetime.date(year, 1, 1)),
            end=min(datetime.date.today(), datetime.date(year, 12, 31)),
            columns=["date", "symbol", "close"],
        )
        if not chunk.empty:
            yearly.append(chunk[chunk["symbol"].isin(wanted)])
        year += 1
    if not yearly:
        return None, None
    panel = pd.concat(yearly, ignore_index=True)
    close = panel.pivot(index="date", columns="symbol", values="close").astype("float32")
    index = close["SPY"].dropna() if "SPY" in close.columns else None
    return close.drop(columns=["SPY"], errors="ignore"), index


@ttl_cache(ttl_seconds=3600)
def _breadth_payload() -> dict:
    close, index = _spx_close_frame()
    if close is None:
        return {
            "available": False,
            "note": "The market panel or the S&P 500 constituent list is unavailable.",
        }
    return regime.breadth_divergence(close, index=index)


@router.get("/breadth")
def get_breadth() -> dict:
    """Share of the index in its own drawdown while the headline sits at a record."""
    return _cached_get_breadth()


def _get_breadth_compute():
    return _breadth_payload()


# TTL-cached: market breadth history (daily data).
_cached_get_breadth = ttl_cache(ttl_seconds=600)(_get_breadth_compute)


@router.get("/status")
def get_status() -> dict:
    """What the deep-history store holds, and when it was built."""
    from data import deep_history

    return deep_history.status()
