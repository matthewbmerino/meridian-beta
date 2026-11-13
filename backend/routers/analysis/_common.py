"""Shared helpers for the analysis router package.

The analysis endpoints were split from a single 919-line router into three
domain modules (risk, sizing, composites). This module holds the helpers they
share: NaN-safe serialization, cached heavy computations, and the Pydantic
request bodies.
"""

import logging
import math

from fastapi import APIRouter
from pydantic import BaseModel

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/analysis", tags=["analysis"])


def _safe_float(v, default=0):
    try:
        f = float(v)
        return default if math.isnan(f) or math.isinf(f) else f
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Cached heavy computations.
#
# The feed fetches are already TTL-cached in data/massive.py; the remaining
# per-request cost is the *math* (frontier cloud = 4000 portfolios, stress =
# 5000 sims). These helpers cache only deterministic, session-independent
# computations keyed on their explicit inputs — never anything derived from
# session-scoped positions (stress stays uncached for that reason).
# ---------------------------------------------------------------------------


def _cached_frontier_cloud(symbols: tuple[str, ...], n_portfolios: int):
    """Efficient-frontier cloud for an explicit symbol set. Deterministic
    given (symbols, n_portfolios), so safe to cache across sessions."""
    from data.cache import ttl_cache

    @ttl_cache(ttl_seconds=300)
    def _compute(symbols_key: tuple[str, ...], n: int):
        from analysis.risk import daily_returns, efficient_frontier_cloud
        from data.market import get_price_history

        returns_dict = {}
        for sym in symbols_key:
            df = get_price_history(sym)
            if not df.empty:
                returns_dict[sym] = daily_returns(df)
        if len(returns_dict) < 2:
            return None
        return efficient_frontier_cloud(returns_dict, n_portfolios=n)

    return _compute(symbols, n_portfolios)


def _cached_correlation_matrix(symbols: tuple[str, ...]):
    """Correlation matrix for an explicit symbol set. Deterministic given
    symbols, so safe to cache across sessions."""
    from data.cache import ttl_cache

    @ttl_cache(ttl_seconds=300)
    def _compute(symbols_key: tuple[str, ...]):
        from analysis.risk import correlation_matrix, daily_returns
        from data.market import get_price_history

        returns_dict = {}
        for sym in symbols_key:
            df = get_price_history(sym)
            if not df.empty:
                returns_dict[sym] = daily_returns(df)
        return correlation_matrix(returns_dict)

    return _compute(symbols)


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------


class CorrelationBody(BaseModel):
    symbols: list[str]
    weights: list[float] | None = None


class VaRBody(BaseModel):
    symbols: list[str]
    weights: list[float]
    confidence: float = 0.95


class FrontierBody(BaseModel):
    symbols: list[str]
    n_portfolios: int = 4000


class SizingBody(BaseModel):
    symbols: list[str] | None = None
    candidates: list[str] | None = None  # non-held names to size prospectively


class SizingPreviewBody(BaseModel):
    symbol: str
    side: str = "BUY"                # BUY | SELL
    quantity: float | None = None    # shares (ignored if dollars set)
    dollars: float | None = None     # notional; wins over quantity
    price: float | None = None       # optional execution-price override


class BacktestBody(BaseModel):
    symbols: list[str] | None = None
    rebalance_days: int = 21
    start: str | None = None
    end: str | None = None


class StressBody(BaseModel):
    symbols: list[str] | None = None
    simulations: int = 5000
    horizon: int = 5
