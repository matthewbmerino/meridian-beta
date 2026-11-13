# Agent Layer — AI Research & Intelligence

Read root `AGENTS.md` first for product vision, verification gates, and the Autonomy Contract.

Two agents: **Compass** (conversational) and **Journalist** (structured research reports), plus an agentic viz agent that decides what to visualize.

**Ollama-first.** Default provider `ollama`. Perplexity only when `llm_provider=perplexity`. Prompts are transient, never stored or trained on.

## Architecture

| File | Purpose |
|------|---------|
| `compute.py` | Provider abstraction (`OpenAICompatClient`) for Ollama + Perplexity; model-catalog fallback chains; dispatch |
| `loop.py` | Compass streaming loop (events: turn_start, token, tool_call, tool_result, text, citations, done; max 12 iterations) |
| `journalist.py` | Research agent: parallel data gathering, Ollama-native writer + reviewer; Perplexity fallback |
| `tools.py` | Tool registry (31 tools at runtime): `TOOLS` dict, `get_tool_schemas()`, `execute_tool()` |
| `tools_search.py` | Web tools: Firecrawl `search_web` (primary), DuckDuckGo fallback, `fetch_page` (SSRF-guarded), news/earnings |
| `viz_agent.py` | Agentic viz agent (Ollama `deepseek-v4-pro`): decides canvas artifacts; deterministic fallback |
| `data_guard.py` | Accuracy guarantee: cross-checks every emitted value; overrides >5% deviations, drops unverifiable values, logs corrections |
| `prompts.py` | Compass system prompt (portfolio context + recent findings) |
| `history.py` | Chat history management |

## Model Tiers

| Tier | Provider | Model | Used By |
|------|----------|-------|---------|
| Chat | Ollama | `deepseek-v4-flash` | Compass |
| Research/viz | Ollama | `deepseek-v4-pro` | Viz agent, Journalist writer + reviewer |
| Writer tuning | Ollama | configurable via `ollama_writer_model` | Journalist writer |
| Fallback | Perplexity | `sonar-reasoning-pro` / `sonar-pro` | Only when `llm_provider=perplexity` |

## Compass — Conversational Analyst

Standard: senior analyst briefing a PM — current data, technicals, risk contribution, sizing, macro, citations. Concise, opinionated, specific.

## Journalist — Research Reports

Standard: credible, concise research. Every claim grounded in tool data or cited web sources. Research types: **Stock Deep Dive**, **Thematic**, **Portfolio Review**, **Opportunity Scan**, **Macro Brief**, **Custom**.

Research canvas renders agentic artifacts: graph, charts, tables, pinned metrics, annotations, callouts, layout.

## Core Tool Set

Market: `get_quote`, `get_price_history`, `search_symbol`, `get_fundamentals`, `get_option_chain_summary`, `get_company_news`, `get_stockanalysis`.
Portfolio: `get_portfolio_summary`, `get_account_breakdown`, `get_liabilities`, `get_debt_analysis`, `get_positions`, `preview_trade`.
Analytics: `get_signals`, `get_stbr`, `get_risk_metrics`, `get_risk_momentum`, `get_factor_exposures`, `get_position_intel`, `sector_rotation`, `find_relationships`, `get_compute_index`, `map_value_chain`.
External: `get_insider_activity`, `get_macro_environment`.
Viz/canvas: `visualize_network`, `visualize_chart`, `visualize_table`, `highlight_metric`, `add_annotation`, `canvas_layout`, `insight_callout`, `suggest_followups`.
Web: `search_web`, `fetch_page`.

## Data Guard

Every value emitted via `visualize_*` / `highlight_metric` passes through `data_guard.guard_tool_call()`. Deviations >5% are overridden; unverifiable values are dropped; every correction is logged as a `data_guard` tool_result. Raw numbers are pre-formatted so the model copies verbatim.

## How the Agents Should Reason

1. **Always ground in data.** Never state a price, P&L, or risk metric from memory.
2. **Be portfolio-aware.** Check whether a symbol is held and at what weight.
3. **Chain tools for depth.** A good risk question uses 5+ tools.
4. **Be opinionated, not certain.** "I'd trim here because...", with inputs and confidence.
5. **Never fabricate an order.** `preview_trade` is estimate-only; no execution tool exists.
6. **Respect the framework:** Sortino over Sharpe, Kelly sizing, STBR bubble risk.
7. **Think in value chains** and surface hidden relationships.
8. **Emit visualizations** when the answer has a structural shape.

## Tool Development Conventions

- Signature: `tool_{name}(args: dict) > dict`
- Return JSON-serializable dicts. Use `_clean_dict()` / `_safe()` helpers.
- Catch exceptions; return `{"error": "message"}`. Never raise through the tool boundary.
- OpenAI function-calling schema in `TOOLS` dict.
- New tool: add to `TOOLS` → add schema → update prompts → add test.
- Viz tools must pass through `data_guard.guard_tool_call()` before yielding.

## Verification

```bash
venv/bin/python -m pytest tests/ -q   # must be green
venv/bin/ruff check .                 # must exit 0
curl -s http://127.0.0.1:8001/api/meta/data-health
```

**Rules:** add a test for a new tool; fix code when a test fails; every emitted value passes through `data_guard`.

## Cross-Reference

- Computation functions: `analysis/AGENTS.md`
- Serving endpoints: `backend/AGENTS.md`
- Frontend event handling: `frontend/AGENTS.md`
- History: `journal/agent.md`

## Recent Changes

Full dated history: `journal/agent.md`. Read when you need context, not every session.
