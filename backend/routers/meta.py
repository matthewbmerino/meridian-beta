"""Meta endpoints: data health, methodology, version."""

import datetime
import logging
import subprocess
from pathlib import Path

import pandas as pd
from fastapi import APIRouter
from fastapi.responses import JSONResponse

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/meta", tags=["meta"])

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def _git_head() -> str:
    """Return the current git commit short hash, or 'unknown' if not a git repo."""
    try:
        out = subprocess.run(
            ["git", "-C", str(_PROJECT_ROOT), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        return out.stdout.strip() or "unknown"
    except Exception as e:
        log.warning("Git commit resolution failed: %s", e)
        return "unknown"


_STARTED_AT = datetime.datetime.now(datetime.timezone.utc).isoformat()
_COMMIT = _git_head()


@router.get("/version")
def version():
    """Commit + start time of the running backend. Used to detect stale servers."""
    return {"commit": _COMMIT, "started_at": _STARTED_AT}


@router.get("/metrics")
def metrics():
    """Operational metrics: request counts, latency percentiles, uptime."""
    from backend.services.metrics import snapshot
    return snapshot()


@router.get("/model-info")
def model_info():
    """Active LLM provider + models for the Compass/Research UI."""
    from agent import compute
    return {
        "provider": compute.current_provider(),
        "chat_model": compute.current_model(),
        "research_model": compute.research_model(),
    }


@router.get("/methodology")
def methodology_list():
    """Available methodology metric names (for discoverability)."""
    from analysis.methodology import list_metrics
    return {"metrics": list_metrics()}


@router.get("/methodology/{metric}")
def methodology(metric: str):
    """Formula-level documentation for a surfaced metric."""
    from analysis.methodology import get_methodology
    entry = get_methodology(metric)
    if entry is None:
        return JSONResponse({"error": f"unknown metric: {metric}"}, status_code=404)
    return entry


@router.get("/data-health")
def data_health():
    """Feed health: each check is (name, fn) where fn returns
    ``{"healthy": bool, "error": str|None, "last_success": str|None}``.
    A raised exception is reported as healthy=False with the error."""
    def _check_portfolio():
        from data.account import get_combined_positions, get_combined_summary
        positions = get_combined_positions()
        if positions is None or positions.empty:
            return {"healthy": False, "error": "No positions", "last_success": None}
        # A zeroed day bar (pre-market/early-session feed degradation) leaves
        # positions stale — the summary would be frozen at import prices. That
        # is a degraded feed, not a healthy one: report it so the UI greys.
        stale_count = int(positions.get("stale", pd.Series(False)).sum())
        if stale_count == len(positions):
            return {"healthy": False, "error": "All quotes stale (feed degraded)", "last_success": None}
        get_combined_summary(positions)
        return _ok()

    def _check_market():
        from data.market import get_price_history
        df = get_price_history("SPY", days=5)
        if df.empty:
            return {"healthy": False, "error": "Empty response", "last_success": None}
        return _ok()

    def _check_snapshots():
        from data.snapshots import load_portfolio_history
        snapshots = load_portfolio_history()
        if snapshots.empty:
            return {"healthy": False, "error": "No snapshots", "last_success": None}
        ts_val = snapshots["ts"].iloc[-1]
        return _ok(str(ts_val) if not hasattr(ts_val, "isoformat") else ts_val.isoformat())

    def _check_fred():
        from data.fred import market_environment
        macro = market_environment()
        if not (macro is not None and len(macro) > 0):
            return {"healthy": False, "error": "Empty environment", "last_success": None}
        return _ok()

    def _check_earnings():
        from data.earnings import get_calendar
        cal = get_calendar()
        if not cal:
            return {"healthy": False, "error": "Empty calendar", "last_success": None}
        return _ok()

    def _check_reference():
        from data.reference import get_ticker_details
        if not get_ticker_details("AAPL"):
            return {"healthy": False, "error": "Empty response", "last_success": None}
        return _ok()

    def _check_edgar():
        from data.edgar import get_insider_activity
        if get_insider_activity("AAPL", months=3) is None:
            return {"healthy": False, "error": "Empty response", "last_success": None}
        return _ok()

    def _check_universe():
        from data.universe import sp500
        if not sp500():
            return {"healthy": False, "error": "Empty universe", "last_success": None}
        return _ok()

    def _check_gpu():
        # Disabled via GPU_SNAPSHOT_ENABLED=0: Vast.ai quota exhausted by the
        # 15-min snapshot cadence (operator decision 2026-10-05 — feed not
        # ready). Report disabled rather than unhealthy so the sidebar counts
        # only feeds that are actually expected to serve.
        import os as _os
        if _os.getenv("GPU_SNAPSHOT_ENABLED", "1") == "0":
            return {
                "healthy": True,
                "disabled": True,
                "error": None,
                "last_success": None,
            }
        from data.gpu import get_gpu_metrics
        metrics = get_gpu_metrics()
        if not metrics:
            return {"healthy": False, "error": "Empty GPU metrics", "last_success": None}
        return _ok()

    def _ok(last_success: str | None = None) -> dict:
        return {
            "healthy": True,
            "error": None,
            "last_success": last_success
            or datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }

    checks = [
        ("Portfolio Data", _check_portfolio),
        ("Market Data Feed", _check_market),
        ("Snapshot Database", _check_snapshots),
        ("FRED Economic Data", _check_fred),
        ("Earnings Data", _check_earnings),
        ("Reference Data", _check_reference),
        ("EDGAR Insider Data", _check_edgar),
        ("Universe Tables", _check_universe),
        ("GPU Prices", _check_gpu),
    ]

    # The checks are independent network probes (EDGAR, index tables, FRED,
    # earnings, GPU); run them side by side. Sequentially the cold call measured
    # ~10s, which is a page-load stall for what is only a diagnostic strip.
    def _run_check(item):
        name, check_fn = item
        try:
            return {"feed": name, **check_fn()}
        except Exception as e:
            return {
                "feed": name,
                "healthy": False,
                "last_success": None,
                "error": str(e)[:200],
            }

    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=min(9, len(checks))) as pool:
        feeds = list(pool.map(_run_check, checks))

    return {"feeds": feeds}
