"""Theme aggregation orchestration — one implementation for every surface.

Lives in ``backend/services/`` (not ``analysis/``) because it fetches data —
it is orchestration, not pure computation (same boundary as ``sizing_book.py``
and ``theses.py``). Loads the recorded theses (the symbol→theme registry),
loads current positions, and composes the pure aggregation from
``analysis/themes.py`` into a single response the router serves.
"""

from __future__ import annotations

import logging

import pandas as pd

log = logging.getLogger(__name__)


def _symbol_to_theme() -> dict[str, str | None]:
    """Build the symbol→theme map from the theses store (the registry)."""
    try:
        from data.theses import get_theses
    except Exception as e:
        log.warning("Theses load failed (theme aggregation): %s", e)
        return {}
    theses = get_theses()
    return {sym: t.get("theme") for sym, t in theses.items()}


def _positions():
    try:
        from data.account import get_combined_positions
        return get_combined_positions()
    except Exception as e:
        log.warning("Positions load failed (theme aggregation): %s", e)
        return pd.DataFrame()


def _returns(symbols: list[str]) -> dict[str, pd.Series]:
    """Daily returns per symbol for intra-theme correlation."""
    from data.market import get_price_history

    out: dict[str, pd.Series] = {}
    for sym in symbols:
        try:
            df = get_price_history(sym)
            if df.empty:
                continue
            out[sym] = df["close"].pct_change().dropna()
        except Exception as e:
            log.debug("Price history fetch failed for %s (themes): %s", sym, e)
            continue
    return out


def theme_aggregation() -> dict:
    """Compose the full theme aggregation response.

    Returns {available, themes[], concentration, pnl[], intra_theme_corr[]}.
    Degrades gracefully: no positions or no theses → available with empty
    lists, never fabricated.
    """
    from analysis.themes import (
        intra_theme_correlation,
        theme_allocation,
        theme_concentration,
        theme_pnl,
    )

    tmap = _symbol_to_theme()
    positions = _positions()

    if positions is None or positions.empty:
        return {
            "available": True,
            "themes": [],
            "concentration": 0.0,
            "pnl": [],
            "intra_theme_corr": [],
        }

    symbols = [s for s in positions["symbol"].unique() if s]
    allocation = theme_allocation(positions, tmap)
    concentration = theme_concentration(allocation)
    pnl = theme_pnl(positions, tmap)
    corr = intra_theme_correlation(_returns(symbols), tmap)

    return {
        "available": True,
        "themes": allocation,
        "concentration": round(concentration, 1),
        "pnl": pnl,
        "intra_theme_corr": corr,
    }
