"""Theme aggregation — the portfolio's recorded reasoning, made measurable.

Pure functions (analysis-layer invariant #1): accept positions/returns dicts,
return structured results. No data fetching. The symbol→theme registry is the
theses store — a position's theme is a field on its recorded thesis
(data/theses.py). Positions without a recorded theme roll into "No Thesis".

Answers the decision layer's core question: *where is my book concentrated?*
A book that is 75% one theme is a single decision, not many — theme
concentration makes that visible the same way HHI makes single-name
concentration visible.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

UNCATEGORIZED = "No Thesis"


def _theme_map(symbol_to_theme: dict[str, str | None] | None) -> dict[str, str]:
    """Normalize a symbol→theme map, mapping None/blank to UNCATEGORIZED.

    Theme names resolve through the canonical registry (data/theme_registry)
    at GROUP time: a legacy alias string surviving in an old store still
    groups with its canonical theme instead of splitting the bucket.
    """
    if not symbol_to_theme:
        return {}
    from data.theme_registry import canonical_theme
    out: dict[str, str] = {}
    for sym, theme in symbol_to_theme.items():
        if theme and str(theme).strip():
            canonical = canonical_theme(theme)
            if canonical:
                out[str(sym).upper()] = canonical
    return out


def theme_allocation(
    positions_df: pd.DataFrame,
    symbol_to_theme: dict[str, str | None] | None = None,
) -> list[dict]:
    """Per-theme weight + market value from a positions frame.

    Positions frame needs ``symbol`` and ``market_value`` columns. Returns a
    list of {theme, market_value, weight, count} sorted by weight descending,
    with a "No Thesis" bucket for symbols with no recorded theme.
    """
    if positions_df is None or positions_df.empty:
        return []
    tmap = _theme_map(symbol_to_theme)

    df = positions_df.copy()
    if "symbol" not in df.columns or "market_value" not in df.columns:
        return []
    df["theme"] = df["symbol"].astype(str).str.upper().map(lambda s: tmap.get(s, UNCATEGORIZED))
    df["abs_mv"] = df["market_value"].abs()

    total = float(df["abs_mv"].sum())
    grouped = df.groupby("theme").agg(
        market_value=("abs_mv", "sum"),
        count=("symbol", "nunique"),
    ).reset_index()

    themes: list[dict] = []
    for _, row in grouped.iterrows():
        themes.append({
            "theme": str(row["theme"]),
            "market_value": round(float(row["market_value"]), 2),
            "weight": round(float(row["market_value"]) / total, 4) if total > 0 else 0.0,
            "count": int(row["count"]),
        })
    themes.sort(key=lambda t: t["weight"], reverse=True)
    return themes


def theme_concentration(theme_weights: list[dict]) -> float:
    """HHI across themes (not positions). 0 = diversified, 10000 = one theme."""
    if not theme_weights:
        return 0.0
    weights = [t["weight"] for t in theme_weights]
    return float(sum(w * w for w in weights) * 10000)


def theme_pnl(
    positions_df: pd.DataFrame,
    symbol_to_theme: dict[str, str | None] | None = None,
) -> list[dict]:
    """Per-theme day P&L + total (unrealized) P&L.

    Positions frame needs ``symbol``, ``day_pl``, and ``total_pl`` columns.
    """
    if positions_df is None or positions_df.empty:
        return []
    tmap = _theme_map(symbol_to_theme)

    df = positions_df.copy()
    required = {"symbol", "day_pl", "total_pl"}
    if not required.issubset(df.columns):
        return []
    df["theme"] = df["symbol"].astype(str).str.upper().map(lambda s: tmap.get(s, UNCATEGORIZED))
    df["day_pl"] = pd.to_numeric(df["day_pl"], errors="coerce").fillna(0)
    df["total_pl"] = pd.to_numeric(df["total_pl"], errors="coerce").fillna(0)

    grouped = df.groupby("theme").agg(
        day_pl=("day_pl", "sum"),
        total_pl=("total_pl", "sum"),
    ).reset_index()

    rows: list[dict] = []
    for _, row in grouped.iterrows():
        rows.append({
            "theme": str(row["theme"]),
            "day_pl": round(float(row["day_pl"]), 2),
            "total_pl": round(float(row["total_pl"]), 2),
        })
    rows.sort(key=lambda t: abs(t["total_pl"]), reverse=True)
    return rows


def intra_theme_correlation(
    returns_dict: dict[str, pd.Series],
    symbol_to_theme: dict[str, str | None] | None = None,
) -> list[dict]:
    """Average pairwise correlation within each theme.

    Uses the pairwise correlation matrix (analysis/risk.py correlation_matrix).
    Returns [{theme, avg_correlation, count}] sorted by avg_correlation
    descending. Themes with <2 symbols or no aligned returns are omitted —
    never a fabricated number.
    """
    if not returns_dict:
        return []
    from analysis.risk import correlation_matrix

    tmap = _theme_map(symbol_to_theme)
    symbols = [s for s in returns_dict if s.upper() in tmap]

    themes: dict[str, list[str]] = {}
    for sym in symbols:
        theme = tmap[sym.upper()]
        themes.setdefault(theme, []).append(sym)

    result: list[dict] = []
    for theme, members in themes.items():
        if len(members) < 2:
            continue
        sub = {s: returns_dict[s] for s in members}
        corr = correlation_matrix(sub)
        if corr.empty or len(corr) < 2:
            continue
        values = []
        for i, a in enumerate(members):
            for b in members[i + 1:]:
                try:
                    c = float(corr.loc[a, b])
                except (KeyError, TypeError):
                    continue
                if np.isfinite(c):
                    values.append(c)
        if not values:
            continue
        result.append({
            "theme": theme,
            "avg_correlation": round(float(np.mean(values)), 4),
            "count": len(members),
        })
    result.sort(key=lambda t: t["avg_correlation"], reverse=True)
    return result
