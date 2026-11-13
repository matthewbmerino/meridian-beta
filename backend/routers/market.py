"""Market data endpoints: quotes, prices, options, fundamentals, search, insider."""

import logging

from fastapi import APIRouter, Query

from data.cache import ttl_cache

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/market", tags=["market"])


@router.get("/quotes/{symbols}")
def quotes(symbols: str):
    from data.market import get_quotes
    syms = tuple(s.strip().upper() for s in symbols.split(",") if s.strip())
    if not syms:
        return {}
    return get_quotes(syms)


@router.get("/snapshot/{symbols}")
def quote_snapshot(symbols: str):
    """The stream's exact payload, over HTTP — the instant prime.

    The WebSocket's first push is now immediate on subscribe, but a socket
    still has to connect, and a reconnect has to wait out its backoff. This
    endpoint answers in ~100ms warm and speaks the identical shape by
    construction (`data.market.quote_view`), so a watchlist can paint prices
    on the first frame and let the stream refine them.
    """
    from data.market import get_quotes, quote_view

    syms = tuple(s.strip().upper() for s in symbols.split(",") if s.strip())
    if not syms:
        return {}
    return {sym: quote_view(qdata) for sym, qdata in get_quotes(syms).items()}


@ttl_cache(ttl_seconds=3600)
def seasonal_build(symbol: str, years: int) -> dict:
    """Cached: one normalized return series per calendar year for a symbol."""
    from analysis.seasonal import seasonal_series
    from data.market import get_price_history

    df = get_price_history(symbol, days=years * 366 + 30)
    if df.empty:
        return {"years": [], "error": f"No price data for {symbol}"}
    df = df.reset_index()
    rows = [{"date": str(d)[:10], "close": float(c)} for d, c in zip(df["date"], df["close"])]
    return seasonal_series(rows, max_years=years)


@router.get("/seasonal/{symbol}")
def seasonal(symbol: str, years: int = 6):
    """Year-over-year seasonal returns — the per-year overlay chart's data.

    One series per calendar year, normalized from that year's first close in
    the tape. Cached 1h, aligned with the price-history cache.
    """
    return seasonal_build(symbol.upper(), max(2, min(int(years), 8)))


@router.get("/prices/{symbol}")
def price_history(symbol: str, days: int = 365):
    from data.market import get_price_history
    df = get_price_history(symbol.upper(), days=days)
    if df.empty:
        return []
    df_reset = df.reset_index()
    df_reset["date"] = df_reset["date"].dt.strftime("%Y-%m-%d")
    return df_reset.to_dict(orient="records")


def _coverage_block(requested: list[str], resolved: list[str]) -> dict:
    """Standard coverage/degraded block used by market endpoints."""
    unresolved = [s for s in requested if s not in resolved]
    return {
        "requested": len(requested),
        "resolved": len(resolved),
        "unresolved": unresolved,
        "degraded": len(unresolved) > 0,
        "note": (
            "All requested symbols resolved."
            if not unresolved else
            f"Could not resolve {len(unresolved)} symbol(s): {', '.join(unresolved)}. "
            "This is a data gap, not a statement that no contracts exist."
        ),
    }


@router.get("/options/{symbol}")
def option_chain(symbol: str):
    from data.market import flatten_option_chain, get_option_chain
    sym = symbol.upper()
    try:
        chain = get_option_chain(sym)
        flat = flatten_option_chain(chain)
        underlying_price = chain.get("underlyingPrice", None)
        rows = flat.to_dict(orient="records") if not flat.empty else []
        available = bool(rows)
        return {
            "available": available,
            "underlying_price": underlying_price,
            "contracts": rows,
            "coverage": _coverage_block([sym], [sym] if available else []),
        }
    except Exception as e:
        # Feed failure (e.g. options access denied) degrades to an empty chain
        # rather than a 500 — but it must not be silent.
        log.warning("Option chain fetch failed for %s: %s", sym, e)
        return {
            "available": False,
            "underlying_price": None,
            "contracts": [],
            "coverage": _coverage_block([sym], []),
        }


@router.get("/fundamentals/{symbol}")
def fundamentals(symbol: str):
    # A crypto asset has no fundamentals in the equity sense. Return an explicit
    # not_applicable rather than the equity path's zeroed shell (marketCap 0,
    # sharesOutstanding 0, sector "") — a market cap of 0 reads as "worthless",
    # not "not applicable", and consumers cannot tell the two apart.
    from data.crypto_assets import is_crypto
    from data.market import get_fundamentals

    if is_crypto(symbol):
        return {
            "symbol": symbol.upper(),
            "applicable": False,
            "reason": "Fundamentals are an equity concept; a crypto asset has no "
                      "earnings, shares outstanding or sector classification.",
        }

    try:
        raw = get_fundamentals(symbol.upper())
        instruments = raw.get("instruments", [])
        result = {}
        for inst in instruments:
            sym = inst.get("symbol", symbol.upper())
            result[sym] = {
                "fundamental": inst.get("fundamental", {}),
                "reference": {"description": inst.get("description")},
            }
        return result or {}
    except Exception as e:
        log.warning("Fundamentals fetch failed for %s: %s", symbol, e)
        return {}


@router.get("/analyst-targets/{symbol}")
def analyst_targets(symbol: str):
    """Sell-side price-target consensus for *symbol* (Yahoo, cached).

    Always a dict: ``available=False`` plus a ``reason`` when there is no coverage, so
    the chart can say "nobody covers this" instead of drawing a line at zero.
    """
    from data.analyst_targets import get_analyst_targets
    try:
        return get_analyst_targets(symbol)
    except Exception as e:
        log.warning("Analyst targets failed for %s: %s", symbol, e)
        return {
            "symbol": symbol.upper(),
            "available": False,
            "reason": f"Target fetch failed ({e.__class__.__name__}).",
        }


@router.get("/search")
def search(q: str = Query(..., min_length=1)):
    from data.crypto_assets import search_crypto
    from data.market import search_instruments

    # Crypto first: the equity reference endpoint has no crypto records, so
    # searching "BTC" there returns ABTC/BDCI and never Bitcoin. Crypto hits are
    # prepended so the asset the operator actually typed is the first row.
    return {"instruments": search_crypto(q) + search_instruments(q.upper()).get("instruments", [])}


@router.get("/insider/{symbol}")
def insider(symbol: str, months: int = 6):
    from data.edgar import get_insider_activity
    result = get_insider_activity(symbol.upper(), months=months)
    if result is None:
        return {"symbol": symbol.upper(), "available": False}
    return result


@router.get("/signals/{symbol}")
@ttl_cache(ttl_seconds=180)
def _signals_for(symbol: str):
    """Cached per symbol: generate_signals over the 1h-cached price frame is
    deterministic, but the endpoint recomputed it (~185ms) on every cold call —
    opportunities fires it once per candidate symbol, the watchlist once per row.
    """
    from analysis.signals import generate_signals
    from data.market import get_price_history

    df = get_price_history(symbol)
    if df.empty:
        return {"error": f"No price data for {symbol}"}
    return generate_signals(symbol, df)


def signals(symbol: str):
    return _signals_for(symbol.upper())
@router.get("/score-breakdown/{symbol}")
def score_breakdown(symbol: str):
    """Composite score decomposed into per-component contributions.

    Answers "why is this score X?" — each component (RSI, Trend, MACD,
    Bollinger, Volume, Vol Regime) reports its contribution in score points
    and its direction. Degrades to an explicit unavailable state on empty data.
    """
    from analysis.signals import generate_signals
    from analysis.signals import score_breakdown as _breakdown
    from data.market import get_price_history
    df = get_price_history(symbol.upper())
    if df.empty:
        return {"symbol": symbol.upper(), "available": False, "score": None, "label": None, "components": []}
    sig = generate_signals(symbol.upper(), df)
    result = _breakdown(sig)
    result["symbol"] = symbol.upper()
    result["available"] = True
    return result


@router.get("/stbr/{symbol}")
def stbr(symbol: str, days: int = Query(default=1825)):
    from analysis.stbr import calculate_stbr
    from data.market import get_price_history
    df = get_price_history(symbol.upper(), days=days)
    if df.empty:
        return {"symbol": symbol.upper(), "available": False}
    stbr_df = calculate_stbr(df["close"])
    stbr_df_out = stbr_df.reset_index()
    stbr_df_out["date"] = stbr_df_out["date"].dt.strftime("%Y-%m-%d")
    return stbr_df_out.to_dict(orient="records")


@router.get("/stbr-extended/{symbol}")
def stbr_extended(symbol: str, days: int = Query(default=1825)):
    """STBR series plus current z-score and velocity (regime-transition signals)."""
    from analysis.stbr import calculate_stbr, current_risk_label, stbr_velocity, stbr_zscore
    from data.market import get_price_history
    df = get_price_history(symbol.upper(), days=days)
    if df.empty:
        return {"symbol": symbol.upper(), "available": False}
    stbr_df = calculate_stbr(df["close"])
    stbr_series = stbr_df["stbr"]
    z = stbr_zscore(stbr_series)
    vel = stbr_velocity(stbr_series)
    label, color = current_risk_label(float(stbr_series.iloc[-1]))
    return {
        "symbol": symbol.upper(),
        "available": True,
        "stbr": round(float(stbr_series.iloc[-1]), 4),
        "risk_label": label,
        "risk_color": color,
        "zscore": round(z, 3),
        "velocity": round(vel, 3),
        "series": [
            {
                "date": r["date"].strftime("%Y-%m-%d") if hasattr(r["date"], "strftime") else str(r["date"]),
                "stbr": round(float(r["stbr"]), 4),
                "risk_label": r["risk_label"],
            }
            for _, r in stbr_df.reset_index().iterrows()
        ],
    }


@router.get("/details/{symbol}")
def ticker_details(symbol: str):
    from data.crypto_assets import crypto_details
    from data.reference import get_ticker_details

    sym = symbol.upper()
    # Crypto has no Massive reference record. Serve its own details rather than
    # letting the equity lookup return a null-filled shell (or, worse, a
    # same-ticker equity).
    crypto = crypto_details(sym)
    if crypto is not None:
        return crypto
    details = get_ticker_details(sym)
    if details is None:
        return {"error": "not found"}
    return details


@router.get("/news/{symbol}")
def ticker_news(symbol: str, limit: int = 10):
    from data.reference import get_ticker_news
    return get_ticker_news(symbol.upper(), limit=min(limit, 25))


@router.get("/iv-rank/{symbol}")
def iv_rank(symbol: str):
    """Options volatility intelligence: HV rank (proxy for IV rank) + chain IV percentile.

    Massive returns option-chain snapshots only (no IV history), so IV rank is
    proxied by historical-volatility rank over 252d, and the current chain's
    IV distribution is summarized as a percentile of the ATM-ish contracts.
    """
    import numpy as np

    from analysis.signals import historical_volatility, iv_percentile
    from analysis.signals import iv_rank as _iv_rank
    from data.market import flatten_option_chain, get_option_chain, get_price_history

    df = get_price_history(symbol.upper(), days=400)
    if df.empty:
        return {"symbol": symbol.upper(), "available": False}

    close = df["close"]
    hv_series = historical_volatility(close, 30).dropna()
    if len(hv_series) < 60:
        return {"symbol": symbol.upper(), "available": False}

    current_hv = float(hv_series.iloc[-1])
    hv_rank = _iv_rank(current_hv, hv_series.tail(252))
    hv_pctile = iv_percentile(current_hv, hv_series.tail(252))

    # Chain IV distribution
    chain_iv = None
    chain_iv_pctile = None
    try:
        chain = get_option_chain(symbol.upper())
        flat = flatten_option_chain(chain)
        if not flat.empty:
            ivs = flat["iv"].replace(0, np.nan).dropna()
            if len(ivs) > 0:
                chain_iv = float(ivs.median())
                chain_iv_pctile = float((ivs < chain_iv).mean() * 100)
    except Exception as e:
        log.warning("Chain IV computation failed for %s: %s", symbol, e)

    return {
        "symbol": symbol.upper(),
        "available": True,
        "hv_30": round(current_hv * 100, 1),
        "hv_rank": round(hv_rank, 1),
        "hv_percentile": round(hv_pctile, 1),
        "chain_iv_median": round(chain_iv * 100, 1) if chain_iv is not None else None,
        "chain_iv_percentile": round(chain_iv_pctile, 1) if chain_iv_pctile is not None else None,
    }


@router.get("/sectors")
def sectors(symbols: str):
    """Symbol → sector map for a comma-separated list."""
    from data.reference import get_sector_map
    syms = [s.strip().upper() for s in symbols.split(",") if s.strip()]
    if not syms:
        return {}
    return get_sector_map(syms)


@router.get("/earnings/portfolio")
def portfolio_earnings():
    """Earnings proximity for all held positions, sorted by days until.

    Registered BEFORE /earnings/{symbol} — FastAPI matches in registration
    order, so the static path must come first or "portfolio" is captured as
    a symbol and this endpoint degrades to available:false.
    """
    from data.account import get_combined_positions
    from data.earnings import get_portfolio_earnings_report

    positions = get_combined_positions()
    if positions.empty:
        return {
            "rows": [],
            "coverage": {
                "requested": 0,
                "resolved": 0,
                "unresolved": [],
                "degraded": False,
                "note": "No positions loaded - the book is empty, so there is "
                        "nothing to check for earnings proximity.",
            },
        }

    equities = positions[positions["asset_type"] != "OPTION"]
    symbols = [s for s in equities["symbol"].unique() if s]
    return get_portfolio_earnings_report(symbols, positions)


@router.get("/earnings/{symbol}")
def earnings(symbol: str):
    """Next earnings date, EPS estimate, prior EPS/surprise for a symbol."""
    from data.earnings import get_ticker_earnings
    data = get_ticker_earnings(symbol.upper())
    if data is None:
        return {"symbol": symbol.upper(), "available": False}
    from datetime import datetime

    import pandas as pd
    earnings_dt = data["earnings_date"]
    if hasattr(earnings_dt, "tzinfo") and earnings_dt.tzinfo is not None:
        earnings_dt = earnings_dt.tz_localize(None)
    days_until = (earnings_dt - pd.Timestamp(datetime.now())).days
    return {
        "symbol": symbol.upper(),
        "available": True,
        "earnings_date": earnings_dt.strftime("%Y-%m-%d"),
        "days_until": int(days_until),
        "urgent": 0 <= days_until <= 7,
        "eps_estimate": data["eps_estimate"],
        "prior_eps_actual": data["prior_eps_actual"],
        "prior_surprise_pct": data["prior_surprise_pct"],
    }
