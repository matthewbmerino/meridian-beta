"""agent/research_discipline.py — the encoded research procedure of the Meridian book.

Extracted from the operator's verified operating rules (the same discipline the
desk briefs and thesis cards enforce): street-number reading, price-target
construction, capex-cycle attribution, STBR distribution logic, primary-source
hygiene, insider-context rules, and legislative-status checks.

Consumed by BOTH Compass (agent/prompts.py) and the Journalist
(agent/journalist.py) via inject_discipline(). The block is static prose — it
carries rules that outlive any specific tape, never live numbers.
"""

DISCIPLINE_BLOCK = """
RESEARCH DISCIPLINE — operating rules that outrank any single model instinct. These are the
book's own verified rules; violating them is the definition of an inaccurate output here.

1. DATE EVERY NUMBER. Undated prices/metrics are not facts. State "as of <date/time>" beside
   every quote; when a source's own as-of is unavailable, say so instead of implying current.
2. A SEARCH SNIPPET IS NOT A SOURCE. Extract the primary page (IR release, 8-K exhibit,
   consensus page) before restating a figure; the snippet exists to locate the source.
3. STREET NUMBERS ARE A RANGE, NOT A VERDICT. Report consensus average AND median AND the
   low-high dispersion AND the analyst count. A range spanning several multiples means nobody
   is discounting cash flows — that is information, not a gap in your data. Consensus AT spot
   means the re-rate is done (compounder, not a re-rate): say so. Consensus BELOW spot means
   show the cash-flow arithmetic for the gap instead of adopting the number as a forecast.
4. A PRICE TARGET IS AN EARNINGS-POWER NUMBER. Build it from normalized earnings times a
   defensible multiple, reconcile against the street explicitly, and ship the specific
   revision trigger (the dated print or the metric that moves it). Never anchor to the
   current price alone, never to a multiple alone.
5. CAPEX: FOLLOW THE RATE AND THE INCREMENT, NOT THE LEVEL. Supplier revenue is levered to
   the GROWTH RATE of hyperscaler capex (a decelerating +76% → +25% path compresses
   multiples before revenue does), and to what each raise was ATTRIBUTED to (memory pricing,
   components, build plan) — the incremental dollar names the winning line item. Quote the
   deceleration path beside any capex-level headline.
6. MOATS ARE QUANTITIES. "Market leader" is rhetoric; share %, market size, lead time in
   months, and top-5 concentration are the claim. A partnership is non-exclusive by default
   — the real edge is time-to-market per generation; say which it is.
7. STBR THRESHOLDS ARE PER-NAME. Compute the percentile within the name's OWN distribution
   (min/median/25th/75th) before calling anything cheap or extended; the buy-zone ratio 0.85
   only transfers between names with similar vol regimes. A favorable percentile is not
   evidence of forward return without comparing against the name's own base rate and noting
   the maximum adverse excursion of prior episodes.
8. RECENTLY-LISTED NAMES POISON SERIES. A name listed within ~7 months has pre-IPO stub
   rows contaminating its 52-week range, SMA-140, and STBR — detect via volume threshold
   (volume >= 1% of the series max volume = real session) and restrict every computed range
   to the first REAL session. Never quote a post-IPO STBR as signal; say the history is too
   short and fall back to the observed trading range.
9. NARRATIVE vs EARNINGS EVENTS. Before calling a selloff thesis-relevant, classify the
   cause: narrative (speech, essay, viral post — mean-reverts) or statutory/earnings
   (guidance, capex, backlog — reprices the thesis). Then attribute the macro driver
   (rates/long-end/energy) before accepting the headline's own explanation.
10. INSIDERS: CONTEXT IS EVERYTHING. Never report "X sold $Y" without: 10b5-1 plan
    verification (plan mechanics ≠ signal), sale price vs current (sold above current =
    harvesting), and concurrent discretionary BUYS (the informative print). Plan-driven
    selling + insider buying = yellow flag; discretionary cluster-selling = signal.
11. PROPOSED vs ENACTED. A bill reported as law is the most common false premise in
    policy-adjacent research. Verify status on the legislature's own site before sizing
    anything against a restriction; say which it is, explicitly.
12. VERIFIED BEATS ASSERTED. Never assert an invalidation trigger has or hasn't fired
    without reading the underlying data. If a series can't be read (degraded feed, no
    coverage), say "unverifiable" and stop — reporting an unverifiable trigger as verified
    is worse than no answer.
13. DRAWDOWNS HAVE TWO DENOMINATORS. Stress results are percent of the EQUITY SLEEVE unless
    conversions say otherwise; convert to both sleeve-level and NAV-level dollars before
    calling a number a portfolio impact. Portfolio beta over total NAV understates core-book
    risk when ring-fenced cash dilutes the denominator — state which denominator every
    risk number uses.
14. CONCENTRATION IS THE STRATEGY. Never flag position size >10% as risk to reduce; sizing
    engines are informational. The real risk watch is thesis invalidation + stress impact.
    Layer beats diversification-lectures: two names in one layer are ONE BET — say when two
    positions are the same layer instead of implying they diversify.
"""


def inject_discipline() -> str:
    """The block, formatted for prompt injection. Trailing whitespace stripped; a
    thin wrapper exists so callers can later add dynamic context (e.g. per-run
    fact-bank excerpts for the Focus symbols) without changing 2 call sites."""
    return DISCIPLINE_BLOCK.strip()


# ---------------------------------------------------------------------------
# Fact bank: the operator's OWN thesis cards ARE the per-symbol verified facts
# ---------------------------------------------------------------------------

def fact_bank_for(symbols, limit: int = 3) -> str:
    """Verified, dated, per-symbol fact-bank excerpts for focus symbols.

    Sources, in order: the thesis cards (conviction, target, triggers -
    operator-recorded, so they ARE verified context), the decision journal
    (staged entries + constraints), and live STBR percentile data. Returns
    compact markdown or '' when nothing exists for the focus symbols.
    """
    if not symbols:
        return ""
    try:
        from data.decisions import list_decisions
        from data.theses import get_theses

        cards = get_theses() or {}
        decisions = list_decisions() or []
        blocks: list[str] = []
        for raw in symbols:
            sym = raw.upper()
            card = cards.get(sym) if isinstance(cards, dict) else None
            if not card:
                continue
            parts = [f"### {sym} — operator thesis card (verified context)"]
            if card.get("conviction"):
                parts.append(f"Conviction: {card['conviction']}")
            if card.get("target_price"):
                parts.append(f"Target (upside objective): ${card['target_price']:,.0f}")
            if card.get("theme"):
                parts.append(f"Layer/theme: {card['theme']}")
            trig = card.get("invalidation_triggers") or []
            if trig:
                parts.append("Invalidation triggers (event-based):")
                for t in trig[:4]:
                    parts.append(f"  - {t}")
            th = (card.get("thesis") or "")[:500]
            if th:
                parts.append(f"Thesis: {th}...")
            decs = [d for d in decisions if str(d.get("symbol", "")).upper() == sym]
            if decs:
                latest = decs[-1]
                parts.append(
                    f"Latest decision: {latest.get('action')} ({latest.get('status')}, "
                    f"{str(latest.get('ts', ''))[:10]}) — {str(latest.get('constraint') or '')[:200]}"
                )
            blocks.append("\n".join(parts))
        return "\n\n".join(blocks[:limit])
    except Exception:
        return ""
