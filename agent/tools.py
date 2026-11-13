"""Compass tool registry — wraps existing Meridian data/ and analysis/ modules as LLM-callable tools."""

import logging

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe(val):
    """Convert numpy/pandas types to JSON-safe Python types."""
    if isinstance(val, (np.integer,)):
        return int(val)
    if isinstance(val, (np.floating,)):
        return round(float(val), 4)
    if isinstance(val, (np.bool_,)):
        return bool(val)
    if isinstance(val, pd.Timestamp):
        return str(val.date())
    if pd.isna(val):
        return None
    return val


def _clean_dict(d: dict) -> dict:
    """Recursively convert a dict to JSON-safe types."""
    out = {}
    for k, v in d.items():
        if isinstance(v, dict):
            out[k] = _clean_dict(v)
        elif isinstance(v, (list, tuple)):
            out[k] = [_safe(i) if not isinstance(i, dict) else _clean_dict(i) for i in v]
        elif isinstance(v, (pd.Series, pd.DataFrame)):
            continue  # skip non-serializable pandas objects
        else:
            out[k] = _safe(v)
    return out


def _fmt_series_date(idx) -> str:
    """Format a series index (Timestamp/datetime/date) as a short date string."""
    if isinstance(idx, pd.Timestamp):
        return idx.strftime("%Y-%m-%d")
    s = str(idx)
    return s[:10] if len(s) > 10 else s


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def tool_get_quote(symbol: str) -> dict:
    """Get current price, change, and volume for a stock."""
    from data.account import extract_live_price
    from data.market import get_price_history, get_quotes

    quotes = get_quotes([symbol.upper()])
    qdata = quotes.get(symbol.upper(), {})
    sym = symbol.upper()

    price, change, pct, _stale, session = extract_live_price(qdata)
    quote = qdata.get("quote", {})

    if price is None:
        return {"error": f"No price data for {symbol}"}

    # 52w high/low: Massive's snapshot bridge hardcodes these to 0, so compute
    # them from 1y price history (the source of truth).
    high_52w = quote.get("52WkHigh")
    low_52w = quote.get("52WkLow")
    if not high_52w or not low_52w:
        try:
            hist = get_price_history(sym, days=365)
            if not hist.empty:
                high_52w = float(hist["high"].max())
                low_52w = float(hist["low"].min())
        except Exception as e:
            log.warning("52w high/low derivation failed for %s: %s", sym, e)

    return _clean_dict({
        "symbol": sym,
        "price": price,
        "change": change,
        "change_pct": pct,
        "session": session,
        "bid": quote.get("bidPrice"),
        "ask": quote.get("askPrice"),
        "volume": quote.get("totalVolume"),
        "high_52w": high_52w,
        "low_52w": low_52w,
        "pe_ratio": quote.get("peRatio"),
        "description": quote.get("description", ""),
    })


def tool_get_price_history(symbol: str, days: int = 180) -> dict:
    """Get price history summary for a symbol."""
    from data.market import get_price_history

    df = get_price_history(symbol.upper(), days=days)
    if df.empty:
        return {"error": f"No price data for {symbol}"}


    close = df["close"]
    # Compact trailing close series so the viz agent can draw time trends
    # (line/area charts) and the writer can cite movement verbatim.
    tail = close.tail(24)
    series = [
        {"date": _fmt_series_date(idx), "close": round(float(v), 2)}
        for idx, v in tail.items()
    ]
    return _clean_dict({
        "symbol": symbol.upper(),
        "days": len(df),
        "current": float(close.iloc[-1]),
        "high": float(close.max()),
        "low": float(close.min()),
        "mean": round(float(close.mean()), 2),
        "start_price": float(close.iloc[0]),
        "return_pct": round((close.iloc[-1] / close.iloc[0] - 1) * 100, 2),
        "avg_volume": int(df["volume"].mean()),
        "series": series,
    })


def tool_search_symbol(query: str) -> dict:
    """Search for stock symbols by name or ticker."""
    from data.market import search_instruments

    results = search_instruments(query)
    instruments = results.get("instruments", [])
    return {
        "query": query,
        "results": [
            {"symbol": i.get("symbol", ""), "description": i.get("description", ""),
             "exchange": i.get("exchange", ""), "type": i.get("assetType", "")}
            for i in instruments[:10]
        ],
    }


def tool_get_fundamentals(symbol: str) -> dict:
    """Get fundamental data like P/E, market cap, sector, dividend yield."""
    from data.market import get_fundamentals

    data = get_fundamentals(symbol.upper())
    inst = data.get("instruments", [{}]) if isinstance(data, dict) else [{}]
    if not inst:
        inst = [{}]

    # Handle both list and dict responses
    if isinstance(data, dict) and symbol.upper() in data:
        fund = data[symbol.upper()].get("fundamental", {})
    elif isinstance(inst, list) and inst:
        fund = inst[0].get("fundamental", {})
    else:
        fund = {}

    # Only return fields that actually exist in the source. Massive's
    # fundamentals endpoint returns marketCap, sharesOutstanding, sector,
    # description, exchange — P/E, EPS, dividend yield are NOT available.
    # Returning None for them invites the model to fabricate values.
    available = {k: v for k, v in {
        "pe_ratio": fund.get("peRatio"),
        "eps": fund.get("epsTTM"),
        "dividend_yield": fund.get("dividendYield"),
        "dividend_amount": fund.get("dividendAmount"),
        "market_cap": fund.get("marketCap"),
        "shares_outstanding": fund.get("sharesOutstanding"),
        "avg_volume_10d": fund.get("vol10DayAvg"),
        "avg_volume_3m": fund.get("vol3MonthAvg"),
        "high_52w": fund.get("high52"),
        "low_52w": fund.get("low52"),
        "pb_ratio": fund.get("pbRatio"),
        "beta": fund.get("beta"),
        "sector": fund.get("sector", ""),
        "industry": fund.get("industry", ""),
    }.items() if v is not None and v != ""}

    return _clean_dict({
        "symbol": symbol.upper(),
        "data_available": bool(available),
        **available,
    })


def tool_get_portfolio_summary() -> dict:
    """Get account balances: net liquidation, cash, buying power."""
    from data.account import get_combined_summary

    return _clean_dict(get_combined_summary())


def tool_get_account_breakdown() -> dict:
    """Get cash and balances broken out by where they are held.

    Answers questions like "where is my cash held?" / "how much cash at each
    brokerage?" — the merged ``get_portfolio_summary`` cannot. Returns one row
    per brokerage (from the imported position files) with its own cash and
    market value, institution-level external entries with their ``kind``
    (``bank`` = liquid cash; ``sportsbook`` = illiquid stake — withdrawable
    balance + open bets at stake, NAV but NOT cash_balance), and the merged
    aggregate for reconciliation.
    """
    from data.account import get_account_breakdown

    return _clean_dict(get_account_breakdown())


def tool_get_liabilities() -> dict:
    """Get outstanding debt: credit cards and installment loans (car loan,
    financing) with each balance, APR, monthly payment, payoff date, and how
    much is short-term (due within 12 months) vs long-term. Also returns the
    total debt, monthly debt service, annual interest cost, the liquid cash
    after near-term obligations, and net worth (net liquidation − total debt).
    Use for any question about what is owed, debt, payments, payoff timing,
    or how much of the cash on hand is already committed."""
    from backend.routers.liabilities import list_liabilities

    return _clean_dict(list_liabilities())


def tool_get_debt_analysis() -> dict:
    """Get DEBT ANALYSIS: whether the recorded debt is cheap or expensive.

    Returns the cost-of-carry verdict (weighted APR vs the FRED 3-month Treasury
    risk-free rate, the spread, and net_carry — the annual dollar value of that
    spread), the avalanche priority ranking (which balance to pay down first and
    why, with each row's share of the book's next-12-month interest), the payoff
    timeline, and a data-quality flag list (rows recorded as cards that read as
    loans, missing payments/due days, balances that never amortize). Use for
    "is my debt good debt", "which debt should I pay off first", "what is my
    debt costing me", "is my leverage accretive", or any qualitative read on
    the liabilities book. The month-by-month paydown series is chart data and is
    omitted here."""
    from backend.routers.liabilities import liability_analysis

    return _clean_dict(liability_analysis(include_projection=False))


def tool_list_alternatives() -> dict:
    """List the alternatives book: illiquid real-asset positions (watches,
    graded cards, other collectibles) with cost, latest mark, and delta;
    plus the sportsbook stakes (kind=sportsbook) and the rollup. NOT part of
    net_liquidation — a parallel record."""
    from backend.routers.alternatives import list_alternatives
    return _clean_dict(list_alternatives())


def tool_add_alternative(kind: str, description: str, cost: float | None = None,
                         acquired_at: str | None = None, psa_cert: str | None = None,
                         detail: str | None = None, refs: list[str] | None = None) -> dict:
    """Record an alternative asset (kind: watch | card | property | vehicle |
    pc_part | other). description should name the item precisely (reference
    number for a watch, set+card number+grade for a card, PSA cert # when it
    exists, full address for property, year/make/trim/options for a vehicle,
    exact model/part number for a PC component)."""
    from data.alternatives import add_item
    try:
        item = add_item(kind, description, detail=detail, cost=cost,
                        acquired_at=acquired_at, psa_cert=psa_cert, refs=refs)
    except ValueError as e:
        return {"error": str(e)}
    return {"item": _clean_dict(item)}


def tool_record_alternative_valuation(item_id: str, value: float, as_of: str,
                                      source: str, note: str | None = None) -> dict:
    """Record a mark against an alternative — APPEND-ONLY, provenance
    required. Only call AFTER the research evidence is stated aloud: value
    must come from a source read this conversation (an aggregator's recorded
    sale, a market index), named in `source` with the date in `as_of`.
    Never invent a number — if no recorded sale exists, report the
    illiquidity instead of marking."""
    from data.alternatives import add_valuation
    try:
        mark = add_valuation(item_id, value, as_of, source, note)
    except ValueError as e:
        return {"error": str(e)}
    return {"valuation": _clean_dict(mark)}


def tool_remove_alternative(item_id: str) -> dict:
    """Remove an alternative item and its valuation history."""
    from data.alternatives import remove_item
    return {"ok": remove_item(item_id)}


def tool_edit_external_pl(entry_id: str, amount: float, source: str,
                          description: str, as_of: str,
                          note: str | None = None) -> dict:
    """Edit an existing manual external-P&L entry in place (the id survives).
    The recurring workflow: 'Marcus interest YTD is now X' — bump amount +
    as_of on the same entry rather than logging a new one. Get entry ids
    from list_external_pl's manual rows."""
    from data.external_pl_manual import update_manual_external_pl
    try:
        entry = update_manual_external_pl(entry_id, amount, source, description, as_of, note)
    except ValueError as e:
        return {"error": str(e)}
    if entry is None:
        return {"error": "entry not found"}
    return {"entry": _clean_dict(entry)}


def tool_log_external_pl(amount: float, source: str, description: str, as_of: str,
                         note: str | None = None) -> dict:
    """Log an external P&L event the operator asserts WITHOUT a document —
    bank interest from a statement-less account (e.g. Marcus savings
    interest), bonuses, fees. amount is signed (negative = a loss/fee).
    source is the family: bank_interest | bonus | fees | external_other.
    Flows into realized external P&L (Overview by-source rows + totals).
    Ask for the amount + as-of date (statement month-end for interest)
    before logging."""
    from data.external_pl_manual import add_manual_external_pl, list_manual_external_pl
    try:
        entry = add_manual_external_pl(amount, source, description, as_of, note)
    except ValueError as e:
        return {"error": str(e)}
    return {"entry": _clean_dict(entry), "ledger_total": sum(e["amount"] for e in list_manual_external_pl())}


def tool_list_external_pl() -> dict:
    """The external P&L ledger: document-extracted rows (sportsbook, prediction
    markets, staking) PLUS manual asserted entries, per source and total.
    Use for 'how much interest did I earn', 'external P&L by source'."""
    from data.imports import get_all_external_pl
    rows, total = get_all_external_pl()
    return {"total": total, "by_source": rows}


def tool_get_positions() -> dict:
    """Get current portfolio positions with market values and P&L."""
    from analysis.portfolio import compute_allocation, herfindahl_index
    from data.account import get_combined_positions

    df = get_combined_positions()
    if df.empty:
        return {"positions": [], "count": 0}

    df = compute_allocation(df)


    # Return top 15 positions by weight + summary
    top = df.nlargest(15, "weight")
    positions = []
    for _, row in top.iterrows():
        positions.append(_clean_dict({
            "symbol": row.get("symbol", ""),
            "asset_type": row.get("asset_type", ""),
            "quantity": row.get("quantity"),
            "avg_cost": row.get("avg_cost"),
            "current_price": row.get("current_price"),
            "market_value": row.get("market_value"),
            "weight_pct": round(row.get("weight", 0), 2),
            "total_pl": row.get("total_pl"),
            "day_pl": row.get("day_pl"),
            "source": row.get("source", ""),
        }))

    total_mv = float(df["market_value"].sum())
    total_pl = float(df["total_pl"].sum())
    total_day_pl = float(df["day_pl"].sum())

    return _clean_dict({
        "count": len(df),
        "showing_top": len(positions),
        "total_market_value": total_mv,
        "total_pl": total_pl,
        "total_day_pl": total_day_pl,
        "hhi": round(herfindahl_index(df), 1),
        "positions": positions,
    })


def tool_get_signals(symbol: str) -> dict:
    """Get technical signals: RSI, MACD, Bollinger, SMA, composite score."""
    from analysis.signals import generate_signals
    from data.market import get_price_history

    df = get_price_history(symbol.upper(), days=365)
    if df.empty:
        return {"error": f"No price data for {symbol}"}


    signals = generate_signals(symbol.upper(), df)
    return _clean_dict(signals)


def tool_get_stbr(symbol: str) -> dict:
    """Get Short Term Bubble Risk (STBR) for a symbol."""
    from analysis.stbr import calculate_stbr, current_risk_label
    from data.market import get_price_history

    df = get_price_history(symbol.upper(), days=365)
    if df.empty or len(df) < 140:
        return {"error": "Insufficient data for STBR (need 140+ days)"}

    stbr_df = calculate_stbr(df["close"])
    if stbr_df.empty:
        return {"error": "STBR calculation failed"}

    current = float(stbr_df["stbr"].iloc[-1])
    label, color = current_risk_label(current)

    # Compact trailing STBR series so the viz agent can draw the bubble-risk
    # trend as a line chart and the writer can cite the trajectory.
    series = [
        {
            "date": _fmt_series_date(idx),
            "stbr": round(float(v), 3),
            "close": round(float(c), 2),
        }
        for idx, v, c in stbr_df[["stbr", "close"]].tail(24).itertuples(index=True)
    ]

    return {
        "symbol": symbol.upper(),
        "stbr": round(current, 3),
        "risk_label": label,
        "sma_140": round(float(stbr_df["sma_140"].iloc[-1]), 2),
        "price": round(float(stbr_df["close"].iloc[-1]), 2),
        "series": series,
    }


def tool_get_insider_activity(symbol: str) -> dict:
    """Get SEC Form 4 insider trading activity for a symbol."""
    from data.edgar import get_insider_activity

    result = get_insider_activity(symbol.upper())
    if result is None:
        return {"error": f"No insider data for {symbol}"}

    # Limit transactions to most recent 10
    txns = result.get("transactions", [])[:10]
    return _clean_dict({
        "symbol": result["symbol"],
        "total_buys": result["total_buys"],
        "total_sells": result["total_sells"],
        "buy_value": result["buy_value"],
        "sell_value": result["sell_value"],
        "net_shares": result["net_shares"],
        "net_sentiment": result["net_sentiment"],
        "recent_transactions": txns,
        "insider_count": len(result.get("insiders", {})),
    })


def tool_get_macro_environment() -> dict:
    """Get macroeconomic signals: yield curve, credit spreads, fed funds rate."""
    from data.fred import market_environment

    env = market_environment()
    result = {}
    for key in ("yield_curve", "credit_spread", "fed_funds"):
        data = env.get(key)
        if data:
            result[key] = {
                "value": _safe(data["value"]),
                "display": data["display"],
                "signal": data["signal"],
                "label": data["label"],
                "description": data["description"],
            }

    term = env.get("term_structure", [])
    if term:
        result["term_structure"] = [
            {"tenor": t[0], "rate": _safe(t[1]), "rate_6m_ago": _safe(t[2])}
            for t in term
        ]
    return result


def tool_get_option_chain_summary(symbol: str) -> dict:
    """Get option chain summary: put/call volumes, IV, top strikes."""
    from data.market import flatten_option_chain, get_option_chain

    try:
        chain = get_option_chain(symbol.upper())
    except Exception as e:
        return {"symbol": symbol.upper(), "available": False, "error": str(e)[:200]}

    df = flatten_option_chain(chain)
    if df.empty:
        return {"symbol": symbol.upper(), "available": False, "error": "No options data"}

    calls = df[df["put_call"] == "CALL"]
    puts = df[df["put_call"] == "PUT"]

    # Top 5 calls and puts by volume (use open_interest column)
    top_cols = ["strike", "expiration", "bid", "ask", "volume", "open_interest", "delta", "iv"]
    top_calls = calls.nlargest(5, "volume")[top_cols].to_dict("records") if not calls.empty and "volume" in calls.columns else []
    top_puts = puts.nlargest(5, "volume")[top_cols].to_dict("records") if not puts.empty and "volume" in puts.columns else []

    return _clean_dict({
        "symbol": symbol.upper(),
        "available": True,
        "total_contracts": len(df),
        "call_volume": int(calls["volume"].sum()) if not calls.empty else 0,
        "put_volume": int(puts["volume"].sum()) if not puts.empty else 0,
        "put_call_ratio": round(float(puts["volume"].sum()) / max(float(calls["volume"].sum()), 1), 2) if not calls.empty else 0,
        "expirations": sorted(df["expiration"].unique().tolist())[:10],
        "avg_iv_calls": round(float(calls["iv"].mean()), 2) if not calls.empty and calls["iv"].mean() else 0,
        "avg_iv_puts": round(float(puts["iv"].mean()), 2) if not puts.empty and puts["iv"].mean() else 0,
        "top_calls": top_calls,
        "top_puts": top_puts,
    })


def tool_get_risk_metrics(symbol: str) -> dict:
    """Get risk metrics for a symbol: beta vs SPY, VaR, max drawdown."""
    from analysis.risk import daily_returns, historical_var, max_drawdown_detail, portfolio_beta
    from data.market import get_price_history

    df = get_price_history(symbol.upper(), days=365)
    spy_df = get_price_history("SPY", days=365)

    if df.empty:
        return {"error": f"No price data for {symbol}"}

    ret = daily_returns(df)
    spy_ret = daily_returns(spy_df) if not spy_df.empty else pd.Series(dtype=float)

    # Beta
    beta_val = None
    if not spy_ret.empty:
        betas = portfolio_beta({symbol.upper(): ret}, spy_ret)
        beta_val = round(betas.get(symbol.upper(), 1.0), 2)

    # VaR
    var_95 = round(historical_var(ret, 0.95) * 100, 2)

    # Max drawdown
    dd = max_drawdown_detail(df["close"])
    dd_info = {}
    if dd:
        dd_info = {
            "max_drawdown_pct": round(dd["max_drawdown"] * 100, 2),
            "peak_date": str(dd["peak_date"].date()) if dd.get("peak_date") else None,
            "trough_date": str(dd["trough_date"].date()) if dd.get("trough_date") else None,
            "current_drawdown_pct": round(dd["current_dd"] * 100, 2),
            "recovery_pct": round(dd["recovery_pct"], 1),
        }

    return _clean_dict({
        "symbol": symbol.upper(),
        "beta_vs_spy": beta_val,
        "var_95_daily_pct": var_95,
        **dd_info,
    })


def tool_preview_trade(symbol: str, side: str, quantity: int,
                       order_type: str = "MARKET", price: float = None) -> dict:
    """Preview a trade and show portfolio impact. Does NOT execute.

    Uses the same trade-resolution and pretrade-impact pipeline as the
    /api/analysis/pretrade-impact endpoint (backend/services/sizing_book.py +
    analysis/pretrade.py) so Compass and the Trade Ticket agree on the risk
    impact of a hypothetical trade.
    """
    from analysis.pretrade import pretrade_impact
    from backend.services.sizing_book import (
        build_pretrade_inputs,
        load_equity_book,
        resolve_trade,
    )

    sym = symbol.upper()
    side = side.upper()
    if side not in ("BUY", "SELL"):
        return {"error": f"side must be BUY or SELL, got {side}"}

    try:
        resolved = resolve_trade(sym, price, None, quantity, caller="preview_trade")
    except ValueError as e:
        return {"error": str(e)}
    exec_price = resolved["exec_price"]
    trade_dollars = resolved["trade_dollars"]

    book = load_equity_book()
    if book is None:
        return {"error": "No positions found in portfolio — import positions to preview impact"}
    equity_pos, nav = book

    inputs = build_pretrade_inputs(equity_pos, nav, sym)
    if inputs is None:
        return {"error": f"Insufficient data for {sym}"}
    current_weights, returns_dict = inputs

    impact = pretrade_impact(
        current_weights, returns_dict, sym, trade_dollars, nav, side
    )

    return _clean_dict({
        "symbol": sym,
        "side": side,
        "quantity": quantity,
        "order_type": order_type,
        "current_price": exec_price,
        "execution_price": exec_price,
        "estimated_cost": trade_dollars,
        "status": "preview_only",
        "note": "This is a preview only. No order is placed.",
        "impact": impact,
    })

def tool_get_risk_momentum() -> dict:
    """Compute Risk x Momentum scores + principled position-sizing deltas.

    Score = ((100 - RSI) / 100) * (1 / STBR) * 100, clamped 0-100.
    Sizing uses Kelly x inverse-vol x conviction with cash/sector/vol caps —
    see analysis/sizing.py for the framework.

    Uses the same book-builder as the /api/analysis/sizing endpoint
    (backend/services/sizing_book.py) so Compass and the Opportunities page
    always agree on weights, sigma, and deltas.
    """
    from analysis.signals import generate_signals
    from analysis.sizing import compute_target_weights, config_from_prefs
    from analysis.stbr import calculate_stbr, current_risk_label
    from backend.services.sizing_book import (
        build_bucket_stats,
        build_factor_covariance,
        build_sizing_book,
        load_equity_book,
    )
    from data import prefs as _prefs
    from data.market import get_price_history

    book = load_equity_book()
    if book is None:
        return {"error": "No positions found in portfolio"}
    equity_pos, nav = book

    inputs, score_by_sym, _sector_map = build_sizing_book(equity_pos, nav)
    if not inputs:
        return {"error": "No equity positions found"}

    cfg = config_from_prefs(_prefs.get_prefs())

    bucket_stats = None
    try:
        bucket_stats = build_bucket_stats([inp.symbol for inp in inputs], cfg)
    except Exception as e:
        log.warning("Empirical bucket build failed (risk_momentum tool): %s", e)

    op_buckets = None
    try:
        from data.prefs import get as get_pref
        if bool(get_pref("calibration_operator_outcomes")):
            from analysis.outcomes import operator_bucket_stats as _op_stats
            from data.outcomes import list_outcomes
            op_buckets = _op_stats(list_outcomes(), min_obs=max(10, int(get_pref("calibration_operator_min_obs") or 30)))
    except Exception as e:
        log.warning("Operator bucket build failed (risk_momentum tool): %s", e)

    covariance = build_factor_covariance([inp.symbol for inp in inputs])
    sizing_results = {
        r.symbol: r for r in compute_target_weights(
            inputs, nav, cfg, bucket_stats, covariance=covariance,
            operator_bucket_stats=op_buckets,
        )
    }

    # Per-symbol R×M display data (RSI + STBR) — the sizing pipeline doesn't
    # carry these, so they're gathered here in one pass over the same symbols.
    per_symbol: dict = {}
    errors = 0
    for sym in inputs:
        try:
            df = get_price_history(sym, days=365)
            if df.empty or len(df) < 140:
                continue

            close = df["close"]
            sig = generate_signals(sym, df)
            rsi_val = sig.get("rsi")
            if rsi_val is None:
                continue

            stbr_df = calculate_stbr(close)
            if stbr_df.empty:
                continue
            stbr_val = float(stbr_df["stbr"].iloc[-1])
            label, color = current_risk_label(stbr_val)

            per_symbol[sym] = {
                "rsi": rsi_val,
                "stbr": stbr_val,
                "label": label,
                "color": color,
            }
        except Exception:
            errors += 1
            continue

    scores = []
    for inp in inputs:
        sr = sizing_results.get(inp.symbol)
        data = per_symbol.get(inp.symbol, {})
        row = {
            "symbol": inp.symbol,
            "score": round(score_by_sym.get(inp.symbol, 50), 1),
            "tier": sr.tier if sr else "Hold",
            "rsi": round(data.get("rsi", 0), 1),
            "stbr": round(data.get("stbr", 0), 3),
            "risk_label": data.get("label", "Unknown"),
            "risk_color": data.get("color", ""),
            "current_weight_pct": round(inp.current_weight * 100, 2),
        }
        if sr is not None:
            row.update({
                "target_weight_pct": round(sr.target_weight * 100, 2),
                "delta_weight_pct": round(sr.delta_weight * 100, 2),
                "delta_dollars": round(sr.delta_dollars, 0),
                "action": sr.action,
                "rationale": sr.rationale,
                "sigma_used": round(sr.sigma_used, 3),
                "tax_deferred": sr.tax_deferred,
            })
        scores.append(row)

    scores.sort(key=lambda s: s["score"], reverse=True)

    return _clean_dict({
        "count": len(scores),
        "errors": errors,
        "portfolio_equity": round(nav, 0),
        "sizing_config": {
            "w_cap_pct": round(cfg.w_cap * 100, 2),
            "sigma_target_pct": round(cfg.sigma_target * 100, 2),
            "k": cfg.k,
            "cash_floor_pct": round(cfg.cash_floor * 100, 2),
        },
        "scores": scores,
    })


def tool_get_position_intel(symbol: str) -> dict:
    """Get per-position intelligence: MA distances, vol regime, relative strength, liquidity."""
    symbol = symbol.upper()
    from analysis.position_intel import position_intelligence
    from analysis.risk import daily_returns
    from data.market import get_fundamentals, get_price_history

    df = get_price_history(symbol)
    if df.empty:
        return {"error": f"No price data for {symbol}"}

    bench_df = get_price_history("SPY")
    bench_returns = daily_returns(bench_df) if not bench_df.empty else None

    fund = {}
    try:
        raw = get_fundamentals(symbol)
        if raw:
            inner = list(raw.values())[0] if raw else {}
            fund = inner.get("fundamental", {}) if isinstance(inner, dict) else {}
    except Exception as e:
        log.warning("Fundamentals fetch failed for %s: %s", symbol, e)

    result = position_intelligence(
        close_series=df["close"],
        symbol_returns=daily_returns(df),
        benchmark_returns=bench_returns,
        hv_30=None,
        avg_volume_10d=float(df["volume"].tail(10).mean()) if "volume" in df else 0,
        avg_volume_3m=float(df["volume"].tail(63).mean()) if "volume" in df else 0,
        market_cap=float(fund.get("marketCap", 0) or 0),
    )
    result["symbol"] = symbol
    return _clean_dict(result)


def tool_sector_rotation() -> dict:
    """Get sector rotation signals across S&P 500."""
    from analysis.risk import daily_returns
    from analysis.sectors import market_regime_breadth, sector_rotation_signals
    from data.market import get_price_history
    from data.universe import sp500_sectors

    sector_map = sp500_sectors()
    sectors: dict[str, list[str]] = {}
    for sym, sec in sector_map.items():
        sectors.setdefault(sec, []).append(sym)

    sampled: list[str] = []
    for sec, syms in sectors.items():
        sampled.extend(syms[:8])

    returns_dict: dict = {}
    for sym in sampled:
        try:
            df = get_price_history(sym, days=90)
            if not df.empty and len(df) > 20:
                returns_dict[sym] = daily_returns(df)
        except Exception:
            continue

    if len(returns_dict) < 10:
        return {"error": "Insufficient data for sector analysis"}

    rotation = sector_rotation_signals(returns_dict, sector_map)
    regime = market_regime_breadth(returns_dict, sector_map)
    return _clean_dict({
        "regime": regime,
        "sectors": rotation[:11],
    })


def tool_find_relationships(symbols: list[str]) -> dict:
    """Find statistical relationships between a set of tickers."""
    from analysis.relationships import correlation_network, find_lead_lag_pairs
    from analysis.risk import daily_returns
    from data.market import get_price_history

    returns_dict: dict = {}
    for sym in symbols:
        try:
            df = get_price_history(sym.upper(), days=252)
            if not df.empty:
                returns_dict[sym.upper()] = daily_returns(df)
        except Exception:
            continue

    if len(returns_dict) < 2:
        return {"error": "Need at least 2 symbols with data"}

    network = correlation_network(returns_dict, min_corr=0.3)
    lead_lag = find_lead_lag_pairs(returns_dict, min_corr=0.3)

    return _clean_dict({
        "symbols_analyzed": list(returns_dict.keys()),
        "clusters": network.get("clusters", {}),
        "top_correlations": [
            {"pair": f"{e['source']}-{e['target']}", "corr": e["correlation"],
             "change": e["rolling_change"]}
            for e in network.get("edges", [])[:10]
        ],
        "lead_lag_pairs": lead_lag[:8],
    })


def tool_get_compute_index() -> dict:
    """Get the Compute Index — the spot price of compute as a financialized commodity.

    Returns the TFLOPS-weighted index (100 at base), median $/GPU-hr, market
    tightness, and the top GPU prices + available-offer counts. Utilization and
    rented counts are not available (the full-market metrics API is host-only)
    and return null. Use when the operator asks about compute prices, GPU
    economics, AI infrastructure costs, or the financialization of compute.
    """
    from analysis.compute_index import compute_index, tightness_composite
    from data.gpu import GPU_BASKET, get_gpu_metrics, load_history

    metrics = get_gpu_metrics()
    if not metrics:
        return {"error": "GPU price feed unavailable — check VAST_API_KEY and feed health"}

    prices = {m["gpu_name"]: m["price_median"] for m in metrics if m["price_median"] and m["price_median"] > 0}
    basket_prices = {g: prices[g] for g in GPU_BASKET if g in prices}
    utilization = {m["gpu_name"]: m["usage"] for m in metrics if m["usage"] is not None}

    base_prices = {}
    hist = load_history()
    if not hist.empty:
        first = hist[hist["ts"] == hist["ts"].min()]
        base_prices = {
            r["gpu_name"]: float(r["price_median"])
            for _, r in first.iterrows() if r["price_median"] and r["price_median"] > 0
        }
    if not base_prices:
        base_prices = basket_prices

    spreads = [m["price_p90"] / m["price_p10"] for m in metrics
               if m["price_p10"] and m["price_p10"] > 0 and m["price_p90"]]
    price_spread = sum(spreads) / len(spreads) if spreads else None

    index = compute_index(basket_prices, base_prices, GPU_BASKET,
                          utilization=utilization, price_spread=price_spread)
    tightness = tightness_composite(price_spread)

    top = sorted(metrics, key=lambda m: m["price_median"] or 0, reverse=True)[:6]
    return _clean_dict({
        "index": index,
        "tightness": tightness,
        "top_gpus": [
            {
                "gpu": m["gpu_name"],
                "median_price": m["price_median"],
                "utilization": m["usage"],
                "available": m["available"],
                "rented": m["rented"],
                "tflops_per_dollar": m["tflops_per_dollar"],
            }
            for m in top
        ],
    })


def tool_map_value_chain(theme: str) -> dict:
    """Research a thematic investment chain using the active research harness.

    Example themes: 'AI infrastructure', 'nuclear energy renaissance',
    'GLP-1 obesity drugs', 'defense spending increase'.
    Returns structured value chain with tickers per tier.

    Routes through the same provider abstraction as Compass and Journalist
    (compute.py) — Ollama Cloud by default (privacy-first, transient
    processing, never trained on). Grounds the analysis in live web search
    (search_web) so tickers come from cited sources, not model memory.
    """

    from agent.tools_search import search_web

    # Ground the analysis with live web search results first — the model
    # synthesizes from cited sources, it never invents tickers.
    search = search_web(f"{theme} investment value chain stocks", limit=5)
    web_block = ""
    for r in search.get("results", [])[:5]:
        web_block += f"\n- {r.get('title', '')} — {r.get('url', '')}\n  {r.get('description', '')[:300]}"

    prompt = f"""Analyze the investment value chain for: "{theme}"

## LIVE WEB SEARCH RESULTS (use these as the primary source of truth)
{web_block if web_block else '(No web results available — rely on your knowledge but clearly mark unverified tickers)'}

Return a structured analysis with:
1. THESIS: 1-2 sentence investment thesis
2. VALUE CHAIN TIERS (with specific US-listed ticker symbols):
   - Tier 1 (Direct beneficiaries): Companies directly selling into this theme
   - Tier 2 (Infrastructure/suppliers): Companies supplying Tier 1
   - Tier 3 (Second-order beneficiaries): Companies benefiting indirectly (power, cooling, construction, etc.)
3. BOTTLENECKS: Where are the supply constraints? Which companies control them?
4. RISKS: What could invalidate this thesis?
5. TIMING: Is this early, mid, or late cycle for this theme?

For each ticker mention the company name, why it's relevant, and rough market cap category (mega/large/mid/small).
Focus on actionable, investable names — not ETFs. Be specific about WHY each company benefits.
Prefer tickers that appear in the web search results; mark any that don't as 'unverified'."""

    from agent.compute import get_client_for_research

    try:
        client, model = get_client_for_research()
        response = client.chat(
            model=model,
            messages=[
                {"role": "system", "content": "You are a thematic equity research analyst. Be specific about ticker symbols and investment rationale. Ground every claim in the provided web search results."},
                {"role": "user", "content": prompt},
            ],
            stream=False,
            reasoning_effort="high",
            role="research",
        )
        content = (response.get("message") or {}).get("content", "")
        citations = [r.get("url", "") for r in search.get("results", []) if r.get("url")]
        return {
            "theme": theme,
            "analysis": content,
            "citations": citations[:5],
            "web_grounded": bool(web_block),
        }
    except Exception as e:
        return {"error": f"Value chain research failed: {str(e)[:200]}"}


# ---------------------------------------------------------------------------
# Output tools — UI effects, not data fetches
# ---------------------------------------------------------------------------

def tool_get_factor_exposures(symbols: list | None = None) -> dict:
    """Compute factor exposures for a symbol set or the current portfolio.

    Uses the factor model (analysis/factors.py): market/size/value/momentum/
    quality betas per symbol + the weight-weighted portfolio aggregate. The
    factor-aware covariance replaces the blanket rho=0.4 assumption — two
    names with shared factor exposure are correctly implied to be correlated.
    """
    from analysis.factors import (
        factor_covariance,
        factor_exposures,
        factor_returns,
        portfolio_factor_exposures,
    )
    from analysis.risk import daily_returns
    from data.account import get_combined_positions, get_combined_summary
    from data.market import get_fundamentals, get_price_history, get_quotes

    weights: dict[str, float] = {}
    if symbols:
        syms = [s.upper() for s in symbols]
    else:
        positions = get_combined_positions()
        if positions.empty:
            return {"error": "No positions found in portfolio"}
        summary = get_combined_summary(positions)
        nav = float(summary.get("net_liquidation", 0) or 0)
        equities = positions[positions["asset_type"] != "OPTION"]
        syms = []
        for _, row in equities.iterrows():
            s = row["symbol"]
            if s:
                syms.append(s)
                if nav > 0:
                    weights[s] = float(row.get("market_value", 0)) / nav
        if not syms:
            return {"error": "No equity positions found"}

    returns_dict = {}
    for sym in syms:
        try:
            df = get_price_history(sym)
            if not df.empty:
                returns_dict[sym] = daily_returns(df)
        except Exception as e:
            return {"error": f"Price history failed for {sym}: {e}"}
    if len(returns_dict) < 2:
        return {"error": "Insufficient price history for factor model (need 2+ symbols)"}

    spy_df = get_price_history("SPY")
    if spy_df.empty:
        return {"error": "SPY benchmark unavailable — factor model needs market returns"}
    market_returns = daily_returns(spy_df)

    market_caps: dict[str, float] = {}
    for sym in syms:
        try:
            raw = get_fundamentals(sym)
            inst = raw.get("instruments", [])
            if inst:
                mc = inst[0].get("fundamental", {}).get("marketCap")
                if mc:
                    market_caps[sym] = float(mc)
        except Exception:
            continue
    missing = [s for s in syms if s not in market_caps]
    if missing:
        try:
            quotes = get_quotes(missing)
            for s in missing:
                q = quotes.get(s, {})
                fund = q.get("fundamental", {})
                shares = fund.get("sharesOutstanding", 0)
                price = q.get("quote", {}).get("lastPrice", 0) or q.get("quote", {}).get("mark", 0)
                if shares and price:
                    market_caps[s] = float(shares * price)
        except Exception:
            pass

    factor_df = factor_returns(
        returns_dict, market_returns, market_caps=market_caps or None
    )
    if factor_df.empty:
        return {"error": "Factor model could not be built from available data"}
    exposures = factor_exposures(returns_dict, factor_df)
    if not exposures:
        return {"error": "Insufficient aligned history for factor exposures"}

    portfolio = portfolio_factor_exposures(exposures, weights)
    cov = factor_covariance(returns_dict, factor_df, exposures)
    implied = []
    if not cov.empty:
        corr = cov.corr()
        syms_cov = list(cov.columns)
        for i in range(len(syms_cov)):
            for j in range(i + 1, len(syms_cov)):
                implied.append({
                    "pair": f"{syms_cov[i]}-{syms_cov[j]}",
                    "correlation": round(float(corr.iloc[i, j]), 3),
                })

    return {
        "factors": [f for f in factor_df.columns if f in ("market", "size", "value", "momentum", "quality")],
        "exposures": {
            sym: {k: round(float(v), 3) for k, v in exp.items()}
            for sym, exp in exposures.items()
        },
        "portfolio": {k: round(float(v), 3) for k, v in portfolio.items()},
        "implied_correlations": implied[:10],
    }


def tool_visualize_network(title: str, nodes: list, edges: list) -> dict:
    """Emit a network graph visualization. Rendering happens on the frontend."""
    return {"ok": True, "rendered": True, "node_count": len(nodes), "edge_count": len(edges)}


def tool_suggest_followups(suggestions: list) -> dict:
    """Emit follow-up research suggestions as buttons. Rendering happens on the frontend."""
    return {"ok": True, "count": len(suggestions)}


def tool_visualize_chart(title: str, chart_type: str, data: list, x_label: str = "",
                         y_label: str = "", stacked: bool = False, bands: list | None = None,
                         markers: list | None = None, scale: str = "linear",
                         value_format: str = "number", note: str = "") -> dict:
    """Emit a financial chart visualization. Rendering happens on the frontend.

    chart_type: 'bar', 'grouped_bar', 'line', 'area', 'waterfall', 'scatter',
                'range', 'timeline', 'heatmap'
    data: list of series, each = {label, values: [{x, y}], color?}
          range charts take TWO series: the low bound first, the high bound second.
          timeline charts take one series per row with exactly two x values.
    bands: shaded x-ranges [{from, to, label?, tone?}] for line/area
    markers: anchored points [{x, y, label?, tone?}] for line/area
    """
    return {"ok": True, "rendered": True, "chart_type": chart_type, "series_count": len(data)}


def tool_visualize_table(title: str, columns: list, rows: list, highlight: str = "") -> dict:
    """Emit a formatted financial comparison table. Rendering happens on the frontend.

    columns: list of {key, label, format?} where format is 'currency', 'percent', 'number', or 'text'
    rows: list of dicts keyed by column keys
    highlight: 'max', 'min', or '' for no highlighting
    """
    return {"ok": True, "rendered": True, "row_count": len(rows), "col_count": len(columns)}


def tool_highlight_metric(label: str, value: str, delta: str = "",
                          color: str = "", note: str = "") -> dict:
    """Emit a key metric to pin on the research canvas metrics strip.

    label: short metric name (e.g. 'Beta', 'STBR', 'Max DD')
    value: formatted value (e.g. '1.87', '-7.1%', '$410M')
    delta: optional change indicator (e.g. '+0.3', '3x')
    color: 'positive' | 'negative' | 'warning' | '' (default neutral)
    note: optional one-line context
    """
    return {"ok": True, "rendered": True, "metric": label}


def tool_add_annotation(chart_id: str, type: str, y: float, x: str = "all",
                        label: str = "", color: str = "") -> dict:
    """Emit a line annotation on a chart (support/resistance/target/stop).

    chart_id: the title of the chart to annotate (must match a visualize_chart title)
    type: 'support' | 'resistance' | 'target' | 'stop' | 'line'
    x: x-axis label where the line sits (or 'all' for a horizontal line)
    y: y-axis value of the line
    label: optional text shown at the line
    color: 'positive' | 'negative' | 'warning' | 'accent' | ''
    """
    return {"ok": True, "rendered": True, "chart_id": chart_id, "type": type}



def tool_visualize_money_flow(title: str, nodes: list, flows: list) -> dict:
    """Emit a money-flow (Sankey-style) visualization.

    nodes: list of {id, label, group?} — the entities money moves between.
    flows: list of {source, target, value, label?, note?} — direction IS the
    money direction; value is the dollar (or %) size of the flow.

    Use for: capex destination flows (hyperscaler -> supplier layer -> line
    item), supplier revenue concentration, where the incremental dollar lands,
    buyback/dividend distribution, segment revenue mix.
    """
    return {"ok": True, "rendered": True, "flow_count": len(flows)}


def tool_canvas_layout(artifacts: list) -> dict:
    """Emit the canvas layout: position/size/connections for all artifacts.

    artifacts: list of {id, type, x, y, w, h, connects_to}
      id: must match a visualize_* title (or 'graph' for the network)
      type: 'graph' | 'chart' | 'table' | 'callout'
      x, y: grid position (0-12 columns, 0-N rows)
      w: width in columns (1-12)
      h: height in rows (1-4)
      connects_to: list of artifact ids this one links to
    """
    return {"ok": True, "rendered": True, "artifact_count": len(artifacts)}


def tool_insight_callout(title: str, body: str, severity: str = "info") -> dict:
    """Emit a highlighted takeaway box above the report.

    title: short headline (e.g. 'Insider selling accelerating')
    body: 1-2 sentence explanation
    severity: 'positive' | 'negative' | 'warning' | 'info'
    """
    return {"ok": True, "rendered": True, "title": title}


# ---------------------------------------------------------------------------
# Tool Registry
# ---------------------------------------------------------------------------

TOOLS = {
    "get_quote": {
        "fn": tool_get_quote,
        "schema": {
            "type": "function",
            "function": {
                "name": "get_quote",
                "description": "Get current price, change, volume, 52-week range for a stock or ETF",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string", "description": "Ticker symbol (e.g. AAPL, SPY)"}
                    },
                    "required": ["symbol"],
                },
            },
        },
    },
    "get_price_history": {
        "fn": tool_get_price_history,
        "schema": {
            "type": "function",
            "function": {
                "name": "get_price_history",
                "description": "Get price history summary and stats for a symbol over N days",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string", "description": "Ticker symbol"},
                        "days": {"type": "integer", "description": "Number of days of history (default 180)"},
                    },
                    "required": ["symbol"],
                },
            },
        },
    },
    "search_symbol": {
        "fn": tool_search_symbol,
        "schema": {
            "type": "function",
            "function": {
                "name": "search_symbol",
                "description": "Search for stock symbols by company name or partial ticker",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search query (company name or ticker)"}
                    },
                    "required": ["query"],
                },
            },
        },
    },
    "get_fundamentals": {
        "fn": tool_get_fundamentals,
        "schema": {
            "type": "function",
            "function": {
                "name": "get_fundamentals",
                "description": "Get fundamental data: P/E ratio, EPS, market cap, sector, dividend yield, beta",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string", "description": "Ticker symbol"}
                    },
                    "required": ["symbol"],
                },
            },
        },
    },
    "get_portfolio_summary": {
        "fn": tool_get_portfolio_summary,
        "schema": {
            "type": "function",
            "function": {
                "name": "get_portfolio_summary",
                "description": "Get account balances: net liquidation value, cash, buying power, long/short market values",
                "parameters": {"type": "object", "properties": {}},
            },
        },
    },
    "get_positions": {
        "fn": tool_get_positions,
        "schema": {
            "type": "function",
            "function": {
                "name": "get_positions",
                "description": "Get current portfolio positions with market values, weights, and P&L. Returns top 15 by weight.",
                "parameters": {"type": "object", "properties": {}},
            },
        },
    },
    "get_account_breakdown": {
        "fn": tool_get_account_breakdown,
        "schema": {
            "type": "function",
            "function": {
                "name": "get_account_breakdown",
                "description": "Get cash and balances broken out BY WHERE THEY ARE HELD: one row per brokerage (Chase, Schwab) with that account's own cash_balance / market value / net_liquidation, plus institution-level external entries each tagged kind (bank = liquid cash; sportsbook = illiquid stake: withdrawable balance + open bets at stake, in NAV but NOT cash_balance) and the merged aggregate. Use this for any question about where cash is held, cash by brokerage/institution, sportsbook/stake balances, account-level balances, or total wealth across locations. get_portfolio_summary returns only merged totals and cannot answer these.",
                "parameters": {"type": "object", "properties": {}},
            },
        },
    },
    "get_liabilities": {
        "fn": tool_get_liabilities,
        "schema": {
            "type": "function",
            "function": {
                "name": "get_liabilities",
                "description": "Get OUTSTANDING DEBT: credit cards (revolving) and installment loans (car loan, furniture financing) each with balance, APR, monthly payment, monthly service amount, months remaining, payoff date, and the split between short-term (principal due within 12 months) and long-term. Also returns total debt, monthly debt service, annual interest cost, weighted-average APR, the next payment due, liquid cash after near-term obligations (cash_coverage), net_liquidation, and net_worth = net_liquidation − total debt. Use for any question about what is owed, debt balances, monthly payments, payoff timing, interest cost, or how much of the cash on hand is already committed. Debt is a PARALLEL ledger: net_liquidation itself is unchanged by debt — quote net_worth for the combined figure.",
                "parameters": {"type": "object", "properties": {}},
            },
        },
    },
    "get_debt_analysis": {
        "fn": tool_get_debt_analysis,
        "schema": {
            "type": "function",
            "function": {
                "name": "get_debt_analysis",
                "description": "Get DEBT ANALYSIS — whether the recorded debt is working for the operator. Returns: (1) cost_of_carry: weighted APR of the whole book vs the risk-free rate (FRED 3-month Treasury, with series + observation date), the spread, net_carry (the annual DOLLAR value of that spread — positive means the debt is cheaper than holding the risk-free asset), and how much balance is accretive vs dilutive; verdict is 'unknown' when the benchmark is unavailable, never guessed. (2) priority: the avalanche ranking — every liability ordered by APR with its share of the book's next-12-month interest and a data-derived action ('Keep — free carry', 'Highest-cost balance — clear this first', 'Float — clears each statement', ...). (3) timeline: payoff order with payoff dates. (4) flags: data-quality problems (a row recorded as a CARD with a term, missing payments or due days, balances whose payment never covers interest). Use for 'is my debt good debt', 'which debt should I pay off first', 'what is my debt costing me', 'am I over-levered', or any qualitative read on liabilities. The 60-month paydown series is chart data and is omitted.",
                "parameters": {"type": "object", "properties": {}},
            },
        },
    },
    "list_alternatives": {
        "fn": tool_list_alternatives,
        "schema": {
            "type": "function",
            "function": {
                "name": "list_alternatives",
                "description": "List the ALTERNATIVES book: illiquid real-asset positions (watches, graded cards, property, vehicles, PC parts, other collectibles) each with cost basis, latest mark + its source/date, and delta; plus sportsbook stakes (open bets at stake) and the total-vs-mark rollup. Use for 'what collectibles do I have', 'what is my Rolex/card/house/car worth' (combined with a valuation question), or the alternatives ledger. NOT in net_liquidation - a parallel record.",
                "parameters": {"type": "object", "properties": {}},
            },
        },
    },
    "add_alternative": {
        "fn": tool_add_alternative,
        "schema": {
            "type": "function",
            "function": {
                "name": "add_alternative",
                "description": "Record an alternative asset into the alternatives book. kind: watch | card | property | vehicle | pc_part | other. description must name the item precisely: reference number for a watch (e.g. Rolex DateJust 41 126334), set + card number + grade for a card (e.g. 2023-24 All Kings Jalen Brunson PSA 9), full address for a property (e.g. 123 Main St Apt 4B, New York NY), model year + trim + drivetrain + options for a vehicle (e.g. 2026 Tesla Model Y Premium AWD, FSD). PSA cert number when one exists. Cost is optional — for property that is purchase price plus capitalized renovation. After adding, research a valuation (hermes_research) and offer to record_alternative_valuation.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "kind": {"type": "string", "enum": ["watch", "card", "property", "vehicle", "pc_part", "other"]},
                        "description": {"type": "string"},
                        "cost": {"type": "number"},
                        "acquired_at": {"type": "string", "description": "YYYY-MM-DD"},
                        "psa_cert": {"type": "string"},
                        "detail": {"type": "string"},
                        "refs": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["kind", "description"],
                },
            },
        },
    },
    "record_alternative_valuation": {
        "fn": tool_record_alternative_valuation,
        "schema": {
            "type": "function",
            "function": {
                "name": "record_alternative_valuation",
                "description": "Record a valuation mark against an alternative item (append-only, provenance mandatory). ONLY after stating the research evidence aloud: value must come from a source read this conversation (WatchCharts/Chrono24 for watches; PSA cert + aggregator sold rows for cards; the exact-grade latest recorded sale is the mark), named in source, dated in as_of (YYYY-MM-DD). If a grade has no recorded sale, report the illiquidity and DO NOT mark - an unknown exit price is a risk, never a hidden upside.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "item_id": {"type": "string"},
                        "value": {"type": "number"},
                        "as_of": {"type": "string"},
                        "source": {"type": "string"},
                        "note": {"type": "string"},
                    },
                    "required": ["item_id", "value", "as_of", "source"],
                },
            },
        },
    },
    "remove_alternative": {
        "fn": tool_remove_alternative,
        "schema": {
            "type": "function",
            "function": {
                "name": "remove_alternative",
                "description": "Remove an alternative item and its valuation history by id (get ids from list_alternatives).",
                "parameters": {
                    "type": "object",
                    "properties": {"item_id": {"type": "string"}},
                    "required": ["item_id"],
                },
            },
        },
    },
    "log_external_pl": {
        "fn": tool_log_external_pl,
        "schema": {
            "type": "function",
            "function": {
                "name": "log_external_pl",
                "description": "Log an external P&L event the operator asserts WITHOUT a document: bank interest from a statement-less savings account (Marcus), referral bonuses, fees. amount is signed (negative = loss/fee); source: bank_interest | bonus | fees | external_other; as_of = event date (statement month-end for interest, YYYY-MM-DD). Flows into realized external P&L surfaced on Overview by source. Confirm the amount and period with the operator before logging.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "amount": {"type": "number"},
                        "source": {"type": "string", "enum": ["bank_interest", "bonus", "fees", "external_other"]},
                        "description": {"type": "string"},
                        "as_of": {"type": "string"},
                        "note": {"type": "string"},
                    },
                    "required": ["amount", "source", "description", "as_of"],
                },
            },
        },
    },
    "edit_external_pl": {
        "fn": tool_edit_external_pl,
        "schema": {
            "type": "function",
            "function": {
                "name": "edit_external_pl",
                "description": "Edit a manual external-P&L entry IN PLACE (id preserved) — the recurring-entry path: 'Marcus YTD interest is now $X' bumps the amount and as_of on the same row instead of a delete+recreate. Requires the entry_id from list_external_pl manual rows (manual: true). Confirm the new amount + period with the operator before editing.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "entry_id": {"type": "string"},
                        "amount": {"type": "number"},
                        "source": {"type": "string", "enum": ["bank_interest", "bonus", "fees", "external_other"]},
                        "description": {"type": "string"},
                        "as_of": {"type": "string"},
                        "note": {"type": "string"},
                    },
                    "required": ["entry_id", "amount", "source", "description", "as_of"],
                },
            },
        },
    },
    "list_external_pl": {
        "fn": tool_list_external_pl,
        "schema": {
            "type": "function",
            "function": {
                "name": "list_external_pl",
                "description": "External P&L ledger by source (sportsbook, prediction markets, staking, bank interest, bonuses) with totals. Use for 'how much interest/bonus/gambling P&L' questions.",
                "parameters": {"type": "object", "properties": {}},
            },
        },
    },
    "get_signals": {
        "fn": tool_get_signals,
        "schema": {
            "type": "function",
            "function": {
                "name": "get_signals",
                "description": "Get technical analysis signals: RSI, MACD, Bollinger Bands, SMA crossover, volume, composite buy/sell score",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string", "description": "Ticker symbol"}
                    },
                    "required": ["symbol"],
                },
            },
        },
    },
    "get_stbr": {
        "fn": tool_get_stbr,
        "schema": {
            "type": "function",
            "function": {
                "name": "get_stbr",
                "description": "Get Short Term Bubble Risk (STBR = price/140-day SMA). Risk levels: Bearish, Neutral, Normal, Heating Up, Risky, Super Risky, Bubble Pop",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string", "description": "Ticker symbol"}
                    },
                    "required": ["symbol"],
                },
            },
        },
    },
    "get_insider_activity": {
        "fn": tool_get_insider_activity,
        "schema": {
            "type": "function",
            "function": {
                "name": "get_insider_activity",
                "description": "Get SEC Form 4 insider trading: buys, sells, net sentiment, recent transactions",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string", "description": "Ticker symbol"}
                    },
                    "required": ["symbol"],
                },
            },
        },
    },
    "get_macro_environment": {
        "fn": tool_get_macro_environment,
        "schema": {
            "type": "function",
            "function": {
                "name": "get_macro_environment",
                "description": "Get macroeconomic signals: yield curve (inverted/flat/normal), credit spreads (stress/tight), fed funds rate (easing/tightening), treasury term structure",
                "parameters": {"type": "object", "properties": {}},
            },
        },
    },
    "get_option_chain_summary": {
        "fn": tool_get_option_chain_summary,
        "schema": {
            "type": "function",
            "function": {
                "name": "get_option_chain_summary",
                "description": "Get option chain overview: put/call volume ratio, average IV, available expirations",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string", "description": "Ticker symbol"}
                    },
                    "required": ["symbol"],
                },
            },
        },
    },
    "get_risk_metrics": {
        "fn": tool_get_risk_metrics,
        "schema": {
            "type": "function",
            "function": {
                "name": "get_risk_metrics",
                "description": "Get risk analysis: beta vs SPY, 95% daily Value-at-Risk, max drawdown with dates and recovery",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string", "description": "Ticker symbol"}
                    },
                    "required": ["symbol"],
                },
            },
        },
    },
    "preview_trade": {
        "fn": tool_preview_trade,
        "schema": {
            "type": "function",
            "function": {
                "name": "preview_trade",
                "description": "Preview a trade with estimated cost. Does NOT execute — no order is placed.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string", "description": "Ticker symbol"},
                        "side": {"type": "string", "enum": ["BUY", "SELL"], "description": "Buy or sell"},
                        "quantity": {"type": "integer", "description": "Number of shares"},
                        "order_type": {"type": "string", "enum": ["MARKET", "LIMIT"], "description": "Order type (default MARKET)"},
                        "price": {"type": "number", "description": "Limit price (required for LIMIT orders)"},
                    },
                    "required": ["symbol", "side", "quantity"],
                },
            },
        },
    },
    "get_risk_momentum": {
        "fn": tool_get_risk_momentum,
        "schema": {
            "type": "function",
            "function": {
                "name": "get_risk_momentum",
                "description": "Compute Risk x Momentum opportunity scores for all portfolio holdings. Score combines RSI (momentum) and STBR (bubble risk). High score = oversold + low risk = buy opportunity. Returns ranked list with tiers: Strong Buy, Buy, Hold, Trim, Reduce.",
                "parameters": {"type": "object", "properties": {}},
            },
        },
    },
    "get_position_intel": {
        "fn": tool_get_position_intel,
        "schema": {
            "type": "function",
            "function": {
                "name": "get_position_intel",
                "description": "Get per-position intelligence for a symbol: MA distances (20/50/100/200), volatility regime, relative strength vs SPY, and liquidity score. Use when analyzing a specific holding or researching a new position.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string", "description": "Ticker symbol"}
                    },
                    "required": ["symbol"],
                },
            },
        },
    },
    "sector_rotation": {
        "fn": tool_sector_rotation,
        "schema": {
            "type": "function",
            "function": {
                "name": "sector_rotation",
                "description": "Get sector rotation signals across S&P 500: which sectors are accelerating, decelerating, leading, lagging. Includes market regime classification (RISK_ON, RISK_OFF, SELECTIVE, TRANSITIONAL).",
                "parameters": {"type": "object", "properties": {}},
            },
        },
    },
    "find_relationships": {
        "fn": tool_find_relationships,
        "schema": {
            "type": "function",
            "function": {
                "name": "find_relationships",
                "description": "Find statistical relationships between tickers: correlation clusters, lead/lag pairs (A predicts B), and correlation regime changes. Use to discover hidden linkages in a set of stocks.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "symbols": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "List of ticker symbols to analyze (e.g. ['NVDA', 'AVGO', 'TSM', 'VST'])",
                        }
                    },
                    "required": ["symbols"],
                },
            },
        },
    },
    "map_value_chain": {
        "fn": tool_map_value_chain,
        "schema": {
            "type": "function",
            "function": {
                "name": "map_value_chain",
                "description": "Research a thematic investment value chain using web search: maps theme to tickers across tiers (direct beneficiaries, suppliers, second-order plays). Identifies bottlenecks and timing. Examples: 'AI infrastructure', 'nuclear energy', 'GLP-1 drugs'.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "theme": {"type": "string", "description": "Investment theme to research (e.g. 'AI data center buildout')"}
                    },
                    "required": ["theme"],
                },
            },
        },
    },
    "get_compute_index": {
        "fn": tool_get_compute_index,
        "schema": {
            "type": "function",
            "function": {
                "name": "get_compute_index",
                "description": "Get the Compute Index — the spot price of compute as a financialized commodity. Returns the TFLOPS-weighted index, median $/GPU-hr, market tightness, and top GPU prices + available-offer counts. Utilization and rented counts are null (the full-market metrics API is host-only). Use for compute prices, GPU economics, AI infrastructure costs, or the financialization of compute.",
                "parameters": {"type": "object", "properties": {}},
            },
        },
    },
    "visualize_network": {
        "fn": tool_visualize_network,
        "schema": {
            "type": "function",
            "function": {
                "name": "visualize_network",
                "description": "Render an interactive network graph showing relationships between companies. Use for value chains, competitive maps, capex flows, supply dependencies. Nodes are companies/entities, edges are relationships.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "Graph title (e.g. 'AI Infrastructure Value Chain')"},
                        "nodes": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "id": {"type": "string", "description": "Ticker or unique ID"},
                                    "label": {"type": "string", "description": "Display name"},
                                    "group": {"type": "string", "description": "Category/tier for coloring (e.g. 'Tier 1 - GPU', 'Tier 2 - Power')"},
                                    "size": {"type": "number", "description": "0-1 scale for importance/market cap relevance. Default 0.5", "default": 0.5},
                                    "description": {"type": "string", "description": "One-line tooltip description (e.g. 'Leading GPU designer, 80% AI training share')"},
                                },
                                "required": ["id", "label"],
                            },
                            "description": "Companies/entities as graph nodes",
                        },
                        "edges": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "source": {"type": "string", "description": "Source node ID"},
                                    "target": {"type": "string", "description": "Target node ID"},
                                    "label": {"type": "string", "description": "Relationship description"},
                                    "strength": {"type": "number", "description": "0-1 strength for line thickness"},
                                    "directional": {"type": "boolean", "description": "True for flow-type edges (capex flow, supply chain, revenue dependency). Default false", "default": False},
                                },
                                "required": ["source", "target"],
                            },
                            "description": "Relationships between nodes",
                        },
                    },
                    "required": ["title", "nodes", "edges"],
                },
            },
        },
    },
    "suggest_followups": {
        "fn": tool_suggest_followups,
        "schema": {
            "type": "function",
            "function": {
                "name": "suggest_followups",
                "description": "Suggest follow-up research topics as clickable buttons. Call this at the end of every report with 2-4 natural next steps the reader might want to explore.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "suggestions": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "label": {"type": "string", "description": "Short button label (e.g. 'Kelly sizing for top picks')"},
                                    "topic": {"type": "string", "description": "Full research topic for follow-up"},
                                    "research_type": {"type": "string", "description": "One of: stock_deep_dive, thematic, portfolio_review, opportunity_scan, macro_brief, custom"},
                                    "symbols": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                        "description": "Optional focus symbols",
                                    },
                                },
                                "required": ["label", "topic", "research_type"],
                            },
                            "description": "2-4 follow-up research suggestions",
                        },
                    },
                    "required": ["suggestions"],
                },
            },
        },
    },
    "visualize_chart": {
        "fn": tool_visualize_chart,
        "schema": {
            "type": "function",
            "function": {
                "name": "visualize_chart",
                "description": "Render a financial chart visualization. Use for capex trends, revenue comparisons, market share, growth rates, valuation multiples over time. Always use this when discussing quantitative data that benefits from visual comparison across companies or time periods.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "Chart title (e.g. 'Hyperscaler Capex Spending ($B)')"},
                        "chart_type": {
                            "type": "string",
                            "enum": ["bar", "grouped_bar", "line", "area", "waterfall", "scatter", "range", "timeline", "heatmap"],
                            "description": "Chart type. line/area = trends over time (supports bands + markers + log scale). bar/grouped_bar = cross-sectional comparison. waterfall = sequential contributions. scatter = two numeric variables. range = TWO series (low bound then high bound) drawn as floating zones — use for scenarios, bottom-case prices and guidance ranges instead of solid bars. timeline = one series per row with exactly two x values (start, end) — use for schedules and phase calendars. heatmap = one series per row, one cell per x — use for returns-by-month and scenario matrices.",
                        },
                        "data": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "label": {"type": "string", "description": "Series name (e.g. company name or metric)"},
                                    "values": {
                                        "type": "array",
                                        "items": {
                                            "type": "object",
                                            "properties": {
                                                "x": {"type": "string", "description": "Category or time label (e.g. '2024', 'Q1 2025', 'MSFT')"},
                                                "y": {"type": "number", "description": "Numeric value"},
                                            },
                                            "required": ["x", "y"],
                                        },
                                    },
                                    "color": {"type": "string", "description": "Optional hex color for this series"},
                                },
                                "required": ["label", "values"],
                            },
                            "description": "Data series to plot. Each series has a label and array of {x, y} points.",
                        },
                        "x_label": {"type": "string", "description": "X-axis label"},
                        "y_label": {"type": "string", "description": "Y-axis label (include unit, e.g. '$B', '%')"},
                        "stacked": {"type": "boolean", "description": "Stack bars/areas (default false)"},
                        "bands": {
                            "type": "array",
                            "description": "Shaded x-ranges on a line/area chart — a cycle window, a recession, a regime. from/to must match x labels that exist in the data.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "from": {"type": "string"},
                                    "to": {"type": "string"},
                                    "label": {"type": "string"},
                                    "tone": {"type": "string", "enum": ["window", "danger", "positive", "neutral"]},
                                },
                                "required": ["from", "to"],
                            },
                        },
                        "markers": {
                            "type": "array",
                            "description": "Anchored points on a line/area chart — the low, today, a level that matters. Keep labels under 24 characters.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "x": {"type": "string"},
                                    "y": {"type": "number"},
                                    "label": {"type": "string"},
                                    "tone": {"type": "string", "enum": ["accent", "positive", "negative", "muted"]},
                                },
                                "required": ["x", "y"],
                            },
                        },
                        "scale": {"type": "string", "enum": ["linear", "log"], "description": "Y-axis scale. Use log for multi-cycle overlays and long growth series."},
                        "value_format": {
                            "type": "string",
                            "enum": ["usd", "pct", "days", "number", "multiple"],
                            "description": "Formats ticks and tooltips in the chart's own unit — usd renders $75.6K, pct -40.1%, days 396d, multiple 3.5x. ALWAYS set it; an unformatted axis is a failed chart.",
                        },
                        "note": {"type": "string", "description": "One-line reading instruction printed under the plot, e.g. 'At day 396 this cycle is 34 points shallower than 2022.'"},
                    },
                    "required": ["title", "chart_type", "data"],
                },
            },
        },
    },
        "visualize_money_flow": {
        "fn": tool_visualize_money_flow,
        "schema": {
            "type": "function",
            "function": {
                "name": "visualize_money_flow",
                "description": "Render a money-flow (Sankey-style) diagram: nodes = entities, flows = {source, target, value, label?} showing where money moves and how much. Use for capex destination flows (hyperscaler -> layer -> line item), supplier revenue concentration, incremental-dollar attribution, segment mix. Direction of each flow = direction of the money.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "Chart title (e.g. 'Hyperscaler Capex Destinations 2026')"},
                        "nodes": {
                            "type": "array",
                            "description": "Entities: [{id, label, group?}] — group colors the node.",
                            "items": {"type": "object", "properties": {"id": {"type": "string"}, "label": {"type": "string"}, "group": {"type": "string"}}},
                        },
                        "flows": {
                            "type": "array",
                            "description": "Money edges: [{source, target, value, label?}] — value in dollars (or %; keep all flows on ONE basis), label is the short edge name (e.g. 'HBM').",
                            "items": {"type": "object", "properties": {"source": {"type": "string"}, "target": {"type": "string"}, "value": {"type": "number"}, "label": {"type": "string"}}},
                        },
                    },
                    "required": ["title", "nodes", "flows"],
                },
            },
        },
    },
"visualize_table": {
        "fn": tool_visualize_table,
        "schema": {
            "type": "function",
            "function": {
                "name": "visualize_table",
                "description": "Render a formatted financial comparison table. Use for company comparisons (revenue, margins, growth), valuation tables, scenario analysis, or any structured multi-column data. Produces a styled table with optional value highlighting.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "Table title (e.g. 'Hyperscaler Comparison — Key Metrics')"},
                        "columns": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "key": {"type": "string", "description": "Column key matching row data keys"},
                                    "label": {"type": "string", "description": "Display header text"},
                                    "format": {
                                        "type": "string",
                                        "enum": ["currency", "percent", "number", "text"],
                                        "description": "How to format values in this column",
                                    },
                                },
                                "required": ["key", "label"],
                            },
                            "description": "Column definitions with keys matching row data",
                        },
                        "rows": {
                            "type": "array",
                            "items": {"type": "object"},
                            "description": "Array of row objects with keys matching column definitions",
                        },
                        "highlight": {
                            "type": "string",
                            "enum": ["max", "min", ""],
                            "description": "Highlight strategy: 'max' highlights highest numeric values green, 'min' highlights lowest green. Empty for no highlighting.",
                        },
                    },
                    "required": ["title", "columns", "rows"],
                },
            },
        },
    },
    "highlight_metric": {
        "fn": tool_highlight_metric,
        "schema": {
            "type": "function",
            "function": {
                "name": "highlight_metric",
                "description": "Pin a key metric to the research canvas metrics strip. Emit 3-5 of the most decision-relevant numbers (beta, STBR, drawdown, insider selling, valuation).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "label": {"type": "string", "description": "Short metric name (e.g. 'Beta', 'STBR', 'Max DD')"},
                        "value": {"type": "string", "description": "Formatted value (e.g. '1.87', '-7.1%', '$410M')"},
                        "delta": {"type": "string", "description": "Optional change indicator (e.g. '+0.3', '3x')"},
                        "color": {"type": "string", "enum": ["positive", "negative", "warning", ""], "description": "Color coding for the value"},
                        "note": {"type": "string", "description": "Optional one-line context"},
                    },
                    "required": ["label", "value"],
                },
            },
        },
    },
    "add_annotation": {
        "fn": tool_add_annotation,
        "schema": {
            "type": "function",
            "function": {
                "name": "add_annotation",
                "description": "Draw a line annotation on a chart (support, resistance, target price, stop level). The chart_id must match a visualize_chart title you emitted.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "chart_id": {"type": "string", "description": "Title of the chart to annotate (must match a visualize_chart title)"},
                        "type": {"type": "string", "enum": ["support", "resistance", "target", "stop", "line"], "description": "Annotation type"},
                        "x": {"type": "string", "description": "X-axis label where the line sits, or 'all' for a horizontal line"},
                        "y": {"type": "number", "description": "Y-axis value of the line"},
                        "label": {"type": "string", "description": "Optional text shown at the line"},
                        "color": {"type": "string", "enum": ["positive", "negative", "warning", "accent", ""], "description": "Line color"},
                    },
                    "required": ["chart_id", "type", "y"],
                },
            },
        },
    },
    "canvas_layout": {
        "fn": tool_canvas_layout,
        "schema": {
            "type": "function",
            "function": {
                "name": "canvas_layout",
                "description": "Specify the research canvas layout: where each artifact sits, its size, and how artifacts connect. Emit ONCE after all visualize_* calls. Artifact ids must match visualize_* titles (or 'graph' for the network).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "artifacts": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "id": {"type": "string", "description": "Artifact id matching a visualize_* title, or 'graph' for the network"},
                                    "type": {"type": "string", "enum": ["graph", "chart", "table", "callout"], "description": "Artifact type"},
                                    "x": {"type": "integer", "description": "Grid column (0-11)"},
                                    "y": {"type": "integer", "description": "Grid row (0-N)"},
                                    "w": {"type": "integer", "description": "Width in columns (1-12)"},
                                    "h": {"type": "integer", "description": "Height in rows (1-4)"},
                                    "connects_to": {"type": "array", "items": {"type": "string"}, "description": "Artifact ids this one links to"},
                                },
                                "required": ["id", "type", "x", "y", "w", "h"],
                            },
                            "description": "Layout for all artifacts",
                        },
                    },
                    "required": ["artifacts"],
                },
            },
        },
    },
    "insight_callout": {
        "fn": tool_insight_callout,
        "schema": {
            "type": "function",
            "function": {
                "name": "insight_callout",
                "description": "Emit a highlighted takeaway box that sits above the report. Use for the single most important insight the fund manager must not miss.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "Short headline (e.g. 'Insider selling accelerating')"},
                        "body": {"type": "string", "description": "1-2 sentence explanation"},
                        "severity": {"type": "string", "enum": ["positive", "negative", "warning", "info"], "description": "Severity / color"},
                    },
                    "required": ["title", "body"],
                },
            },
        },
    },
    "get_factor_exposures": {
        "fn": tool_get_factor_exposures,
        "schema": {
            "type": "function",
            "function": {
                "name": "get_factor_exposures",
                "description": "Compute factor exposures (market, size, value, momentum, quality) for a set of symbols or the current portfolio. Use to answer 'what factors is my portfolio exposed to?', 'is this concentrated in one factor?', or 'how correlated are these names in factor space?'. Returns per-symbol betas + portfolio aggregate.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "symbols": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional list of tickers. Defaults to current portfolio holdings.",
                        },
                    },
                },
            },
        },
    },
}


def _load_browser_tools():
    """Lazily merge search tools into the registry.

    Replaces the old Playwright browser tools (broken in production — browsers
    not installed, systemd ProtectHome=read-only blocks the cache). The search
    tools are plain HTTP and always work.
    """
    if "_browser_tools_loaded" not in TOOLS:
        try:
            from agent.hermes_bridge import HERMES_TOOLS
            from agent.tools_search import FRED_TOOLS, SEARCH_TOOLS
            TOOLS.update(SEARCH_TOOLS)
            TOOLS.update(FRED_TOOLS)
            TOOLS.update(HERMES_TOOLS)
            TOOLS["_browser_tools_loaded"] = {"fn": None, "schema": None}
        except ImportError:
            pass


def get_tool_schemas() -> list[dict]:
    """Return list of Ollama-format tool schemas."""
    _load_browser_tools()
    return [t["schema"] for t in TOOLS.values() if t["schema"] is not None]


def _normalize_result(name: str, result: dict) -> dict:
    """Enforce the tool error contract: every failure carries
    ``{"error": str, "recoverable": bool}``.

    ``recoverable`` tells the agent whether to retry/skip (True — data
    availability, transient) vs explain (False — hard failure). Explicit
    ``{"error": ...}`` returns from tools are data-availability failures
    (recoverable); unexpected exceptions are hard failures (not recoverable).
    """
    if not isinstance(result, dict) or "error" not in result:
        return result
    if "recoverable" in result:
        return result
    return {**result, "recoverable": True}


#: Tools that mutate the operator's records. Everything else is read-only.
#: The dispatcher refuses these when the turn has ingested third-party content,
#: because a write call arriving after a fetched page may be an instruction the
#: page carried rather than the operator's intent.
WRITE_TOOLS = frozenset({
    "add_alternative",
    "add_annotation",
    "record_alternative_valuation",
    "remove_alternative",
    "log_external_pl",
    "edit_external_pl",
})


def tool_effect(name: str) -> str:
    """Return "write" for a record-mutating tool, "read" otherwise."""
    return "write" if name in WRITE_TOOLS else "read"


def _audit_tool_call(name: str, args: dict, *, blocked: bool) -> None:
    """Append every write-tool call to the audit trail. Never raises.

    Before this, a model-issued deletion left no trace at all: the store lost
    the row, one ``.bak`` was replaced, and nothing recorded that a tool did it.
    """
    try:
        from data import audit

        safe = {
            key: value
            for key, value in (args or {}).items()
            if isinstance(value, (str, int, float, bool)) and len(str(value)) <= 120
        }
        audit.record(
            "tool_blocked" if blocked else "tool_write",
            actor="agent",
            after={"tool": name, "args": safe},
            detail=f"{name} ({'blocked' if blocked else 'applied'})",
        )
    except Exception:  # auditing must never break the turn
        pass


def execute_tool(name: str, args: dict, *, allow_write: bool = True) -> dict:
    """Execute a tool by name with the given arguments. Returns result dict.

    ``allow_write=False`` refuses any tool that mutates the operator's records.
    The caller sets it when the conversation has ingested third-party content,
    so an instruction embedded in a page cannot become a deletion.
    """
    _load_browser_tools()
    tool = TOOLS.get(name)
    if tool is None:
        return {"error": f"Unknown tool: {name}", "recoverable": False}

    if name in WRITE_TOOLS and not allow_write:
        _audit_tool_call(name, args, blocked=True)
        return {
            "error": (
                f"{name} changes the operator's records and is blocked in this "
                "conversation: it has ingested third-party content that may carry "
                "instructions. Read-only tools remain available; ask the operator "
                "to make this change directly."
            ),
            "blocked": True,
            "recoverable": False,
        }

    try:
        fn = tool["fn"]
        result = _normalize_result(name, fn(**args))
    except Exception as e:
        error_msg = str(e)[:200]
        return {"error": f"{name} failed: {error_msg}", "tool": name, "recoverable": False}

    if name in WRITE_TOOLS:
        _audit_tool_call(name, args, blocked=False)
    return result
