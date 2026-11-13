"""Compute exposure — the operator's book mapped against the Compute Index.

PRIVATE — reads the operator's theses + positions, so it is GLOBAL-store data
and gated by require_auth (fail-closed, matches the private-data-gate audit
check). The mapping is pure (analysis/compute_exposure.py); this module only
assembles inputs from the stores + the persisted GPU history.
"""

from fastapi import APIRouter, Depends

from backend.auth import require_auth

router = APIRouter(prefix="/api/compute", tags=["compute"], dependencies=[Depends(require_auth)])


@router.get("/exposure")
def compute_exposure_endpoint():
    """Compute-linked holdings + weights. NO index signal.

    Returns the operator's capital exposed to the price of compute as a weight
    table, and an explicit flag saying the index is retired.

    The drop-from-peak was REMOVED 2026-10-08 (operator directive: anything
    judged not ready must not be displayed). The index is retired as a gate —
    available-offer spot prices, one GPU carrying ~66% of the TFLOPS weight,
    ~2 weeks of history, 3-8 offers on the dominant part. Publishing a
    "−11.5% from peak" computed from that is publishing a number the operator
    already ruled out as a basis for capital, so the backend no longer computes
    or returns it. ``available: false`` with a reason when no holding is
    compute-linked.
    """
    from analysis.compute_exposure import compute_exposure
    from data.account import get_combined_positions, get_combined_summary
    from data.theses import get_theses

    theses = get_theses()
    compute_symbols = {
        sym.upper() for sym, t in theses.items() if t.get("compute_exposed")
    }

    positions_df = get_combined_positions()
    if positions_df is None or positions_df.empty:
        return {
            "available": False,
            "positions": [],
            "reason": "No positions imported yet",
            "drop": None,
            "linked_count": 0,
            "index_available": False,
            "index_status": "retired",
        }
    summary = get_combined_summary(positions_df)
    nav = float(summary.get("net_liquidation", 0) or 0)
    positions = positions_df.to_dict(orient="records")

    result = compute_exposure(positions, compute_symbols, [], nav)
    # The index is retired book-wide; say so rather than leaving the field absent.
    result["index_available"] = False
    result["index_status"] = "retired"
    result["drop"] = None
    return result
