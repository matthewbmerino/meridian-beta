"""Persistent portfolio snapshots.

Builds a time-series reserve of portfolio state on every snapshot call:
- portfolio_snapshots  : account-level aggregates per timestamp
- position_snapshots   : per-symbol holdings per timestamp
- transactions         : deduplicated trade log

Storage: SQLite at data/snapshots/portfolio.db (global, shared across sessions).
All writes are best-effort and must never raise into the page render path —
use ``maybe_snapshot`` for that and catch at the call site.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

import pandas as pd

from data.crypto import decrypt_sqlite_to_runtime, encrypt_sqlite_from_runtime

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_ROOT = Path(__file__).resolve().parent.parent
_DB_DIR = _ROOT / "data" / "snapshots"

# A collapsed store is the failure that cost a day of history: the plaintext
# decrypted to an almost-empty DB, the app kept writing, and the shutdown
# re-encrypt replaced the good ciphertext. The tripwire remembers the last
# healthy count and refuses to write or re-encrypt when the live count halves.
_COUNT_FILE = _DB_DIR / "row-count.json"
_WEDGED = False


def db_path() -> Path:
    """Return the global snapshot DB path.

    The DB is encrypted at rest (data/crypto.py, AES-GCM) at
    data/snapshots/portfolio.db.enc. On access it is decrypted to the runtime
    plaintext (0600); the scheduler re-encrypts on shutdown so no plaintext
    survives once the process stops.
    """
    _DB_DIR.mkdir(parents=True, exist_ok=True)
    db = _DB_DIR / "portfolio.db"
    try:
        decrypt_sqlite_to_runtime(db, "portfolio_db")
    except Exception as e:
        log.warning("Snapshot DB decrypt failed: %s", e)
    return db


def reencrypt() -> None:
    """Re-encrypt the snapshot DB and remove the runtime plaintext.

    Called on shutdown. Best-effort — never raises.

    Refuses while the store is WEDGED: if the runtime DB came up with a
    collapsed row count, encrypting it would overwrite the only good at-rest
    copy with the collapse.
    """
    if _WEDGED:
        log.error("refusing to re-encrypt a collapsed snapshot DB — the at-rest copy is untouched")
        return
    try:
        encrypt_sqlite_from_runtime(_DB_DIR / "portfolio.db", "portfolio_db")
    except Exception as e:
        log.warning("Snapshot DB re-encrypt failed: %s", e)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS portfolio_snapshots (
        ts                   TEXT PRIMARY KEY,
        net_liquidation      REAL,
        cash_balance         REAL,
        buying_power         REAL,
        long_market_value    REAL,
        short_market_value   REAL,
        position_count       INTEGER,
        total_day_pl         REAL,
        total_unrealized_pl  REAL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS position_snapshots (
        ts              TEXT NOT NULL,
        symbol          TEXT NOT NULL,
        source          TEXT NOT NULL,
        description     TEXT,
        asset_type      TEXT,
        quantity        REAL,
        avg_cost        REAL,
        current_price   REAL,
        market_value    REAL,
        day_pl          REAL,
        total_pl        REAL,
        underlying      TEXT,
        PRIMARY KEY (ts, symbol, source)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS transactions (
        id           TEXT PRIMARY KEY,
        ts           TEXT,
        type         TEXT,
        symbol       TEXT,
        description  TEXT,
        quantity     REAL,
        price        REAL,
        amount       REAL,
        fees         REAL,
        raw_json     TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_position_snapshots_ts ON position_snapshots(ts)",
    "CREATE INDEX IF NOT EXISTS idx_position_snapshots_symbol ON position_snapshots(symbol)",
    "CREATE INDEX IF NOT EXISTS idx_transactions_ts ON transactions(ts)",
    "CREATE INDEX IF NOT EXISTS idx_transactions_symbol ON transactions(symbol)",
]


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path())
    conn.row_factory = sqlite3.Row
    # WAL lets concurrent readers coexist with writers — useful if an
    # analysis notebook opens the DB while the app is also writing.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    for stmt in _SCHEMA:
        conn.execute(stmt)
    conn.commit()
    _guard_collapse(conn)
    return conn


# A collapsed store is the failure that cost a day of history: the plaintext
# decrypted to an almost-empty DB, the app kept writing, and the shutdown
# re-encrypt replaced the good ciphertext. The tripwire remembers the last
# healthy count and refuses to write or re-encrypt when the live count halves.
def _read_expected_count(count_file: Path | None = None) -> int:
    path = count_file or _COUNT_FILE
    try:
        return int(json.loads(path.read_text(encoding="utf-8")).get("count", 0))
    except (OSError, ValueError, TypeError):
        return 0


def _write_expected_count(count: int) -> None:
    count_file = db_path().parent / "row-count.json"
    tmp = count_file.with_suffix(".json.tmp")
    payload = {"count": count, "ts": _dt.datetime.now(_dt.timezone.utc).isoformat()}
    try:
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(count_file)
    except OSError:
        pass


def _guard_collapse(conn: sqlite3.Connection) -> None:
    """Trip a latch when the live row count is a fraction of the last known one."""
    global _WEDGED
    try:
        live = conn.execute("select count(*) from portfolio_snapshots").fetchone()[0]
    except sqlite3.Error:
        return
    # The sidecar travels with the DB it describes: tests (and any caller that
    # redirects db_path) must not be judged against production's row count.
    count_file = db_path().parent / "row-count.json"
    expected = _read_expected_count(count_file)
    if expected >= 100 and live < expected // 2:
        _WEDGED = True
        log.error(
            "SNAPSHOT HISTORY COLLAPSED: %d rows live, %d last known. Writes and the "
            "shutdown re-encrypt are DISABLED so the at-rest copy is not overwritten. "
            "Restore portfolio.db.enc (or .enc.prev) before restarting.", live, expected,
        )
        return
    if live > expected:
        _write_expected_count(live)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _iso_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


def _f(v: Any) -> float | None:
    """Coerce to float, returning None for missing/NaN."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f:  # NaN
        return None
    return f


def _extract_tx_fields(tx: dict) -> dict:
    """Flatten a Schwab transaction dict into our row shape.

    Schwab's transaction response is deeply nested and varies by type; we
    stash the full JSON as well so nothing is ever lost.
    """
    tx_id = tx.get("activityId") or tx.get("transactionId") or tx.get("id")
    ts = tx.get("time") or tx.get("transactionDate") or tx.get("tradeDate")
    ttype = tx.get("type") or tx.get("activityType") or tx.get("transactionType")

    # transferItems is the usual carrier for fills
    items = tx.get("transferItems") or []
    symbol = ""
    description = ""
    quantity = None
    price = None
    amount = _f(tx.get("netAmount"))
    fees = _f(tx.get("fees", {}).get("totalFees") if isinstance(tx.get("fees"), dict) else None)

    for item in items:
        inst = item.get("instrument") or {}
        sym = inst.get("symbol") or ""
        if sym and inst.get("assetType") != "CURRENCY":
            symbol = sym
            description = inst.get("description", "") or description
            quantity = _f(item.get("amount"))
            price = _f(item.get("price"))
            break

    return {
        "id": str(tx_id) if tx_id is not None else None,
        "ts": ts,
        "type": ttype,
        "symbol": symbol,
        "description": description,
        "quantity": quantity,
        "price": price,
        "amount": amount,
        "fees": fees,
        "raw_json": json.dumps(tx, default=str),
    }


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------

def record_snapshot(summary: dict, positions: pd.DataFrame) -> str:
    """Append one portfolio + position snapshot.  Returns the timestamp written."""
    ts = _iso_now()

    net_liq = _f(summary.get("net_liquidation"))
    cash    = _f(summary.get("cash_balance"))
    bp      = _f(summary.get("buying_power"))
    long_mv = _f(summary.get("long_market_value"))
    short_mv= _f(summary.get("short_market_value"))

    if positions is None or positions.empty:
        position_count = 0
        total_day_pl = 0.0
        total_unrealized_pl = 0.0
        pos_rows: list[tuple] = []
    else:
        position_count = int(len(positions))
        day_pl = pd.to_numeric(positions.get("day_pl", pd.Series(dtype=float)), errors="coerce")
        total_day_pl = float(day_pl.fillna(0).sum())
        unrealized = pd.to_numeric(positions.get("total_pl", pd.Series(dtype=float)), errors="coerce")
        total_unrealized_pl = float(unrealized.fillna(0).sum())

        pos_rows = []
        for _, r in positions.iterrows():
            pos_rows.append((
                ts,
                str(r.get("symbol") or ""),
                str(r.get("source") or "Schwab"),
                str(r.get("description") or ""),
                str(r.get("asset_type") or ""),
                _f(r.get("quantity")),
                _f(r.get("avg_cost")),
                _f(r.get("current_price")),
                _f(r.get("market_value")),
                _f(r.get("day_pl")),
                _f(r.get("total_pl")),
                str(r.get("underlying") or ""),
            ))

    with _connect() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO portfolio_snapshots
                (ts, net_liquidation, cash_balance, buying_power,
                 long_market_value, short_market_value, position_count,
                 total_day_pl, total_unrealized_pl)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (ts, net_liq, cash, bp, long_mv, short_mv,
             position_count, total_day_pl, total_unrealized_pl),
        )
        if pos_rows:
            conn.executemany(
                """
                INSERT OR REPLACE INTO position_snapshots
                    (ts, symbol, source, description, asset_type, quantity,
                     avg_cost, current_price, market_value, day_pl, total_pl,
                     underlying)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                pos_rows,
            )
        conn.commit()
    return ts


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def last_snapshot_time() -> _dt.datetime | None:
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT ts FROM portfolio_snapshots ORDER BY ts DESC LIMIT 1"
            ).fetchone()
    except Exception as e:
        log.warning("Snapshot DB read failed (last_snapshot_time): %s", e)
        return None
    if not row:
        return None
    try:
        return _dt.datetime.fromisoformat(row["ts"])
    except ValueError:
        return None


def snapshot_count() -> int:
    try:
        with _connect() as conn:
            return int(conn.execute(
                "SELECT COUNT(*) FROM portfolio_snapshots"
            ).fetchone()[0])
    except Exception as e:
        log.warning("Snapshot DB read failed (snapshot_count): %s", e)
        return 0


def maybe_snapshot(min_interval_seconds: int = 300) -> bool:
    """Take a snapshot if the most recent one is older than ``min_interval_seconds``.

    Never raises: returns False on any failure so it's safe to call from the
    app's render path on every page load.

    Refuses to record a snapshot when the portfolio is empty — recording
    NAV=0 from a no-imports context fabricates a fake total drawdown on the
    equity curve (this corrupted the history before the storage unify).
    """
    try:
        if _WEDGED:
            log.error("skipping snapshot: the store is wedged after a history collapse")
            return False
        last = last_snapshot_time()
        if last is not None:
            age = (_dt.datetime.now(_dt.timezone.utc) - last).total_seconds()
            if age < min_interval_seconds:
                return False

        from data.account import get_combined_positions, get_combined_summary

        positions = get_combined_positions()
        if positions is None or positions.empty:
            return False

        summary   = get_combined_summary(positions)
        record_snapshot(summary, positions)

        return True
    except Exception as e:
        # Snapshot failures must be visible, not silent: this is the scheduler
        # path that builds the entire performance/equity-curve surface.
        log.warning("Snapshot failed: %s", e)
        return False


# ---------------------------------------------------------------------------
# Readers
# ---------------------------------------------------------------------------

def _query_snapshots(table: str, symbol: str | None = None,
                     start: _dt.datetime | None = None,
                     end: _dt.datetime | None = None) -> pd.DataFrame:
    """Query a snapshot table with optional symbol/date filters.

    Shared by load_portfolio_history and load_position_history so the
    WHERE-clause construction and ts coercion can't drift.
    """
    q = f"SELECT * FROM {table}"
    clauses, params = [], []
    if symbol:
        clauses.append("symbol = ?")
        params.append(symbol)
    if start is not None:
        clauses.append("ts >= ?")
        params.append(start.isoformat())
    if end is not None:
        clauses.append("ts <= ?")
        params.append(end.isoformat())
    if clauses:
        q += " WHERE " + " AND ".join(clauses)
    q += " ORDER BY ts"
    try:
        with _connect() as conn:
            df = pd.read_sql(q, conn, params=params)
    except Exception as e:
        log.warning("%s read failed: %s", table, e)
        return pd.DataFrame()
    if df.empty:
        return df
    df["ts"] = pd.to_datetime(df["ts"], utc=True, errors="coerce")
    return df.dropna(subset=["ts"]).reset_index(drop=True)


def load_portfolio_history(
    start: _dt.datetime | None = None,
    end: _dt.datetime | None = None,
) -> pd.DataFrame:
    """Return the portfolio_snapshots table as a DataFrame, sorted by ts."""
    return _query_snapshots("portfolio_snapshots", start=start, end=end)


def load_position_history(
    symbol: str | None = None,
    start: _dt.datetime | None = None,
    end: _dt.datetime | None = None,
) -> pd.DataFrame:
    return _query_snapshots("position_snapshots", symbol=symbol, start=start, end=end)
