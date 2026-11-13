"""Theme aggregation endpoint.

PRIVATE — reads the global theses store (operator's recorded reasoning) and
positions. Gated by require_auth (fail-closed).
"""

from fastapi import APIRouter, Depends

from backend.auth import require_auth

router = APIRouter(prefix="/api/themes", tags=["themes"], dependencies=[Depends(require_auth)])


@router.get("")
def get_themes():
    """Theme exposure across the book: weight, concentration, P&L, correlation.

    The symbol→theme registry is the theses store — a position's theme is a
    field on its recorded thesis. Positions without a recorded theme roll
    into "No Thesis".
    """
    from backend.services.themes import theme_aggregation
    return theme_aggregation()
