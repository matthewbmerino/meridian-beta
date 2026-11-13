"""Per-symbol chart state endpoints (scale mode, overlays, drawings).

Two chart surfaces persist independently:
  * ``/{symbol}``        — the price chart (originally the only surface)
  * ``/{symbol}/stbr``   — the STBR bubble-risk chart

PRIVATE — chart state is GLOBAL (the operator's drawings and settings are
their own). Gated by require_auth (fail-closed).
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend.auth import require_auth

router = APIRouter(prefix="/api/chart-state", tags=["chart-state"], dependencies=[Depends(require_auth)])


class ChartStateBody(BaseModel):
    scale_mode: str = "linear"
    overlays: list[str] = []
    drawings: list = []
    # The analyst-target overlay. Pydantic DROPS unknown fields silently, so a field
    # added to the store but not here returns 200 and never persists.
    analyst: bool = False


@router.get("/{symbol}")
def get_state(symbol: str):
    from data.chart_state import get_chart_state
    state = get_chart_state(symbol, "price")
    if state is None:
        return {"state": None}
    return {"state": state}


@router.put("/{symbol}")
def put_state(symbol: str, body: ChartStateBody):
    from data.chart_state import ChartStateUnavailable, save_chart_state
    try:
        state = save_chart_state(symbol, body.model_dump(), "price")
    except ChartStateUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"state": state}


@router.get("/{symbol}/stbr")
def get_stbr_state(symbol: str):
    from data.chart_state import get_chart_state
    state = get_chart_state(symbol, "stbr")
    if state is None:
        return {"state": None}
    return {"state": state}


@router.put("/{symbol}/stbr")
def put_stbr_state(symbol: str, body: ChartStateBody):
    from data.chart_state import ChartStateUnavailable, save_chart_state
    try:
        state = save_chart_state(symbol, body.model_dump(), "stbr")
    except ChartStateUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"state": state}
