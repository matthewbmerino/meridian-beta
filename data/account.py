"""Account data: positions, balances — imports-only mode (no broker connection)."""

import logging

import pandas as pd

log = logging.getLogger(__name__)


def extract_live_price(qdata):
    """Extract live price, net change, pct change, staleness, and session.

    Supports the Schwab-compatible shape produced by massive.get_quotes_schwab_compat().
    Returns ``(price, net_change, net_pct, stale, session)`` where ``stale`` is
    True when the price came from a fallback (zeroed day bar) *during the
    regular session* — a pre-market/after-hours/closed print is live data, not
    stale. ``session`` is the market-session label ("regular", "pre-market",
    "after-hours", "closed").
    """
    regular = qdata.get("regular", {})
    quote = qdata.get("quote", {})
    price = (
        regular.get("regularMarketLastPrice")
        or regular.get("lastPrice")
        or quote.get("lastPrice")
    )
    net_change = (
        regular.get("regularMarketNetChange")
        or regular.get("netChange")
        or quote.get("netChange")
    )
    net_pct = (
        regular.get("regularMarketPercentChange")
        or regular.get("netPercentChangeInDouble")
        or quote.get("netPercentChangeInDouble")
    )
    stale = bool(quote.get("stale"))
    session = quote.get("session") or "regular"
    return price, net_change, net_pct, stale, session


def _refresh_positions(df, quotes):
    """Refresh current_price, market_value, total_pl, day_pl from live quotes."""
    if "stale" not in df.columns:
        df["stale"] = False
    if "session" not in df.columns:
        df["session"] = "regular"
    for idx, row in df.iterrows():
        sym = row["symbol"]
        if row.get("asset_type") == "OPTION":
            continue

        qdata = quotes.get(sym, {})
        if not qdata:
            # Symbol missing from the quote response — mark stale rather than
            # silently keeping the import price.
            df.at[idx, "stale"] = True
            continue
        price, net_change, net_pct, stale, session = extract_live_price(qdata)
        if not price:
            # No price at all (feed down, symbol missing) — mark the row stale
            # so consumers can grey it. Never silently leave import prices
            # looking live.
            df.at[idx, "stale"] = True
            continue

        qty = row.get("quantity", 0)
        mult = row.get("option_multiplier", 1)
        if pd.isna(mult) or mult == 0:
            mult = 1
        avg_cost = row.get("avg_cost", 0) or 0

        if avg_cost == 0 and qty != 0:
            mv = row.get("market_value", 0) or 0
            tpl = row.get("total_pl", 0) or 0
            if mv != 0 and tpl != 0:
                cost_basis = mv - tpl
                avg_cost = cost_basis / (qty * mult)
                if avg_cost > 0:
                    df.at[idx, "avg_cost"] = avg_cost

        df.at[idx, "current_price"] = price
        df.at[idx, "market_value"] = price * qty * mult
        df.at[idx, "stale"] = stale
        df.at[idx, "session"] = session
        if avg_cost > 0:
            df.at[idx, "total_pl"] = (price - avg_cost) * qty * mult

        if net_change is not None:
            df.at[idx, "day_pl"] = net_change * qty * mult
        if net_pct is not None:
            df.at[idx, "day_pl_pct"] = net_pct


def get_combined_positions():
    """Return imported positions, refreshed with live Massive quotes."""
    from data.imports import get_all_visible_positions
    from data.market import get_quotes

    imported = get_all_visible_positions()
    if imported.empty:
        return pd.DataFrame()

    # Equity-only symbol set (option quotes aren't available via this feed).
    equities = imported[imported["asset_type"] != "OPTION"]
    all_symbols = set(equities["symbol"].dropna().unique())

    quotes = {}
    if all_symbols:
        try:
            quotes = get_quotes(tuple(sorted(all_symbols)))
        except Exception as e:
            log.warning("Live quote refresh failed for %d symbols: %s", len(all_symbols), e)

    if quotes:
        _refresh_positions(imported, quotes)
    else:
        # No quotes at all (feed down) — every position is stale. Never leave
        # import prices looking live.
        imported["stale"] = True
        imported["session"] = "closed"

    return imported

def get_combined_summary(positions=None):
    """Compute account summary from imported positions + cash override.

    ``positions`` is optional: pass the result of ``get_combined_positions()``
    when the caller already has it (it fetches live quotes, so calling both
    functions separately doubles the quote fetch). When omitted, positions are
    fetched here.
    """
    from data import prefs as _prefs
    from data.external_cash import get_cash_totals
    from data.imports import get_all_visible_summaries

    try:
        cash_override = float(_prefs.get("cash_override") or 0)
    except (TypeError, ValueError):
        cash_override = 0.0

    ext = get_cash_totals()
    external_cash_total = ext["total"]
    external_cash_liquid = ext["liquid"]
    imported = get_all_visible_summaries()

    if positions is None:
        positions = get_combined_positions()
    if positions.empty:
        long_mv = 0
        short_mv = 0
    else:
        long_mv = positions.loc[positions["market_value"] > 0, "market_value"].sum()
        short_mv = positions.loc[positions["market_value"] < 0, "market_value"].sum()

    # Cash comes from the explicit override when positive, otherwise from the
    # imports summary (0 when nothing is imported) plus LIQUID external
    # balances (kind=bank). External balances roll into NAV always; only the
    # liquid kind rolls into the balance. Illiquid external (kind=sportsbook —
    # withdrawable balance + open bets at stake, binary outcome) is NAV-only:
    # not cash, not buying power. Nothing external is buying power — it isn't
    # deployable at the brokerage.
    if cash_override > 0:
        cash = cash_override
        brokerage_cash = cash_override
    elif imported:
        brokerage_cash = imported.get("cash_balance", 0)
        cash = brokerage_cash + external_cash_liquid
    else:
        brokerage_cash = 0
        cash = external_cash_liquid

    # NAV = positions + cash + ALL external balances' illiquid remainder.
    # - No override: `cash` = brokerage + liquid external, so adding the
    #   illiquid stake (total − liquid) yields positions + brokerage + ALL
    #   external — identical to the old arithmetic when nothing is illiquid.
    # - Override: `cash` is the override alone; the illiquid stake is still
    #   added (an override of "cash I actually control" says nothing about
    #   committed stakes), while the liquid external stays excluded — the
    #   override wins over imports AND external cash, no double counting.
    external_illiquid = external_cash_total - external_cash_liquid
    net_liquidation = long_mv + short_mv + cash + external_illiquid

    return {
        "net_liquidation": net_liquidation,
        "cash_balance": cash,
        "buying_power": brokerage_cash,
        "long_market_value": long_mv,
        "short_market_value": short_mv,
        "buying_power_day_trades": 0,
        "cash_override": cash_override,
        "external_cash_total": external_cash_total,
        "external_cash_liquid": external_cash_liquid,
    }


def _num(v) -> float:
    """Coerce a possibly-None/NaN numeric to a plain float (never None)."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    if f != f or f in (float("inf"), float("-inf")):
        return 0.0
    return round(f, 2)


def get_account_breakdown() -> dict:
    """Cash and balances broken out by *where they are held*.

    ``get_combined_summary`` merges every source into single scalars, so it can
    answer "how much cash" but not "at which brokerage". This returns the
    per-location detail:

      - ``brokerages``: one row per visible positions import (Chase, Schwab),
        with that import's own cash / market value / net liquidation. The
        broker label is content-derived where possible (see imports.py).
      - ``external``: institution-level balance entries with their ``kind``
        (bank = liquid cash; sportsbook = illiquid stake, NAV-only) plus the
        total. External balances roll into NAV but NOT buying power; only the
        bank kind rolls into ``cash_balance``.
      - ``aggregate``: the merged view from ``get_combined_summary`` so both
        numbers are present and their relationship is explicit.

    Read-only; does not refresh quotes. Never fabricates a location — an
    import without a summary is omitted.
    """
    from data.external_cash import list_external_cash, normalize_kind
    from data.imports import get_visible_imports_with_summary

    brokerages = []
    for entry in get_visible_imports_with_summary():
        s = entry.get("summary") or {}
        brokerages.append({
            "source": entry.get("source", "Import"),
            "filename": entry.get("filename", ""),
            "cash_balance": _num(s.get("cash_balance")),
            "long_market_value": _num(s.get("long_market_value")),
            "short_market_value": _num(s.get("short_market_value")),
            "net_liquidation": _num(s.get("net_liquidation")),
        })

    external = []
    for e in list_external_cash():
        try:
            amount = float(e.get("amount", 0))
        except (TypeError, ValueError):
            continue
        external.append({
            "institution": e.get("institution", ""),
            "kind": normalize_kind(e.get("kind")),
            "amount": round(amount, 2),
        })

    aggregate = get_combined_summary()
    return {
        "brokerages": brokerages,
        "external": external,
        "aggregate": {
            "net_liquidation": _num(aggregate.get("net_liquidation")),
            "cash_balance": _num(aggregate.get("cash_balance")),
            "buying_power": _num(aggregate.get("buying_power")),
            "external_cash_total": _num(aggregate.get("external_cash_total")),
            "external_cash_liquid": _num(aggregate.get("external_cash_liquid")),
            "cash_override": _num(aggregate.get("cash_override")),
            "long_market_value": _num(aggregate.get("long_market_value")),
            "short_market_value": _num(aggregate.get("short_market_value")),
        },
    }
