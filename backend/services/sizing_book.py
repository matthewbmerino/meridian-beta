"""Shared sizing book construction — one source for every sizing surface.

The /api/analysis/sizing endpoints and the Compass ``get_risk_momentum`` tool
must construct the identical book (same symbols, same weights, same sigma) or
Compass can recommend a different size than the Opportunities page shows for
the same position. This module is the single implementation both consume.

Lives in ``backend/services/`` (not ``analysis/``) because it fetches data —
it is orchestration, not pure computation. ``agent/`` already imports from
``backend/services/`` (see ``agent/history.py``), so the tool can use it
without violating the layer boundary.
"""

import logging

import pandas as pd

log = logging.getLogger(__name__)

_BUCKET_STATS_TTL = 3600
_MIN_SCORE_RETURN_PAIRS = 50


def load_equity_book():
    """Load the current equity book + NAV, or None when there's no portfolio.

    Returns ``(equity_pos, nav)`` where ``equity_pos`` is the equity-only
    positions frame and ``nav`` is net liquidation. Returns None when there
    are no positions or NAV is non-positive (nothing to size).
    """
    from data.account import get_combined_positions, get_combined_summary

    positions = get_combined_positions()
    if positions is None or positions.empty:
        return None
    summary = get_combined_summary(positions)
    nav = float(summary.get("net_liquidation", 0) or 0)
    if nav <= 0:
        return None
    equity_pos = positions[positions["asset_type"] != "OPTION"]
    return equity_pos, nav


def _signal_input(sym: str, market_value: float, nav: float, sector: str):
    """Score, sigma, and a PositionInput for one symbol — or None if no history.

    The single implementation of the score/sigma math every sizing surface
    uses: same composite score, same full-sample annualized sigma, same
    ``0.25`` fallback when history is thin. A candidate sized here is
    therefore identical to a held name sized here.
    """
    from analysis.risk import daily_returns
    from analysis.signals import generate_signals
    from analysis.sizing import PositionInput
    from data.market import get_price_history

    df = get_price_history(sym)
    if df.empty:
        return None
    sig = generate_signals(sym, df)
    score = sig.get("composite_score", 50)
    rets = daily_returns(df)
    sigma = float(rets.std() * (252 ** 0.5)) if len(rets) > 20 else 0.25

    inp = PositionInput(
        symbol=sym,
        score=score,
        sigma=sigma,
        current_weight=float(market_value) / nav,
        sector=sector,
    )
    return inp, score


def build_sizing_book(equity_pos, nav: float) -> tuple[list, dict, dict]:
    """Build the PositionInput list for a book of equity positions.

    Aggregates duplicate symbols across brokerages (a name held in both
    Schwab and Chase is one position, summed market value) — the same merge
    the Overview page applies. Returns ``(inputs, score_by_sym, sector_map)``.
    Shared by /sizing, /sizing-preview, and the get_risk_momentum tool so
    every surface constructs the identical book.
    """
    sector_map = {}
    try:
        from data.reference import get_sector_map
        held = [s.upper() for s in equity_pos["symbol"].unique() if s]
        sector_map = get_sector_map(held) or {}
    except Exception as e:
        log.warning("Sector map fetch failed (sizing book): %s", e)

    agg = equity_pos.groupby("symbol", dropna=False).agg(
        market_value=("market_value", "sum"),
    ).to_dict("index")

    inputs = []
    score_by_sym = {}
    for sym, row in agg.items():
        built = _signal_input(sym, row["market_value"], nav, sector_map.get(sym, ""))
        if built is None:
            continue
        inp, score = built
        inputs.append(inp)
        score_by_sym[sym] = score

    return inputs, score_by_sym, sector_map


def build_candidate_inputs(candidates: list[str], nav: float,
                           sector_map: dict | None = None) -> tuple[list, dict, dict]:
    """Prospective PositionInputs for symbols not currently held.

    ``current_weight`` is 0 — the trade hasn't happened, so the sizing engine
    asks "what should this name be?" rather than assuming a guessed dollar
    amount (the /sizing-preview weakness). Uses the same ``_signal_input``
    math as held names, so a candidate's score/sigma/target are directly
    comparable to the book's. Returns ``(inputs, score_by_sym, sector_map)``.
    Callers must exclude already-held symbols.
    """
    sector_map = dict(sector_map or {})
    wanted = list(dict.fromkeys(s.upper() for s in candidates if s))
    missing = [s for s in wanted if s not in sector_map]
    if missing:
        try:
            from data.reference import get_sector_map
            sector_map.update(get_sector_map(missing) or {})
        except Exception as e:
            log.warning("Sector map fetch failed (candidates): %s", e)

    inputs = []
    score_by_sym = {}
    for sym in wanted:
        built = _signal_input(sym, 0.0, nav, sector_map.get(sym, ""))
        if built is None:
            continue
        inp, score = built
        inputs.append(inp)
        score_by_sym[sym] = score

    return inputs, score_by_sym, sector_map


def build_bucket_stats(symbols: list[str], cfg) -> dict | None:
    """Empirical score→return calibration buckets for a symbol set, or None."""
    from analysis.calibration import compute_empirical_buckets
    from analysis.signals import generate_signals
    from data.cache import ttl_cache
    from data.market import get_price_history

    @ttl_cache(ttl_seconds=_BUCKET_STATS_TTL)
    def _compute(symbols_key: tuple[str, ...], forward_days: int, method: str):
        score_return_pairs: list[tuple[float, float]] = []

        for sym in symbols_key:
            df = get_price_history(sym)
            if df.empty or len(df) < 60:
                continue

            close = df["close"].values
            step = 5
            max_i = len(df) - forward_days
            for i in range(50, max_i, step):
                sig = generate_signals(sym, df.iloc[:i + 1])
                score = sig.get("composite_score")
                if score is None:
                    continue
                fwd_ret = float(close[i + forward_days] / close[i] - 1)
                score_return_pairs.append((float(score), fwd_ret))

        if len(score_return_pairs) < _MIN_SCORE_RETURN_PAIRS:
            return None
        return compute_empirical_buckets(score_return_pairs, cfg.calibration)

    return _compute(tuple(sorted(s.upper() for s in symbols)), cfg.calibration.forward_days, cfg.calibration.method)


def build_factor_model(symbols: tuple[str, ...]):
    """Fetch returns + characteristics and build the factor model.

    Returns (returns_dict, factor_df, exposures) or (None, None, None) when
    the model is unusable. Deterministic given the symbol set, so the heavy
    parts are TTL-cached. Shared by the factors endpoints and the sizing
    pipeline so the Risk page and sizing never diverge.
    """
    from data.cache import ttl_cache

    @ttl_cache(ttl_seconds=300)
    def _compute(symbols_key: tuple[str, ...]):
        from analysis.factors import factor_exposures, factor_returns
        from analysis.risk import daily_returns
        from data.market import get_fundamentals, get_price_history, get_quotes

        returns_dict = {}
        for sym in symbols_key:
            df = get_price_history(sym)
            if not df.empty:
                returns_dict[sym] = daily_returns(df)
        if len(returns_dict) < 2:
            return None

        # Market returns from SPY (the benchmark).
        spy_df = get_price_history("SPY")
        if spy_df.empty:
            return None
        market_returns = daily_returns(spy_df)

        # Cross-sectional characteristics: market cap is the size characteristic.
        # Value/quality characteristics aren't fetched from the feed yet, so
        # those factor columns are omitted (factor_returns degrades gracefully).
        market_caps: dict[str, float] = {}
        for sym in symbols_key:
            try:
                raw = get_fundamentals(sym)
                inst = raw.get("instruments", [])
                if not inst:
                    continue
                fund = inst[0].get("fundamental", {})
                mc = fund.get("marketCap")
                if mc:
                    market_caps[sym] = float(mc)
            except Exception as e:
                log.warning("Factor fundamentals fetch failed for %s: %s", sym, e)

        # Fall back to quote-derived market caps for names without fundamentals.
        missing = [s for s in symbols_key if s not in market_caps]
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
            except Exception as e:
                log.warning("Quote-derived market-cap fallback failed: %s", e)

        # Market cap as the size characteristic; value/quality columns are
        # omitted because the feed doesn't provide those characteristics.
        factor_df = factor_returns(
            returns_dict,
            market_returns,
            market_caps=market_caps or None,
        )
        if factor_df.empty:
            return None
        exposures = factor_exposures(returns_dict, factor_df)
        return returns_dict, factor_df, exposures

    return _compute(symbols)


def build_factor_covariance(symbols: list[str]):
    """Factor-aware covariance for a symbol set, or None when unusable.

    Reuses the TTL-cached factor model so the sizing pipeline and the Risk
    page never diverge.
    """
    try:
        result = build_factor_model(tuple(s.upper() for s in symbols))
        if result is None:
            return None
        returns_dict, factor_df, exposures = result
        from analysis.factors import factor_covariance
        cov = factor_covariance(returns_dict, factor_df, exposures)
        return cov if not cov.empty else None
    except Exception as e:
        log.warning("Factor covariance build failed (sizing): %s", e)
        return None


def resolve_trade(sym: str, price: float | None, dollars: float | None,
                  quantity: float | None, caller: str) -> dict:
    """Resolve the execution price + notional of a hypothetical trade.

    Returns {"exec_price": float, "trade_dollars": float} or raises a
    ValueError whose message is safe to surface to the operator. Shared by
    /sizing-preview, /pretrade-impact, and the preview_trade tool so the
    three surfaces can't drift on price/notional resolution.
    """
    from data.account import extract_live_price
    from data.market import get_quotes

    if price and price > 0:
        exec_price = float(price)
    else:
        try:
            quotes = get_quotes([sym])
            qdata = quotes.get(sym, {})
            price, _, _, _, _ = extract_live_price(qdata)
        except Exception as e:
            log.warning("Live price fetch failed for %s (%s): %s", sym, caller, e)
            price = None
        if not price or price <= 0:
            raise ValueError(f"No price data for {sym} — pass an explicit price")
        exec_price = float(price)

    if dollars and dollars > 0:
        trade_dollars = float(dollars)
    elif quantity and quantity > 0:
        trade_dollars = float(quantity) * exec_price
    else:
        raise ValueError("Provide quantity (shares) or dollars")

    return {"exec_price": exec_price, "trade_dollars": trade_dollars}


def build_pretrade_inputs(equity_pos, nav: float, trade_symbol: str):
    """Build the pretrade-impact inputs for a book + hypothetical trade.

    Returns (current_weights, returns_dict) where returns_dict includes SPY
    as the first (beta-proxy) symbol and guarantees the trade symbol's returns
    are present. Returns None when the book is unusable. Shared by
    /pretrade-impact and the preview_trade tool.
    """
    from analysis.risk import daily_returns
    from data.market import get_price_history

    current_weights: dict[str, float] = {}
    returns_dict: dict[str, pd.Series] = {}
    for _, row in equity_pos.iterrows():
        s = row["symbol"]
        df = get_price_history(s)
        if df.empty:
            continue
        returns_dict[s] = daily_returns(df)
        current_weights[s] = float(row.get("market_value", 0)) / nav

    # Ensure the trade symbol's returns are present (new name).
    if trade_symbol not in returns_dict:
        df = get_price_history(trade_symbol)
        if df.empty:
            return None
        returns_dict[trade_symbol] = daily_returns(df)

    # Add SPY as the beta proxy (first symbol).
    spy_df = get_price_history("SPY")
    if not spy_df.empty:
        returns_dict = {"SPY": daily_returns(spy_df), **returns_dict}

    return current_weights, returns_dict
