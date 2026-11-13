"""Ticker reference data: details, news, sector classification — Massive API.

This module used to be called ``polygon.py`` with a Polygon.io docstring, but
every request hits Massive (the same API as ``massive.py``). The rename to
``reference.py`` reflects what the module actually is: reference/auxiliary
data that complements the market-feed module.
"""

import logging

import requests

from config import MASSIVE_API_KEY
from data.cache import ttl_cache

log = logging.getLogger(__name__)

_BASE = "https://api.massive.com"


def _get(path: str, params: dict | None = None) -> dict:
    params = params or {}
    params["apiKey"] = MASSIVE_API_KEY
    resp = requests.get(f"{_BASE}{path}", params=params, timeout=10)
    resp.raise_for_status()
    return resp.json()


@ttl_cache(ttl_seconds=86400)
def get_ticker_details(symbol: str) -> dict | None:
    """Ticker reference data: sector, industry, market cap, description, logo."""
    try:
        data = _get(f"/v3/reference/tickers/{symbol.upper()}")
        r = data.get("results", {})
        return {
            "symbol": r.get("ticker", symbol),
            "name": r.get("name"),
            "description": r.get("description"),
            "sic_code": r.get("sic_code"),
            "sic_description": r.get("sic_description"),
            "market_cap": r.get("market_cap"),
            "total_employees": r.get("total_employees"),
            "homepage_url": r.get("homepage_url"),
            "logo_url": (r.get("branding") or {}).get("icon_url"),
            "list_date": r.get("list_date"),
            "shares_outstanding": r.get("share_class_shares_outstanding"),
        }
    except Exception as e:
        log.warning("Ticker details fetch failed for %s: %s", symbol, e)
        return None


@ttl_cache(ttl_seconds=900)
def get_ticker_news(symbol: str, limit: int = 10) -> list[dict]:
    """Recent news articles for a symbol with sentiment."""
    try:
        data = _get("/v2/reference/news", {"ticker": symbol.upper(), "limit": limit})
        articles = []
        for item in data.get("results", []):
            insight = None
            for ins in item.get("insights", []):
                if ins.get("ticker", "").upper() == symbol.upper():
                    insight = ins
                    break

            articles.append({
                "title": item.get("title"),
                "url": item.get("article_url"),
                "published": item.get("published_utc"),
                "publisher": (item.get("publisher") or {}).get("name"),
                "image_url": item.get("image_url"),
                "description": item.get("description"),
                "sentiment": insight.get("sentiment") if insight else None,
                "sentiment_reasoning": insight.get("sentiment_reasoning") if insight else None,
                "tickers": item.get("tickers", []),
            })
        return articles
    except Exception as e:
        log.warning("Ticker news fetch failed for %s: %s", symbol, e)
        return []


@ttl_cache(ttl_seconds=86400)
def get_sector_map(symbols: list[str]) -> dict[str, str]:
    """Build symbol → sector mapping for a list of symbols."""
    result = {}
    for sym in symbols:
        details = get_ticker_details(sym)
        if details and details.get("sic_description"):
            result[sym] = details["sic_description"]
    return result
