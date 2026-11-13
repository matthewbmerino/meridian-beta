"""Sizing endpoints: sizing, sizing-preview, backtest."""

from backend.routers.analysis._common import (
    BacktestBody,
    SizingBody,
    SizingPreviewBody,
    log,
    router,
)
from backend.services.sizing_book import (
    build_bucket_stats,
    build_candidate_inputs,
    build_factor_covariance,
    build_pretrade_inputs,
    build_sizing_book,
    resolve_trade,
)
from data.cache import ttl_cache

_MIN_SCORE_RETURN_PAIRS = 50


def _operator_bucket_stats(cfg):
    """The operator's Decision Book buckets for hybrid sizing, or None.

    Gated by the pref flag (default off) AND sufficient resolved samples —
    the hybrid_edge blend enforces its own min_obs per bucket, but we refuse
    to even compute the buckets without a meaningful base of observations.
    Never fabricates: None means "use the market/prior calibration".
    """
    from data.prefs import get as get_pref

    if not bool(get_pref("calibration_operator_outcomes")):
        return None
    try:
        from analysis.outcomes import operator_bucket_stats
        from data.outcomes import list_outcomes
        min_obs = max(10, get_pref("calibration_operator_min_obs") or 30)
        return operator_bucket_stats(list_outcomes(), min_obs=min_obs)
    except Exception as e:
        log.warning("Operator bucket build failed (sizing): %s", e)
        return None


def _calibration_for(symbol: str, score_by_sym: dict, cfg, bucket_stats,
                     operator_bucket_stats=None) -> dict:
    """Return the calibrated (p, b, q) triple for a symbol's score."""
    from analysis.calibration import calibrate_pbq
    score = score_by_sym.get(symbol, 50)
    p, b, q = calibrate_pbq(score, cfg.calibration, bucket_stats, operator_bucket_stats)
    return {
        "p": round(p, 4),
        "b": round(b, 4),
        "q": round(q, 4),
    }


def calibrate_edge_for(symbol: str, score_by_sym: dict, cfg, bucket_stats,
                       operator_bucket_stats=None) -> float:
    """Return the calibrated Kelly edge for a symbol's score."""
    from analysis.calibration import calibrate_edge
    score = score_by_sym.get(symbol, 50)
    return calibrate_edge(score, cfg.calibration, bucket_stats, operator_bucket_stats)


@router.post("/sizing")
def position_sizing(body: SizingBody):
    return _cached_position_sizing(body)


def _position_sizing_compute(body: SizingBody):
    from analysis.sizing import compute_target_weights, config_from_prefs
    from data.account import get_combined_positions, get_combined_summary
    from data.prefs import get_prefs

    positions = get_combined_positions()
    summary = get_combined_summary(positions)
    prefs = get_prefs()
    cfg = config_from_prefs(prefs)
    nav = summary.get("net_liquidation", 0)

    if positions.empty or nav <= 0:
        return {"results": [], "nav": nav}

    equity_pos = positions[positions["asset_type"] != "OPTION"]
    # "held" is the full book, independent of the symbols filter — a candidate
    # is by definition a name not held, and must never be sized as a candidate
    # merely because the symbols filter excluded it.
    held_set = {s.upper() for s in equity_pos["symbol"].unique() if s}
    if body.symbols:
        equity_pos = equity_pos[equity_pos["symbol"].isin([s.upper() for s in body.symbols])]

    held_inputs, score_by_sym, sector_map = build_sizing_book(equity_pos, nav)

    # Candidates: names not already held — sized prospectively at weight 0
    # (same score/sigma math as held names, so targets are comparable).
    candidate_syms = [s for s in (body.candidates or []) if s.upper() not in held_set]
    candidate_inputs, candidate_scores, _sector_map = build_candidate_inputs(
        candidate_syms, nav, sector_map)
    inputs = held_inputs + candidate_inputs
    score_by_sym = score_by_sym | candidate_scores

    if not inputs:
        return {"results": [], "nav": nav}

    bucket_stats = None
    try:
        # Calibration buckets come from the held book only, so adding
        # candidates never changes how a held position is calibrated.
        bucket_stats = build_bucket_stats([inp.symbol for inp in held_inputs], cfg)
    except Exception as e:
        log.warning("Empirical bucket build failed (sizing): %s", e)

    op_buckets = _operator_bucket_stats(cfg)
    covariance = build_factor_covariance([inp.symbol for inp in inputs])
    results = compute_target_weights(inputs, nav, cfg, bucket_stats, covariance=covariance,
                                     operator_bucket_stats=op_buckets)
    return {
        "nav": nav,
        "calibration_method": cfg.calibration.method,
        "empirical_buckets_available": bucket_stats is not None,
        "operator_calibration_used": op_buckets is not None,
        "factor_covariance_used": covariance is not None,
        "results": [
            {
                "symbol": r.symbol,
                "score": score_by_sym.get(r.symbol, 0),
                "tier": r.tier,
                "candidate": r.symbol not in held_set,
                "current_weight": round(r.current_weight, 4),
                "target_weight": round(r.target_weight, 4),
                "delta_weight": round(r.delta_weight, 4),
                "delta_dollars": round(r.delta_dollars, 2),
                "action": r.action,
                "rationale": r.rationale,
                "constraint": r.constraint,
                "edge": round(calibrate_edge_for(r.symbol, score_by_sym, cfg, bucket_stats, op_buckets), 4),
                "calibration": _calibration_for(r.symbol, score_by_sym, cfg, bucket_stats, op_buckets),
                "sigma": round(r.sigma_used, 4),
            }
            for r in results
        ],
    }


# TTL-cached (short): sizing reads the LIVE book, so staleness stays within a minute.
_cached_position_sizing = ttl_cache(ttl_seconds=60)(_position_sizing_compute)


@router.post("/sizing-preview")
def sizing_preview(body: SizingPreviewBody):
    """Risk-budget verdict on a hypothetical trade.

    Constructs the hypothetical position, inserts it into the current book,
    and re-runs the sizing pipeline. The verdict answers: is this trade
    within w_cap / sector cap / cash floor? What does sizing *actually want*
    the position to be? What gets displaced? Does NOT execute anything.
    """
    from analysis.risk import daily_returns
    from analysis.signals import generate_signals
    from analysis.sizing import (
        PositionInput,
        compute_post_trade_weight,
        compute_target_weights,
        config_from_prefs,
        portfolio_sigma,
    )
    from data.account import get_combined_positions, get_combined_summary
    from data.market import get_price_history
    from data.prefs import get_prefs

    sym = body.symbol.upper()
    side = body.side.upper()
    if side not in ("BUY", "SELL"):
        return {"available": False, "error": f"side must be BUY or SELL, got {body.side}"}

    try:
        resolved = resolve_trade(sym, body.price, body.dollars, body.quantity, caller="sizing-preview")
    except ValueError as e:
        return {"available": False, "error": str(e)}
    exec_price = resolved["exec_price"]
    trade_dollars = resolved["trade_dollars"]

    # --- Base book: current holdings, same construction as /sizing ---
    positions = get_combined_positions()
    summary = get_combined_summary(positions)
    prefs = get_prefs()
    cfg = config_from_prefs(prefs)
    nav = float(summary.get("net_liquidation", 0))

    if positions.empty or nav <= 0:
        return {"available": False,
                "error": "No positions — import positions on the Data page to size hypothetical trades"}

    equity_pos = positions[positions["asset_type"] != "OPTION"].copy()
    inputs, score_by_sym, sector_map = build_sizing_book(equity_pos, nav)
    if not inputs:
        return {"available": False, "error": "No equity positions to size against"}

    # --- Hypothetical position ---
    existing_row = equity_pos[equity_pos["symbol"] == sym]
    existing_mv = float(existing_row["market_value"].iloc[0]) if not existing_row.empty else 0.0
    is_held = not existing_row.empty

    post_weight = float(compute_post_trade_weight(existing_mv, trade_dollars, nav, side))
    if post_weight < 0:
        return {"available": False, "error": f"SELL {trade_dollars:.0f} exceeds the {sym} position ({existing_mv:.0f})"}

    # Signals for the hypothetical name (same path as held positions).
    df_sym = get_price_history(sym)
    if df_sym.empty:
        return {"available": False, "error": f"Insufficient data for {sym} signals"}
    sig_sym = generate_signals(sym, df_sym)
    score_sym = sig_sym.get("composite_score", 50)
    rets_sym = daily_returns(df_sym)
    sigma_sym = float(rets_sym.std() * (252 ** 0.5)) if len(rets_sym) > 20 else 0.25
    sector_sym = sector_map.get(sym)
    if sector_sym is None:
        try:
            from data.reference import get_sector_map
            sector_sym = (get_sector_map([sym]) or {}).get(sym)
        except Exception as e:
            log.warning("Sector lookup failed for %s (sizing-preview): %s", sym, e)

    post_nav = float(nav + (trade_dollars if side == "BUY" else -trade_dollars))
    if post_nav <= 0:
        return {"available": False, "error": "Trade would drive NAV to zero"}

    holding_days = None
    unrealized_pct = None
    if is_held:
        avg_cost = float(existing_row["avg_cost"].iloc[0]) or 0
        if avg_cost > 0:
            unrealized_pct = float((exec_price - avg_cost) / avg_cost)
        # holding_days not tracked in imports — skip tax-deferral on hypothetical.

    hypothetical = PositionInput(
        symbol=sym,
        score=score_sym,
        current_weight=post_weight,
        sigma=sigma_sym,
        sector=sector_sym or "",
        holding_days=holding_days,
        unrealized_pct=unrealized_pct,
    )

    aug_inputs = inputs + [hypothetical]
    bucket_stats = None
    try:
        held_symbols = [inp.symbol for inp in aug_inputs]
        bucket_stats = build_bucket_stats(held_symbols, cfg)
    except Exception as e:
        log.warning("Empirical bucket build failed (sizing-preview): %s", e)

    op_buckets = _operator_bucket_stats(cfg)
    covariance = build_factor_covariance([inp.symbol for inp in aug_inputs])
    results = compute_target_weights(aug_inputs, post_nav, cfg, bucket_stats, covariance=covariance,
                                     operator_bucket_stats=op_buckets)

    # --- Verdict + impact ---
    verdict = None
    displaced = []
    score_by_sym_all = score_by_sym | {sym: score_sym}
    for r in results:
        entry = {
            "symbol": r.symbol,
            "tier": r.tier,
            "current_weight": round(r.current_weight, 4),
            "target_weight": round(r.target_weight, 4),
            "delta_weight": round(r.delta_weight, 4),
            "delta_dollars": round(r.delta_dollars, 2),
            "action": r.action,
            "rationale": r.rationale,
            "constraint": r.constraint,
            "edge": round(calibrate_edge_for(r.symbol, score_by_sym_all, cfg, bucket_stats, op_buckets), 4),
            "calibration": _calibration_for(r.symbol, score_by_sym_all, cfg, bucket_stats, op_buckets),
            "sigma": round(r.sigma_used, 4),
        }
        if r.symbol == sym:
            verdict = entry
        elif r.delta_dollars < 0:
            displaced.append(entry)

    if verdict is None:
        return {"available": False, "error": "Sizing produced no verdict"}

    # Constraint audit for the hypothetical.
    input_sector = {inp.symbol: (inp.sector or "") for inp in aug_inputs}
    cap_breach = {
        "w_cap": {
            "cap": round(cfg.w_cap, 4),
            "requested": round(post_weight, 4),
            "breached": post_weight > cfg.w_cap + 1e-6,
        },
        "sector_cap": {"cap": round(cfg.sector_cap, 4), "sector": sector_sym, "breached": False, "post_trade": None},
        "cash_floor": {"min_cash": round(cfg.cash_floor, 4), "post_trade_cash": None, "breached": False},
    }
    if sector_sym:
        total = sum(
            r.current_weight for r in results
            if r.symbol != sym and input_sector.get(r.symbol) == sector_sym
        )
        post_trade_sec = total + post_weight
        cap_breach["sector_cap"]["post_trade"] = round(post_trade_sec, 4)
        cap_breach["sector_cap"]["breached"] = post_trade_sec > cfg.sector_cap + 1e-6
    gross_after = sum(r.current_weight for r in results)
    cash_after = 1.0 - gross_after
    cap_breach["cash_floor"]["post_trade_cash"] = round(cash_after, 4)
    cap_breach["cash_floor"]["breached"] = cash_after < cfg.cash_floor - 1e-6

    # Portfolio vol impact (stage-6 formula on post-trade book). Apply the
    # same low-vol floor compute_target_weights uses so before/after is fair.
    from statistics import median
    sigmas_present = [r.sigma for r in inputs if r.sigma and r.sigma > 0]
    sigma_floor = cfg.low_vol_floor_multiplier * (median(sigmas_present) if sigmas_present else cfg.default_sigma)

    def _floored(r):
        return max(r.sigma or cfg.default_sigma, sigma_floor)

    rho = cfg.corr_assumed
    gross_before = sum(r.current_weight for r in inputs)
    sigma_before = portfolio_sigma(
        [r.current_weight for r in inputs],
        [_floored(r) for r in inputs],
        rho,
    )
    sigma_after = portfolio_sigma(
        [r.current_weight for r in results],
        [r.sigma_used for r in results],
        rho,
    )

    return {
        "available": True,
        "symbol": sym,
        "side": side,
        "nav": round(post_nav, 2),
        "requested": {
            "shares": round(body.quantity, 4) if body.quantity else round(trade_dollars / exec_price, 4),
            "price": round(exec_price, 2),
            "dollars": round(trade_dollars, 2),
            "weight": round(post_weight, 4),
        },
        "verdict": verdict,
        "cap_breach": cap_breach,
        "portfolio_impact": {
            "gross_before": round(gross_before, 4),
            "gross_after": round(gross_after, 4),
            "sigma_before": round(sigma_before, 4),
            "sigma_after": round(sigma_after, 4),
            "cash_after": round(cash_after, 4),
        },
        "displaced": sorted(displaced, key=lambda d: d["delta_dollars"])[:5],
        "calibration_method": cfg.calibration.method,
        "empirical_buckets_available": bucket_stats is not None,
        "factor_covariance_used": covariance is not None,
    }


@router.post("/pretrade-impact")
def pretrade_impact_endpoint(body: SizingPreviewBody):
    """Risk impact of a hypothetical trade: VaR, vol, beta, concentration.

    Complements /sizing-preview (which answers "what weight does sizing
    want") with "what does the trade do to the book's risk profile".
    """
    from analysis.pretrade import pretrade_impact
    from data.account import get_combined_positions, get_combined_summary

    sym = body.symbol.upper()
    side = body.side.upper()
    if side not in ("BUY", "SELL"):
        return {"available": False, "error": f"side must be BUY or SELL, got {body.side}"}

    try:
        resolved = resolve_trade(sym, body.price, body.dollars, body.quantity, caller="pretrade")
    except ValueError as e:
        return {"available": False, "error": str(e)}
    trade_dollars = resolved["trade_dollars"]

    positions = get_combined_positions()
    summary = get_combined_summary(positions)
    nav = float(summary.get("net_liquidation", 0))
    if positions.empty or nav <= 0:
        return {"available": False, "error": "No positions — import positions on the Data page"}

    equity_pos = positions[positions["asset_type"] != "OPTION"]
    inputs = build_pretrade_inputs(equity_pos, nav, sym)
    if inputs is None:
        return {"available": False, "error": f"Insufficient data for {sym}"}
    current_weights, returns_dict = inputs

    result = pretrade_impact(
        current_weights, returns_dict, sym, trade_dollars, nav, side
    )
    return result


@router.post("/backtest")
def backtest(body: BacktestBody):
    """Walk-forward backtest of the sizing system vs a buy-and-hold benchmark."""
    import datetime

    from analysis.backtest import run_backtest
    from analysis.sizing import config_from_prefs
    from data.account import get_combined_positions
    from data.market import get_price_history
    from data.prefs import get_prefs

    symbols = body.symbols
    if not symbols:
        positions = get_combined_positions()
        if positions is None or positions.empty:
            return {"error": "No holdings to backtest — pass symbols or import positions"}
        equities = positions[positions["asset_type"] != "OPTION"]
        symbols = [s for s in equities["symbol"].unique() if s]
    symbols = [s.upper() for s in symbols]

    if not symbols:
        return {"error": "No symbols to backtest"}

    start = None
    end = None
    if body.start:
        try:
            start = datetime.datetime.fromisoformat(body.start)
        except ValueError:
            log.warning("Backtest start date invalid: %r", body.start)
    if body.end:
        try:
            end = datetime.datetime.fromisoformat(body.end)
        except ValueError:
            log.warning("Backtest end date invalid: %r", body.end)

    price_data = {}
    for sym in symbols:
        try:
            df = get_price_history(sym, days=1825)
            if not df.empty:
                price_data[sym] = df
        except Exception as e:
            log.warning("Backtest price history failed for %s: %s", sym, e)
            continue

    if len(price_data) < 2:
        return {"error": "Insufficient price data — need at least 2 symbols with history"}

    cfg = config_from_prefs(get_prefs())
    result = run_backtest(
        symbols,
        price_data,
        cfg=cfg,
        start=start,
        end=end,
        rebalance_days=max(5, min(body.rebalance_days, 63)),
    )
    return result
