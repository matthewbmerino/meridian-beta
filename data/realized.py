"""Realized P&L from brokerage realized gain/loss CSV exports.

Parses pre-computed realized gain/loss reports from Chase and Schwab,
aggregates by underlying symbol, and provides combined totals.
"""

import re

import pandas as pd

from data.chase import _parse_numeric, _read_any_format

# ---------------------------------------------------------------------------
# Underlying symbol extraction
# ---------------------------------------------------------------------------

# OCC-style: "CRWV 03/20/2026 75.00 P" or "AAPL Call 02/13/26 $280.00"
_OCC_RE = re.compile(r'^([A-Z.]+)\s+', re.IGNORECASE)


def _extract_underlying(symbol):
    """Pull the root equity ticker from an option or equity symbol."""
    s = symbol.strip()
    m = _OCC_RE.match(s)
    return m.group(1).upper() if m else s.upper()


# ---------------------------------------------------------------------------
# Schwab realized G/L report parser
# ---------------------------------------------------------------------------

def _parse_schwab_realized(df):
    """Parse Schwab 'Realized Gain/Loss - Lot Details' CSV.

    Schwab puts a metadata title on the first row, so the real column
    names end up as df.iloc[0] and actual data starts at df.iloc[1].
    """
    # Detect if columns are the metadata row (all "Unnamed:" or first col has "Realized")
    first_col = str(df.columns[0]).strip()
    has_second_col = len(df.columns) > 1
    if "realized" in first_col.lower() or (has_second_col and "unnamed" in str(df.columns[1]).lower()):
        df.columns = [str(v).strip() for v in df.iloc[0]]
        df = df.iloc[1:].reset_index(drop=True)
    df.columns = df.columns.str.strip()

    # Not a realized G/L file (e.g. a positions export routed here by filename) —
    # return empty so the upload flow falls through instead of crashing.
    realized_cols = {"Total Gain/Loss ($)", "Gain/Loss ($)", "Proceeds", "Cost Basis (CB)"}
    if "Symbol" not in df.columns or not realized_cols.intersection(df.columns):
        return pd.DataFrame()

    rows = []
    for _, row in df.iterrows():
        symbol = str(row.get("Symbol", "")).strip()
        if not symbol or symbol.upper() in ("", "NAN", "SYMBOL", "TOTAL"):
            continue

        gl = _parse_numeric(
            row.get("Total Gain/Loss ($)", row.get("Gain/Loss ($)", 0))
        )
        proceeds = _parse_numeric(row.get("Proceeds", 0))
        cost_basis = _parse_numeric(row.get("Cost Basis (CB)", 0))
        qty = abs(_parse_numeric(row.get("Quantity", 0)))
        close_date = str(row.get("Closed Date", "")).strip()
        st_gl = _parse_numeric(
            row.get("Short Term (ST) Gain/Loss ($)",
                    row.get("Short Term Gain/Loss", 0))
        )
        lt_gl = _parse_numeric(
            row.get("Long Term (LT) Gain/Loss ($)",
                    row.get("Long Term Gain/Loss", 0))
        )
        term = str(row.get("Term", "")).strip()
        if not term:
            term = "Short Term" if st_gl else ("Long Term" if lt_gl else "")
        name = str(row.get("Name", "")).strip()

        underlying = _extract_underlying(symbol)
        is_option = bool(re.search(r'\d{2}/\d{2}/\d{4}\s+[\d.]+\s+[CP]', symbol))

        rows.append({
            "symbol": symbol,
            "underlying": underlying,
            "name": name,
            "is_option": is_option,
            "quantity": qty,
            "proceeds": proceeds,
            "cost_basis": cost_basis,
            "realized_pl": gl,
            "st_gl": st_gl,
            "lt_gl": lt_gl,
            "term": term,
            "close_date": close_date,
            "source": "Schwab",
        })

    return pd.DataFrame(rows) if rows else pd.DataFrame()


# ---------------------------------------------------------------------------
# Chase realized G/L report parser
# ---------------------------------------------------------------------------

def _parse_chase_realized(df):
    """Parse Chase realized gain/loss CSV export."""
    df.columns = df.columns.str.strip()

    # Not a realized G/L file — return empty so the upload flow falls through.
    # A Chase *positions* export also has a "Ticker" column, so require a
    # realized-specific column too (regression: positions were misrouted here
    # by filename and stored as activity with zeroed P&L).
    realized_cols = {
        "Total Realized Gain Loss USD",
        "Cost Basis USD",
        "Sale Date",
        "Market Cost/Proceeds USD",
    }
    if "Ticker" not in df.columns or not realized_cols.intersection(df.columns):
        return pd.DataFrame()

    rows = []
    for _, row in df.iterrows():
        symbol = str(row.get("Ticker", "")).strip()
        if not symbol or symbol.upper() in ("", "NAN", "TICKER"):
            continue

        gl = _parse_numeric(row.get("Total Realized Gain Loss USD", 0))
        proceeds = _parse_numeric(row.get("Market Cost/Proceeds USD", 0))
        cost_basis = _parse_numeric(row.get("Cost Basis USD", 0))
        qty = abs(_parse_numeric(row.get("Quantity", 0)))
        sale_date = str(row.get("Sale Date", "")).strip()
        st_gl = _parse_numeric(row.get("Short Term Realized Gain Loss USD", 0))
        lt_gl = _parse_numeric(row.get("Long Term Realized Gain Loss USD", 0))
        desc = str(row.get("Description", "")).strip()
        sec_type = str(row.get("Security Type", "")).strip().lower()

        underlying = _extract_underlying(symbol)
        is_option = sec_type == "options" or bool(
            re.search(r'(CALL|PUT)\s+', symbol, re.IGNORECASE)
        )

        rows.append({
            "symbol": symbol,
            "underlying": underlying,
            "name": desc,
            "is_option": is_option,
            "quantity": qty,
            "proceeds": proceeds,
            "cost_basis": cost_basis,
            "realized_pl": gl,
            "st_gl": st_gl,
            "lt_gl": lt_gl,
            "term": "Short Term" if st_gl else ("Long Term" if lt_gl else ""),
            "close_date": sale_date,
            "source": "Chase",
        })

    return pd.DataFrame(rows) if rows else pd.DataFrame()


# ---------------------------------------------------------------------------
# Unified parser entry point
# ---------------------------------------------------------------------------

def parse_realized_csv(file_content, filename=""):
    """Parse a realized gain/loss CSV from Chase or Schwab.

    Auto-detects format from column headers.

    Args:
        file_content: bytes, str, or Streamlit UploadedFile.
        filename: original filename for source inference.

    Returns:
        (lot_df: DataFrame with per-lot details,
         summary_df: DataFrame aggregated by underlying,
         total: float total realized P&L)
    """
    if hasattr(file_content, "read"):
        if hasattr(file_content, "seek"):
            file_content.seek(0)
        raw = file_content.read()
    elif isinstance(file_content, (bytes, str)):
        raw = file_content
    else:
        raw = file_content

    raw_bytes = raw if isinstance(raw, bytes) else raw.encode("utf-8")
    if len(raw_bytes) == 0:
        return pd.DataFrame(), pd.DataFrame(), 0.0

    df = _read_any_format(raw_bytes)
    df.columns = df.columns.str.strip()

    if df.empty:
        return pd.DataFrame(), pd.DataFrame(), 0.0

    # Detect format and parse
    # For Schwab, the first row is metadata so the real column names
    # end up as the first data row. Check both actual columns and first row.
    all_text = " ".join(str(c) for c in df.columns) + " "
    if len(df) > 0:
        all_text += " ".join(str(v) for v in df.iloc[0])

    if "Gain/Loss ($)" in all_text or "Cost Basis (CB)" in all_text:
        lot_df = _parse_schwab_realized(df)
    elif "Total Realized Gain Loss USD" in all_text or "Cost Basis USD" in all_text:
        lot_df = _parse_chase_realized(df)
    elif "schwab" in filename.lower():
        lot_df = _parse_schwab_realized(df)
    elif "chase" in filename.lower():
        lot_df = _parse_chase_realized(df)
    else:
        return pd.DataFrame(), pd.DataFrame(), 0.0

    if lot_df.empty:
        return pd.DataFrame(), pd.DataFrame(), 0.0

    # Aggregate by underlying
    summary_df = lot_df.groupby(["underlying", "source"]).agg(
        realized_pl=("realized_pl", "sum"),
        st_gl=("st_gl", "sum"),
        lt_gl=("lt_gl", "sum"),
        proceeds=("proceeds", "sum"),
        cost_basis=("cost_basis", "sum"),
        trade_count=("realized_pl", "count"),
    ).reset_index()

    total = lot_df["realized_pl"].sum()
    return lot_df, summary_df, round(total, 2)


# ---------------------------------------------------------------------------
# Combined realized P&L (all imported activity files)
# ---------------------------------------------------------------------------

def get_combined_realized_pl():
    """Load realized P&L from all imported activity CSVs.

    Returns:
        (summary_df: DataFrame aggregated by underlying with source,
         float total realized P&L)
    """
    from data.imports import get_all_activity_realized_pl
    return get_all_activity_realized_pl()
