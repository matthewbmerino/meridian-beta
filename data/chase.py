"""Parse Chase brokerage CSV exports into portfolio DataFrames."""

import io
import logging
import re

import pandas as pd

log = logging.getLogger(__name__)

# Chase option tickers: "AAPL CALL 03/06/26 $275.00" or "NVDA PUT 03/06/26 $167.50"
_OPTION_RE = re.compile(
    r'^([A-Z.]+)\s+(CALL|PUT)\s+(\d{1,2}/\d{1,2}/\d{2,4})\s+\$?([\d,.]+)',
    re.IGNORECASE,
)


def _parse_numeric(value):
    """Convert a string like '1,234.56' or '$1,234.56' to float. Returns 0.0 for empty/NaN."""
    if pd.isna(value) or value == "":
        return 0.0
    s = str(value).replace(",", "").replace("$", "").replace("%", "").strip()
    if s == "" or s == "-":
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def _detect_separator(content: str) -> str:
    """Auto-detect tab vs comma separator from the first few lines."""
    first_lines = content.split("\n", 5)[:5]
    tabs = sum(line.count("\t") for line in first_lines)
    commas = sum(line.count(",") for line in first_lines)
    return "\t" if tabs > commas else ","


def _is_cash_row(row) -> bool:
    """Heuristic: row is cash/money-market, not an equity position."""
    ticker = str(row.get("Ticker", "")).strip()
    if ticker == "" or pd.isna(row.get("Ticker")):
        return True
    desc = str(row.get("Description", "")).upper()
    asset_class = str(row.get("Asset Class", "")).upper()
    for keyword in ("CASH", "SWEEP", "MONEY MARKET", "MMKT"):
        if keyword in desc or keyword in asset_class:
            return True
    return False


def _decode(raw_bytes: bytes) -> str:
    """Decode bytes to text, trying the encodings brokers actually emit."""
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return raw_bytes.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw_bytes.decode("latin-1")  # latin-1 never fails


def _read_any_format(raw_bytes: bytes) -> pd.DataFrame:
    """Detect file format from magic bytes and return a DataFrame.

    Handles real xlsx/xls, HTML-disguised-as-xls (common from Chase,
    Fidelity, etc.), and CSV/TSV text files.  Falls back through each
    parser so a misdetection never surfaces as an engine error.
    """
    header = raw_bytes[:8]

    # 1) ZIP archive → real .xlsx
    if header[:2] == b"PK":
        return pd.read_excel(io.BytesIO(raw_bytes), dtype=str, engine="openpyxl")

    # 2) OLE2 compound document → real .xls
    if header[:4] == b"\xd0\xcf\x11\xe0":
        return pd.read_excel(io.BytesIO(raw_bytes), dtype=str, engine="xlrd")

    # 3) HTML table (many brokerages save .xls as HTML)
    #    Strip BOM + whitespace before checking for '<'
    stripped = raw_bytes.lstrip(b"\xef\xbb\xbf \t\r\n")
    if stripped[:1] == b"<":
        try:
            dfs = pd.read_html(io.BytesIO(raw_bytes))
            if dfs:
                return dfs[0].astype(str)
        except Exception as e:
            log.debug("HTML table parse failed, falling through to CSV: %s", e)

    # 4) CSV / TSV / TXT
    text = _decode(raw_bytes)
    sep = _detect_separator(text)
    return pd.read_csv(io.StringIO(text), sep=sep, dtype=str)


def _read_content(file_content) -> bytes:
    """Normalize a file-like object, bytes, or str into raw bytes."""
    if hasattr(file_content, "read"):
        if hasattr(file_content, "seek"):
            file_content.seek(0)
        raw = file_content.read()
    elif isinstance(file_content, (bytes, str)):
        raw = file_content
    else:
        raw = file_content
    return raw if isinstance(raw, bytes) else raw.encode("utf-8")


def _derive_fields(quantity, current_price, market_value, avg_cost, total_cost, total_pl):
    """Fill missing position fields from the ones present.

    Shared by the Chase and Schwab parsers so the fallback logic can't drift:
    avg_cost ← total cost basis, market_value ← price×qty, current_price ←
    mv/qty, total_pl ← mv − avg_cost×qty. Each step only fires when the field
    is still zero.
    """
    if avg_cost == 0 and total_cost != 0 and quantity != 0:
        avg_cost = total_cost / abs(quantity)
    if market_value == 0 and current_price != 0 and quantity != 0:
        market_value = current_price * quantity
    if current_price == 0 and market_value != 0 and quantity != 0:
        current_price = market_value / quantity
    if total_pl == 0 and avg_cost != 0 and market_value != 0:
        total_pl = market_value - (avg_cost * quantity)
    return current_price, market_value, avg_cost, total_pl


def _build_row(symbol, description, quantity, current_price, market_value,
               day_pl, day_pl_pct, total_pl, avg_cost, source,
               option_fields=None) -> dict:
    """One position row in the canonical schema, shared by both parsers.

    *option_fields* is a (underlying, put_call, strike, expiration) tuple when
    the symbol is an option; None for an equity.
    """
    if option_fields:
        underlying, put_call, strike, expiration = option_fields
        return {
            "symbol": symbol,
            "description": description,
            "asset_type": "OPTION",
            "quantity": quantity,
            "avg_cost": avg_cost,
            "market_value": market_value,
            "current_price": current_price,
            "day_pl": day_pl,
            "day_pl_pct": day_pl_pct,
            "total_pl": total_pl,
            "underlying": underlying,
            "put_call": put_call,
            "strike": strike,
            "expiration": expiration,
            "option_multiplier": 100,
            "source": source,
        }
    return {
        "symbol": symbol,
        "description": description,
        "asset_type": "EQUITY",
        "quantity": quantity,
        "avg_cost": avg_cost,
        "market_value": market_value,
        "current_price": current_price,
        "day_pl": day_pl,
        "day_pl_pct": day_pl_pct,
        "total_pl": total_pl,
        "underlying": symbol,
        "put_call": "",
        "strike": 0,
        "expiration": "",
        "option_multiplier": 1,
        "source": source,
    }


def build_position_row(symbol, description, quantity, current_price, market_value,
                       day_pl, day_pl_pct, total_pl, avg_cost, source,
                       option_fields=None) -> dict:
    """Public alias for the canonical position row.

    Any broker adapter (the Schwab API pull included) must emit THIS shape, so
    a new source cannot quietly invent its own column names and land in the
    book as a half-populated row.
    """
    return _build_row(symbol, description, quantity, current_price, market_value,
                      day_pl, day_pl_pct, total_pl, avg_cost, source, option_fields)


def _compute_summary(positions_df: pd.DataFrame, cash_balance: float) -> dict:
    """Summary dict from a parsed positions frame, shared by both parsers."""
    if positions_df.empty:
        return {
            "net_liquidation": cash_balance,
            "cash_balance": cash_balance,
            "buying_power": 0,
            "long_market_value": 0,
            "short_market_value": 0,
            "day_pl": 0,
        }
    long_mv = float(positions_df["market_value"].clip(lower=0).sum())
    short_mv = float(positions_df["market_value"].clip(upper=0).sum())
    day_pl_total = float(positions_df["day_pl"].sum())
    return {
        "net_liquidation": long_mv + short_mv + cash_balance,
        "cash_balance": cash_balance,
        "buying_power": 0,
        "long_market_value": long_mv,
        "short_market_value": short_mv,
        "day_pl": day_pl_total,
    }


def _empty_summary():
    return _compute_summary(pd.DataFrame(), 0.0)


def parse_chase_csv(file_content):
    """Parse a Chase brokerage CSV/TSV/Excel export.

    Args:
        file_content: bytes, str, or a Streamlit UploadedFile object.

    Returns:
        (positions_df, chase_summary): DataFrame with same schema as
        get_positions() plus source='Chase', and a summary dict mirroring
        get_account_summary() keys.
    """
    raw_bytes = _read_content(file_content)
    if len(raw_bytes) == 0:
        return pd.DataFrame(), _empty_summary()

    df = _read_any_format(raw_bytes)

    # Normalize column names (strip whitespace)
    df.columns = df.columns.str.strip()

    if df.empty:
        return pd.DataFrame(), _empty_summary()

    cash_balance = 0.0
    rows = []

    for _, raw in df.iterrows():
        if _is_cash_row(raw):
            val = _parse_numeric(raw.get("Value", raw.get("Market Value", 0)))
            if val != 0:
                cash_balance += val
            continue

        ticker = str(raw.get("Ticker", "")).strip()
        if not ticker:
            continue

        quantity = _parse_numeric(raw.get("Quantity", 0))
        current_price = _parse_numeric(raw.get("Price", 0))
        market_value = _parse_numeric(raw.get("Value", raw.get("Market Value", 0)))
        avg_cost = _parse_numeric(raw.get("Unit Cost", raw.get("Average Cost", 0)))
        total_cost = _parse_numeric(raw.get("Cost", raw.get("Cost Basis", 0)))
        day_pl = _parse_numeric(raw.get("Today's Value Change", raw.get("Day Change", 0)))
        day_pl_pct = _parse_numeric(raw.get("Price Change %", raw.get("Day Change %", 0)))
        total_pl = _parse_numeric(raw.get("Unrealized G/L Amt.", raw.get("Unrealized Gain/Loss", 0)))

        current_price, market_value, avg_cost, total_pl = _derive_fields(
            quantity, current_price, market_value, avg_cost, total_cost, total_pl
        )

        # Detect option tickers: "AAPL CALL 03/06/26 $275.00"
        opt_match = _OPTION_RE.match(ticker)
        option_fields = None
        if opt_match:
            option_fields = (
                opt_match.group(1).upper(),
                opt_match.group(2).upper(),
                _parse_numeric(opt_match.group(4)),
                opt_match.group(3),
            )

        # A `Source` column lets a non-broker file (the demo book, a manual
        # export) keep its own label instead of being called Chase.
        source_label = str(raw.get("Source", "")).strip() or "Chase"
        rows.append(_build_row(
            ticker, str(raw.get("Description", "")).strip(), quantity,
            current_price, market_value, day_pl, day_pl_pct, total_pl, avg_cost,
            source_label, option_fields,
        ))

    positions_df = pd.DataFrame(rows) if rows else pd.DataFrame()
    return positions_df, _compute_summary(positions_df, cash_balance)


# ---------------------------------------------------------------------------
# Schwab positions parser
# ---------------------------------------------------------------------------

# Schwab option tickers: "AAPL 03/20/26 275.0 C" or "AAPL 03/20/26 275.0 P"
_SCHWAB_OPTION_RE = re.compile(
    r'^([A-Z.]+)\s+(\d{1,2}/\d{1,2}/\d{2,4})\s+([\d.]+)\s+([CP])$',
    re.IGNORECASE,
)


def _schwab_col(row, *names):
    """First non-empty value among candidate column names."""
    for name in names:
        v = row.get(name)
        if v is not None and str(v).strip() not in ("", "nan", "NaN"):
            return v
    return None


# Schwab "Positions" export column variants → canonical names. The download
# includes the human-readable label in parens (e.g. "Qty (Quantity)"), and
# older exports use the bare names. Normalize so the parser sees one schema.
_SCHWAB_COLUMN_ALIASES = {
    "Qty (Quantity)": "Quantity",
    "Qty": "Quantity",
    "Mkt Val (Market Value)": "Market Value",
    "Mkt Val": "Market Value",
    "Gain $ (Gain/Loss $)": "Unrealized Gain/Loss",
    "Gain $": "Unrealized Gain/Loss",
    "Gain/Loss $": "Unrealized Gain/Loss",
    "Day Chng $ (Day Change $)": "Today's Gain/Loss",
    "Day Chng $": "Today's Gain/Loss",
    "Day Chng % (Day Change %)": "Today's Gain/Loss %",
    "Day Chng %": "Today's Gain/Loss %",
    "Price Chng % (Price Change %)": "Price Change %",
    "Price Chng $ (Price Change $)": "Price Change $",
    "Cost Basis Total": "Cost Basis",
}


def _normalize_schwab_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Rename Schwab column variants to canonical names."""
    df = df.rename(columns=_SCHWAB_COLUMN_ALIASES)
    return df


def _strip_title_line(raw_bytes: bytes) -> bytes:
    """Drop a leading title line (e.g. 'Positions as of 08/08/2026' or the
    quoted 'Positions for account ... as of ...' variant) from a text export
    so the header row is the first line. No-op for binary formats (xlsx/xls)
    and for files whose first line already looks like a header."""
    if raw_bytes[:2] == b"PK" or raw_bytes[:4] == b"\xd0\xcf\x11\xe0":
        return raw_bytes
    stripped = raw_bytes.lstrip(b"\xef\xbb\xbf \t\r\n")
    if stripped[:1] == b"<":
        return raw_bytes  # HTML table — leave to pd.read_html

    text = _decode(raw_bytes)
    lines = text.splitlines()
    if not lines:
        return raw_bytes
    first = lines[0].strip()
    # A title line has no tab/comma structure and doesn't look like a header.
    # The quoted variant ("Positions for account ... as of ...") contains
    # commas inside quotes, so also treat a line that is a single quoted
    # field as a title.
    if first and "Symbol" not in first and "\t" not in first and "," not in first:
        return "\n".join(lines[1:]).encode("utf-8")
    if first.startswith('"') and first.count('"') == 2 and not first.startswith('"Symbol"'):
        return "\n".join(lines[1:]).encode("utf-8")
    return raw_bytes


def parse_schwab_positions(file_content):
    """Parse a Schwab positions CSV/TSV/Excel export.

    Handles the standard Schwab "Positions" download (Symbol, Description,
    Quantity, Price, Cost Basis, Market Value, Unrealized Gain/Loss, etc.)
    and the "All Accounts" variant. Returns (positions_df, summary) with the
    same schema as parse_chase_csv, source='Schwab'.
    """
    raw_bytes = _read_content(file_content)
    if len(raw_bytes) == 0:
        return pd.DataFrame(), _empty_summary()

    # Schwab sometimes puts a title line above the header (e.g. "Positions as
    # of 08/08/2026"). Strip it so the header is the first line.
    raw_bytes = _strip_title_line(raw_bytes)

    df = _read_any_format(raw_bytes)
    df.columns = df.columns.str.strip()
    df = _normalize_schwab_columns(df)

    if df.empty:
        return pd.DataFrame(), _empty_summary()

    if "Symbol" not in df.columns:
        return pd.DataFrame(), _empty_summary()

    cash_balance = 0.0
    rows = []

    for _, raw in df.iterrows():
        symbol = str(raw.get("Symbol", "")).strip()
        if not symbol or symbol.upper() in ("", "NAN", "TOTAL", "POSITIONS TOTAL"):
            continue

        desc = str(_schwab_col(raw, "Description", "Name") or "").strip()
        desc_upper = desc.upper()
        if any(k in desc_upper for k in ("CASH", "SWEEP", "MONEY MARKET", "MMKT")) or "CASH" in symbol.upper():
            val = _parse_numeric(_schwab_col(raw, "Market Value", "Value", "Price"))
            if val != 0:
                cash_balance += val
            continue

        quantity = _parse_numeric(_schwab_col(raw, "Quantity", "Qty"))
        current_price = _parse_numeric(_schwab_col(raw, "Price", "Last Price"))
        market_value = _parse_numeric(_schwab_col(raw, "Market Value", "Value"))
        cost_basis = _parse_numeric(_schwab_col(raw, "Cost Basis", "Cost Basis Total", "Cost"))
        total_pl = _parse_numeric(_schwab_col(raw, "Unrealized Gain/Loss", "Unrealized Gain/Loss ($)", "Gain/Loss ($)"))
        day_pl = _parse_numeric(_schwab_col(raw, "Today's Gain/Loss", "Today's Gain/Loss ($)", "Day Gain/Loss"))
        day_pl_pct = _parse_numeric(_schwab_col(raw, "Today's Gain/Loss %", "Day Gain/Loss %"))

        # Derive avg_cost from cost basis when no per-share cost provided.
        # Pass cost_basis as total_cost and 0 as avg_cost so the shared
        # fallback logic applies identically to both brokers.
        current_price, market_value, avg_cost, total_pl = _derive_fields(
            quantity, current_price, market_value, 0.0, cost_basis, total_pl
        )

        # Detect option tickers: "AAPL 03/20/26 275.0 C"
        opt_match = _SCHWAB_OPTION_RE.match(symbol)
        option_fields = None
        if opt_match:
            option_fields = (
                opt_match.group(1).upper(),
                "CALL" if opt_match.group(4).upper() == "C" else "PUT",
                _parse_numeric(opt_match.group(3)),
                opt_match.group(2),
            )

        rows.append(_build_row(
            symbol, desc, quantity, current_price, market_value,
            day_pl, day_pl_pct, total_pl, avg_cost, "Schwab", option_fields,
        ))

    positions_df = pd.DataFrame(rows) if rows else pd.DataFrame()
    return positions_df, _compute_summary(positions_df, cash_balance)
