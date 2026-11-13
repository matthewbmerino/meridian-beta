"""FRED macroeconomic data for market environment signals."""

import logging
import os

import pandas as pd
from dotenv import load_dotenv
from fredapi import Fred

from data.cache import ttl_cache

log = logging.getLogger(__name__)

load_dotenv()
_fred = Fred(api_key=os.getenv("FRED_API_KEY")) if os.getenv("FRED_API_KEY") else None
if _fred is None:
    log.warning(
        "FRED_API_KEY not set — all macro signals (VIX, yield curve, credit spread, "
        "fed funds) will be unavailable"
    )

# Series IDs
_YIELD_CURVE = "T10Y2Y"      # 10Y-2Y Treasury spread
_CREDIT_SPREAD = "BAA10Y"    # Moody's BAA corporate bond spread over 10Y
_FED_FUNDS = "FEDFUNDS"      # Effective federal funds rate
_VIXCLS = "VIXCLS"           # CBOE VIX close

# Term structure tenors
_TENORS = [
    ("1M", "DGS1MO"), ("3M", "DGS3MO"), ("6M", "DGS6MO"),
    ("1Y", "DGS1"), ("2Y", "DGS2"), ("5Y", "DGS5"),
    ("10Y", "DGS10"), ("20Y", "DGS20"), ("30Y", "DGS30"),
]

# Curated catalog of FRED series available for chart overlays. Each entry:
#   label: display name for the overlay picker
#   unit:  display unit for the overlay axis
#   description: one-line context for the picker tooltip
FRED_SERIES = {
    "M2SL": {"label": "M2 Money Supply", "unit": "$B", "description": "M2 money stock, seasonally adjusted"},
    "CPIAUCSL": {"label": "CPI", "unit": "index", "description": "Consumer Price Index for All Urban Consumers"},
    "UNRATE": {"label": "Unemployment Rate", "unit": "%", "description": "Civilian unemployment rate"},
    "VIXCLS": {"label": "VIX", "unit": "index", "description": "CBOE Volatility Index"},
    "DGS10": {"label": "10Y Treasury", "unit": "%", "description": "10-year constant maturity Treasury yield"},
    "BAA10Y": {"label": "BAA Credit Spread", "unit": "%", "description": "Moody's BAA corporate spread over 10Y"},
    "FEDFUNDS": {"label": "Fed Funds Rate", "unit": "%", "description": "Effective federal funds rate"},
    "T10Y2Y": {"label": "10Y-2Y Spread", "unit": "%", "description": "Treasury term spread (10Y minus 2Y)"},
}


def get_series(series_id, days=365):
    """Fetch a FRED series, returning a pandas Series indexed by date.

    Public wrapper over the raw FRED fetcher so the backend can serve
    arbitrary catalogued series (M2, CPI, VIX, ...) for chart overlays.
    """
    if _fred is None:
        return pd.Series(dtype=float)
    start = pd.Timestamp.now() - pd.Timedelta(days=days)
    data = _fred.get_series(series_id, observation_start=start)
    if data is None or data.empty:
        return pd.Series(dtype=float)
    return data.dropna()


def _fetch_or_empty(label: str, fetch_fn) -> pd.Series:
    """Fetch a series, degrading to an empty Series on failure.

    Every macro block in ``market_environment`` shares this wrapper so a
    failing series greys out (empty) instead of raising or fabricating.
    """
    try:
        return fetch_fn()
    except Exception as e:
        log.warning("FRED %s fetch failed: %s", label, e)
        return pd.Series(dtype=float)


@ttl_cache(ttl_seconds=3600)
def get_yield_curve(days=365):
    return get_series(_YIELD_CURVE, days)


@ttl_cache(ttl_seconds=3600)
def get_credit_spread(days=365):
    return get_series(_CREDIT_SPREAD, days)


@ttl_cache(ttl_seconds=3600)
def get_fed_funds_rate(days=365):
    return get_series(_FED_FUNDS, days)


@ttl_cache(ttl_seconds=3600)
def get_vix(days=365):
    return get_series(_VIXCLS, days)


# Short-dated Treasury used as the risk-free benchmark when comparing a debt
# APR against "what the money could earn safely" (see the Liabilities page).
_RISK_FREE = "DGS3MO"


@ttl_cache(ttl_seconds=3600)
def risk_free_rate(days: int = 30) -> dict | None:
    """Current risk-free benchmark with provenance, or None when unavailable.

    Returns ``{rate, as_of, series, label}`` — a percentage, the observation
    date it came from, and the FRED series id, so a consumer can always say
    where the number came from. None (never an assumed rate) when FRED is
    unconfigured or the series is empty; the caller must degrade, not default.
    """
    series = get_series(_RISK_FREE, days=days)
    if series.empty:
        return None
    try:
        value = float(series.iloc[-1])
    except (TypeError, ValueError):
        return None
    if value != value:  # NaN
        return None
    return {
        "rate": round(value, 4),
        "as_of": pd.Timestamp(series.index[-1]).date().isoformat(),
        "series": _RISK_FREE,
        "label": "3-Month Treasury yield",
    }


# The short end of the curve, as the cash page's "what could this earn" ladder.
# One entry per tenor, each carrying its own observation date: a partially
# fetched curve is honest about which points are real, and an empty list means
# FRED is unavailable (the surface says so rather than drawing a remembered line).
_CASH_TENORS = (("1M", "DGS1MO"), ("3M", "DGS3MO"), ("6M", "DGS6MO"), ("1Y", "DGS1"))


@ttl_cache(ttl_seconds=3600)
def cash_curve() -> list[dict]:
    """Short Treasury curve with per-point provenance; [] when unavailable."""
    out: list[dict] = []
    for tenor, series_id in _CASH_TENORS:
        series = _fetch_or_empty(series_id, lambda sid=series_id: get_series(sid, days=30))
        if series.empty:
            continue
        try:
            value = float(series.iloc[-1])
        except (TypeError, ValueError):
            continue
        if value != value:  # NaN
            continue
        out.append(
            {
                "tenor": tenor,
                "rate": round(value, 3),
                "as_of": pd.Timestamp(series.index[-1]).date().isoformat(),
                "series": series_id,
            }
        )
    return out


@ttl_cache(ttl_seconds=3600)
def get_term_structure():
    """Fetch current treasury yields across all tenors for the yield curve shape.

    Returns a list of (label, current_rate, rate_6m_ago) tuples.
    """
    if _fred is None:
        return []
    result = []
    for label, series_id in _TENORS:
        data = get_series(series_id, days=200)
        if data.empty:
            continue
        current = float(data.iloc[-1])
        # Find value closest to 6 months ago
        target = pd.Timestamp.now() - pd.Timedelta(days=180)
        past_data = data[data.index <= target]
        ago_6m = float(past_data.iloc[-1]) if not past_data.empty else None
        result.append((label, current, ago_6m))
    return result


def market_environment():
    """Return a dict of macro signals with current values, trends, and regime labels.

    Each signal includes:
        value: latest reading
        series: full pandas Series for charting
        trend: list of recent values (for sparkline)
        signal: "positive" | "warning" | "negative"
        label: human-readable regime label
        description: one-line actionable context
    """
    result = {}

    # --- Yield Curve ---
    yc = _fetch_or_empty("yield-curve", lambda: get_yield_curve(days=730))
    if not yc.empty:
        val = float(yc.iloc[-1])
        if val < 0:
            signal, label = "negative", "Inverted"
            desc = "Yield curve inverted — historically precedes recession. Favor defensive positioning."
        elif val < 0.5:
            signal, label = "warning", "Flat"
            desc = "Yield curve flattening — late-cycle signal. Monitor for inversion."
        else:
            signal, label = "positive", "Normal"
            desc = "Yield curve healthy — expansionary environment supports risk assets."
        result["yield_curve"] = {
            "value": val, "series": yc,
            "trend": yc.tail(120).tolist(),
            "signal": signal, "label": label, "description": desc,
            "display": f"{val:+.2f}%",
        }

    # --- Credit Spread ---
    cs = _fetch_or_empty("credit-spread", lambda: get_credit_spread(days=730))
    if not cs.empty:
        val = float(cs.iloc[-1])
        avg = float(cs.mean())
        pct = (cs < val).sum() / len(cs) * 100
        if val > 3.0:
            signal, label = "negative", "Stress"
            desc = "Credit spreads elevated — market pricing higher default risk. Reduce leverage."
        elif val > avg * 1.15:
            signal, label = "warning", "Widening"
            desc = "Spreads above average — credit conditions tightening."
        else:
            signal, label = "positive", "Tight"
            desc = "Credit spreads tight — risk appetite healthy, favorable for equities."
        result["credit_spread"] = {
            "value": val, "series": cs,
            "trend": cs.tail(120).tolist(),
            "signal": signal, "label": label, "description": desc,
            "display": f"{val:.2f}%",
            "avg": avg, "percentile": pct,
        }

    # --- Fed Funds Rate ---
    ff = _fetch_or_empty("fed-funds", lambda: get_fed_funds_rate(days=1825))
    if not ff.empty:
        val = float(ff.iloc[-1])
        prev_6m = float(ff.iloc[-min(6, len(ff))]) if len(ff) > 1 else val
        if val < prev_6m - 0.25:
            signal, label = "positive", "Easing"
            desc = "Fed cutting rates — tailwind for equities and growth."
        elif val > prev_6m + 0.25:
            signal, label = "negative", "Tightening"
            desc = "Fed raising rates — headwind for valuations. Favor cash flow."
        else:
            signal, label = "warning", "Holding"
            desc = "Fed on hold — watch for policy shift signals."
        result["fed_funds"] = {
            "value": val, "series": ff,
            "trend": ff.tail(120).tolist(),
            "signal": signal, "label": label, "description": desc,
            "display": f"{val:.2f}%",
        }

    # --- Term Structure ---
    try:
        term = get_term_structure()
    except Exception as e:
        log.warning("FRED term-structure fetch failed: %s", e)
        term = []
    if term:
        result["term_structure"] = term

    return result
