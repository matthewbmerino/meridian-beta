"""Agentic visualization agent — lets the research model decide what to visualize.

The deterministic `_build_visualizations` in journalist.py produces the same
generic charts for every report. This agent runs an Ollama research model
(deepseek-v4-pro) with the `visualize_network/chart/table` tools and lets it
decide what best communicates the research — a value-chain network for a
thematic report, a peer-valuation table for a deep dive, a capex-trend chart
for AI infrastructure.

Runs in parallel with the report writer (both consume the same gathered data).
If the agent under-produces (no charts, or fewer than 2 artifacts), the
deterministic `_build_visualizations` supplements the canvas from the gathered
data so a report never ships without figures. Follow-up suggestions are always
ensured (the agent rarely emits them, and they power the Continue Research UI).
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator

from agent import compute
from agent.tools import execute_tool, get_tool_schemas

MAX_ITERATIONS = 6

_VIZ_TOOLS = [
    "visualize_network", "visualize_chart", "visualize_table",
    "highlight_metric", "add_annotation", "canvas_layout", "insight_callout",
]

_SYSTEM_PROMPT = """You are the data visualization analyst inside Meridian, a portfolio analytics terminal.

You are given a research brief and the data gathered for it. Your job: build a research canvas that a fund manager can absorb in seconds. Emit visualizations AND the canvas layout that arranges them into a narrative.

MESSAGE-DRIVEN VISUALIZATION — the figures must serve the report's thesis, not fill a template:
1. Read the research brief. Ask: what is the ONE comparison or trend that best supports this thesis? That is your centerpiece figure.
2. Emit 2-4 visualizations that genuinely help a fund manager understand the research. Do NOT emit generic filler.
3. ALWAYS emit at least one visualize_chart and at least one visualize_table when the data supports it. A report with only a network graph is under-visualized.
4. For a stock deep dive: peer valuation comparison (table), price/technical context (chart), competitive positioning (network).
5. For a thematic report: the value chain (network with tiers), capex/spending trends (chart), company comparison across tiers (table).
6. For a portfolio review: weight allocation (chart), sector exposure (chart), position book (table), correlation clusters (network).
7. For a macro brief: rate/yield trends (chart), factor transmission (network), regime summary (table).
8. Use REAL numbers from the provided data. Never invent figures.
9. visualize_network: nodes = [{id, label, group, size, description}], edges = [{source, target, label, strength, directional}]. Size by market cap/relevance. Groups = tiers/sectors.
10. visualize_chart: data = [{label, values: [{x, y}], color?}]. ALWAYS set x_label (what the x-axis measures) and y_label (with the unit) and value_format (usd/pct/days/number/multiple) — the axes and the tooltip read in that unit.

FIGURE CRAFT — a chart is an argument, not a colour block:
10a. line/area for anything over time. Set `scale: "log"` for multi-decade growth and multi-cycle overlays.
10b. Use `bands` to shade the WINDOW the argument depends on (a cycle phase, a recession, a regime): [{from, to, label, tone}] with tone window/danger/positive/neutral. A shaded window turns a squiggle into a claim about time.
10c. Use `markers` to name the two or three points the reader must see — the low, today, the level that decides the thesis: [{x, y, label, tone}]. Labels under 24 characters.
10d. `range` (two series: low bound then high bound) for SCENARIOS and price/guidance zones. Never draw six solid bars to show six estimates — the reader reads area, and area is not the message. A floating zone shows the uncertainty itself.
10e. `timeline` (one series per row, exactly two x values = start and end, ISO dates) for schedules: phases, halvings, entry and exit tranches. This is the ONLY correct type for a calendar.
10f. `heatmap` (one series per row, one cell per x) for returns by month, scenario matrices, any row x column grid of numbers.
10g. `waterfall` for sequential contributions (P&L attribution, build-up to a total).
10h. Prefer ONE chart that carries the central comparison over four that decorate it. Two bars of the same metric in different colours is filler.
11. visualize_table: columns = [{key, label, format: currency/percent/number/text}], rows = [{key: value}], highlight = max/min/''.

DATA HONESTY — the ONLY numbers you may display come from the Gathered Data block above:
12. Every figure you emit must be built from values in the Gathered Data block. Never invent a ticker, metric, or number that is not in that block.
13. For peer/comparison tables, you may ONLY include symbols present in the Gathered Data. Do NOT summon peers (e.g. "BX", "APO") that were not gathered — a table with rows for symbols you have no data for will have every cell stripped and be thrown away. It is better to emit no table than a table of empty cells.
14. If a metric (e.g. AUM, forward P/E, revenue growth) is not in the Gathered Data, do not include that column.

CHART TYPE VARIETY — a canvas of identical chart types is a failed visualization:
15. Emit at least one line or area chart for a TIME TREND and at least one bar or grouped_bar chart for a CROSS-SECTIONAL comparison. Do not emit more than one bar chart of the same category.
16. A stock deep dive MUST include a price-trend line/area chart using the get_price_history series (the last ~24 daily closes). Use line when the trend is the message, area when you want to emphasize the level envelope.
17. Use the get_stbr series to draw a line chart of bubble risk over time when STBR is decision-relevant.
18. Charts of the same category must differ in the QUESTION they answer (e.g. price trend vs insider flow vs key levels) — never two bar charts of the same metric.

PEER COMPARISON — a deep dive is a position against its competitors, not a single-name island:
19. A stock deep dive MUST include a peer comparison table (and, when the data supports it, one cross-sectional bar chart — market cap, P/E, or dividend yield) across the PEERS in the Gathered Data. The peers are the symbols beyond the focus name (e.g. for KKR the peers are BX, APO, ARES, CG, TPG, OWL).
20. Only include symbols present in the Gathered Data. The peer set is already gathered for you — copy their metrics verbatim.

CANVAS LAYOUT (emit after the visualize_* calls):
21. highlight_metric: pin 3-5 decision-relevant numbers to the metrics strip (beta, STBR, drawdown, insider selling, valuation). label + formatted value + optional color (positive/negative/warning).
22. add_annotation: draw support/resistance/target/stop lines on charts where levels matter. chart_id must match a visualize_chart title.
23. insight_callout: emit ONE highlighted takeaway box — the single most important insight the fund manager must not miss.
24. canvas_layout: emit ONCE, arranging all artifacts into a narrative reading top-to-bottom: thesis (graph) → evidence (charts/tables) → risk (callout). Each artifact: {id (matches a visualize_* title, or 'graph' for the network), type (graph/chart/table/callout), x (0-11), y (0-N), w (1-12), h (1-4), connects_to (artifact ids it links to)}. The graph is the centerpiece — give it the largest footprint. Connect related artifacts so clicking a node highlights its evidence.

Emit the tool calls. Do not write prose — the report writer handles that."""


def _preformat_data(gathered: dict[str, dict]) -> str:
    """Build a compact, pre-formatted data block for the model.

    Raw numbers are converted to human-readable strings (e.g. market_cap
    5309727620000.0 -> "$5.31T") so the model copies them verbatim instead of
    doing arithmetic. Error results are filtered out so the model never sees
    broken-tool output.
    """
    data_sections: list[str] = []
    for label, result in gathered.items():
        if not isinstance(result, dict):
            continue
        if result.get("error") or result.get("available") is False:
            continue
        # Pre-format known large/currency fields
        formatted = dict(result)
        for key in ("market_cap", "sell_value", "buy_value"):
            if isinstance(formatted.get(key), (int, float)):
                formatted[key] = _fmt_money(float(formatted[key]))
        for key in ("price", "high_52w", "low_52w", "avg_cost", "current_price"):
            if isinstance(formatted.get(key), (int, float)):
                formatted[key] = f"${float(formatted[key]):,.2f}"
        snippet = json.dumps(formatted, default=str)[:1500]
        data_sections.append(f"### {label}\n```json\n{snippet}\n```")
    return "\n\n".join(data_sections) if data_sections else "(No local data available)"


def _fmt_money(v: float) -> str:
    abs_v = abs(v)
    if abs_v >= 1e12:
        return f"${v / 1e12:.2f}T"
    if abs_v >= 1e9:
        return f"${v / 1e9:.2f}B"
    if abs_v >= 1e6:
        return f"${v / 1e6:.2f}M"
    return f"${v:,.0f}"


def _derive_symbols_from_gathered(gathered: dict[str, dict]) -> list[str]:
    """Extract the focus symbols from the gathered data keys (get_quote(SYM))."""
    syms: list[str] = []
    for label in gathered:
        if label.startswith("get_quote(") and label.endswith(")"):
            sym = label[len("get_quote("):-1]
            if sym and sym not in syms:
                syms.append(sym)
    return syms


def _supplement_fallback(
    topic: str,
    research_type: str,
    gathered: dict[str, dict],
    symbols: list[str],
    emitted_names: list[str],
    emitted_titles: set[str],
    emitted_chart_types: set[str],
    abort_flag: Callable[[], bool] | None,
) -> Iterator[dict]:
    """Supplement the canvas with deterministic figures when the agent
    under-produced. Fires when the agent emitted no charts or no tables (the
    Continue Research suggestions are ensured separately and are not a
    blocking gap). Charts are added only to fill missing categories — a report
    the agent already gave a price trend, a table, and a network does not get
    the generic template bars tacked on."""
    from agent.journalist import _build_visualizations

    has_chart = "visualize_chart" in emitted_names
    has_table = "visualize_table" in emitted_names
    has_network = "visualize_network" in emitted_names
    has_followups = "suggest_followups" in emitted_names

    # Chart-type coverage: line/area are the time-trend shapes; a report with
    # none of them is under-visualized for any multi-day research. Track each
    # shape separately so the price-trend area and the STBR line (two distinct
    # questions) aren't collapsed into one slot.
    has_line = "line" in emitted_chart_types
    has_area = "area" in emitted_chart_types
    has_bar = "bar" in emitted_chart_types or "grouped_bar" in emitted_chart_types

    if has_chart and has_table and (has_line or has_area):
        return

    for ev in _build_visualizations(gathered, symbols, research_type, topic):
        if abort_flag and abort_flag():
            return
        if ev.get("type") != "tool_call":
            continue
        name = ev.get("name", "")
        args = ev.get("args", {}) or {}
        title = args.get("title", "")
        chart_type = args.get("chart_type") if name == "visualize_chart" else None

        # Skip what the agent already covered. Charts/tables dedup by title
        # (the fallback price chart shares the agent's title); the network and
        # followups are skipped wholesale to avoid duplicates.
        if name == "visualize_network" and has_network:
            continue
        if name == "suggest_followups" and has_followups:
            continue
        if title and title in emitted_titles:
            continue
        # Only fill category gaps: a time-trend area is added only when the
        # agent produced no area, a line only when it produced no line, a bar
        # only when it produced no bar, a table/network only when it produced
        # none. The fallback never tacks template figures onto a category the
        # agent already covered.
        if name == "visualize_chart":
            if chart_type == "area" and has_area:
                continue
            if chart_type == "line" and has_line:
                continue
            if chart_type in ("bar", "grouped_bar") and has_bar:
                continue
        if name == "visualize_table" and has_table:
            continue

        from agent.data_guard import guard_tool_call
        guarded_args, corrections = guard_tool_call(name, args, gathered)
        if corrections:
            yield {
                "type": "tool_result",
                "name": "data_guard",
                "result": {"corrections": corrections},
                "id": "guard_fallback",
            }
        # A dropped table (guarded args are None) is never emitted.
        if guarded_args is None:
            continue
        yield {"type": "tool_call", "name": name, "args": guarded_args, "id": "viz_fallback"}
        result = execute_tool(name, guarded_args)
        yield {"type": "tool_result", "name": name, "result": result, "id": "viz_fallback"}
        if title:
            emitted_titles.add(title)
        if name == "visualize_chart":
            if chart_type == "area":
                has_area = True
            elif chart_type == "line":
                has_line = True
            elif chart_type in ("bar", "grouped_bar"):
                has_bar = True


def run_viz_agent(
    topic: str,
    research_type: str,
    gathered: dict[str, dict],
    abort_flag: Callable[[], bool] | None = None,
    symbols: list[str] | None = None,
) -> Iterator[dict]:
    """Run the agentic viz agent. Yields tool_call/tool_result events.

    Falls back to deterministic visualizations if the agent produces nothing
    valid. Every emitted tool call passes through the Data Guard
    (agent/data_guard.py) so the model cannot ship an unverifiable or wrong
    number.
    """
    started = time.perf_counter()

    if not symbols:
        symbols = _derive_symbols_from_gathered(gathered)

    # Build a compact, pre-formatted data block for the model
    data_block = _preformat_data(gathered)

    user_message = f"""Research brief: {topic}
Research type: {research_type}

## Gathered Data

{data_block}

Decide what to visualize and emit the visualize_* tool calls. Copy values EXACTLY as given — never recompute, round, or convert them."""

    try:
        client, model = compute.get_client_for_research()
    except Exception as e:
        yield {"type": "error", "kind": compute.classify_error(e), "content": str(e)}
        return

    viz_schemas = [
        t for t in get_tool_schemas()
        if t.get("function", {}).get("name") in _VIZ_TOOLS
    ]

    emitted = 0
    emitted_names: list[str] = []
    emitted_titles: set[str] = set()
    emitted_chart_types: set[str] = set()
    try:
        response = client.chat(
            model=model,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_message},
            ],
            tools=viz_schemas,
            stream=False,
            reasoning_effort="high",
            role="research",
        )
        tool_calls = (response.get("message") or {}).get("tool_calls") or []
        for tc in tool_calls:
            if abort_flag and abort_flag():
                return
            func = tc.get("function", {})
            name = func.get("name", "")
            if name not in _VIZ_TOOLS:
                continue
            try:
                args = func.get("arguments", {})
                if isinstance(args, str):
                    args = json.loads(args) if args else {}
            except json.JSONDecodeError:
                args = {}

            # Data Guard: verify every emitted value against the gathered data.
            # The model cannot ship an unverifiable or wrong number.
            from agent.data_guard import guard_tool_call
            guarded_args, corrections = guard_tool_call(name, args, gathered)
            if corrections:
                yield {
                    "type": "tool_result",
                    "name": "data_guard",
                    "result": {"corrections": corrections},
                    "id": f"guard_{emitted}",
                }
            # A dropped table (guarded args are None) is never emitted.
            if guarded_args is None:
                continue
            yield {"type": "tool_call", "name": name, "args": guarded_args, "id": f"viz_agent_{emitted}"}
            result = execute_tool(name, guarded_args)
            yield {"type": "tool_result", "name": name, "result": result, "id": f"viz_agent_{emitted}"}
            emitted += 1
            emitted_names.append(name)
            if name == "visualize_chart":
                ct = guarded_args.get("chart_type") if isinstance(guarded_args, dict) else None
                if ct:
                    emitted_chart_types.add(str(ct))
            if isinstance(guarded_args, dict) and guarded_args.get("title"):
                emitted_titles.add(guarded_args["title"])
    except Exception as e:
        yield {"type": "error", "kind": compute.classify_error(e), "content": str(e)}

    # Supplement when the agent under-produced (no charts/tables/followups).
    yield from _supplement_fallback(
        topic, research_type, gathered, symbols,
        emitted_names, emitted_titles, emitted_chart_types, abort_flag,
    )

    yield {"type": "viz_done", "emitted": emitted, "elapsed_ms": (time.perf_counter() - started) * 1000.0}
