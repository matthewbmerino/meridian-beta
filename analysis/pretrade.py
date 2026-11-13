"""Pre-trade impact analysis.

Answers: what does a hypothetical trade do to the portfolio's risk profile
before you place it? This is the analysis the Trade Ticket needs — the
sizing-preview endpoint answers "what weight does sizing want", this answers
"what does the trade do to VaR, vol, beta, and concentration".

Pure functions — no data fetching, no side effects. Accepts the current book
(weights, sigmas, returns) and a hypothetical trade, returns the impact
metrics. Follows the analysis-layer invariants: NaN-safe outputs, type-hinted
public functions, degrade-don't-fabricate.

The module was deleted in P0 hardening; this is the rebuild the backend brief
documents as the highest-value missing surface.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def _beta(port_ret, bench) -> float:
    """Portfolio beta vs *bench* returns (the caller's market proxy).

    Falls back to 1.0 (the market's own beta) when there are too few
    observations or the benchmark is flat.
    """
    if len(port_ret) < 20:
        return 1.0
    bvar = float(np.var(bench))
    if bvar == 0:
        return 1.0
    return float(np.cov(port_ret, bench)[0, 1] / bvar)


def _hhi(w) -> float:
    """Herfindahl-Hirschman index of concentration (0-10000) for a weight vector."""
    w = np.abs(w)
    s = w.sum()
    if s <= 0:
        return 0.0
    return float(((w / s) ** 2).sum() * 10000)


def pretrade_impact(
    current_weights: dict[str, float],
    returns_dict: dict[str, pd.Series],
    trade_symbol: str,
    trade_dollars: float,
    nav: float,
    side: str = "BUY",
    confidence: float = 0.95,
) -> dict:
    """Compute the risk impact of a hypothetical trade.

    current_weights: {symbol: weight} of the current book (fractions of NAV).
    returns_dict: {symbol: daily returns Series} for the book + the trade name.
    trade_symbol: the symbol being traded (may or may not be held).
    trade_dollars: notional of the trade (positive).
    nav: current portfolio NAV.
    side: "BUY" or "SELL".

    Returns:
        {
          "available": bool,
          "symbol": str,
          "side": str,
          "requested_weight": float,     # post-trade weight of the name
          "var_before": float,           # daily 95% VaR (negative)
          "var_after": float,
          "var_delta": float,            # change in VaR (negative = more risk)
          "vol_before": float,           # annualized portfolio vol
          "vol_after": float,
          "beta_before": float,          # vs the first symbol's returns as proxy
          "beta_after": float,
          "hhi_before": float,           # concentration (0-10000)
          "hhi_after": float,
          "gross_before": float,
          "gross_after": float,
          "cash_after": float,
        }
    """
    if nav <= 0 or not current_weights or not returns_dict:
        return {"available": False, "symbol": trade_symbol, "side": side}

    sym = trade_symbol.upper()
    side = side.upper()
    if side not in ("BUY", "SELL"):
        return {"available": False, "symbol": sym, "side": side}

    signed = trade_dollars if side == "BUY" else -trade_dollars
    post_nav = nav + signed
    if post_nav <= 0:
        return {"available": False, "symbol": sym, "side": side}

    # Build the post-trade book: current weights scaled to post-trade NAV,
    # plus the trade's weight.
    post_weights = {s: w * nav / post_nav for s, w in current_weights.items()}
    current_mv = current_weights.get(sym, 0.0) * nav
    post_mv = current_mv + signed
    post_weights[sym] = post_mv / post_nav

    # Align returns to a common index.
    df = pd.DataFrame(returns_dict).dropna(how="all")
    if df.empty or sym not in df.columns:
        return {"available": False, "symbol": sym, "side": side}
    df = df.dropna(subset=[sym])
    if df.empty:
        return {"available": False, "symbol": sym, "side": side}

    # Portfolio returns before/after.
    syms_before = [s for s in current_weights if s in df.columns]
    syms_after = [s for s in post_weights if s in df.columns]
    if not syms_before or not syms_after:
        return {"available": False, "symbol": sym, "side": side}

    w_before = np.array([current_weights[s] for s in syms_before])
    w_before = w_before / w_before.sum() if w_before.sum() > 0 else w_before
    w_after = np.array([post_weights[s] for s in syms_after])
    w_after = w_after / w_after.sum() if w_after.sum() > 0 else w_after

    ret_before = df[syms_before].values @ w_before
    ret_after = df[syms_after].values @ w_after

    # VaR (historical).
    var_before = float(np.percentile(ret_before, (1 - confidence) * 100))
    var_after = float(np.percentile(ret_after, (1 - confidence) * 100))

    # Annualized vol.
    vol_before = float(ret_before.std(ddof=1) * np.sqrt(252)) if len(ret_before) > 1 else 0.0
    vol_after = float(ret_after.std(ddof=1) * np.sqrt(252)) if len(ret_after) > 1 else 0.0

    # Beta vs the first symbol's returns as a market proxy (the caller can
    # pass SPY as the first symbol for a true market beta).
    proxy = syms_before[0]
    bench = df[proxy].values
    beta_before = _beta(ret_before, bench)
    beta_after = _beta(ret_after, bench)

    hhi_before = _hhi(w_before)
    hhi_after = _hhi(w_after)

    gross_before = float(sum(abs(w) for w in current_weights.values()))
    gross_after = float(sum(abs(w) for w in post_weights.values()))
    cash_after = 1.0 - gross_after

    return {
        "available": True,
        "symbol": sym,
        "side": side,
        "requested_weight": round(post_weights.get(sym, 0.0), 4),
        "var_before": round(var_before, 6),
        "var_after": round(var_after, 6),
        "var_delta": round(var_after - var_before, 6),
        "vol_before": round(vol_before, 4),
        "vol_after": round(vol_after, 4),
        "vol_delta": round(vol_after - vol_before, 4),
        "beta_before": round(beta_before, 3),
        "beta_after": round(beta_after, 3),
        "hhi_before": round(hhi_before, 1),
        "hhi_after": round(hhi_after, 1),
        "gross_before": round(gross_before, 4),
        "gross_after": round(gross_after, 4),
        "cash_after": round(cash_after, 4),
    }
