"""Persistent storage for imported position files (CSV/Excel).

Stores parsed position DataFrames as CSV files on disk in ``data/imports/``,
with a JSON manifest tracking metadata and visibility.  All writes to the
manifest are atomic (write → tmp, rename) to prevent corruption.
"""

from __future__ import annotations

import io
import logging
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from data.crypto import load_json_store, read_bytes_store, write_bytes_store, write_json
from data.session import data_root

log = logging.getLogger(__name__)

# Chase option tickers: "AAPL CALL 03/06/26 $275.00"
_OPTION_RE = re.compile(
    r'^([A-Z.]+)\s+(CALL|PUT)\s+(\d{1,2}/\d{1,2}/\d{2,4})\s+\$?([\d,.]+)',
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Paths — session-scoped
# ---------------------------------------------------------------------------


def _imports_dir() -> Path:
    """Return the global imports directory (single-operator, private)."""
    imports = data_root() / "imports"
    imports.mkdir(parents=True, exist_ok=True)
    return imports


def _manifest_path() -> Path:
    return _imports_dir() / "manifest.json"

# ---------------------------------------------------------------------------
# Column dtypes to preserve across CSV round-trips
# ---------------------------------------------------------------------------
_POSITION_DTYPES = {
    "symbol": str,
    "description": str,
    "asset_type": str,
    "quantity": float,
    "avg_cost": float,
    "market_value": float,
    "current_price": float,
    "day_pl": float,
    "day_pl_pct": float,
    "total_pl": float,
    "underlying": str,
    "put_call": str,
    "strike": float,
    "expiration": str,
    "option_multiplier": float,
    "source": str,
    "stale": bool,
    "session": str,
}

# ---------------------------------------------------------------------------
# Source inference
# ---------------------------------------------------------------------------
_SOURCE_PATTERNS = {
    "chase": "Chase",
    "fidelity": "Fidelity",
    "schwab": "Schwab",
    "vanguard": "Vanguard",
    "etrade": "E*TRADE",
    "robinhood": "Robinhood",
    "td": "TD Ameritrade",
    "merrill": "Merrill",
    "interactive": "IBKR",
    "webull": "Webull",
}


def _infer_source(filename: str) -> str:
    """Best-effort source label from the filename."""
    lower = filename.lower()
    for pattern, label in _SOURCE_PATTERNS.items():
        if pattern in lower:
            return label
    return "Import"


# ---------------------------------------------------------------------------
# Manifest helpers
# ---------------------------------------------------------------------------

def _read_manifest() -> list[dict]:
    mp = _manifest_path()
    data = load_json_store(mp, "import_manifest", [])
    return data if isinstance(data, list) else []


def _write_manifest(entries: list[dict]):
    try:
        write_json(_manifest_path(), "import_manifest", entries)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _fmt_file_size(n_bytes: int) -> str:
    """Human-readable file size."""
    if n_bytes < 1024:
        return f"{n_bytes} B"
    if n_bytes < 1024 * 1024:
        return f"{n_bytes / 1024:.1f} KB"
    return f"{n_bytes / (1024 * 1024):.1f} MB"


def _source_from_rows(positions_df: pd.DataFrame) -> str | None:
    """Return the broker label carried on the parsed rows, if any.

    Parsers stamp each row with a canonical ``source`` (``Chase``, ``Schwab``)
    derived from the file's *contents* — strictly more reliable than the
    filename. Returns the single distinct value, or None when the column is
    absent/empty/ambiguous (multiple brokers in one file can't be labeled).
    """
    if positions_df is None or "source" not in positions_df.columns:
        return None
    values = {
        str(v).strip()
        for v in positions_df["source"].dropna().unique()
        if str(v).strip()
    }
    return values.pop() if len(values) == 1 else None


def _import_source(entry: dict, positions_df: pd.DataFrame | None = None) -> str:
    """Resolve an import's broker label at read time.

    The manifest ``source`` is preferred — but records created before row-source
    capture carry the generic ``"Import"`` (filename inference). In that case
    fall back to the frame's row-level ``source`` so legacy imports label
    correctly without a destructive migration.
    """
    manifest_source = str(entry.get("source") or "").strip()
    if manifest_source and manifest_source != "Import":
        return manifest_source
    row_source = _source_from_rows(positions_df) if positions_df is not None else None
    if row_source:
        return row_source
    return manifest_source or _infer_source(entry.get("filename", ""))


def _load_positions_frame(entry: dict) -> pd.DataFrame | None:
    """Load one positions import's DataFrame, or None if unreadable."""
    csv_path = _imports_dir() / entry.get("csv_file", "")
    if not csv_path.exists():
        return None
    try:
        raw = read_bytes_store(csv_path, f"import:{entry.get('id', '')}")
        if raw is None:
            return None
        return pd.read_csv(io.BytesIO(raw), dtype=_POSITION_DTYPES)
    except Exception as e:
        log.warning("Failed to read imported positions file %s: %s", csv_path, e)
        return None


def add_import(filename: str, positions_df: pd.DataFrame, summary: dict) -> str:
    """Persist a parsed import to disk. Returns the new document id."""
    idir = _imports_dir()
    doc_id = uuid.uuid4().hex[:12]
    csv_file = f"{doc_id}.csv"
    csv_path = idir / csv_file

    write_bytes_store(csv_path, f"import:{doc_id}", positions_df.to_csv(index=False).encode())

    # Prefer the parser's row-level broker label (content-derived) over the
    # filename heuristic, so "where is my cash held" can trust the label.
    source = _source_from_rows(positions_df) or _infer_source(filename)

    entry = {
        "id": doc_id,
        "filename": filename,
        "imported_at": datetime.now(timezone.utc).isoformat(),
        "visible": True,
        "type": "positions",
        "position_count": len(positions_df),
        "file_size": _fmt_file_size(csv_path.stat().st_size),
        "summary": summary,
        "csv_file": csv_file,
        "source": source,
    }

    manifest = _read_manifest()
    manifest.append(entry)
    _write_manifest(manifest)
    return doc_id


def list_imports() -> list[dict]:
    """Return metadata for position imports (no DataFrame loading)."""
    return [e for e in _read_manifest()
            if e.get("type") not in ("activity", "external")]


def get_visible_imports_with_summary() -> list[dict]:
    """Per-import metadata + summary for visible positions imports.

    Unlike ``get_all_visible_summaries`` (which merges every import into one
    dict), this preserves the per-brokerage boundary so callers can answer
    "where is my cash held". The broker label is resolved through
    ``_import_source`` (row-level fallback for legacy records). Imports with no
    summary are skipped rather than yielding a bare entry.
    """
    entries: list[dict] = []
    for entry in _read_manifest():
        if entry.get("type") in ("activity", "external"):
            continue
        if not entry.get("visible", True):
            continue
        summary = entry.get("summary")
        if summary is None:
            continue
        frame = _load_positions_frame(entry)
        entries.append({
            "id": entry.get("id"),
            "filename": entry.get("filename", ""),
            "imported_at": entry.get("imported_at"),
            "source": _import_source(entry, frame),
            "summary": summary,
        })
    return entries


def set_visible(doc_id: str, visible: bool) -> bool:
    """Set (not flip) an import's visibility. Returns True when it existed.

    The automated Schwab pull needs "make the previous snapshot invisible".
    Toggling would depend on whatever the flag happened to be and can silently
    leave two live snapshots of one account — which doubles the book, because
    get_all_visible_positions concatenates every visible import.
    """
    manifest = _read_manifest()
    for entry in manifest:
        if entry.get("id") == doc_id:
            entry["visible"] = bool(visible)
            _write_manifest(manifest)
            return True
    return False


def latest_positions_import(source: str) -> dict | None:
    """Newest VISIBLE positions import labeled *source* (case-insensitive).

    Resolves the label the way the rest of the module does — the manifest's
    ``source`` first, then a legacy row-level label — so an old import written
    before labels were captured is still recognised as the same broker.
    """
    wanted = str(source or "").strip().lower()
    newest = None
    for entry in _read_manifest():
        if entry.get("type") in ("activity", "external"):
            continue
        if not entry.get("visible", True):
            continue
        frame = None
        if not str(entry.get("source") or "").strip() or entry.get("source") == "Import":
            frame = _load_positions_frame(entry)
        if _import_source(entry, frame).strip().lower() != wanted:
            continue
        newest = entry
    return newest


def toggle_visibility(doc_id: str) -> bool:
    """Flip the visible flag for *doc_id*. Returns the new state."""
    manifest = _read_manifest()
    for entry in manifest:
        if entry["id"] == doc_id:
            entry["visible"] = not entry["visible"]
            _write_manifest(manifest)
            return entry["visible"]
    return False


def remove_import(doc_id: str):
    """Delete a document's CSV and remove it from the manifest (idempotent)."""
    idir = _imports_dir()
    manifest = _read_manifest()
    new_manifest = []
    for entry in manifest:
        if entry["id"] == doc_id:
            # The manifest is locally written, but never trust stored
            # filenames in a path join — resolve and confirm the file is
            # actually inside the session's imports directory.
            for key in ("csv_file", "lot_file", "summary_file", "external_file"):
                fname = entry.get(key, "")
                if not fname:
                    continue
                candidate = (idir / fname).resolve()
                if candidate.is_relative_to(idir.resolve()) and candidate.exists():
                    candidate.unlink()
        else:
            new_manifest.append(entry)
    _write_manifest(new_manifest)


def get_all_visible_positions() -> pd.DataFrame:
    """Load and concatenate positions from all *visible* imports."""
    frames = []
    for entry in _read_manifest():
        # Activity (realized G/L) and external (LLM-extracted) imports are not
        # position files — their CSV schema differs and must never be parsed as
        # holdings (it logged spurious read failures before this guard).
        if entry.get("type") in ("activity", "external"):
            continue
        if not entry.get("visible", True):
            continue
        df = _load_positions_frame(entry)
        if df is not None:
            frames.append(df)
    if not frames:
        return pd.DataFrame()
    combined = pd.concat(frames, ignore_index=True)

    # Normalize: detect option tickers stored as EQUITY and fix fields
    for idx, row in combined.iterrows():
        if row.get("asset_type") == "OPTION":
            continue
        m = _OPTION_RE.match(str(row.get("symbol", "")))
        if m:
            combined.at[idx, "asset_type"] = "OPTION"
            combined.at[idx, "underlying"] = m.group(1).upper()
            combined.at[idx, "put_call"] = m.group(2).upper()
            combined.at[idx, "strike"] = float(m.group(4).replace(",", ""))
            combined.at[idx, "expiration"] = m.group(3)
            combined.at[idx, "option_multiplier"] = 100.0

    # Derive avg_cost from market_value and total_pl for legacy imports
    for idx, row in combined.iterrows():
        avg_cost = row.get("avg_cost", 0) or 0
        if avg_cost != 0:
            continue
        qty = row.get("quantity", 0) or 0
        mv = row.get("market_value", 0) or 0
        tpl = row.get("total_pl", 0) or 0
        mult = row.get("option_multiplier", 1) or 1
        if qty != 0 and mv != 0:
            cost_basis = mv - tpl
            derived = cost_basis / (qty * mult)
            if derived > 0:
                combined.at[idx, "avg_cost"] = derived

    return combined


def add_activity_import(filename: str, lot_df: pd.DataFrame,
                        summary_df: pd.DataFrame, total_pl: float) -> str:
    """Persist a parsed realized G/L import. Returns the new document id."""
    idir = _imports_dir()
    doc_id = uuid.uuid4().hex[:12]

    # Store lot-level detail
    lot_file = f"activity_lots_{doc_id}.csv"
    lot_path = idir / lot_file
    write_bytes_store(lot_path, f"activity:{doc_id}", lot_df.to_csv(index=False).encode())

    # Store summary (by underlying)
    summary_file = f"activity_summary_{doc_id}.csv"
    summary_path = idir / summary_file
    write_bytes_store(summary_path, f"activity:{doc_id}", summary_df.to_csv(index=False).encode())

    # Infer source from the data itself if available
    source = "Import"
    if "source" in lot_df.columns and not lot_df.empty:
        source = lot_df["source"].iloc[0]
    elif "source" in summary_df.columns and not summary_df.empty:
        source = summary_df["source"].iloc[0]
    else:
        source = _infer_source(filename)

    entry = {
        "id": doc_id,
        "filename": filename,
        "imported_at": datetime.now(timezone.utc).isoformat(),
        "visible": True,
        "type": "activity",
        "lot_count": len(lot_df),
        "symbol_count": len(summary_df),
        "total_realized_pl": total_pl,
        "file_size": _fmt_file_size(lot_path.stat().st_size),
        "lot_file": lot_file,
        "summary_file": summary_file,
        "csv_file": summary_file,  # backward compat
        "source": source,
    }

    manifest = _read_manifest()
    manifest.append(entry)
    _write_manifest(manifest)
    return doc_id


def list_activity_imports() -> list[dict]:
    """Return metadata for activity imports only."""
    return [e for e in _read_manifest() if e.get("type") == "activity"]


def get_all_activity_realized_pl():
    """Load and concatenate realized P&L summaries from all visible activity imports.

    Returns:
        (DataFrame with columns: underlying, source, realized_pl, st_gl, lt_gl,
         proceeds, cost_basis, trade_count;  float total)
    """
    idir = _imports_dir()
    manifest = _read_manifest()
    frames = []
    for entry in manifest:
        if entry.get("type") != "activity":
            continue
        if not entry.get("visible", True):
            continue
        summary_file = entry.get("summary_file", entry.get("csv_file", ""))
        csv_path = idir / summary_file
        if not csv_path.exists():
            continue
        try:
            raw = read_bytes_store(csv_path, f"activity:{entry.get('id', '')}")
            if raw is None:
                continue
            df = pd.read_csv(io.BytesIO(raw))
            frames.append(df)
        except Exception as e:
            log.warning("Failed to read imported activity file %s: %s", csv_path, e)
            continue

    if not frames:
        return pd.DataFrame(), 0.0

    combined = pd.concat(frames, ignore_index=True)
    total = combined["realized_pl"].sum() if "realized_pl" in combined.columns else 0.0
    return combined, round(total, 2)


def get_all_visible_summaries() -> dict | None:
    """Merge summaries of all visible imports. Returns None if nothing visible."""
    manifest = _read_manifest()
    combined = None
    for entry in manifest:
        if entry.get("type") == "activity":
            continue
        if not entry.get("visible", True):
            continue
        summary = entry.get("summary")
        if summary is None:
            continue
        if combined is None:
            combined = {k: v or 0 for k, v in summary.items()}
        else:
            for k, v in summary.items():
                combined[k] = combined.get(k, 0) + (v or 0)
    return combined


# ---------------------------------------------------------------------------
# External P&L imports (LLM-extracted: gambling, prediction markets, etc.)
# ---------------------------------------------------------------------------

# Canonical column order for persisted external entry rows.
_EXTERNAL_COLUMNS = ["date", "description", "stake", "payout", "net"]


def add_external_import(filename: str, record: dict) -> str:
    """Persist an LLM-extracted external P&L record. Returns the doc id.

    ``record`` is the validated extraction: {total_net, source, schema_name,
    entries: [{date, description, stake, payout, net}], period_start/end, ...}.
    """
    idir = _imports_dir()
    doc_id = uuid.uuid4().hex[:12]

    entries = record.get("entries") or []
    rows = []
    for e in entries:
        rows.append({
            "date": e.get("date"),
            "description": e.get("description"),
            "stake": e.get("stake"),
            "payout": e.get("payout"),
            "net": e.get("net"),
        })
    if not rows:
        # A summary-only record still stores one aggregate row so the ledger
        # shows the period total.
        rows.append({
            "date": record.get("period_end") or record.get("period_start"),
            "description": record.get("description") or record.get("schema_name", "External"),
            "stake": record.get("total_staked"),
            "payout": record.get("total_payout"),
            "net": record.get("total_net"),
        })

    ext_df = pd.DataFrame(rows, columns=_EXTERNAL_COLUMNS)
    ext_file = f"external_{doc_id}.csv"
    write_bytes_store(idir / ext_file, f"external:{doc_id}", ext_df.to_csv(index=False).encode())

    entry = {
        "id": doc_id,
        "filename": filename,
        "imported_at": datetime.now(timezone.utc).isoformat(),
        "visible": True,
        "type": "external",
        "source": record.get("source", "external_other"),
        "schema_name": record.get("schema_name", "Other External P&L"),
        "total_net": record.get("total_net"),
        "needs_review": bool(record.get("needs_review")),
        "entry_count": len(rows),
        "period_start": record.get("period_start"),
        "period_end": record.get("period_end"),
        "description": record.get("description"),
        "file_size": _fmt_file_size((idir / ext_file).stat().st_size),
        "external_file": ext_file,
        "csv_file": ext_file,  # backward-compat with remove_import's path set
    }

    manifest = _read_manifest()
    manifest.append(entry)
    _write_manifest(manifest)
    return doc_id


def list_external_imports() -> list[dict]:
    """Return metadata for external P&L imports only."""
    return [e for e in _read_manifest() if e.get("type") == "external"]


def get_all_external_pl(include_unreviewed: bool = False):
    """Aggregate net P&L across all visible external imports.

    Excludes imports flagged ``needs_review`` by default — a non-reconciled
    LLM extraction is a wrong number and must never roll into total returns.
    Pass ``include_unreviewed=True`` to include them (e.g. a review surface).

    Returns:
        (rows: list[dict] with source, total_net, needs_review, period_*,
         total: float)
    """
    # Manual (no-document) entries merge into the same ledger — operator-
    # asserted numbers with provenance (bank interest without a statement,
    # bonuses). A non-reconciled extraction is excluded; an asserted entry
    # drops out only by deletion.
    from data.external_pl_manual import manual_pl_rows
    manual_rows, manual_total = manual_pl_rows()

    manifest = _read_manifest()
    rows = []
    total = 0.0
    for entry in manifest:
        if entry.get("type") != "external":
            continue
        if not entry.get("visible", True):
            continue
        if entry.get("needs_review") and not include_unreviewed:
            continue
        net = entry.get("total_net")
        if net is None:
            continue
        net_f = float(net)
        total += net_f
        rows.append({
            "source": entry.get("source", "external_other"),
            "schema_name": entry.get("schema_name", "Other External P&L"),
            "total_net": net_f,
            "needs_review": bool(entry.get("needs_review")),
            "filename": entry.get("filename", ""),
            "period_start": entry.get("period_start"),
            "period_end": entry.get("period_end"),
            "id": entry.get("id"),
        })
    rows.extend(manual_rows)
    total += manual_total
    return rows, round(total, 2)


def get_all_activity_lots():
    """Concatenate the LOT-LEVEL tape from all visible activity imports.

    The rawest material the book holds: the broker's per-lot record (close
    date, term, proceeds, basis, gain). The summaries digest it; this returns
    it undigested for the tax reads in analysis/tax.py.
    """
    idir = _imports_dir()
    manifest = _read_manifest()
    frames = []
    for entry in manifest:
        if entry.get("type") != "activity" or not entry.get("visible", True):
            continue
        lot_file = entry.get("lot_file", "")
        lot_path = idir / lot_file
        if not lot_file or not lot_path.exists():
            continue
        try:
            raw = read_bytes_store(lot_path, f"activity:{entry['id']}")
            if raw is None:
                continue
            frames.append(pd.read_csv(io.BytesIO(raw)))
        except Exception:
            continue
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)
