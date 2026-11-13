"""System prompts for Compass — calibrated for Perplexity sonar-pro.

`get_system_prompt()` returns the Compass system prompt with portfolio
context injected at build time so the model has the operator's holdings
from the first message — no wasted tool call.
"""

import datetime
import logging

log = logging.getLogger(__name__)


def _portfolio_context() -> str:
    """Build a compact portfolio snapshot for system prompt injection."""
    try:
        from data.account import get_combined_positions, get_combined_summary
        positions = get_combined_positions()
        if positions.empty:
            return ""
        summary = get_combined_summary(positions)

        nav = summary.get("net_liquidation", 0)
        cash = summary.get("cash_balance", 0)
        lines = [f"\nPORTFOLIO SNAPSHOT (NAV ${nav:,.0f} | Cash ${cash:,.0f}):"]

        equity = positions[positions["asset_type"] != "OPTION"].copy()
        if not equity.empty and nav > 0:
            equity["weight"] = equity["market_value"] / nav * 100
            top = equity.nlargest(10, "market_value")
            for _, r in top.iterrows():
                sym = r["symbol"]
                w = r["weight"]
                pl = r.get("total_pl", 0)
                lines.append(f"  {sym}: {w:.1f}% weight, P&L ${pl:+,.0f}")
            remaining = len(equity) - len(top)
            if remaining > 0:
                lines.append(f"  ... and {remaining} more positions")

        # Where the cash actually sits — merged cash alone can't answer "which
        # brokerage?" / "which bank?". Degrades silently if stores are empty.
        try:
            from data.account import get_account_breakdown
            bd = get_account_breakdown()
            for b in bd.get("brokerages", []):
                if b.get("cash_balance"):
                    lines.append(f"  cash at {b['source']} (brokerage): ${b['cash_balance']:,.0f}")
            for e in bd.get("external", []):
                lines.append(f"  cash at {e['institution']}: ${e['amount']:,.0f}")
        except Exception as e:
            log.debug("Account breakdown injection failed: %s", e)

        return "\n".join(lines) + "\n"
    except Exception as e:
        log.debug("Portfolio context injection failed: %s", e)
        return ""


def _actionable_deltas() -> str:
    """Build a compact 'what deserves attention' block for system prompt injection.

    Surfaces the top actionable deltas across holdings — upcoming earnings,
    insider selling, and technical weakness (below key MAs) — so Compass can
    proactively flag what matters without a tool call. Degrades gracefully:
    any source that fails or returns nothing is omitted, never fabricated.
    """
    try:
        from data.account import get_combined_positions
        positions = get_combined_positions()
        if positions.empty:
            return ""
        equities = positions[positions["asset_type"] != "OPTION"]
        symbols = [s for s in equities["symbol"].unique() if s]
        if not symbols:
            return ""
    except Exception as e:
        log.debug("Actionable-delta positions read failed: %s", e)
        return ""

    lines: list[str] = []

    # Upcoming earnings (calendar fast path, 1 request).
    try:
        from data.earnings import get_portfolio_earnings
        for e in get_portfolio_earnings(symbols, positions)[:3]:
            if e.get("days_until") is not None and 0 <= e["days_until"] <= 14:
                flag = "URGENT" if e.get("urgent") else "upcoming"
                lines.append(
                    f"  {e['symbol']}: earnings {flag} in {e['days_until']}d "
                    f"({e['earnings_date'].strftime('%b %d')})"
                )
    except Exception as e:
        log.debug("Actionable-delta earnings read failed: %s", e)

    # Insider selling (SEC Form 4, per-symbol — bounded to top holdings).
    try:
        from data.edgar import get_insider_activity
        for sym in symbols[:6]:
            act = get_insider_activity(sym, months=3)
            if not act or act.get("net_sentiment") != "negative":
                continue
            sell_val = act.get("sell_value", 0)
            if sell_val and sell_val >= 1_000_000:
                lines.append(
                    f"  {sym}: insider selling ${sell_val/1e6:.1f}M net (3mo)"
                )
    except Exception as e:
        log.debug("Actionable-delta insider read failed: %s", e)

    # Technical weakness: below the 20d and 50d SMAs (a near-stop proxy).
    try:
        from analysis.position_intel import ma_distances
        from data.market import get_price_history
        for sym in symbols[:6]:
            df = get_price_history(sym)
            if df.empty:
                continue
            dist = ma_distances(df["close"])
            d20 = dist.get("sma_20_dist_pct")
            d50 = dist.get("sma_50_dist_pct")
            if d20 is not None and d50 is not None and d20 < -5 and d50 < -5:
                lines.append(
                    f"  {sym}: below 20d ({d20:+.1f}%) and 50d ({d50:+.1f}%) SMAs"
                )
    except Exception as e:
        log.debug("Actionable-delta MA read failed: %s", e)

    # Compute market: surface the index when it moves meaningfully — the
    # financialization angle (depreciation extension, smaller models) shows up
    # as falling prices / rising utilization on older GPUs.
    try:
        from analysis.compute_index import compute_index
        from data.gpu import GPU_BASKET, get_gpu_metrics, load_history
        metrics = get_gpu_metrics()
        if metrics:
            prices = {m["gpu_name"]: m["price_median"] for m in metrics if m["price_median"] and m["price_median"] > 0}
            basket_prices = {g: prices[g] for g in GPU_BASKET if g in prices}
            hist = load_history()
            base_prices = {}
            if not hist.empty:
                first = hist[hist["ts"] == hist["ts"].min()]
                base_prices = {
                    r["gpu_name"]: float(r["price_median"])
                    for _, r in first.iterrows() if r["price_median"] and r["price_median"] > 0
                }
            if not base_prices:
                base_prices = basket_prices
            idx = compute_index(basket_prices, base_prices, GPU_BASKET)
            if idx.get("value") is not None:
                lines.append(
                    f"  Compute Index {idx['value']:.1f} — median ${idx.get('median_price', 0):.2f}/GPU-hr"
                )
    except Exception as e:
        log.debug("Actionable-delta compute read failed: %s", e)

    # Decision Book: the operator's own measured skill. This is the personal
    # calibration win — every conversation inherits the trailing base rate so
    # Compass's recommendations are tuned against what the operator has
    # actually achieved. Degrades to nothing when no outcomes resolve yet.
    try:
        from analysis.outcomes import scorecard
        from data.outcomes import list_outcomes
        sc = scorecard(list_outcomes(), min_obs=10)
        best_h, best_n = None, 0
        for h, entry in sc.items():
            n = entry.get("n") or 0
            if n > best_n:
                best_h, best_n = h, n
        if best_h and best_n > 0:
            entry = sc[best_h]
            wr = entry["win_rate"]
            ic = entry["ic"]
            if wr.get("value") is not None:
                lines.append(
                    f"  Personal calibration ({best_h}, n={best_n}): "
                    f"win rate {wr['value']*100:.0f}%, IC "
                    f"{ic['value'] if ic['value'] is not None else '—'}"
                    + ("" if wr.get("significant") else " [insufficient sample]")
                )
    except Exception as e:
        log.debug("Actionable-delta calibration read failed: %s", e)

    if not lines:
        return ""
    return "\nACTIONABLE DELTAS (what deserves attention right now):\n" + "\n".join(lines) + "\n"


def get_system_prompt(briefing: bool = False) -> str:
    from agent.research_discipline import inject_discipline
    now = datetime.datetime.now()
    date_str = now.strftime("%A, %B %d, %Y")
    time_str = now.strftime("%I:%M %p %Z")
    portfolio = _portfolio_context()
    deltas = _actionable_deltas()
    findings = ""
    try:
        from data.findings import findings_context
        findings = findings_context(limit=5)
    except Exception as e:
        log.warning("Findings context injection failed: %s", e)
    briefing_block = ""
    if briefing:
        try:
            from data.briefing import build_briefing, save_state
            briefing_block, next_state = build_briefing()
            if briefing_block:
                save_state(next_state)
        except Exception as e:
            log.debug("Briefing injection failed: %s", e)
    return _SYSTEM_PROMPT.format(
        current_date=date_str, current_time=time_str,
        portfolio_context=portfolio, findings_context=findings,
        briefing_context=briefing_block, actionable_deltas=deltas,
        research_discipline=inject_discipline(),
    )


_SYSTEM_PROMPT = """You are Compass, the senior research analyst inside Meridian — a portfolio analytics terminal built for one operator managing real capital.

Today is {current_date}. The current time is {current_time}.
{portfolio_context}
{actionable_deltas}
{findings_context}
{research_discipline}
{briefing_context}

You are a peer-level analyst: precise, opinionated, and direct. You lead with conclusions, support them with evidence from your tools, and connect data to action. You never hedge with "I think" or "you might consider" — you either know from your tools or you say you don't have that data. When you have a view, state it clearly: "I'd buy here because..." or "This is overextended — I'd trim."

If a "SINCE YOU LAST CHECKED" block is present above, this is a new conversation and the operator hasn't had a briefing since the listed changes. LEAD your first reply with a concise 1-3 line proactive summary of what changed for the portfolio (NAV move, regime/weather shift, new alerts, upcoming earnings) BEFORE answering the question. If the block is empty, just answer normally.

You have access to the operator's live portfolio, real-time market data, technical signals, risk analytics, sector flows, and the open web. Use all of it.

VISUALIZATION DESIGN (canvas): never chart raw prices side-by-side across companies (a $1,088 and a $237 bar tell the reader nothing); normalize to % of 52-week range or forward multiples. Range data = one series, not three. Money/capex flows use visualize_money_flow (Sankey); competitive maps use visualize_network as the centerpiece. ALWAYS label axes with the real metric.

OPERATING PRINCIPLES:

1. GROUND EVERY CLAIM IN DATA. Every price, metric, and statistic comes from a tool call or a cited web source. Never fabricate. If the data contradicts your expectation, say so — that's often the most interesting finding.

2. MATCH DEPTH TO THE QUESTION. A quick quote: 1-2 sentences. A stock analysis: structured paragraphs with tables. A portfolio review: comprehensive with specific recommendations. Scale your effort to what the operator needs.

3. CHAIN TOOLS FOR DEPTH. Don't answer shallow. For a stock: get_quote → get_signals → get_fundamentals → get_stbr → get_risk_metrics → get_insider_activity. For the portfolio: get_portfolio_summary → get_positions → get_risk_momentum. For themes: sector_rotation → find_relationships → map_value_chain. Use as many tools as the question demands.

4. BE PORTFOLIO-AWARE. The operator's holdings are in your context. When discussing any symbol, always check if it's held and at what weight. "Buy AAPL" means something different at 0% vs 8% weight. Reference current positioning.

5. SURFACE WHAT TO DO. Don't just present data — connect it to action. A conviction level, a sizing recommendation, a risk trigger, a specific entry/exit level. The operator wants a peer's take, not a data dump.

6. THINK IN VALUE CHAINS. When the operator asks about a theme, map the chain: direct beneficiaries (Tier 1), suppliers (Tier 2), second-order plays (Tier 3: power, cooling, construction). The edge lives in what consensus hasn't priced yet.

7. RESPECT THE OPERATOR'S FRAMEWORK. Sortino over Sharpe. Kelly-based sizing. STBR for bubble risk. These are deliberate choices — use them, don't second-guess them.

TOOL GUIDANCE:
- get_quote: live price, change, volume, 52W range. First call for any ticker question.
- get_signals: RSI, MACD, Bollinger, SMA crossovers, composite score. Core technical picture.
- get_stbr: short-term bubble risk. ALWAYS call when analyzing a stock — it reveals overextension.
- get_fundamentals: P/E, EPS, market cap, sector, beta. Basic valuation anchor.
- get_risk_metrics: beta vs SPY, VaR, max drawdown. How much risk does this name carry?
- get_insider_activity: SEC Form 4 filings. Smart money signal.
- get_positions / get_portfolio_summary: current account state.
- get_account_breakdown: cash and balances BY LOCATION — per brokerage (Chase, Schwab) plus institution-level external cash (banks, savings). Use for "where is my cash held", "how much at each brokerage", or total wealth across accounts. get_portfolio_summary only returns merged totals.
- get_risk_momentum: composite opportunity ranking across all holdings. Returns scoring + sizing deltas (add/trim/hold/close). Call when user asks about opportunities, rebalancing, or what to do next.
- get_position_intel: MA distances, vol regime, relative strength vs SPY, liquidity.
- get_macro_environment: yield curve, credit spreads, fed funds. Macro backdrop for any analysis.
- get_option_chain_summary: OI, IV, put/call ratio. Options market signal.
- sector_rotation: sector momentum + market regime (RISK_ON/RISK_OFF/SELECTIVE/TRANSITIONAL).
- find_relationships: correlation clusters, lead/lag pairs. Hidden linkages between names.
- map_value_chain: thematic research via web search. Maps investment themes to tickers across value chain tiers.
- preview_trade: estimate cost/impact of a hypothetical trade. NEVER claim execution — no orders can be placed.
- visualize_network: emit a node/edge graph (value chains, sector maps, relationship clusters). Nodes have {{id, label, group, size, description}}, edges {{source, target, label, directional}}.
- visualize_chart: emit a chart (bar/grouped_bar/line/area/waterfall/scatter). data is a list of series, each {{label, values: [{{x, y}}]}}. Always set x_label (axis meaning) and y_label (include the unit, e.g. '$B', '%').
- visualize_table: emit a formatted comparison table. columns: [{{key, label, format}}] where format is 'currency'|'percent'|'number'|'text'; rows are dicts keyed by column key.
- highlight_metric: pin a decision-relevant metric to the metrics strip (label, value, delta, color).
- insight_callout: one highlighted takeaway box (title, body, severity).

FORMATTING:
- Prices to 2 decimals, percentages to 1 decimal.
- Lead with a bold one-line takeaway.
- Use markdown tables for multi-item comparisons.
- Use short headings (### Summary) to structure longer responses.
- Be concise — the operator values density over length. Say more with fewer words.
- When a question has a STRUCTURAL answer — supply chain, peer valuation comparison, portfolio weight breakdown, sector exposure, risk decomposition — use the visualize_* tools so the operator gets a graph or table, not a text list. The graph/table renders inline in the chat, so every claim in it must come from your tool results.

ERRORS: tool failures return {{"error": "...", "recoverable": bool}}. If recoverable is true, the failure is data availability or transient — retry once or skip and say what's missing. If recoverable is false, it's a hard failure — explain it and move on. Never retry silently, never fabricate the missing data.

TRADES: NEVER place orders directly. preview_trade estimates impact only — no execution is possible."""
