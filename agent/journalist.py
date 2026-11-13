"""Journalist agent — runs structured research via the agent harness.

The report writer runs on the Ollama-native harness by default (streaming,
privacy-first — prompts processed transiently by Ollama Cloud, never trained
on), with the Perplexity Agent API (sonar-reasoning-pro) as the explicit
fallback when llm_provider is "perplexity". Both paths consume pre-gathered
Meridian data + the shared tool harness; every emitted number passes through
the data guard.

Event grammar yielded by `run_research()`:

    {"type": "phase",           "phase": str, "description": str}
    {"type": "turn_start",      "iteration": int, "model": str}
    {"type": "token",           "content": str}
    {"type": "tool_call",       "name": str, "args": dict, "id": str}
    {"type": "tool_result",     "name": str, "result": dict, "id": str}
    {"type": "text",            "content": str}
    {"type": "citations",       "urls": list, "results": list}
    {"type": "error",           "kind": str, "content": str}
    {"type": "report",          "content": str, "title": str, "research_type": str}
    {"type": "done",            "elapsed_ms": float}
"""

from __future__ import annotations

import datetime
import json
import logging
import time
from collections.abc import Callable, Iterator

from agent import compute
from agent.data_guard import guard_tool_result
from agent.prompts import _portfolio_context
from agent.report_text import clean_citations, extract_actions, strip_narration
from agent.tools import TOOLS, execute_tool, get_tool_schemas

log = logging.getLogger(__name__)

MODEL = "sonar-reasoning-pro"
MAX_ITERATIONS = 20

RESEARCH_TYPES = {
    "stock_deep_dive": "Comprehensive single-name analysis",
    "thematic": "Investment theme / value chain research",
    "portfolio_review": "Full portfolio health check",
    "opportunity_scan": "Best setups across holdings",
    "macro_brief": "Market environment & regime analysis",
    "custom": "Freeform research question",
}

_OUTPUT_TOOLS = [
    "visualize_network", "visualize_chart", "visualize_table",
    "suggest_followups",
]

# Data tools exposed to the report writer so it can fill gaps (e.g. a price or
# signal it doesn't already have) instead of inventing numbers. Viz tools are
# intentionally NOT here — the viz agent owns chart/table/network emission.
# On the Ollama-native writer path the FULL harness is available, including the
# web-research tools (search_web/fetch_page/get_company_news/get_stockanalysis)
# which replace Perplexity's built-in web/finance search. On the Perplexity
# Agent API path these are filtered to the Meridian data tools only (the API
# provides its own built-in search types).
_WRITER_TOOLS = [
    "get_quote", "get_fundamentals", "get_signals", "get_stbr",
    "get_risk_metrics", "get_insider_activity", "get_position_intel",
    "get_option_chain_summary", "get_risk_momentum", "sector_rotation",
    "find_relationships", "get_macro_environment", "get_account_breakdown",
]

# The Ollama-native writer gets the web-research harness too — this is what
# replaces Perplexity's built-in web_search/finance_search.
_WRITER_WEB_TOOLS = [
    "search_web", "fetch_page", "get_company_news", "get_stockanalysis",
    "hermes_research",  # deep multi-source escalation (local Hermes agent, skills + web)
]

MAX_WRITER_ITERATIONS = 12


def _writer_tool_schemas() -> list[dict]:
    """Build the Agent API tool list for the report writer.

    Built-in web/finance search plus Meridian data tools in the Agent API's
    FLAT function format (type/name/description/parameters) — not the nested
    OpenAI format get_tool_schemas() returns.
    """
    tools: list[dict] = [{"type": "web_search"}, {"type": "finance_search"}]
    schemas = get_tool_schemas()
    for s in schemas:
        fn = s.get("function", {})
        name = fn.get("name")
        if name in _WRITER_TOOLS:
            tools.append({
                "type": "function",
                "name": name,
                "description": fn.get("description", ""),
                "parameters": fn.get("parameters", {"type": "object", "properties": {}}),
                "strict": True,
            })
    return tools


def _agent_tools() -> list[dict]:
    """Build output-only tool list for Agent API (visualization + followups)."""
    tools: list[dict] = [
        {"type": "web_search"},
        {"type": "finance_search"},
    ]
    for name in _OUTPUT_TOOLS:
        entry = TOOLS.get(name)
        if not entry or not entry["schema"]:
            continue
        func = entry["schema"].get("function", {})
        tools.append({
            "type": "function",
            "name": func.get("name", name),
            "description": func.get("description", ""),
            "parameters": func.get("parameters", {}),
        })
    return tools


def _ollama_writer_tool_schemas() -> list[dict]:
    """Build the OpenAI-format tool list for the Ollama-native writer.

    The full harness: Meridian data tools + the web-research tools. This is
    what replaces Perplexity's built-in web_search/finance_search on the
    Ollama-native path. Returns the nested OpenAI format get_tool_schemas()
    already produces (name/description/parameters).
    """
    names = set(_WRITER_TOOLS) | set(_WRITER_WEB_TOOLS)
    schemas = get_tool_schemas()
    return [
        s for s in schemas
        if s.get("function", {}).get("name") in names
    ]


def _adapt_prompt_for_ollama(prompt: str) -> str:
    """Map Perplexity-only tool references to the Ollama harness tool names.

    The shared _JOURNALIST_PROMPT + type guidance mention web_search and
    finance_search (Perplexity's built-ins). On the Ollama-native path those
    are replaced by the harness's search_web/fetch_page/get_company_news.
    """
    prompt = prompt.replace("finance_search", "search_web")
    prompt = prompt.replace("web_search", "search_web")
    prompt = prompt.replace(
        "Use finance_search for", "Use search_web for"
    )
    prompt += (
        "\n\nWEB RESEARCH (Ollama harness): use `search_web` for current "
        "events, news, analyst commentary, and any topic needing up-to-date "
        "information. Use `fetch_page` to read a specific article or report "
        "in full. Use `get_company_news` for per-symbol news with sentiment. "
        "Cite the URLs returned by these tools in the report."
    )
    prompt += (
        "\n\nDEEP ESCALATION (hermes_research): when a claim needs "
        "verification you cannot complete with search snippets — street "
        "consensus with the analyst count and low/high range, a primary "
        "release extracted verbatim, or arbitration between conflicting "
        "sources — call `hermes_research` with a SELF-CONTAINED question "
        "(it sees none of this conversation) and cite what comes back with "
        "its source lines. Budget: it may take minutes; do not call it more "
        "than twice per report, and never for data your own Meridian tools "
        "already carry."
    )
    prompt += (
        "\n\nPRE-GATHERED DATA: the Pre-Gathered Data block above already "
        "contains live quotes, fundamentals, signals, and web results for the "
        "focus symbols. Do NOT re-gather what is already there — only call a "
        "tool to fill a specific gap (e.g. a price or signal not in the "
        "snapshot, or a web search for a topic not yet covered)."
    )
    prompt += (
        "\n\nVISUALIZATION IS HANDLED SEPARATELY: a dedicated viz agent "
        "builds the charts, tables, and network graph from the same data. You "
        "do NOT have the visualize_* or suggest_followups tools and must not "
        "attempt to call them. Your job is the written report — gather any "
        "missing data, then write the full markdown report."
    )
    return prompt


def _write_report_ollama(
    topic: str,
    research_type: str,
    user_message: str,
    instructions: str,
    tool_schemas: list[dict],
    gathered: dict[str, dict],
    abort_flag: Callable[[], bool] | None = None,
) -> Iterator[dict]:
    """Ollama-native report writer. Streams tokens; runs the harness tool loop.

    Drives compute.get_client_for_writer() with the full harness (Meridian
    data tools + search_web/fetch_page/get_company_news/get_stockanalysis).
    Yields the same event grammar as the Perplexity Agent API writer path PLUS
    streaming token events, so the frontend streams the report as it's written.

    Every data-tool result passes through guard_tool_result (the accuracy
    guarantee is provider-agnostic). Citation URLs are collected from the web
    tools' results and emitted as a citations event.
    """
    client, model = compute.get_client_for_writer()
    messages: list[dict] = [
        {"role": "system", "content": instructions},
        {"role": "user", "content": user_message},
    ]
    citations: list[str] = []

    def _collect_citations(name: str, result: dict) -> None:
        if isinstance(result, dict):
            if name in ("search_web", "get_company_news"):
                for item in result.get("results", []) or []:
                    url = item.get("url")
                    if url and url not in citations:
                        citations.append(url)
            elif name == "fetch_page" and result.get("url"):
                url = result["url"]
                if url not in citations:
                    citations.append(url)

    for iteration in range(MAX_WRITER_ITERATIONS):
        if abort_flag is not None and abort_flag():
            yield {"type": "aborted"}
            return

        # On the final iteration, force the report: a tool-less call cannot
        # loop, so the writer always produces a written report instead of a
        # hard tool_limit failure. The cap is a safety valve, not a dead end.
        finalize = iteration == MAX_WRITER_ITERATIONS - 1

        try:
            stream = client.chat(
                model=model,
                messages=messages,
                tools=None if finalize else tool_schemas,
                stream=True,
                role="writer",
            )
            accumulated = ""
            tool_calls = None
            for chunk in stream:
                if abort_flag is not None and abort_flag():
                    yield {"type": "aborted"}
                    return
                msg = chunk.get("message", {})
                delta = msg.get("content") or ""
                if delta:
                    accumulated += delta
                    yield {"type": "token", "content": delta}
                tc = msg.get("tool_calls")
                if tc:
                    tool_calls = tc
        except Exception as e:
            # A network/model failure must degrade to an error event, never
            # kill the SSE stream silently.
            yield {"type": "error", "kind": compute.classify_error(e), "content": str(e)}
            return

        if tool_calls and not finalize:
            messages.append({
                "role": "assistant",
                "content": accumulated,
                "tool_calls": tool_calls,
            })
            for tc in tool_calls:
                if abort_flag is not None and abort_flag():
                    yield {"type": "aborted"}
                    return
                func = tc.get("function", {}) if isinstance(tc, dict) else {}
                name = func.get("name", "unknown")
                raw_args = func.get("arguments", {}) or {}
                if isinstance(raw_args, str):
                    try:
                        args = json.loads(raw_args) if raw_args else {}
                    except json.JSONDecodeError:
                        args = {}
                else:
                    args = raw_args
                tc_id = tc.get("id", "") if isinstance(tc, dict) else f"call_{iteration}_{len(messages)}"
                yield {"type": "tool_call", "name": name, "args": args, "id": tc_id}
                try:
                    result = execute_tool(name, args)
                except Exception as e:
                    result = {"error": f"{type(e).__name__}: {e}"}
                guarded_result, corrections = guard_tool_result(name, result, gathered)
                if corrections:
                    yield {
                        "type": "tool_result",
                        "name": "data_guard",
                        "result": {"corrections": corrections},
                        "id": f"guard_{tc_id or 'call'}",
                    }
                _collect_citations(name, guarded_result)
                yield {"type": "tool_result", "name": name, "result": guarded_result, "id": tc_id}
                messages.append({
                    "role": "tool",
                    "content": json.dumps(guarded_result, default=str),
                    "tool_call_id": tc_id,
                })
            continue

        # No tool calls (or the forced finalize) — the report is complete.
        if not accumulated:
            yield {"type": "error", "kind": "empty_report", "content": "Model returned no text."}
            return
        accumulated = strip_narration(accumulated)
        title = _extract_title(accumulated, topic)
        yield {"type": "text", "content": accumulated}
        yield {"type": "report", "content": accumulated, "title": title, "research_type": research_type}
        if citations:
            yield {"type": "citations", "urls": clean_citations(citations), "results": []}
        return

    # Unreachable: the finalize on the last iteration always returns above.
    yield {"type": "error", "kind": "tool_limit",
           "content": f"Report writer exceeded {MAX_WRITER_ITERATIONS} tool iterations."}


def _review_report(
    report_text: str,
    topic: str,
    research_type: str,
    gathered: dict[str, dict],
) -> list[str]:
    """Second-model reviewer pass: cross-check the writer's numbers and facts.

    A cheaper Ollama model reads the finished report against the pre-gathered
    data + live price snapshot and flags any claim that deviates from the
    verified source. This is the omp "advisor" pattern applied to research —
    the writer is the author; the reviewer is the quality gate. Corrections
    are surfaced to the operator as data_guard-style events.

    Returns a list of correction notes (empty = report passed review).
    Runs only on the Ollama-native path (a second Perplexity call would cost
    a second paid API request per report).
    """
    try:
        from agent.compute import get_client_for_research
        client, model = get_client_for_research()
    except Exception as e:
        log.warning("Reviewer pass unavailable: %s", e)
        return []

    # Build a compact verified-data block the reviewer can check against.
    data_block = _live_price_snapshot(gathered, _derive_focus_symbols(None, research_type, topic))
    data_block += _preformat_gathered(gathered)

    prompt = f"""You are the quality reviewer for a research report. Your job: verify the report's numbers and factual claims against the verified data below.

## VERIFIED DATA (from Meridian's live feeds)
{data_block}

## REPORT TO REVIEW
{report_text}

CROSS-CHECK:
1. Every price/range in the report must match the LIVE PRICE SNAPSHOT verbatim. Flag any price that differs.
2. Every metric (market cap, P/E, RSI, STBR, beta, etc.) must match the verified data above. Flag deviations >5%.
3. Flag any claim presented as fact that has NO source in the verified data (unless it's clearly attributed to a cited web source).

Respond with a JSON list of corrections, each: {{"claim": "the claim as written", "correction": "what it should be", "severity": "error|warning"}}.
If everything checks out, respond with an empty JSON list: []"""
    try:
        response = client.chat(
            model=model,
            messages=[
                {"role": "system", "content": "You are a meticulous fact-checker. Respond only with a JSON list of corrections."},
                {"role": "user", "content": prompt},
            ],
            stream=False,
            role="research",
        )
        content = (response.get("message") or {}).get("content", "")
        return _parse_review_corrections(content)
    except Exception as e:
        log.warning("Reviewer pass failed: %s", e)
        return []


def _preformat_gathered(gathered: dict[str, dict]) -> str:
    """Compact, JSON-formatted verified data for the reviewer (and prompts)."""
    sections: list[str] = []
    for label, result in gathered.items():
        if not isinstance(result, dict):
            continue
        if result.get("error") or result.get("available") is False:
            continue
        sections.append(f"### {label}\n```json\n{json.dumps(result, default=str)[:800]}\n```")
    return "\n\n".join(sections) if sections else "(No verified data available)"


def _parse_review_corrections(content: str) -> list[str]:
    """Extract correction strings from the reviewer's JSON response."""
    import json as _json
    import re as _re

    text = content.strip()
    # Strip code fences if the model wrapped the JSON.
    m = _re.search(r"\[.*\]", text, _re.S)
    if not m:
        return []
    try:
        items = _json.loads(m.group(0))
    except (ValueError, TypeError):
        return []
    out: list[str] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        claim = it.get("claim", "")
        correction = it.get("correction", "")
        severity = it.get("severity", "warning")
        if correction:
            out.append(f"[{severity}] {claim}: {correction}" if claim else f"[{severity}] {correction}")
    return out[:20]


def _gather_data(
    symbols: list[str] | None,
    research_type: str,
) -> Iterator[tuple[str, dict]]:
    """Pre-gather data from Meridian tools for each symbol. Yields (label, data)."""
    syms = symbols or []
    for sym in syms:
        for tool_name in ["get_quote", "get_fundamentals", "get_signals"]:
            try:
                result = execute_tool(tool_name, {"symbol": sym})
                yield (f"{tool_name}({sym})", result)
            except Exception as e:
                yield (f"{tool_name}({sym})", {"error": str(e)})

    if research_type in ("stock_deep_dive",) and syms:
        for sym in syms:
            for tool_name in ["get_stbr", "get_risk_metrics", "get_insider_activity",
                              "get_position_intel", "get_option_chain_summary"]:
                try:
                    result = execute_tool(tool_name, {"symbol": sym})
                    yield (f"{tool_name}({sym})", result)
                except Exception as e:
                    log.warning("Data-gather tool %s(%s) failed: %s", tool_name, sym, e)

    if research_type in ("portfolio_review", "opportunity_scan"):
        for tool_name in ["get_portfolio_summary", "get_account_breakdown", "get_positions", "get_risk_momentum"]:
            try:
                result = execute_tool(tool_name, {})
                yield (f"{tool_name}()", result)
            except Exception as e:
                log.warning("Data-gather tool %s() failed: %s", tool_name, e)

    if research_type in ("macro_brief", "thematic"):
        try:
            result = execute_tool("get_macro_environment", {})
            yield ("get_macro_environment()", result)
        except Exception as e:
            log.warning("Data-gather tool get_macro_environment() failed: %s", e)

    if research_type in ("thematic",) and syms:
        try:
            result = execute_tool("sector_rotation", {})
            yield ("sector_rotation()", result)
        except Exception as e:
            log.warning("Data-gather tool sector_rotation() failed: %s", e)


def _build_visualizations(
    gathered: dict[str, dict],
    symbols: list[str] | None,
    research_type: str,
    topic: str,
) -> Iterator[dict]:
    """Build chart/table/network tool_call events from gathered data."""
    syms = symbols or []
    if not syms:
        return

    # The full comparison set: focus symbols + any peers that were actually
    # gathered (deep dives fetch peer fundamentals/quotes). This is what lets
    # the peer comparison table render with real, guard-verifiable rows.
    compare_syms = list(syms)
    for label in gathered:
        if label.startswith("get_fundamentals("):
            peer = label[len("get_fundamentals("):-1]
            if peer and peer.upper() not in {s.upper() for s in compare_syms}:
                compare_syms.append(peer)

    # --- Price trend chart (area over the last ~20 trading days) ---
    # The institutional centerpiece for a single-name report: the price
    # trajectory, not a point-in-time bar. Uses the close series from
    # tool_get_price_history so the fallback always has trend data to draw.
    trend_series = []
    for sym in syms:
        ph = gathered.get(f"get_price_history({sym})", {})
        series = ph.get("series") if isinstance(ph, dict) else None
        pts = [
            {"x": str(p.get("date", "")), "y": p["close"]}
            for p in (series or [])
            if isinstance(p, dict) and p.get("close") is not None
        ]
        if pts:
            trend_series.append({"label": sym, "values": pts})
    if trend_series:
        yield {
            "type": "tool_call",
            "name": "visualize_chart",
            "args": {
                "title": "Price Trend (20D)",
                "chart_type": "area",
                "data": trend_series,
                "x_label": "Date",
                "y_label": "Price ($)",
            },
            "id": "viz_price_trend",
        }

    # --- Current price comparison bar (only meaningful across symbols) ---
    if len(compare_syms) > 1:
        price_series = []
        for sym in compare_syms:
            q = gathered.get(f"get_quote({sym})", {})
            price = q.get("last") or q.get("price") or q.get("lastPrice")
            if price:
                price_series.append({"x": sym, "y": float(price)})
        if price_series:
            yield {
                "type": "tool_call",
                "name": "visualize_chart",
                "args": {
                    "title": "Current Price Comparison",
                    "chart_type": "bar",
                    "data": [{"label": "Price ($)", "values": price_series}],
                    "x_label": "Symbol",
                    "y_label": "Price ($)",
                },
                "id": "viz_price",
            }

    # --- Fundamentals comparison table ---
    table_rows = []
    for sym in compare_syms:
        f = gathered.get(f"get_fundamentals({sym})", {})
        q = gathered.get(f"get_quote({sym})", {})
        if f or q:
            row: dict = {"symbol": sym}
            if f.get("market_cap"):
                row["market_cap"] = f["market_cap"]
            if f.get("pe_ratio"):
                row["pe_ratio"] = f["pe_ratio"]
            if f.get("eps"):
                row["eps"] = f["eps"]
            if f.get("dividend_yield"):
                row["div_yield"] = f["dividend_yield"]
            if q.get("change_pct") is not None:
                row["day_change"] = q["change_pct"]
            elif q.get("changePct") is not None:
                row["day_change"] = q["changePct"]
            if row.keys() - {"symbol"}:
                table_rows.append(row)

    if table_rows:
        cols = [{"key": "symbol", "label": "Symbol", "format": "text"}]
        if any("market_cap" in r for r in table_rows):
            cols.append({"key": "market_cap", "label": "Market Cap", "format": "currency"})
        if any("pe_ratio" in r for r in table_rows):
            cols.append({"key": "pe_ratio", "label": "P/E", "format": "number"})
        if any("eps" in r for r in table_rows):
            cols.append({"key": "eps", "label": "EPS", "format": "currency"})
        if any("div_yield" in r for r in table_rows):
            cols.append({"key": "div_yield", "label": "Div Yield", "format": "percent"})
        if any("day_change" in r for r in table_rows):
            cols.append({"key": "day_change", "label": "Day Change", "format": "percent"})
        yield {
            "type": "tool_call",
            "name": "visualize_table",
            "args": {
                "title": "Fundamentals Comparison",
                "columns": cols,
                "rows": table_rows,
                "highlight": "max",
            },
            "id": "viz_fundamentals",
        }

    # --- STBR (bubble-risk) trend line chart ---
    # STBR = price / 140d SMA is a decision-relevant risk trajectory for a
    # single name — a line chart over time, not a point bar.
    stbr_series = []
    for sym in syms:
        st = gathered.get(f"get_stbr({sym})", {})
        series = st.get("series") if isinstance(st, dict) else None
        pts = [
            {"x": str(p.get("date", "")), "y": p["stbr"]}
            for p in (series or [])
            if isinstance(p, dict) and p.get("stbr") is not None
        ]
        if pts:
            stbr_series.append({"label": sym, "values": pts})
    if stbr_series:
        yield {
            "type": "tool_call",
            "name": "visualize_chart",
            "args": {
                "title": "Bubble Risk (STBR) Trend",
                "chart_type": "line",
                "data": stbr_series,
                "x_label": "Date",
                "y_label": "STBR (price / 140d SMA)",
            },
            "id": "viz_stbr",
        }

    # --- Signals composite chart (bar only across symbols) ---
    signal_series = []
    for sym in syms:
        s = gathered.get(f"get_signals({sym})", {})
        composite = s.get("composite") or s.get("composite_score")
        if composite is not None:
            signal_series.append({"x": sym, "y": float(composite)})
    if len(signal_series) > 1:
        yield {
            "type": "tool_call",
            "name": "visualize_chart",
            "args": {
                "title": "Technical Composite Score",
                "chart_type": "bar",
                "data": [{"label": "Composite", "values": signal_series}],
                "x_label": "Symbol",
                "y_label": "Score",
            },
            "id": "viz_signals",
        }

    # --- Network graph ---
    nodes = []
    edges = []
    # Central theme node
    nodes.append({
        "id": "theme",
        "label": topic[:30],
        "group": "Theme",
        "size": 1.0,
        "description": topic,
    })
    for sym in syms:
        f = gathered.get(f"get_fundamentals({sym})", {})
        sector = f.get("sector", "")
        cap = f.get("market_cap", 0)
        size = 0.8 if cap and cap > 500e9 else 0.5
        nodes.append({
            "id": sym,
            "label": sym,
            "group": sector or "Equity",
            "size": size,
            "description": f.get("name", sym),
        })
        edges.append({
            "source": "theme",
            "target": sym,
            "label": "exposure",
            "strength": 0.7,
            "directional": True,
        })
    if len(nodes) > 1:
        yield {
            "type": "tool_call",
            "name": "visualize_network",
            "args": {
                "title": f"Value Chain: {topic[:40]}",
                "nodes": nodes,
                "edges": edges,
            },
            "id": "viz_network",
        }

    # --- Follow-up suggestions ---
    suggestions: list[dict] = []
    if research_type == "thematic":
        if syms:
            suggestions.append({
                "label": f"Deep dive: {syms[0]}",
                "topic": f"Deep dive on {syms[0]} capex allocation and competitive position",
                "research_type": "stock_deep_dive",
                "symbols": [syms[0]],
            })
        suggestions.append({
            "label": "Supplier exposure",
            "topic": f"Compare supplier and infrastructure exposure across {', '.join(syms[:4])}",
            "research_type": "thematic",
            "symbols": syms[:4],
        })
        suggestions.append({
            "label": "Power & cooling",
            "topic": "Analyze power and cooling infrastructure demand from AI data center buildout",
            "research_type": "thematic",
        })
        if len(syms) >= 2:
            suggestions.append({
                "label": "Concentration risk",
                "topic": f"Risk assessment: portfolio concentration in {', '.join(syms[:3])}",
                "research_type": "portfolio_review",
                "symbols": syms[:3],
            })
    elif research_type == "stock_deep_dive" and syms:
        suggestions.append({
            "label": "Entry timing",
            "topic": f"Technical setup and entry timing for {syms[0]}",
            "research_type": "stock_deep_dive",
            "symbols": [syms[0]],
        })
        suggestions.append({
            "label": "Competitors",
            "topic": f"Competitive landscape around {syms[0]}",
            "research_type": "thematic",
            "symbols": syms[:1],
        })
        suggestions.append({
            "label": "Options flow",
            "topic": f"Options flow and implied volatility for {syms[0]}",
            "research_type": "stock_deep_dive",
            "symbols": [syms[0]],
        })
    else:
        suggestions.append({
            "label": "Highest conviction",
            "topic": "Drill deeper into the highest-conviction name from this research",
            "research_type": "stock_deep_dive",
        })
        suggestions.append({
            "label": "Concentration risk",
            "topic": "Assess portfolio concentration risk",
            "research_type": "portfolio_review",
        })
        suggestions.append({
            "label": "Macro impact",
            "topic": "Check macro regime impact on this thesis",
            "research_type": "macro_brief",
        })
    yield {
        "type": "tool_call",
        "name": "suggest_followups",
        "args": {"suggestions": suggestions},
        "id": "viz_followups",
    }


def _derive_focus_symbols(
    symbols: list[str] | None,
    research_type: str,
    topic: str = "",
) -> list[str]:
    """Return the symbols a research report should focus on.

    Explicit symbols win. For scan types (opportunity_scan, portfolio_review)
    with no symbols given, derive the top-5 from get_risk_momentum scores so
    the writer gets live per-symbol prices instead of hallucinated ranges. For
    a single-name type (stock_deep_dive) with no symbols, resolve the name
    from the topic ("KKR", "the KKR thesis", "Nvidia Corp") through the
    instrument search so the report still pre-gathers real data to visualize.
    """
    if symbols:
        return [s.upper() for s in symbols]
    if research_type in ("portfolio_review", "opportunity_scan"):
        try:
            result = execute_tool("get_risk_momentum", {})
            scores = result.get("scores", []) if isinstance(result, dict) else []
            return [s["symbol"] for s in scores[:5] if isinstance(s, dict) and s.get("symbol")]
        except Exception as e:
            log.warning("Focus-symbol derivation failed (get_risk_momentum): %s", e)
    if research_type == "stock_deep_dive" and topic:
        try:
            # A bare uppercase ticker word in the topic ("the KKR thesis")
            # wins without a search.
            import re as _re
            for token in _re.split(r"[^A-Za-z]+", topic):
                if _re.fullmatch(r"[A-Z]{1,5}", token):
                    return [token]
            from agent.tools import tool_search_symbol
            result = tool_search_symbol(topic)
            results = result.get("results", []) if isinstance(result, dict) else []
            candidates = [
                item for item in results
                if isinstance(item, dict) and item.get("symbol") and ":" not in item["symbol"]
            ]
            if candidates:
                # Prefer an exact ticker match ("KKR" -> KKR, not the first
                # alphabetically-earlier result like FSK). Fall back to the
                # first candidate (e.g. a company name like "NVIDIA" -> NVDA).
                topic_upper = topic.strip().upper()
                for item in candidates:
                    if item["symbol"].upper() == topic_upper:
                        return [item["symbol"].upper()]
                return [candidates[0]["symbol"].upper()]
        except Exception as e:
            log.warning("Topic-to-symbol derivation failed: %s", e)
    return []


def _gather_data_parallel(
    symbols: list[str] | None,
    research_type: str,
    topic: str = "",
) -> list[tuple[str, dict]]:
    """Gather data from Meridian tools in parallel (independent HTTP calls)."""
    from concurrent.futures import ThreadPoolExecutor

    tasks: list[tuple[str, str, dict]] = []
    syms = _derive_focus_symbols(symbols, research_type, topic)

    for sym in syms:
        for tool_name in ["get_quote", "get_fundamentals", "get_signals"]:
            tasks.append((f"{tool_name}({sym})", tool_name, {"symbol": sym}))
        tasks.append((f"get_price_history({sym})", "get_price_history", {"symbol": sym, "days": 20}))

    if research_type in ("stock_deep_dive",) and syms:
        for sym in syms:
            for tool_name in ["get_stbr", "get_risk_metrics", "get_insider_activity",
                              "get_position_intel", "get_option_chain_summary"]:
                tasks.append((f"{tool_name}({sym})", tool_name, {"symbol": sym}))

        # Peers: a deep dive positions the name against its competitors, so
        # gather real peer fundamentals + quotes. The viz agent can only build
        # a verifiable peer table from symbols it actually has data for.
        try:
            from agent.peers import peer_symbols
            peers = peer_symbols(syms[0])
        except Exception as e:
            log.warning("Peer resolution failed for %s: %s", syms[0], e)
            peers = []
        for peer in peers:
            if peer == syms[0].upper():
                continue
            for tool_name in ["get_fundamentals", "get_quote"]:
                tasks.append((f"{tool_name}({peer})", tool_name, {"symbol": peer}))

    if research_type in ("portfolio_review", "opportunity_scan"):
        for tool_name in ["get_portfolio_summary", "get_account_breakdown", "get_positions", "get_risk_momentum"]:
            tasks.append((f"{tool_name}()", tool_name, {}))

    if research_type in ("macro_brief", "thematic"):
        tasks.append(("get_macro_environment()", "get_macro_environment", {}))

    if research_type in ("thematic",) and syms:
        tasks.append(("sector_rotation()", "sector_rotation", {}))

    def _run(task: tuple[str, str, dict]) -> tuple[str, dict]:
        label, tool_name, args = task
        try:
            return label, execute_tool(tool_name, args)
        except Exception as e:
            return label, {"error": str(e)}

    # ThreadPoolExecutor does NOT copy contextvars (unlike asyncio.to_thread),
    # so the per-request context is invisible inside the gather threads.
    # Capture the current context in the main thread; each worker enters its
    # own copy (a shared Context object can't be entered concurrently).
    import contextvars
    ctx = contextvars.copy_context()

    def _run_in_ctx(task: tuple[str, str, dict]) -> tuple[str, dict]:
        return ctx.copy().run(_run, task)

    with ThreadPoolExecutor(max_workers=8) as pool:
        return list(pool.map(_run_in_ctx, tasks))


def _live_price_snapshot(gathered: dict[str, dict], symbols: list[str]) -> str:
    """Build a timestamped live-price block the writer must cite verbatim.

    The writer must never invent a price or range. Every price/level in the
    report either comes from this snapshot (as of its timestamp) or from a
    cited web source. This block is what keeps reports from going stale.
    """
    rows: list[str] = []
    for sym in symbols:
        q = gathered.get(f"get_quote({sym})", {})
        if not q or q.get("error"):
            continue
        price = q.get("price")
        if price is None:
            continue
        h = q.get("high_52w") or None
        low52 = q.get("low_52w") or None
        ph = gathered.get(f"get_price_history({sym})", {})
        low_20d = ph.get("low") if isinstance(ph, dict) else None
        high_20d = ph.get("high") if isinstance(ph, dict) else None

        def _fmt_price(v) -> str:
            return f"${v:,.2f}" if isinstance(v, (int, float)) else "n/a"

        rows.append(
            f"- {sym}: ${price:,.2f} "
            f"(52w {_fmt_price(low52)}–{_fmt_price(h)}, "
            f"20d {_fmt_price(low_20d)}–{_fmt_price(high_20d)})"
        )
    if not rows:
        return ""
    now = datetime.datetime.now().astimezone()
    stamp = now.strftime("%Y-%m-%d %H:%M %Z")
    return (
        f"\n\n## LIVE PRICE SNAPSHOT (as of {stamp})\n"
        "These are the current prices from the operator's live feed. Use them verbatim "
        "for any 'current price' or 'trading range' claim. NEVER invent, estimate, or "
        "round a price/range — if a symbol is not here, you do not have live data for it "
        "and must say so rather than guess.\n\n"
        + "\n".join(rows)
    )


# Fallback conviction when the report has no "## Verdict / Conviction:" line —
# the action block's direction IS a conviction, expressed in trade language.
_DIRECTION_CONVICTION = {
    "add": "Buy",
    "trim": "Trim",
    "hold": "Hold",
    "watch": "Watch",
    "avoid": "Avoid",
}


def _extract_research_state(
    content: str,
    title: str,
    symbols: list[str],
    gathered: dict[str, dict],
    actions: list[dict] | None = None,
) -> dict:
    """Deterministically extract structured research state from a finished report.

    The writer produces enforced markdown sections (## Verdict, ## The Bull
    Case, ## The Bear Case, ## Positioning). We extract the decision-relevant
    fields from those sections. key_metrics come from the data guard's source
    index (already-verified numbers) — never extracted from prose.

    Graceful fallback: a missing section yields None, never a fabricated value.
    """
    import re as _re

    def _section(name: str) -> str | None:
        m = _re.search(rf"##\s*{name}\s*\n(.*?)(?=\n##\s|\Z)", content, _re.S)
        if not m:
            return None
        text = m.group(1).strip()
        return text[:600] if text else None

    # Conviction from the Verdict section's "Conviction: X" line, or a
    # "**Verdict:** **Buy**" inline style the writer sometimes uses.
    conviction = None
    verdict = _section("Verdict")
    if verdict:
        cm = _re.search(r"Conviction:\s*([A-Za-z /-]+)", verdict)
        if cm:
            conviction = cm.group(1).strip()
        else:
            cm2 = _re.search(r"\*\*Verdict:\*\*\s*\*\*([A-Za-z /-]+)\*\*", verdict)
            if cm2:
                conviction = cm2.group(1).strip()

    # Thesis from the title line (strip the markdown # and trailing dash).
    thesis = None
    t = title.strip().lstrip("#").strip()
    if t:
        thesis = t.split("—")[0].strip() if "—" in t else t

    # Key metrics from the data guard's verified source index.
    key_metrics: dict[str, dict[str, float]] = {}
    try:
        from agent.data_guard import _build_source_index
        key_metrics = _build_source_index(gathered)
    except Exception as e:
        log.warning("Source-index build failed (research state): %s", e)

    # The action block is the report's OWN machine-readable decision, so it
    # outranks a prose section the writer may have phrased differently. Without
    # this fallback a `custom` report — whose spine is Key Finding / Analysis /
    # Implications — landed with conviction, risks and recommendation ALL None,
    # so the reader rendered an empty Research State card. Section regex stays
    # as the first choice; the action block is the floor.
    action = actions[0] if actions else {}
    if not conviction:
        conviction = _DIRECTION_CONVICTION.get((action.get("direction") or "").strip().lower())

    risks = (
        _section("The Bear Case")
        or _section("What Could Go Wrong")
        or _section("Risks")
        or action.get("falsifier")
    )
    recommendation = _section("Positioning") or action.get("recommendation")

    return {
        "thesis": thesis,
        "conviction": conviction,
        "key_metrics": key_metrics,
        "risks": risks,
        "recommendation": recommendation,
        "symbols": symbols,
    }


def _prior_context_block(parent: dict, gathered: dict[str, dict]) -> str:
    """Build the PRIOR RESEARCH STATE + WHAT CHANGED block for a follow-up.

    Carries the parent's structured state (thesis, conviction, verified key
    metrics, risks, recommendation) plus a what-changed delta computed from
    freshly gathered data vs the parent's as-of metrics. This is what makes a
    follow-up build on the prior thesis instead of restarting — and prevents
    stale-thesis drift by surfacing what actually changed.
    """
    state = parent.get("research_state") or {}
    lines: list[str] = []

    thesis = state.get("thesis")
    conviction = state.get("conviction")
    metrics = state.get("key_metrics") or {}
    risks = state.get("risks")
    rec = state.get("recommendation")
    symbols = state.get("symbols") or parent.get("symbols") or []

    lines.append("## PRIOR RESEARCH STATE (from your earlier report)")
    if thesis:
        lines.append(f"Thesis: {thesis}")
    if conviction:
        lines.append(f"Conviction: {conviction}")
    if metrics:
        for sym, m in list(metrics.items())[:5]:
            parts = []
            for k in ("price", "stbr", "pe_ratio", "beta", "rsi"):
                if k in m:
                    parts.append(f"{k}={m[k]:g}")
            if parts:
                lines.append(f"  {sym}: " + " · ".join(parts))
    if risks:
        lines.append(f"Risks: {risks[:300]}")
    if rec:
        lines.append(f"Recommendation: {rec[:300]}")
    if symbols:
        lines.append(f"Symbols: {', '.join(symbols)}")

    # --- What changed since the prior report ---
    changed: list[str] = []
    for sym, m in (metrics or {}).items():
        q = gathered.get(f"get_quote({sym})", {})
        if not q or q.get("error"):
            continue
        cur = q.get("price")
        prev = m.get("price")
        if cur is not None and prev:
            pct = (cur - prev) / prev * 100
            changed.append(f"{sym} price {prev:g} → {cur:g} ({pct:+.1f}%)")
        cur_stbr = q.get("stbr")
        prev_stbr = m.get("stbr")
        if cur_stbr is not None and prev_stbr:
            changed.append(f"{sym} STBR {prev_stbr:g} → {cur_stbr:g}")

    if changed:
        lines.append("\n## WHAT CHANGED SINCE PRIOR REPORT")
        lines.extend(f"- {c}" for c in changed[:8])

    lines.append(
        "\nThe operator is following up on this report. Build on this state — "
        "reference the verified key metrics verbatim, don't restart from scratch, "
        "and do NOT contradict the prior thesis unless the WHAT CHANGED data "
        "justifies it. If you change the conviction, state exactly what new "
        "information changed it."
    )
    return "\n".join(lines)


def _truncate_report(content: str, limit: int = 1500) -> str:
    """Truncate report content at a markdown heading boundary."""
    if len(content) <= limit:
        return content
    cut = content[:limit]
    # Back up to the last heading boundary.
    idx = cut.rfind("\n## ")
    if idx > 0:
        cut = cut[:idx]
    return cut + "\n…[truncated]"


def run_research(
    topic: str,
    research_type: str = "custom",
    symbols: list[str] | None = None,
    abort_flag: Callable[[], bool] | None = None,
    parent_report_id: str | None = None,
) -> Iterator[dict]:
    """Run a journalist research project.

    Architecture:
      Phase 1 — Pre-gather data from Meridian tools in parallel
      Phase 2 — Agentic viz agent (Ollama deepseek-v4-pro) + report writer run
                in PARALLEL, both consuming the gathered data. The writer
                runs on the Ollama-native harness by default (streaming,
                privacy-first — prompts processed transiently, never trained
                on); the Perplexity Agent API path is the explicit fallback
                when llm_provider is "perplexity".
    """
    started = time.perf_counter()

    yield {
        "type": "phase",
        "phase": "researching",
        "description": f"Starting {RESEARCH_TYPES.get(research_type, 'research')}",
    }

    # Phase 1: Pre-gather data (parallel)
    yield {"type": "phase", "phase": "gathering", "description": "Gathering portfolio data"}
    gathered: dict[str, dict] = {}
    data_sections: list[str] = []
    for label, result in _gather_data_parallel(symbols, research_type, topic):
        if abort_flag and abort_flag():
            return
        tool_name = label.split("(")[0]
        yield {"type": "tool_call", "name": tool_name, "args": {}, "id": label}
        yield {"type": "tool_result", "name": tool_name, "result": result, "id": label}
        gathered[label] = result
        snippet = json.dumps(result, default=str)[:2000]
        data_sections.append(f"### {label}\n```json\n{snippet}\n```")

    focus_symbols = _derive_focus_symbols(symbols, research_type, topic)
    data_block = "\n\n".join(data_sections) if data_sections else "(No local data available)"
    data_block += _live_price_snapshot(gathered, focus_symbols)

    # Phase 2: Viz agent + report writer in parallel
    yield {"type": "phase", "phase": "visualizing", "description": "Building visualizations"}

    from concurrent.futures import ThreadPoolExecutor

    from agent.viz_agent import run_viz_agent

    instructions = _build_research_prompt(topic, research_type, focus_symbols)
    brief = _build_research_brief(topic, research_type, focus_symbols)

    provider = compute.current_provider()
    use_ollama_writer = provider == compute.OLLAMA

    # Follow-up context: carry the parent's structured state + what changed,
    # plus the accumulated research memory bank (prior convictions across the
    # thread, filtered to relevant symbols). The memory bank is what lets a
    # follow-up build on everything learned before, not just the immediate
    # parent report.
    prior_block = ""
    if parent_report_id:
        try:
            from data.research_reports import load_report
            parent = load_report(parent_report_id)
            if parent and parent.get("content"):
                prior_block = _prior_context_block(parent, gathered)
                prior_block += "\n\n## PRIOR REPORT (excerpt)\n" + _truncate_report(parent["content"])
        except Exception:
            prior_block = ""

    memory_block = ""
    try:
        from data.findings import research_memory_context
        memory_block = research_memory_context(symbols=focus_symbols, limit=8)
    except Exception as e:
        log.debug("Research memory injection failed: %s", e)

    fact_bank = ""
    try:
        from agent.research_discipline import fact_bank_for
        fact_bank = fact_bank_for(focus_symbols)
    except Exception as e:
        log.debug("Fact-bank injection failed: %s", e)

    user_message = f"""{brief}

## Pre-Gathered Data

{data_block}

{fact_bank}

{memory_block}

{prior_block}

Use this data first. You may call get_quote and the other Meridian data tools to fill gaps (e.g. a price or signal not already in the snapshot), and search_web for external context. Then write the full research report in markdown."""

    def _run_viz() -> list[dict]:
        return list(run_viz_agent(topic, research_type, gathered, abort_flag))

    corrections: list = []
    writer_model: str | None = None
    reviewer_model: str | None = None

    if use_ollama_writer:
        # Ollama-native writer: streams tokens in the foreground while the viz
        # agent runs in parallel. This is the privacy-first default — prompts
        # are processed transiently by Ollama Cloud and never trained on.
        tool_schemas = _ollama_writer_tool_schemas()
        adapted_instructions = _adapt_prompt_for_ollama(instructions)
        try:
            writer_model = compute.writer_model()
            reviewer_model = compute.research_model()
        except Exception:
            pass
        with ThreadPoolExecutor(max_workers=1) as pool:
            viz_future = pool.submit(_run_viz)
            report_events = list(_write_report_ollama(
                topic, research_type, user_message, adapted_instructions,
                tool_schemas, gathered, abort_flag,
            ))
            viz_events = viz_future.result()

        # Yield viz events first (they render as the report streams in)
        for ev in viz_events:
            if ev.get("type") in ("tool_call", "tool_result"):
                yield ev
        for ev in report_events:
            yield ev

        # Reviewer pass: a second model cross-checks the report's numbers
        # against the verified data, surfacing corrections as data_guard
        # events. This is the quality gate that keeps the open model at
        # institutional standard.
        if abort_flag is None or not abort_flag():
            report_event = next((e for e in report_events if e.get("type") == "report"), None)
            if report_event and report_event.get("content"):
                yield {"type": "phase", "phase": "reviewing", "description": "Reviewing report for accuracy"}
                corrections = _review_report(
                    report_event["content"], topic, research_type, gathered
                )
                if corrections:
                    yield {
                        "type": "tool_result",
                        "name": "data_guard",
                        "result": {"corrections": corrections},
                        "id": "reviewer_pass",
                    }
    else:
        # Perplexity Agent API writer (fallback path — kept intact).
        writer_model = MODEL
        agent = compute.get_agent_client()
        tools = _writer_tool_schemas()
        input_items: list[dict] = [{"role": "user", "content": user_message}]

        def _run_report() -> list[dict]:
            events: list[dict] = []
            try:
                items = list(input_items)
                response = None
                for _ in range(MAX_WRITER_ITERATIONS):
                    response = agent.run(
                        model=MODEL,
                        input_items=items,
                        tools=tools,
                        instructions=instructions,
                    )
                    calls = agent.extract_function_calls(response)
                    if not calls:
                        break

                    for call in calls:
                        name = call.get("name", "unknown")
                        args = call.get("arguments", {}) or {}
                        call_id = call.get("call_id", "")
                        events.append({"type": "tool_call", "name": name, "args": args, "id": call_id})
                        try:
                            result = execute_tool(name, args)
                        except Exception as e:
                            result = {"error": f"{type(e).__name__}: {e}"}
                        guarded_result, corrections = guard_tool_result(name, result, gathered)
                        if corrections:
                            events.append({
                                "type": "tool_result",
                                "name": "data_guard",
                                "result": {"corrections": corrections},
                                "id": f"guard_{call_id or 'call'}",
                            })
                        events.append({"type": "tool_result", "name": name, "result": guarded_result, "id": call_id})
                        items.append(call.get("raw", {
                            "type": "function_call",
                            "call_id": call_id,
                            "name": name,
                            "arguments": json.dumps(args),
                        }))
                        items.append({
                            "type": "function_call_output",
                            "call_id": call_id,
                            "output": json.dumps(guarded_result, default=str),
                        })
                else:
                    events.append({"type": "error", "kind": "tool_limit",
                                   "content": f"Report writer exceeded {MAX_WRITER_ITERATIONS} tool iterations."})
                    return events

                text = agent.extract_text(response)
                citations = agent.extract_citations(response)
                if not text:
                    text = response.get("output_text", "")
                if citations:
                    events.append(
                        {"type": "citations", "urls": clean_citations(citations), "results": []}
                    )
                if text:
                    text = strip_narration(text)
                    title = _extract_title(text, topic)
                    events.append({"type": "text", "content": text})
                    events.append({
                        "type": "report",
                        "content": text,
                        "title": title,
                        "research_type": research_type,
                    })
                else:
                    events.append({"type": "error", "kind": "empty_report", "content": "Model returned no text."})
            except Exception as e:
                events.append({"type": "error", "kind": compute.classify_error(e), "content": str(e)})
            return events

        with ThreadPoolExecutor(max_workers=2) as pool:
            # Same contextvar caveat as _gather_data_parallel: pool threads
            # don't inherit the per-request context, so bind it explicitly.
            import contextvars
            ctx = contextvars.copy_context()
            report_future = pool.submit(ctx.copy().run, _run_report)
            viz_future = pool.submit(ctx.copy().run, _run_viz)
            viz_events = viz_future.result()
            report_events = report_future.result()

        # Yield viz events first (they render as the report streams in)
        for ev in viz_events:
            if ev.get("type") in ("tool_call", "tool_result"):
                yield ev
        for ev in report_events:
            yield ev

    # Extract structured research state from the finished report so follow-ups
    # can build on it (thesis, conviction, verified metrics, risks).
    research_state = {}
    report_content = next((e.get("content") for e in report_events if e.get("type") == "report"), None)
    report_title = next((e.get("title") for e in report_events if e.get("type") == "report"), topic)
    actions = extract_actions(report_content or "")
    if report_content:
        research_state = _extract_research_state(
            report_content, report_title, focus_symbols, gathered, actions=actions
        )

    yield {
        "type": "done",
        "elapsed_ms": (time.perf_counter() - started) * 1000.0,
        "symbols": focus_symbols,
        "research_state": research_state,
        # Provenance travels with the report: which model wrote it, which
        # reviewed it, what the reviewer flagged, and the parsed action
        # block. The UI shows all four; the store keeps them.
        "corrections": corrections,
        "writer_model": writer_model,
        "reviewer_model": reviewer_model,
        "actions": actions,
    }


def _build_research_prompt(topic: str, research_type: str, symbols: list[str] | None) -> str:
    now = datetime.datetime.now()
    date_str = now.strftime("%A, %B %d, %Y")
    time_str = now.strftime("%I:%M %p %Z")
    portfolio = _portfolio_context()

    type_guidance = _TYPE_GUIDANCE.get(research_type, _TYPE_GUIDANCE["custom"])

    from agent.research_discipline import inject_discipline
    return _JOURNALIST_PROMPT.format(
        current_date=date_str,
        current_time=time_str,
        portfolio_context=portfolio,
        type_guidance=type_guidance,
        research_discipline=inject_discipline(),
    )


def _build_research_brief(topic: str, research_type: str, symbols: list[str] | None) -> str:
    brief = f"Research brief: {topic}"
    if symbols:
        brief += f"\nFocus symbols: {', '.join(symbols)}"
    brief += f"\nResearch type: {RESEARCH_TYPES.get(research_type, research_type)}"
    brief += "\n\nGather all relevant data using your tools, then write the full research report."
    return brief


def _extract_title(content: str, fallback: str) -> str:
    for line in content.split("\n"):
        line = line.strip()
        if line.startswith("# ") and not line.startswith("## "):
            return line[2:].strip()
        if line.startswith("**") and line.endswith("**") and len(line) < 120:
            return line.strip("*").strip()
    return fallback[:80]


# ---------------------------------------------------------------------------
# Type-specific research guidance
# ---------------------------------------------------------------------------

_TYPE_GUIDANCE = {
    "stock_deep_dive": """RESEARCH TYPE: STOCK DEEP DIVE

You are writing a comprehensive single-name research report. Use these tools in sequence:
1. get_quote — current price and trading data
2. get_fundamentals — valuation, sector, market cap
3. get_signals — full technical picture (RSI, MACD, SMA, composite)
4. get_stbr — bubble risk assessment
5. get_risk_metrics — beta, VaR, drawdown history
6. get_insider_activity — smart money signal
7. get_position_intel — MA distances, vol regime, relative strength
8. get_option_chain_summary — options market sentiment
9. Use finance_search for: recent earnings, analyst consensus, revenue guidance
10. Use web_search for: competitive dynamics, catalysts, management commentary

VISUALIZATION:
- **Network:** Call visualize_network with the target company at center (size: 1.0), surrounded by suppliers, customers, competitors, and beneficiaries. Groups: "Target", "Suppliers", "Customers", "Competitors". Use directional edges for supply chain flows. 15-20 nodes.
- **Charts:** Call visualize_chart for: revenue/earnings trend (line), peer valuation comparison (grouped_bar with P/E, EV/EBITDA), margin trends (line), capex/R&D spend if relevant (bar). Use real numbers from finance_search.
- **Tables:** Call visualize_table for: peer comparison table (revenue, margins, growth, valuation multiples across competitors).

REPORT STRUCTURE:
# [Company Name] ([TICKER]) — [One-Line Thesis]

## Verdict
Conviction: [Strong Buy / Buy / Hold / Trim / Sell]
One paragraph: what you'd do with this name and why.

## The Setup
Current price, valuation metrics, where it sits technically. What's the market pricing in?

## What the Data Says
Technical signals, STBR, risk metrics, insider activity. Let the numbers tell the story.

## The Bull Case
What gets this name meaningfully higher? Be specific about catalysts and timing.

## The Bear Case
What breaks the thesis? Where are you wrong?

## Value Chain Position
Where does this company sit in its ecosystem? Who benefits if it wins? Who's at risk?

## Positioning
If held: size appropriate? Trim or add?
If not held: entry criteria, sizing, stop level.

## Sources
Cite specific data points and web sources.""",

    "thematic": """RESEARCH TYPE: THEMATIC / VALUE CHAIN RESEARCH

You are writing a thematic investment research piece that maps an investment trend to specific, actionable opportunities. Use these tools:
1. map_value_chain — web research on the theme's value chain
2. sector_rotation — which sectors are seeing inflows
3. For each promising name: get_quote, get_fundamentals, get_signals, get_stbr
4. find_relationships — statistical linkages between theme beneficiaries
5. Use finance_search for: market sizing, earnings estimates, institutional ownership
6. Use web_search for: adoption curves, regulatory catalysts, supply constraints

VISUALIZATION:
- **Network:** Call visualize_network with a multi-tier value chain. Groups: "Tier 1 — Direct", "Tier 2 — Infrastructure", "Tier 3 — Supply Chain", "Tier 4 — Second-Order". Use directional edges to show capex/revenue flow. Label edges with relationship type. 20-30 nodes. Size by market cap relevance.
- **Charts:** Call visualize_chart for: capex/spending trends across key players (grouped_bar), market size/TAM growth (area or line), revenue exposure breakdown by company (bar). Use real numbers. This is CRITICAL for thematic research — the reader needs to SEE the capital flows.
- **Tables:** Call visualize_table for: company comparison across tiers (market cap, revenue, growth rate, theme exposure %, valuation).

REPORT STRUCTURE:
# [Theme Name] — Investment Thesis

## Verdict
One paragraph: is this investable now? Early, mid, or late cycle?

## The Thesis
What structural shift is driving this? Why does it matter for capital allocation? Size the opportunity.

## Value Chain Map
### Tier 1: Direct Beneficiaries
[Companies directly selling into this theme. For each: ticker, why, current setup]
### Tier 2: Infrastructure & Suppliers
[Companies supplying Tier 1. Often the better risk/reward.]
### Tier 3: Second-Order Plays
[Power, cooling, construction, logistics — what consensus hasn't priced]

## Bottlenecks & Moats
Where are the supply constraints? Who controls them? This is where pricing power lives.

## What Could Go Wrong
Regulatory risk, demand assumptions, competitive dynamics, timing risk.

## The Portfolio Play
Specific recommendations: which names, what sizing, entry criteria. How does this theme interact with current holdings?

## Sources
Cite data and web sources.""",

    "portfolio_review": """RESEARCH TYPE: PORTFOLIO REVIEW

You are writing a comprehensive portfolio health check. Use these tools:
1. get_portfolio_summary — account state
2. get_account_breakdown — cash and balances by location (per brokerage + external institutions)
3. get_positions — all holdings with weights
4. get_risk_momentum — opportunity scores + sizing deltas for every position
5. get_macro_environment — macro backdrop
6. sector_rotation — sector flow context
7. find_relationships — correlation clusters across holdings
8. For any flagged positions: get_signals, get_stbr
9. Use finance_search for: earnings calendar, analyst sentiment on flagged names

VISUALIZATION:
- **Network:** Call visualize_network showing all portfolio holdings grouped by sector. Size nodes by portfolio weight. Add edges between correlated holdings (strength = correlation). Groups = sector names.
- **Charts:** Call visualize_chart for: portfolio weight allocation (bar), sector exposure (bar), P&L contribution by holding (waterfall).
- **Tables:** Call visualize_table for: full position book with key metrics (weight, P&L, score, STBR, action recommendation).

REPORT STRUCTURE:
# Portfolio Review — [Date]

## Executive Summary
NAV, P&L, cash position, overall health assessment. One paragraph verdict.

## Position Book
Table of all positions with key metrics. Flag anything that needs attention.

## Risk Assessment
Concentration (HHI), correlation clusters, sector exposure, beta profile. Where are you vulnerable?

## What to Do
### Add
Positions that deserve more capital. Why, and how much.
### Trim
Positions that are overweight, overextended, or have deteriorating setups.
### Watch
Positions at inflection points that need monitoring.

## Macro Context
How does the current regime affect your portfolio? Positioning implications.

## Sources
Cite specific metrics from tools.""",

    "opportunity_scan": """RESEARCH TYPE: OPPORTUNITY SCAN

You are scanning the portfolio and watchlist for the best risk/reward setups right now. Use these tools:
1. get_risk_momentum — ranked opportunity scores across all holdings
2. get_positions — current weights and P&L
3. For the top 5 opportunities: get_quote, get_fundamentals, get_signals, get_stbr
4. sector_rotation — sector momentum context
5. get_macro_environment — regime context
6. Use finance_search for: upcoming earnings, analyst revisions on top picks

CURRENT PRICES: The LIVE PRICE SNAPSHOT in your data has the live price + 52w/20d ranges for the top picks, captured at a timestamp. Cite those verbatim for any "current price" or "trading range" claim. Never invent or round a price — if a symbol's price isn't in the snapshot, call get_quote to fetch it live.

VISUALIZATION:
- **Network:** Call visualize_network showing top opportunities and their sector context. Groups: "Top Picks", "Related Plays", "Current Holdings". Size by opportunity score. Directional edges from catalysts to beneficiaries.
- **Charts:** Call visualize_chart for: opportunity scores ranked (bar), risk/reward scatter — use a grouped_bar with score vs STBR per symbol.
- **Tables:** Call visualize_table for: top picks comparison (score, RSI, STBR, entry price, target weight, rationale).

REPORT STRUCTURE:
# Opportunity Scan — [Date]

## Top Opportunities
Ranked list. For each:
- Ticker, current price, composite score
- Why this is a setup (technical + fundamental confluence)
- Entry criteria, sizing guidance, risk level
- What could go wrong

## Portfolio Sizing Adjustments
Based on risk-momentum scores: what to add, trim, or close. Dollar amounts.

## Regime Context
How does the current market environment affect these setups?

## Sources
Cite tool data.""",

    "macro_brief": """RESEARCH TYPE: MACRO BRIEF

You are writing a market environment analysis with portfolio positioning implications. Use these tools:
1. get_macro_environment — yield curve, credit spreads, fed funds
2. sector_rotation — sector momentum and market regime
3. get_portfolio_summary + get_positions — current exposure
4. Use finance_search for: economic data releases, Fed commentary, treasury auctions
5. Use web_search for: geopolitical developments, trade policy, market sentiment

VISUALIZATION:
- **Network:** Call visualize_network mapping macro factors to affected sectors and holdings. Groups: "Macro Drivers", "Sector Impact", "Portfolio Exposure". Directional edges showing transmission (e.g., "rate sensitive", "USD exposed").
- **Charts:** Call visualize_chart for: yield curve shape (line), sector momentum scores (bar), credit spread trend (line or area).
- **Tables:** Call visualize_table for: regime indicators (metric, current value, signal, portfolio implication).

REPORT STRUCTURE:
# Macro Brief — [Date]

## Regime Classification
RISK_ON / RISK_OFF / SELECTIVE / TRANSITIONAL — and why. What changed recently?

## Rates & Credit
Yield curve shape, credit spreads, what they're signaling. Rate path expectations.

## Sector Flows
Where is capital rotating? Which sectors are accelerating, which are rolling over?

## What This Means for the Portfolio
Specific implications for current holdings. Positioning adjustments.

## Risks on the Horizon
What could shift the regime? Timeline for key events.

## Sources
Cite macro data and web sources.""",

    "custom": """RESEARCH TYPE: CUSTOM RESEARCH

You are answering a freeform research question with institutional rigor. Use whatever tools are relevant to produce a comprehensive, evidence-backed answer. Leverage finance_search and web_search for real-time context.

VISUALIZATION:
- **Network:** Call visualize_network to map the key entities, relationships, and flow of your research topic. Choose groups that reflect the structure of the answer. 15-25 nodes.
- **Charts:** Call visualize_chart for any quantitative data you discover — spending trends, growth comparisons, market sizing, financial metrics over time. At least 1-2 charts minimum.
- **Tables:** Call visualize_table when comparing 3+ entities across multiple metrics.

REPORT STRUCTURE:
# [Title derived from the question]

## Verdict
Conviction: [High / Medium / Low]
One paragraph: the direct answer to the question, and what to do about it.

## Key Finding
Lead with the most important conclusion.

## Analysis
Structured evidence supporting your finding. Use data from tools and web sources.

## Implications
What does this mean for the portfolio? What action follows?

## What Could Go Wrong
The specific, dated observation that would prove this wrong. Name the number and
the release that would print it.

## Sources
Cite data and web sources.""",
}


# ---------------------------------------------------------------------------
# Core journalist system prompt
# ---------------------------------------------------------------------------

_JOURNALIST_PROMPT = """You are the Journalist — Meridian's investment research analyst. Growth-minded, opinionated, institutional-grade. Today is {current_date} {current_time}.
{portfolio_context}

CRITICAL WORKFLOW — follow this exact sequence:
1. Call get_quote, get_fundamentals, get_signals for each relevant symbol
2. Use web_search and finance_search for real-time data (capex, revenue, earnings)
3. Call visualize_chart with real numbers (capex comparison, revenue trends, etc.)
4. Call visualize_table for multi-company metric comparisons
5. Call visualize_network mapping the value chain / relationships
6. Write the full research report in markdown
7. Call suggest_followups with 2-4 next research steps

You MUST call your custom tools (get_quote, get_fundamentals, visualize_chart, etc.) — do NOT skip them. A report without tool calls is a failed report.

VISUALIZATION TOOLS — use all four:
- visualize_chart: bar/grouped_bar/line/area/waterfall. Use for ANY numeric comparison (capex, revenue, growth rates, margins). data=[{{label, values:[{{x,y}}]}}]. Include real numbers.
- visualize_money_flow: SANKEY-STYLE money flows. nodes=[{{id,label,group}}], flows=[{{source,target,value,label}}]. Use for: where capex lands (hyperscaler -> layer -> line item), supplier revenue concentration, the incremental-dollar path, segment mix. Direction = money direction; keep every flow on ONE basis ($ or %).
- visualize_table: columns=[{{key,label,format}}] rows=[{{...}}]. format: currency/percent/number/text. highlight: max/min. Use for peer comparisons.
- visualize_network: nodes=[{{id,label,group,size,description}}] edges=[{{source,target,label,strength,directional}}]. Map value chains, competitive landscapes (this is the competitive map - make it the report's centerpiece, lay it out FIRST and large).

CHART DESIGN RULES (a mis-specified chart is a wrong chart, not a style choice):
- NEVER chart raw price levels side-by-side across companies. A $1,088 stock next to a $237 stock reads as one bar and one sliver. Cross-company price position is a RATIO: chart "percent of 52-week range" = (price - low) / (high - low) * 100, labeled "% of 52w range", one series. Range data (current/high/low per company) belongs in the x-axis category names or a table, not three bars of raw dollars.
- NEVER repeat a value as a placeholder: if high == current for a company, chart the gap as 0 — never fabricate a distinct bar. A "Current vs High vs Low" chart where three series print the same value for the same x is a data error; check get_quote's high_52w/low_52w and use the REAL range.
- ALWAYS set x_label/y_label to what the axis actually shows ("Company", "% of 52-week range" - never bare "Company" under a percentage chart).
- Normalize per-company comparisons to a common basis: % of range, forward P/E, revenue growth %. Raw dollars only when the companies are on comparable scales.

STANDARDS: Every claim grounded in tool data or cited sources. Lead with thesis + conviction (High/Medium/Low). Be specific with numbers. Write for a professional fund manager.

{research_discipline}

PRICE ACCURACY — NON-NEGOTIABLE:
- The LIVE PRICE SNAPSHOT block in your data contains the current price, 52-week range, and 20-day range for each focus symbol, captured at a specific timestamp. Use those numbers verbatim for any current-price or trading-range claim.
- NEVER invent, estimate, interpolate, or round a price/range from memory or model knowledge. A stale or made-up price is worse than no price.
- If a symbol's price is not in the LIVE PRICE SNAPSHOT, either call get_quote to fetch it live or state "no live price available" — never guess.
- Always label a price with its context: current (as of the snapshot time), 52-week range, or 20-day range. Don't blur them into a vague "trading range".

TOOL ERRORS: tool failures return {{"error": "...", "recoverable": bool}}. If recoverable is true, the failure is data availability or transient — retry once or skip and note what's missing. If recoverable is false, it's a hard failure — explain it and move on. Never fabricate the missing data.

{type_guidance}

SOURCES — EVERY CITATION EARNS ITS PLACE:
- A source exists to support a SPECIFIC number or claim. Cite the deep link. Never
  cite a bare domain root, a search-landing page, a definition or reference page
  (a Wikipedia article on the word "market"), a directory or event listing, or a
  social video/post — those are search results you never opened, not evidence.
- When two sources disagree on the same figure (two prediction markets quoting
  different odds for the SAME event), say so in the prose and reconcile or discard
  one. Never print both and then conclude as though they agreed.
- Meridian's own data is cited by TOOL NAME (get_quote, get_signals, Meridian
  macro), never as a URL. Every external number carries a real link.

ACTION BLOCK — REQUIRED FINAL SECTION:
End every report with a "## Proposed Action" section whose body is one fenced json block:
```json
{{"recommendation": "<what to do, one sentence, naming the instrument>", "direction": "add|trim|hold|watch|avoid", "size": "<size and the constraint it respects: buying power, cash floor, concentration>", "falsifier": "<the specific dated observation that would prove this wrong>", "horizon": "<e.g. through Q3 earnings, ~3 months>"}}
```
The app parses this block into an actionable item the operator can accept into the journal — it must be the report's FINAL fenced block and valid JSON. If the honest answer is "no action", use direction "hold" or "watch" and say why. Never omit it.

Write concisely. Bold key numbers. End with actionable positioning. Do NOT write follow-up questions inline — use suggest_followups tool instead."""
