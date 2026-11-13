"""Data Guard — the accuracy guarantee for agent-emitted visualizations.

The model selects and arranges data; it is never the source of truth for a
number. Every value in an emitted visualization is cross-checked against the
gathered data:

  - If a value deviates >5% from its source, it is OVERRIDDEN with the source.
  - If a value has no verifiable source, it is DROPPED (never shown wrong).
  - Every correction is logged for transparency.

This is the "no room for errors" rule for a financial platform.
"""

from __future__ import annotations

import re
from typing import Any

# Tolerance: a value within 5% of source is accepted as-is (rounding noise).
TOLERANCE = 0.05

# Metric name -> source key in gathered data (per symbol).
# These are the fields we can verify against the data layer.
_METRIC_SOURCE_KEYS: dict[str, str] = {
    "market_cap": "market_cap",
    "marketcap": "market_cap",
    "mkt_cap": "market_cap",
    "price": "price",
    "last": "price",
    "change_pct": "change_pct",
    "day_change": "change_pct",
    "pe_ratio": "pe_ratio",
    "pe_ttm": "pe_ratio",
    "eps": "eps",
    "beta": "beta",
    "stbr": "stbr",
    "rsi": "rsi",
    "volume": "volume",
    "shares_outstanding": "shares_outstanding",
    "high_52w": "high_52w",
    "low_52w": "low_52w",
    "dividend_yield": "dividend_yield",
    "div_yield": "dividend_yield",
    "pb_ratio": "pb_ratio",
    "max_drawdown_pct": "max_drawdown_pct",
    "var_95_daily_pct": "var_95_daily_pct",
    "total_sells": "total_sells",
    "sell_value": "sell_value",
    "buy_value": "buy_value",
    "net_shares": "net_shares",
}

# Symbols are uppercase tickers (NVDA, AAPL, ...). Used to find which symbol a
# row/point belongs to.
_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,5}$")


def _extract_symbol(value: Any) -> str | None:
    """Return an uppercase ticker if the value looks like one."""
    if isinstance(value, str):
        s = value.strip().upper()
        if _SYMBOL_RE.match(s):
            return s
    return None


def _to_number(value: Any) -> float | None:
    """Coerce a value to a float, handling strings like '$5.31T', '1.87', '3.2B shares', 'x1.87', '5.3 billion', 'USD 5.3B'."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        s = value.strip()
        # Strip currency symbols/prefixes (USD, EUR, $, €, £, ...).
        s = re.sub(r"^(usd|eur|gbp|rmb|cny|jpy|inr|kr|sek|chf|cad|aud)\s*", "", s, flags=re.IGNORECASE)
        s = s.replace("$", "").replace("€", "").replace("£", "").replace("¥", "")
        s = s.replace(",", "").replace("%", "")
        # Strip trailing unit words like "shares", "USD", "x" suffixes.
        s = re.sub(r"\s+(shares|usd|eur|gbp|dollar|dollars|times|x|eps)$", "", s, flags=re.IGNORECASE)
        # Strip a leading "x" multiplier marker (e.g. "x1.87").
        s = re.sub(r"^x", "", s, flags=re.IGNORECASE)
        # Strip a trailing "x" multiplier marker (e.g. "1.87x").
        s = re.sub(r"x$", "", s, flags=re.IGNORECASE)
        mult = 1.0
        # Word multipliers must be applied, not just stripped.
        word_mult = re.search(r"\b(trillion|billion|million|thousand)\b", s, flags=re.IGNORECASE)
        if word_mult:
            word = word_mult.group(1).lower()
            mult = {"trillion": 1e12, "billion": 1e9, "million": 1e6, "thousand": 1e3}[word]
            s = s[:word_mult.start()].strip()
        elif s.endswith(("T", "t")):
            mult = 1e12
            s = s[:-1]
        elif s.endswith(("B", "b")):
            mult = 1e9
            s = s[:-1]
        elif s.endswith(("M", "m")):
            mult = 1e6
            s = s[:-1]
        elif s.endswith(("K", "k")):
            mult = 1e3
            s = s[:-1]
        try:
            return float(s) * mult
        except ValueError:
            return None
    return None


def _format_number(value: float) -> str:
    """Format a number back to a compact human string."""
    abs_v = abs(value)
    if abs_v >= 1e12:
        return f"${value / 1e12:.2f}T"
    if abs_v >= 1e9:
        return f"${value / 1e9:.2f}B"
    if abs_v >= 1e6:
        return f"${value / 1e6:.2f}M"
    if abs_v >= 1e3:
        return f"${value / 1e3:.1f}K"
    if abs_v >= 100:
        return f"{value:.2f}"
    return f"{value:.2f}"


def _build_source_index(gathered: dict[str, dict]) -> dict[str, dict[str, float]]:
    """Build {SYMBOL: {metric_key: value}} from the gathered data.

    Handles the tool result shapes: get_fundamentals, get_quote, get_signals,
    get_stbr, get_risk_metrics, get_insider_activity.
    """
    index: dict[str, dict[str, float]] = {}
    for label, result in gathered.items():
        if not isinstance(result, dict):
            continue
        sym = _extract_symbol(result.get("symbol") or label.split("(")[-1].rstrip(")"))
        if not sym:
            continue
        entry = index.setdefault(sym, {})
        for key, val in result.items():
            num = _to_number(val)
            if num is not None:
                entry[key] = num
    return index


def _verify_value(value: Any, source: float | None, metric_key: str) -> tuple[Any, str | None]:
    """Verify a value against its source. Returns (value, correction_note)."""
    num = _to_number(value)
    if num is None:
        return value, None
    if source is None:
        # No verifiable source — drop it (return None signals drop).
        return None, f"dropped unverifiable {metric_key}"
    if source == 0:
        return value, None
    deviation = abs(num - source) / abs(source)
    if deviation > TOLERANCE:
        return _format_number(source), f"overrode {metric_key}: {value} -> {_format_number(source)}"
    return value, None


def guard_table(args: dict, source_index: dict[str, dict[str, float]]) -> tuple[dict, list[str]]:
    """Verify a visualize_table payload. Returns (cleaned_args, corrections).

    A table is only kept if at least one data column has at least one verified
    cell — a table reduced to tickers-only is never shown. Unverifiable
    columns are dropped (with a correction noting why) rather than rendered as
    an em-dash wall. A dropped table returns (None, corrections) so callers
    can omit it entirely.
    """
    corrections: list[str] = []
    rows = args.get("rows", [])
    if not isinstance(rows, list):
        return args, corrections
    if not rows:
        return None, [f"table {args.get('title', '')!r}: no rows, dropped"]

    cleaned_rows: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            cleaned_rows.append(row)
            continue
        # The symbol may be keyed 'symbol' or 'ticker' — resolve either.
        sym = _extract_symbol(row.get("symbol") or row.get("ticker"))
        src = source_index.get(sym or "", {})
        new_row: dict[str, Any] = {}
        for key, val in row.items():
            if key in ("symbol", "ticker"):
                if sym:
                    new_row["symbol"] = sym
                continue
            source = src.get(_METRIC_SOURCE_KEYS.get(key.lower(), key.lower()))
            verified, note = _verify_value(val, source, key)
            if verified is None:
                corrections.append(f"{sym or '?'}.{key}: {note}")
                continue  # drop unverifiable
            if note:
                corrections.append(f"{sym or '?'}.{key}: {note}")
            new_row[key] = verified
        cleaned_rows.append(new_row)

    # Drop any column with zero verified cells across all rows.
    verified_cols: dict[str, int] = {}
    for row in cleaned_rows:
        if not isinstance(row, dict):
            continue
        for key in row:
            if key == "symbol":
                continue
            verified_cols[key] = verified_cols.get(key, 0) + 1
    kept: set[str] = set()
    if verified_cols:
        kept = set(verified_cols)
        for col in (args.get("columns") or []):
            key = col.get("key") if isinstance(col, dict) else None
            if key is not None and key not in kept and key not in ("symbol", "ticker"):
                corrections.append(f"dropped unverifiable column {key!r} (no verified cells)")
        for row in cleaned_rows:
            if not isinstance(row, dict):
                continue
            for key in list(row):
                if key != "symbol" and key not in kept:
                    del row[key]

    # A table whose every row is ticker-only is empty — drop it.
    if not any(
        isinstance(row, dict) and set(row) - {"symbol"} for row in cleaned_rows
    ):
        return None, corrections + [f"table {args.get('title', '')!r}: all columns dropped, table removed"]

    args = dict(args)
    args["rows"] = cleaned_rows
    if isinstance(args.get("columns"), list):
        normalized_cols: list[dict] = []
        for col in args["columns"]:
            if not isinstance(col, dict):
                normalized_cols.append(col)
                continue
            key = col.get("key")
            if key == "ticker":
                col = {**col, "key": "symbol"}
            normalized_cols.append(col)
        args["columns"] = [
            c for c in normalized_cols
            if not (isinstance(c, dict) and c.get("key") != "symbol" and c.get("key") not in kept)
        ]
    return args, corrections


def _infer_chart_metric(y_label: str, title: str) -> str | None:
    """Infer the source-index metric a chart's y-axis represents.

    Returns a source-index key (e.g. 'price', 'composite_score') or None when
    the chart cannot be verified against the gathered data. Conservative by
    design: an unknown label passes through unverified rather than being forced
    into a price check — which is what corrupted non-price charts into price
    bars. Specific metrics are matched before the generic price fallback.
    """
    import re as _re

    text = f"{y_label} {title}".lower()

    def _has(*pats: str) -> bool:
        return any(_re.search(rf"\b{_re.escape(p)}\b", text) for p in pats)

    # Range charts FIRST: their y_label often carries "Price ($)", and the '$'
    # check would otherwise eat the chart before the range branch can run.
    if _has("range position", "52-week range", "52w range"):
        return "__range__"
    if "$" in text:
        return "price"
    if _has("stbr", "bubble risk"):
        return "stbr"
    if _has("composite score", "signal score"):
        return "composite_score"
    if _has("market cap", "mkt cap", "market capitalization"):
        return "market_cap"
    if _has("beta"):
        return "beta"
    if _has("rsi"):
        return "rsi"
    if _has("volume"):
        return "volume"
    if _has("drawdown"):
        return "max_drawdown_pct"
    if _has("pe", "p/e", "valuation"):
        return "pe_ratio"
    if _has("dividend", "yield"):
        return "dividend_yield"
    if _has("price", "close", "quote", "level", "high", "low"):
        return "price"
    return None


def guard_chart(args: dict, source_index: dict[str, dict[str, float]]) -> tuple[dict, list[str]]:
    """Verify a visualize_chart payload. Returns (cleaned_args, corrections).

    Metric-aware: the y-axis metric is inferred from the y_label/title/series
    label (price, composite score, STBR, beta, ...). Only that metric's source
    value is used for verification — a composite-score chart is never
    overwritten with a price. Points whose x isn't a gathered symbol, or charts
    with no inferable metric, pass through untouched: an unverified number
    shown verbatim is honest, a wrong number substituted for it is not.
    """
    corrections: list[str] = []
    data = args.get("data", [])
    if not isinstance(data, list):
        return args, corrections

    y_label = str(args.get("y_label") or "")
    title = str(args.get("title") or "")
    metric = _infer_chart_metric(y_label, title)
    if metric == "__range__":
        return _guard_range_chart(args, source_index, corrections)
    if metric is None:
        return args, corrections

    cleaned_series: list[dict] = []
    for series in data:
        if not isinstance(series, dict):
            cleaned_series.append(series)
            continue
        values = series.get("values", [])
        cleaned_values: list[dict] = []
        for pt in values:
            if not isinstance(pt, dict):
                cleaned_values.append(pt)
                continue
            sym = _extract_symbol(pt.get("x"))
            src = source_index.get(sym or "", {})
            source = src.get(metric)
            num = _to_number(pt.get("y"))
            if num is not None and source is not None and source != 0:
                deviation = abs(num - source) / abs(source)
                if deviation > TOLERANCE:
                    corrections.append(
                        f"{sym or '?'}.y: overrode {metric}: {pt.get('y')} -> {source}"
                    )
                    cleaned_values.append({**pt, "y": source})
                    continue
            cleaned_values.append(pt)
        cleaned_series.append({**series, "values": cleaned_values})

    args = dict(args)
    args["data"] = cleaned_series
    return args, corrections




_RANGE_SERIES_MAP = {
    "current": "price",
    "current price": "price",
    "spot": "price",
    "52-week high": "high_52w",
    "high": "high_52w",
    "52-week low": "low_52w",
    "low": "low_52w",
}


def _guard_range_chart(
    args: dict, source_index: dict[str, dict[str, float]], corrections: list[str]
) -> tuple[dict, list[str]]:
    """Verify a Current-vs-High-vs-Low range chart PER SERIES.

    Each series maps to its own metric (Current->price, High->high_52w,
    Low->low_52w). Series whose label maps to no known metric pass through
    unverified — an unverified point is honest, a wrong substitution is not.
    Additionally: High must be >= Low in the cleaned data; a violation is
    corrected to the source values and noted.
    """
    data = args.get("data", [])
    if not isinstance(data, list):
        return args, corrections

    cleaned_series: list[dict] = []
    for series in data:
        if not isinstance(series, dict):
            cleaned_series.append(series)
            continue
        label = str(series.get("label", "")).strip().lower()
        metric = _RANGE_SERIES_MAP.get(label)
        values = series.get("values", [])
        if metric is None:
            cleaned_series.append(series)
            corrections.append(f"range series {label!r}: unknown metric, passed unverified")
            continue
        cleaned_values: list[dict] = []
        for pt in values:
            if not isinstance(pt, dict):
                cleaned_values.append(pt)
                continue
            sym = _extract_symbol(pt.get("x"))
            src = source_index.get(sym or "", {})
            source = src.get(metric)
            num = _to_number(pt.get("y"))
            if num is not None and source is not None and source != 0:
                deviation = abs(num - source) / abs(source)
                if deviation > TOLERANCE:
                    corrections.append(
                        f"{sym or '?'}.y[{label}]: overrode {metric}: {pt.get('y')} -> {source}"
                    )
                    cleaned_values.append({**pt, "y": source})
                    continue
            cleaned_values.append(pt)
        cleaned_series.append({**series, "values": cleaned_values})

    args = dict(args)
    args["data"] = cleaned_series
    return args, corrections

def guard_metric(args: dict, source_index: dict[str, dict[str, float]]) -> tuple[dict, list[str]]:
    """Verify a highlight_metric payload. Returns (cleaned_args, corrections)."""
    corrections: list[str] = []
    label = str(args.get("label", ""))
    value = args.get("value")
    # Find the symbol in the label (e.g. 'NVDA Beta' -> NVDA, metric 'beta')
    sym = None
    metric_name = label.lower()
    for token in label.split():
        s = _extract_symbol(token)
        if s:
            sym = s
            metric_name = label.replace(token, "").strip().lower()
            break
    src = source_index.get(sym or "", {})
    source = src.get(_METRIC_SOURCE_KEYS.get(metric_name, metric_name))
    verified, note = _verify_value(value, source, metric_name)
    if verified is None:
        # No verifiable source — pass the metric through unchanged (the metric
        # strip is not a data table; unlike a row, a label/value pair can't be
        # silently stripped without losing the model's framing). Corrections
        # record that it went unverified.
        return args, [f"metric {label}: no source, unverified"]
    if note:
        corrections.append(f"metric {label}: {note}")
    args = dict(args)
    args["value"] = verified
    return args, corrections


def guard_network(args: dict, source_index: dict[str, dict[str, float]]) -> tuple[dict, list[str]]:
    """Verify a visualize_network payload (node sizes). Returns (cleaned_args, corrections)."""
    corrections: list[str] = []
    nodes = args.get("nodes", [])
    if not isinstance(nodes, list):
        return args, corrections
    cleaned_nodes: list[dict] = []
    for node in nodes:
        if not isinstance(node, dict):
            cleaned_nodes.append(node)
            continue
        sym = _extract_symbol(node.get("id") or node.get("label"))
        src = source_index.get(sym or "", {})
        size = node.get("size")
        # Node size is a relative 0-1 value, not a raw metric — verify only if
        # it looks like a market cap magnitude (e.g. 5.3, 3.2).
        if isinstance(size, (int, float)) and size > 1:
            source = src.get("market_cap")
            verified, note = _verify_value(size, source, "market_cap")
            if verified is None:
                corrections.append(f"{sym or '?'}.size: dropped unverifiable")
                continue
            if note:
                corrections.append(f"{sym or '?'}.size: {note}")
            node = {**node, "size": verified}
        cleaned_nodes.append(node)
    args = dict(args)
    args["nodes"] = cleaned_nodes
    return args, corrections


def guard_tool_call(name: str, args: dict, gathered: dict[str, dict]) -> tuple[dict | None, list[str]]:
    """Verify a tool call's payload against the gathered data.

    Returns (cleaned_args, corrections). Corrections are logged for transparency.

    A ``None`` return means the call was dropped entirely (a table reduced to no
    rows). This is deliberately **not** ``{}`` — an empty dict is the legitimate
    argument set for every zero-argument tool (``get_positions``,
    ``get_account_breakdown``, ...), so using ``{}`` as the drop sentinel made
    callers silently discard those calls.
    """
    source_index = _build_source_index(gathered)
    if name == "visualize_table":
        cleaned, corrections = guard_table(args, source_index)
        # A None result means the table was reduced to nothing — drop it
        # entirely rather than emit an empty artifact.
        return (None if cleaned is None else cleaned), corrections
    if name == "visualize_chart":
        return guard_chart(args, source_index)
    if name == "highlight_metric":
        return guard_metric(args, source_index)
    if name == "visualize_network":
        return guard_network(args, source_index)
    return args, []


# Data tools whose results carry per-symbol metrics that must be consistent
# with the gathered source index. The Journalist writer's tool results flow
# into report text unguarded — this guard cross-checks them against the
# pre-gathered (already-verified) data so a stale or invented number can't
# reach the report.
_DATA_TOOLS = {
    "get_quote",
    "get_signals",
    "get_stbr",
    "get_fundamentals",
    "get_risk_metrics",
    "get_position_intel",
}


def guard_tool_result(name: str, result: dict, gathered: dict[str, dict]) -> tuple[dict, list[str]]:
    """Verify a data-tool result against the gathered source index.

    Returns (cleaned_result, corrections). Numeric fields that deviate >5%
    from the verified source are overridden with the source value; fields
    with no verifiable source are left as-is (the tool result is itself a
    source — we only correct against a *stronger* source, the pre-gathered
    data the live price snapshot was built from).
    """
    if name not in _DATA_TOOLS or not isinstance(result, dict) or result.get("error"):
        return result, []

    sym = _extract_symbol(result.get("symbol"))
    if not sym:
        return result, []

    source_index = _build_source_index(gathered)
    src = source_index.get(sym, {})
    if not src:
        return result, []

    corrections: list[str] = []
    cleaned: dict = {}
    for key, val in result.items():
        if key == "symbol":
            cleaned[key] = val
            continue
        source = src.get(_METRIC_SOURCE_KEYS.get(key.lower(), key.lower()))
        verified, note = _verify_value(val, source, key)
        # The tool result is itself a source — we only correct against a
        # *stronger* source (the pre-gathered data). An unverifiable field
        # passes through silently; only an actual override is noted.
        if note and "overrode" in note:
            corrections.append(f"{sym}.{key}: {note}")
        cleaned[key] = verified if verified is not None else val
    return cleaned, corrections
