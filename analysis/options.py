"""Options analysis: aggregate greeks, expiry timeline, P&L scenarios."""

import pandas as pd


def aggregate_greeks(positions_df):
    """Sum portfolio-level greeks across all option positions.

    Expects columns: delta, gamma, theta, vega, quantity, option_multiplier.
    Returns dict with total delta, gamma, theta, vega.
    """
    opts = positions_df[positions_df["asset_type"] == "OPTION"].copy()
    if opts.empty:
        return {"delta": 0, "gamma": 0, "theta": 0, "vega": 0}

    for col in ("delta", "gamma", "theta", "vega"):
        if col not in opts.columns:
            opts[col] = 0

    mult = opts.get("option_multiplier", pd.Series(100, index=opts.index))
    qty = opts["quantity"]

    return {
        "delta": float((opts["delta"] * qty * mult).sum()),
        "gamma": float((opts["gamma"] * qty * mult).sum()),
        "theta": float((opts["theta"] * qty * mult).sum()),
        "vega": float((opts["vega"] * qty * mult).sum()),
    }
