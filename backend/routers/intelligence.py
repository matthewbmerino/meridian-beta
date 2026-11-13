"""Intelligence endpoints: sector rotation, correlation network, lead/lag, value chain."""

import logging

import pandas as pd
from fastapi import APIRouter, Query
from pydantic import BaseModel

from data.cache import ttl_cache

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/intelligence", tags=["intelligence"])


class NetworkBody(BaseModel):
    symbols: list[str]
    min_corr: float = 0.4
    window: int = 63


class LeadLagBody(BaseModel):
    symbols: list[str]
    min_corr: float = 0.3
    max_lag: int = 10


class PairsBody(BaseModel):
    symbols: list[str]
    p_threshold: float = 0.05


# ── Sector Intelligence ──────────────────────────────────────────────


def _sample_sector_symbols(sector_map: dict[str, str], max_per_sector: int = 10) -> list[str]:
    """Deterministic per-sector sample (max N per sector) for latency control."""
    sectors: dict[str, list[str]] = {}
    for sym, sec in sector_map.items():
        sectors.setdefault(sec, []).append(sym)
    sampled: list[str] = []
    for sec, syms in sectors.items():
        sampled.extend(sorted(syms)[:max_per_sector])
    return sampled


def _fetch_sector_closes(symbols: list[str], days: int = 90) -> dict[str, pd.Series]:
    """Fetch close-price series for a symbol set in parallel.

    Network-bound, so a ThreadPoolExecutor is safe and effective — 110 symbols
    at 8 workers ≈ 1.5s cold vs 5.7s sequential. The result is TTL-cached keyed
    on the explicit symbol set, so the three sector endpoints (which sample the
    same universe) share one fetch instead of three.
    """
    from concurrent.futures import ThreadPoolExecutor

    from data.market import get_price_history

    def _one(sym: str) -> tuple[str, pd.Series | None]:
        try:
            df = get_price_history(sym, days=days)
            if df.empty or len(df) <= 20:
                return sym, None
            return sym, df["close"]
        except Exception as e:
            # Per-symbol feed failures are expected at this scale (delisted,
            # sparse) — drop the symbol, but log so it's distinguishable from
            # a full feed outage.
            log.debug("Sector close fetch failed for %s: %s", sym, e)
            return sym, None

    with ThreadPoolExecutor(max_workers=8) as pool:
        pairs = list(pool.map(_one, symbols))
    return {sym: closes for sym, closes in pairs if closes is not None}


def _cached_sector_closes(symbols: list[str], days: int = 90) -> dict[str, pd.Series]:
    """TTL-cached close prices keyed on the explicit symbol set.

    Deterministic given (symbols, days), so safe to cache across sessions —
    the same pattern as the frontier/correlation caches in analysis.py.
    """
    @ttl_cache(ttl_seconds=300)
    def _compute(symbols_key: tuple[str, ...], d: int):
        return _fetch_sector_closes(list(symbols_key), d)

    return _compute(tuple(sorted(symbols)), days)


@router.get("/sectors/rotation")
def sector_rotation(universe: str = Query("S&P 500")):
    """Sector rotation signals: which sectors are accelerating, decelerating, leading, lagging."""
    from analysis.sectors import market_regime_breadth, sector_rotation_signals
    from data.universe import UNIVERSE_SECTORS, UNIVERSES

    if universe not in UNIVERSES:
        return {"error": f"Unknown universe: {universe}"}

    symbols = UNIVERSES[universe]()
    sector_map = UNIVERSE_SECTORS[universe]()

    if not symbols or not sector_map:
        return {"rotation": [], "regime": {}}

    sampled_symbols = _sample_sector_symbols(sector_map)
    closes = _cached_sector_closes(sampled_symbols)
    returns_dict = {sym: closes.pct_change().dropna() for sym, closes in closes.items()}

    if len(returns_dict) < 10:
        return {"rotation": [], "regime": {}}

    rotation = sector_rotation_signals(returns_dict, sector_map)
    regime = market_regime_breadth(returns_dict, sector_map)

    return {"rotation": rotation, "regime": regime}


@router.get("/sectors/dispersion")
def sector_dispersion_endpoint(universe: str = Query("S&P 500")):
    """Sector dispersion: where stock-picking matters most."""
    from analysis.sectors import sector_dispersion
    from data.universe import UNIVERSE_SECTORS

    sector_map = UNIVERSE_SECTORS.get(universe, lambda: {})()
    sampled_symbols = _sample_sector_symbols(sector_map)
    closes = _cached_sector_closes(sampled_symbols)
    returns_dict = {sym: closes.pct_change().dropna() for sym, closes in closes.items()}

    result = sector_dispersion(returns_dict, sector_map)
    return {"dispersion": result}


@router.get("/sectors/breadth")
def sector_breadth_endpoint(universe: str = Query("S&P 500")):
    """Sector breadth: % of stocks above 20-day SMA per sector."""
    from analysis.sectors import sector_breadth
    from data.universe import UNIVERSE_SECTORS

    sector_map = UNIVERSE_SECTORS.get(universe, lambda: {})()
    sampled_symbols = _sample_sector_symbols(sector_map)
    closes = _cached_sector_closes(sampled_symbols)

    result = sector_breadth(closes, sector_map)
    return {"breadth": result}


# ── Relationship Intelligence ─────────────────────────────────────────

@router.post("/network")
def correlation_network_endpoint(body: NetworkBody):
    """Correlation network for a set of symbols."""
    from analysis.relationships import correlation_network
    from analysis.risk import daily_returns
    from data.market import get_price_history

    returns_dict: dict = {}
    for sym in body.symbols:
        df = get_price_history(sym.upper(), days=252)
        if not df.empty:
            returns_dict[sym.upper()] = daily_returns(df)

    if len(returns_dict) < 3:
        return {"nodes": [], "edges": [], "clusters": {}}

    result = correlation_network(returns_dict, min_corr=body.min_corr, window=body.window)
    return result


@router.post("/lead-lag")
def lead_lag_endpoint(body: LeadLagBody):
    """Find lead/lag relationships between tickers."""
    from analysis.relationships import find_lead_lag_pairs
    from analysis.risk import daily_returns
    from data.market import get_price_history

    returns_dict: dict = {}
    for sym in body.symbols:
        df = get_price_history(sym.upper(), days=252)
        if not df.empty:
            returns_dict[sym.upper()] = daily_returns(df)

    if len(returns_dict) < 2:
        return {"pairs": []}

    pairs = find_lead_lag_pairs(returns_dict, min_corr=body.min_corr, max_lag=body.max_lag)
    return {"pairs": pairs}


@router.post("/pairs")
def cointegration_pairs_endpoint(body: PairsBody):
    """Find cointegrated (mean-reverting) pairs for pairs trading."""
    from analysis.relationships import cointegration_pairs
    from data.market import get_price_history

    price_dict: dict = {}
    for sym in body.symbols:
        df = get_price_history(sym.upper(), days=365)
        if not df.empty:
            price_dict[sym.upper()] = df["close"]

    if len(price_dict) < 2:
        return {"pairs": []}

    pairs = cointegration_pairs(price_dict, p_threshold=body.p_threshold)
    return {"pairs": pairs}


@router.get("/portfolio/relationships")
def portfolio_relationships():
    """Analyze relationships within the current portfolio holdings."""
    return _cached_portfolio_relationships()


def _portfolio_relationships_compute():
    from analysis.relationships import (
        correlation_network,
        find_lead_lag_pairs,
        relative_strength_divergences,
    )
    from analysis.risk import daily_returns
    from data.account import get_combined_positions
    from data.market import get_price_history

    positions = get_combined_positions()
    if positions.empty:
        return {"network": {"nodes": [], "edges": [], "clusters": {}}, "lead_lag": [], "divergences": []}

    equities = positions[positions["asset_type"] != "OPTION"]
    symbols = equities["symbol"].tolist()

    returns_dict: dict = {}
    for sym in symbols:
        df = get_price_history(sym, days=252)
        if not df.empty:
            returns_dict[sym] = daily_returns(df)

    if len(returns_dict) < 2:
        return {"network": {"nodes": [], "edges": [], "clusters": {}}, "lead_lag": [], "divergences": []}

    network = correlation_network(returns_dict, min_corr=0.3)
    lead_lag = find_lead_lag_pairs(returns_dict, min_corr=0.3, max_lag=10)

    # RS divergences vs SPY
    spy_df = get_price_history("SPY", days=252)
    divergences = []
    if not spy_df.empty:
        bench = daily_returns(spy_df)
        divergences = relative_strength_divergences(returns_dict, bench)

    return {
        "network": network,
        "lead_lag": lead_lag[:20],
        "divergences": divergences,
    }


# TTL-cached: correlation graph over the book.
_cached_portfolio_relationships = ttl_cache(ttl_seconds=600)(_portfolio_relationships_compute)


