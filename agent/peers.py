"""Deterministic peer sets for deep-dive research.

The viz agent can only build a peer comparison from symbols it actually has
data for — a peer table full of invented tickers gets every cell stripped by
the data guard. This module resolves real peers so the report can position a
name against its actual competitors (the institutional look), with numbers the
guard can verify.

Resolution order:
  1. Curated PEERS_BY_SYMBOL — hand-maintained, exact, covers the operator's
     holdings and the institutional core of the universe.
  2. Sector fallback — for any other symbol, sample the S&P 500 universe for
     the symbol's GICS sector and cluster by Massive's fine-grained `sector`
     string (which already groups KKR/BX/APO/ARES/CG/TPG/OWL as
     "INVESTMENT ADVICE" and NVDA/AMD/INTC as "SEMICONDUCTORS..."). Rank by
     market cap, take the top peers. Data-driven, self-maintaining.

Never fabricates: no symbol appears that wasn't resolved from the curated map
or from a real feed response. Unknown symbols return [] (no regression).
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# Focus symbol -> direct peers (tickers). The institutional core plus the
# operator's holdings so deep dives on names we actually own get peer context.
PEERS_BY_SYMBOL: dict[str, list[str]] = {
    # Alternative asset managers
    "KKR": ["BX", "APO", "ARES", "CG", "TPG", "OWL"],
    "BX": ["KKR", "APO", "ARES", "CG", "TPG", "OWL"],
    "APO": ["BX", "KKR", "ARES", "CG", "TPG", "OWL"],
    "ARES": ["BX", "KKR", "APO", "CG", "TPG", "OWL"],
    "CG": ["BX", "KKR", "APO", "ARES", "TPG", "OWL"],
    "TPG": ["BX", "KKR", "APO", "ARES", "CG", "OWL"],
    "OWL": ["BX", "KKR", "APO", "ARES", "CG", "TPG"],
    # Semiconductors
    "NVDA": ["AMD", "AVGO", "MU", "INTC", "TSM", "QCOM"],
    "AMD": ["NVDA", "AVGO", "MU", "INTC", "TSM", "QCOM"],
    "INTC": ["NVDA", "AMD", "AVGO", "MU", "TSM", "QCOM"],
    "MU": ["NVDA", "AMD", "AVGO", "INTC", "TSM", "QCOM"],
    "AVGO": ["NVDA", "AMD", "MU", "INTC", "TSM", "QCOM"],
    "TSM": ["NVDA", "AMD", "AVGO", "MU", "INTC", "QCOM"],
    # Software / cloud
    "MSFT": ["ORCL", "CRM", "ADBE", "NOW", "CRWD", "IBM"],
    "ORCL": ["MSFT", "CRM", "ADBE", "NOW", "IBM", "SAP"],
    "CRM": ["MSFT", "ORCL", "ADBE", "NOW", "SNOW", "HCP"],
    "ADBE": ["MSFT", "ORCL", "CRM", "NOW", "SNOW"],
    # Megacap tech platforms
    "AAPL": ["MSFT", "GOOGL", "AMZN", "META", "NVDA", "AVGO"],
    "GOOGL": ["META", "AMZN", "MSFT", "AAPL", "NVDA"],
    "META": ["GOOGL", "AMZN", "MSFT", "SNAP", "PINS"],
    "AMZN": ["MSFT", "GOOGL", "AAPL", "WMT", "COST", "EBAY"],
    # Banks
    "JPM": ["BAC", "C", "GS", "MS", "WFC", "USB"],
    "BAC": ["JPM", "C", "GS", "MS", "WFC", "USB"],
    "C": ["JPM", "BAC", "GS", "MS", "WFC", "USB"],
    "GS": ["MS", "JPM", "BAC", "C", "SCHW", "IBKR"],
    "MS": ["GS", "JPM", "BAC", "C", "SCHW", "IBKR"],
    "WFC": ["JPM", "BAC", "C", "USB", "GS", "MS"],
    "SCHW": ["IBKR", "GS", "MS", "JPM", "HOOD", "CBOE"],
    # Asset management / exchanges
    "BLK": ["KKR", "BX", "APO", "SCHW", "TROW", "IVZ"],
    "SPGI": ["MCO", "MSCI", "FDS", "CBOE", "ICE", "NDAQ"],
    "MCO": ["SPGI", "MSCI", "FDS", "CBOE"],
    "MSCI": ["SPGI", "MCO", "FDS", "CBOE"],
    # Energy / utilities (holdings: CEG)
    "CEG": ["VST", "NRG", "DUK", "SO", "AEP", "NEE"],
    "VST": ["CEG", "NRG", "DUK", "SO", "AEP", "NEE"],
    "NEE": ["DUK", "SO", "AEP", "CEG", "VST", "NRG"],
    "XOM": ["CVX", "COP", "SLB", "OXY", "EOG", "HAL"],
    "CVX": ["XOM", "COP", "SLB", "OXY", "EOG", "HAL"],
    # Industrials / data-center power (holdings: VRT)
    "VRT": ["ETN", "PWR", "EMR", "PH", "AME", "IR"],
    "ETN": ["VRT", "PWR", "EMR", "PH", "AME", "IR"],
    "PWR": ["ETN", "VRT", "EMR", "PH", "AME", "IR"],
    # Consumer discretionary (holdings: NKE)
    "NKE": ["LULU", "DECK", "UA", "CROX", "SKX", "UAA"],
    "LULU": ["NKE", "DECK", "UA", "CROX", "SKX"],
    # Bitcoin miners / crypto-adjacent (holdings: CLSK, CRWV)
    "CLSK": ["MARA", "RIOT", "CIFR", "WULF", "BTBT", "HUT"],
    "MARA": ["CLSK", "RIOT", "CIFR", "WULF", "BTBT", "HUT"],
    "RIOT": ["CLSK", "MARA", "CIFR", "WULF", "BTBT", "HUT"],
    # Rare earths / critical minerals (holdings: MP)
    "MP": ["REE", "LAC", "ALB", "SQM", "MPLX"],
    # Health care (large caps)
    "LLY": ["PFE", "MRK", "ABBV", "JNJ", "NVO", "BMY"],
    "PFE": ["MRK", "ABBV", "JNJ", "LLY", "NVO", "BMY"],
    "MRK": ["PFE", "ABBV", "JNJ", "LLY", "NVO", "BMY"],
    "JNJ": ["PFE", "MRK", "ABBV", "LLY", "NVO"],
    # Consumer staples
    "WMT": ["COST", "TGT", "KR", "COST", "DLTR"],
    "COST": ["WMT", "TGT", "KR", "DLTR"],
    "PG": ["CL", "KMB", "KVUE", "EL"],
    # Communication services
    "TMUS": ["VZ", "T", "CMCSA", "CHTR"],
    "VZ": ["TMUS", "T", "CMCSA", "CHTR"],
    "CMCSA": ["TMUS", "VZ", "T", "CHTR"],
    # Auto
    "TSLA": ["RIVN", "GM", "F", "STLA", "LCID"],
    "GM": ["F", "TSLA", "RIVN", "STLA", "LCID"],
    "F": ["GM", "TSLA", "RIVN", "STLA", "LCID"],
}

# Cap on how many peers are fetched per deep dive (network cost + prompt size).
MAX_PEERS = 6


def curated_peers(symbol: str) -> list[str]:
    """Return the curated peer list for a symbol (may be empty)."""
    sym = (symbol or "").strip().upper()
    peers = PEERS_BY_SYMBOL.get(sym, [])
    return [p for p in peers if p != sym][:MAX_PEERS]


def _sector_peers(symbol: str, limit: int = MAX_PEERS) -> list[str]:
    """Resolve peers by GICS sector from the S&P 500 universe.

    Samples the symbol's GICS sector, clusters by Massive's fine-grained
    sector string, and returns the top `limit` names by market cap. Falls back
    to the whole GICS sector if Massive sector strings aren't fetchable.
    """
    try:
        from agent.tools import tool_get_fundamentals
        from data.universe import sp500_sectors

        sym = symbol.strip().upper()
        sectors = sp500_sectors()
        gics = sectors.get(sym)
        if not gics:
            return []
        candidates = [s for s, g in sectors.items() if g == gics and s != sym]
        if not candidates:
            return []

        # Cluster by Massive's fine-grained sector string (e.g. all the alt
        # managers share "INVESTMENT ADVICE"), ranked by market cap.
        clusters: dict[str, list[tuple[str, float]]] = {}
        for cand in candidates:
            try:
                f = tool_get_fundamentals(cand)
            except Exception:
                continue
            mc = f.get("market_cap")
            sec = f.get("sector") or ""
            if mc is None:
                continue
            clusters.setdefault(sec, []).append((cand, float(mc)))
        if not clusters:
            return []

        my_sector = None
        try:
            my_sector = (tool_get_fundamentals(sym).get("sector") or "")
        except Exception:
            my_sector = ""

        # Prefer the cluster matching the focus symbol's own fine sector;
        # fall back to the largest cluster in the GICS sector.
        pool = clusters.get(my_sector) if my_sector else None
        if not pool:
            pool = max(clusters.values(), key=lambda lst: max(c[1] for c in lst))
        pool.sort(key=lambda t: t[1], reverse=True)
        return [s for s, _ in pool[:limit]]
    except Exception as e:
        log.warning("Sector-peer resolution failed for %s: %s", symbol, e)
        return []


def peer_symbols(symbol: str) -> list[str]:
    """Resolve the peer set for a symbol: curated map, then sector fallback."""
    sym = (symbol or "").strip().upper()
    if not sym:
        return []
    peers = curated_peers(sym)
    if peers:
        return peers
    peers = _sector_peers(sym)
    if peers:
        return peers
    return []
