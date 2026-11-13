"""Fast HTTP search tools for Compass and Journalist — no browser required.

Replaces the Playwright browser tools (agent/tools_browser.py), which were
broken in production (browsers not installed, systemd ProtectHome=read-only
blocks the cache). All tools here are plain HTTP, ~0.2-1s, and return
LLM-ready text.

Sources:
  - Firecrawl search/scrape: AI-agent-native, clean markdown, free tier
  - Massive news: per-symbol news with sentiment (already exists)
  - StockAnalysis: structured fundamentals + earnings date
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

import httpx

import config as _config
from data.cache import ttl_cache

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
}
_FIRECRAWL_SEARCH = "https://api.firecrawl.dev/v1/search"
_FIRECRAWL_SCRAPE = "https://api.firecrawl.dev/v1/scrape"

# SSRF guard: fetch_page asks Firecrawl to fetch a URL server-side. Restrict
# to public http(s) URLs so the tool can't be pointed at internal services
# (metadata endpoints, localhost, RFC1918 ranges).
_BLOCKED_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0", "metadata.google.internal"}


def _is_safe_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").lower()
    if host in _BLOCKED_HOSTS:
        return False
    # RFC1918 + link-local private ranges.
    import ipaddress
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            return False
    except ValueError:
        pass  # hostname, not an IP — allow (DNS rebinding is out of scope for a single-operator tool)
    return True


def _firecrawl_headers() -> dict:
    h = dict(_HEADERS)
    h["Content-Type"] = "application/json"
    if _config.FIRECRAWL_API_KEY:
        h["Authorization"] = f"Bearer {_config.FIRECRAWL_API_KEY}"
    return h


def _search_firecrawl(query: str, limit: int) -> dict:
    """Firecrawl search — returns full page content as clean markdown."""
    r = httpx.post(
        _FIRECRAWL_SEARCH,
        headers=_firecrawl_headers(),
        json={"query": query, "limit": limit},
        timeout=20,
    )
    r.raise_for_status()
    data = r.json()
    results = data.get("data", [])
    out = []
    for item in results:
        out.append({
            "title": item.get("title", ""),
            "url": item.get("url", ""),
            "description": item.get("description", ""),
            "content": (item.get("markdown") or item.get("content") or "")[:4000],
        })
    return {"query": query, "result_count": len(out), "results": out, "provider": "firecrawl"}


def _search_duckduckgo(query: str, limit: int) -> dict:
    """Keyless DuckDuckGo HTML search fallback.

    Returns snippet-only results (no full page content, but URLs + titles +
    descriptions). Good enough for discovery; the writer can fetch_page a
    promising URL for full content.
    """
    r = httpx.get(
        "https://html.duckduckgo.com/html/",
        params={"q": query},
        headers=_HEADERS,
        timeout=15,
    )
    r.raise_for_status()
    results = []
    # DuckDuckGo HTML returns results in <a class="result__a"> and
    # <a class="result__snippet"> elements. Extract via regex (the HTML is
    # simple and stable enough for a fallback path).
    for m in re.finditer(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', r.text, re.S):
        url = m.group(1)
        title = re.sub(r"<[^>]+>", "", m.group(2)).strip()
        # DuckDuckGo wraps the real URL in a redirect param.
        uddg = re.search(r"uddg=([^&]+)", url)
        if uddg:
            from urllib.parse import unquote
            url = unquote(uddg.group(1))
        results.append({"title": title, "url": url, "description": "", "content": ""})
        if len(results) >= limit:
            break
    return {"query": query, "result_count": len(results), "results": results, "provider": "duckduckgo"}


@ttl_cache(ttl_seconds=3600)
def search_web(query: str, limit: int = 5) -> dict:
    """Search the web. Firecrawl primary, keyless DuckDuckGo fallback.

    A single provider going down must never silently empty research grounding.
    The fallback chain: Firecrawl (full page content) → DuckDuckGo HTML
    (keyless snippets). Each result carries a 'provider' tag so callers can
    see the source of truth. The writer can fetch_page any promising URL for
    full content.
    """
    providers = [
        ("firecrawl", _search_firecrawl),
        ("duckduckgo", _search_duckduckgo),
    ]
    errors: list[str] = []
    for name, fn in providers:
        try:
            result = fn(query, limit)
            if result.get("results"):
                return result
            errors.append(f"{name}: no results")
        except Exception as e:
            errors.append(f"{name}: {str(e)[:120]}")
    return {"error": "All search providers failed: " + "; ".join(errors)}


@ttl_cache(ttl_seconds=3600)
def fetch_page(url: str) -> dict:
    """Fetch a URL and return its content as clean LLM-ready markdown."""
    if not _is_safe_url(url):
        return {"error": "Refusing to fetch non-public URL (SSRF guard)"}
    try:
        r = httpx.post(
            _FIRECRAWL_SCRAPE,
            headers=_firecrawl_headers(),
            json={"url": url, "formats": ["markdown"]},
            timeout=25,
        )
        r.raise_for_status()
        data = r.json()
        md = (data.get("data") or {}).get("markdown", "")
        if not md:
            return {"url": url, "content": "", "note": "No content extracted"}
        # Head+tail window (the extract-util pattern): an earnings release is
        # 15-25K chars of markdown; a hard 8K cap truncates the guidance
        # tables — exactly the section research needs. Head 20K + tail 8K
        # keeps the lede AND the numbers.
        if len(md) > 28000:
            md = md[:20000] + "\n\n[...middle truncated...]\n\n" + md[-8000:]
        return {"url": url, "content": md, "char_count": len(md)}
    except Exception as e:
        return {"error": f"Fetch failed: {str(e)[:200]}"}


@ttl_cache(ttl_seconds=900)
def get_company_news(symbol: str, limit: int = 5) -> dict:
    """Per-symbol news with sentiment from Massive."""
    try:
        from data.reference import get_ticker_news
        articles = get_ticker_news(symbol.upper(), limit=limit)
        return {
            "symbol": symbol.upper(),
            "result_count": len(articles),
            "articles": [
                {
                    "title": a.get("title", ""),
                    "url": a.get("url", ""),
                    "published": a.get("published", ""),
                    "publisher": a.get("publisher", ""),
                    "sentiment": a.get("sentiment"),
                    "description": a.get("description", ""),
                }
                for a in articles
            ],
        }
    except Exception as e:
        return {"error": f"News failed: {str(e)[:200]}"}


@ttl_cache(ttl_seconds=86400)
def get_stockanalysis(symbol: str) -> dict:
    """Structured fundamentals + earnings date from StockAnalysis.com."""
    try:
        r = httpx.get(
            f"https://stockanalysis.com/stocks/{symbol.lower()}/",
            headers=_HEADERS,
            timeout=12,
        )
        r.raise_for_status()
        text = r.text
        m = re.search(r'earningsDate:"([^"]+)"', text)
        page_date = m.group(1) if m else None
        # Second source: the repo's own earnings-calendar reader (same
        # weeks-payload the scheduler parses - symbols carry {s, e(eps est)}).
        # When overview page and calendar disagree, BOTH dates are returned
        # with a conflict flag: the conflict is information; a single tool
        # date is a single point of failure (the VRT Oct-21 vs Oct-26/28
        # discrepancy surfaced exactly this way on 2026-10-08).
        cal_date = None
        try:
            from data.earnings import get_ticker_earnings

            cal_row = get_ticker_earnings(symbol.upper())
            if cal_row:
                raw_date = cal_row.get("earnings_date") or cal_row.get("date")
                cal_date = str(raw_date)[:10] or None
        except Exception:
            pass  # calendar fetch failure degrades to the page date only
        def _norm_date(val) -> str | None:
            """Normalize page ('Oct 21, 2026') and ISO ('2026-10-21') dates."""
            if not val:
                return None
            val = str(val).strip()
            m = re.match(r"[A-Z][a-z]{2} (\d{1,2}), (\d{4})", val)
            if m:
                import datetime as _dt

                try:
                    return _dt.date(int(m.group(2)), int(m.group(1)), _MONTH_MAP[val.split(" ")[0]]).isoformat()
                except (KeyError, ValueError):
                    return None
            m2 = re.match(r"(\d{4})-(\d{2})-(\d{2})", val)
            return m2.group(0) if m2 else None

        pn, cn = _norm_date(page_date), _norm_date(cal_date)
        conflict = bool(pn and cn and pn != cn)
        return {
            "symbol": symbol.upper(),
            "source": "stockanalysis.com",
            "earnings_date": page_date,
            "earnings_date_calendar": cal_date,
            "earnings_date_conflict": conflict,
            "note": (
                f"OVERVIEW PAGE AND CALENDAR DISAGREE ({page_date} vs {cal_date}) - verify against the IR page before sizing around a print."
                if conflict else None
            ),
        }
    except Exception as e:
        return {"error": f"StockAnalysis failed: {str(e)[:200]}"}


# ---------------------------------------------------------------------------
# Tool schemas (OpenAI function-calling format)
# ---------------------------------------------------------------------------

SEARCH_TOOLS = {
    "search_web": {
        "fn": search_web,
        "schema": {
            "type": "function",
            "function": {
                "name": "search_web",
                "description": "Search the web and return top results with full page content. Use for current events, news, analyst commentary, and any topic needing up-to-date information.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search query (e.g. 'NVDA Q2 FY2027 earnings date')"},
                        "limit": {"type": "integer", "description": "Number of results (default 5)"},
                    },
                    "required": ["query"],
                },
            },
        },
    },
    "fetch_page": {
        "fn": fetch_page,
        "schema": {
            "type": "function",
            "function": {
                "name": "fetch_page",
                "description": "Fetch a URL and return its content as clean markdown. Use to read a specific article, report, or web page.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "Full URL to fetch (e.g. https://example.com/article)"},
                    },
                    "required": ["url"],
                },
            },
        },
    },
    "get_company_news": {
        "fn": get_company_news,
        "schema": {
            "type": "function",
            "function": {
                "name": "get_company_news",
                "description": "Get recent news for a stock symbol with sentiment labels. Use for company-specific developments.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string", "description": "Ticker symbol (e.g. NVDA)"},
                        "limit": {"type": "integer", "description": "Number of articles (default 5)"},
                    },
                    "required": ["symbol"],
                },
            },
        },
    },
    "get_stockanalysis": {
        "fn": get_stockanalysis,
        "schema": {
            "type": "function",
            "function": {
                "name": "get_stockanalysis",
                "description": "Get structured fundamentals and the next earnings date for a stock from StockAnalysis.com.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string", "description": "Ticker symbol (e.g. NVDA)"},
                    },
                    "required": ["symbol"],
                },
            },
        },
    },
}


# ---------------------------------------------------------------------------
# FRED macro yields (keyless CSV; rates are the operator's primary lens)
# ---------------------------------------------------------------------------

_FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv"


_MONTH_MAP: dict[str, int] = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}


@ttl_cache(ttl_seconds=3600)
def get_fred_yields(series: str = "DGS10,DGS30,DGS2,DGS5") -> dict:
    """Latest daily Treasury yields from FRED (keyless CSV, no key required).

    series: comma-separated FRED codes. Default DGS2/DGS5/DGS10/DGS30.
    Returns the most recent observation plus a 10-session history, so briefs
    can state direction, not just level.
    """
    import datetime as _dt

    try:
        params = {"id": series}
        # Bound the payload: full history since 1962 is wasted bytes for a
        # yields snapshot. 400 days covers ~260 sessions + long weekends.
        params["observation_start"] = (_dt.date.today() - _dt.timedelta(days=400)).isoformat()
        r = httpx.get(_FRED_CSV, params=params, timeout=20)
        r.raise_for_status()
        # FRED silently returns a ZIP (not an error!) for some series
        # COMBINA TIONS (e.g. T10YIE,DFF) even at 200 — detect by magic bytes
        # and unzip transparently. Without this, half the series space
        # "succeeds" and parses as garbage.
        if r.content[:2] == b"PK":
            import io
            import zipfile

            with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
                csv_name = next(n for n in zf.namelist() if n.endswith(".csv"))
                text = zf.read(csv_name).decode("utf-8", errors="replace")
        else:
            text = r.text
        lines = [ln for ln in text.strip().splitlines() if ln.strip()]
        if len(lines) < 2:
            return {"error": "FRED returned no data rows"}
        header = lines[0].split(",")
        series_names = [h for h in header[1:] if h]
        data_lines = lines[1:]  # header must never enter the history window
        history: list[dict] = []
        for line in data_lines[-251:]:  # cap at ~1y of sessions; keep window slice below
            parts = line.split(",")
            row = {"date": parts[0]}
            for name, val in zip(series_names, parts[1:]):
                try:
                    row[name] = float(val)
                except (TypeError, ValueError):
                    row[name] = None  # FRED prints "." for missing observations
            history.append(row)
        latest = history[-1] if history else {}
        # 10-session history for direction, excluding missing-observation rows
        dense_window = [h for h in history if any(v is not None for k, v in h.items() if k != "date")]
        return {
            "latest": latest,
            "history": dense_window[-10:],
            "as_of": latest.get("date"),
            "series": series_names,
            "note": "Daily close, percent. Other codes: T10YIE breakeven, DFF fed funds, BAMLH0A0HYM2 HY spread.",
        }
    except Exception as e:
        return {"error": f"FRED fetch failed: {str(e)[:200]}"}


FRED_TOOLS = {
    "get_fred_yields": {
        "fn": get_fred_yields,
        "schema": {
            "type": "function",
            "function": {
                "name": "get_fred_yields",
                "description": "Latest US Treasury yields (2y/5y/10y/30y) with 10-session history from FRED. Use for rates context in any brief - policy stance, long-end direction, duration sensitivity.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "series": {
                            "type": "string",
                            "description": "Comma-separated FRED series codes (default 'DGS10,DGS30,DGS2,DGS5'). Other useful codes: T10YIE, DFF, BAMLH0A0HYM2.",
                        },
                    },
                    "required": [],
                },
            },
        },
    },
}
