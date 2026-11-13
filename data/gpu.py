"""GPU / compute-market feed — Vast.ai marketplace prices.

The largest GPU rental marketplace — the closest thing to a spot market for
compute. Provides per-GPU median $/GPU-hr, price percentiles, and TFLOPS
efficiency from the marketplace's *available offers*.

Data source: `POST /api/v0/bundles` (search offers) — the same data the Vast
dashboard shows when browsing GPUs. The full-market metrics endpoints
(`/metrics/gpu/*` — utilization, supply/demand, tightness) are host-only and
require an account with active machines, so this feed is scoped honestly as an
**available-offer spot price index**: a GPU class that is fully rented out
drops out of the sample. See `analysis/methodology.py` `compute_index`.

Persistence: SQLite at data/gpu/compute.db (global, shared across sessions —
the snapshot-unify lesson). The feed self-accumulates history on every
scheduler run so Meridian's compute history only grows. No retroactive
backfill is possible (the trends API is host-only), so history accumulates
forward from the first snapshot.

Degrades gracefully: when VAST_API_KEY is missing or a fetch fails, every
function returns None/empty — never fabricates.
"""

from __future__ import annotations

import datetime as _dt
import logging
import os
import random
import sqlite3
import time
from pathlib import Path

import pandas as pd
import requests

from data.crypto import decrypt_sqlite_to_runtime, encrypt_sqlite_from_runtime

log = logging.getLogger(__name__)

_VAST_API_KEY = os.getenv("VAST_API_KEY")
_VAST_BASE = "https://console.vast.ai/api/v0"

# Rate-limit handling. Vast.ai throttles with 429s (observed ~20% of hourly
# snapshot runs). _post backs off exponentially with jitter and retries within
# a per-run budget; a sustained throttle degrades to a skipped snapshot.
_MAX_ATTEMPTS_PER_RUN = 3
_BACKOFF_BASE = 2.0
_BACKOFF_MULTIPLIER = 2.0
_BACKOFF_MAX = 30.0
# Cooldown after a failed run: within this window a fetch degrades to None
# instead of re-hitting a throttled API (the negative TTL cache would
# otherwise re-trigger a fetch within a minute).
_last_failure: float = 0.0
_COOLDOWN_AFTER_FAILURE = 300.0
# Raw offers captured during the last get_gpu_metrics fetch (no extra
# requests) — consumed by the scheduler's record_offers persistence.
_last_offers: list[dict] = []

_ROOT = Path(__file__).resolve().parent.parent
_DB_DIR = _ROOT / "data" / "gpu"

# The datacenter GPU basket the index tracks. TFLOPS ratings are the
# efficiency weights (analysis/compute_index.py) — FP8 Tensor-Core specs from
# NVIDIA, the precision that matters for AI compute. Names match the Vast.ai
# search-offers API exactly. Evolve as GPUs launch.
GPU_BASKET: dict[str, float] = {
    "H100 SXM": 989.0,
    "H200": 989.0,
    "A100 SXM4": 312.0,
    "B200": 2250.0,
    "B300": 10000.0,
    "RTX 4090": 82.6,
    "RTX 5090": 104.8,
    "L40S": 362.0,
    "RTX 6000Ada": 38.7,
}

_SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS gpu_snapshots (
        ts          TEXT NOT NULL,
        gpu_name    TEXT NOT NULL,
        price_median REAL,
        price_p10   REAL,
        price_p25   REAL,
        price_p75   REAL,
        price_p90   REAL,
        total       INTEGER,
        available   INTEGER,
        rented      INTEGER,
        usage       REAL,
        tflops      REAL,
        tflops_per_dollar REAL,
        PRIMARY KEY (ts, gpu_name)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_gpu_ts ON gpu_snapshots(ts)",
    "CREATE INDEX IF NOT EXISTS idx_gpu_name ON gpu_snapshots(gpu_name)",
    """
    CREATE TABLE IF NOT EXISTS gpu_offers (
        ts          TEXT NOT NULL,
        gpu_name    TEXT NOT NULL,
        dph_total   REAL,
        num_gpus    INTEGER,
        dph_per_gpu REAL,
        total_flops REAL,
        verification TEXT,
        geolocation TEXT,
        reliability REAL,
        rented      INTEGER,
        is_bid      INTEGER,
        start_date  TEXT,
        end_date    TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_offers_ts ON gpu_offers(ts)",
    "CREATE INDEX IF NOT EXISTS idx_offers_gpu ON gpu_offers(gpu_name)",
]


def db_path() -> Path:
    _DB_DIR.mkdir(parents=True, exist_ok=True)
    db = _DB_DIR / "compute.db"
    try:
        decrypt_sqlite_to_runtime(db, "gpu_db")
    except Exception as e:
        log.warning("GPU DB decrypt failed: %s", e)
    return db


def reencrypt() -> None:
    """Re-encrypt the GPU DB and remove the runtime plaintext (on shutdown)."""
    try:
        encrypt_sqlite_from_runtime(_DB_DIR / "compute.db", "gpu_db")
    except Exception as e:
        log.warning("GPU DB re-encrypt failed: %s", e)


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    for stmt in _SCHEMA:
        conn.execute(stmt)
    conn.commit()
    return conn


def _post(path: str, json_data: dict | None = None, timeout: int = 20) -> dict | None:
    """Vast.ai POST with bearer auth. Returns None on any failure.

    Rate-limit handling: Vast.ai throttles with 429s (observed ~20% of hourly
    snapshot runs). On 429 we back off exponentially with jitter and retry,
    respecting ``Retry-After`` when present. A per-run request budget caps the
    total attempts so a sustained throttle degrades to a skipped snapshot
    (logged) instead of hammering the API. A failed run sets ``_last_failure``
    so ``get_gpu_metrics`` degrades to None during the cooldown window instead
    of re-hitting a throttled API.
    """
    if not _VAST_API_KEY:
        return None
    budget = _MAX_ATTEMPTS_PER_RUN
    delay = _BACKOFF_BASE
    while budget > 0:
        budget -= 1
        try:
            resp = requests.post(
                f"{_VAST_BASE}{path}",
                json=json_data or {},
                headers={"Authorization": f"Bearer {_VAST_API_KEY}"},
                timeout=timeout,
            )
            if resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After")
                wait = float(retry_after) if retry_after else delay
                if wait > _BACKOFF_MAX:
                    # Quota exhausted for a long window (observed Vast.ai daily
                    # quotas reporting Retry-After of hours). Sleeping here would
                    # block the scheduler worker for the whole window; abort the
                    # run immediately and let the cooldown absorb the wait.
                    log.warning(
                        "Vast.ai rate-limited (%s): Retry-After %.0fs exceeds "
                        "budget — aborting run",
                        path, wait,
                    )
                    return None
                wait *= random.uniform(0.8, 1.2)  # jitter — avoid synchronized retries
                log.warning(
                    "Vast.ai rate-limited (%s): retrying in %.1fs (%d attempts left)",
                    path, wait, budget,
                )
                time.sleep(wait)
                delay = min(delay * _BACKOFF_MULTIPLIER, _BACKOFF_MAX)
                continue
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            log.warning("Vast.ai POST failed (%s): %s", path, e)
            return None
    log.warning("Vast.ai POST gave up after %d attempts (%s): rate-limited", _MAX_ATTEMPTS_PER_RUN, path)
    return None


# ---------------------------------------------------------------------------
# Fetch (TTL-cached)
# ---------------------------------------------------------------------------

def _percentile(values: list[float], p: float) -> float | None:
    """Nearest-rank percentile of a sorted list. None when empty."""
    if not values:
        return None
    values = sorted(values)
    idx = min(len(values) - 1, max(0, int(round(p * (len(values) - 1)))))
    return values[idx]


def _mean(values: list[float]) -> float | None:
    """Arithmetic mean. None when empty."""
    if not values:
        return None
    return sum(values) / len(values)


def _std(values: list[float]) -> float | None:
    """Population standard deviation. None when empty or single-valued."""
    if len(values) < 2:
        return None
    m = sum(values) / len(values)
    return (sum((v - m) ** 2 for v in values) / len(values)) ** 0.5


def _offers_to_metric(gpu_name: str, offers: list[dict]) -> dict | None:
    """Derive a per-GPU metric dict from a search-offers response.

    price_* are percentiles of the per-GPU hourly rate (dph_total/num_gpus)
    across the available offers. `available` is the offer count. `total`,
    `rented`, and `usage` are not available from search offers (the full-market
    metrics API is host-only) — they degrade to None, never fabricated.
    """
    rates = []
    tflops = []
    tflops_per_dollar = []
    for o in offers:
        dph = o.get("dph_total")
        ng = o.get("num_gpus") or 1
        if dph and ng:
            rates.append(dph / ng)
        tf = o.get("total_flops")
        if tf and ng:
            tflops.append(tf / ng)
        if tf and dph:
            tflops_per_dollar.append(tf / dph)
    if not rates:
        return None
    return {
        "gpu_name": gpu_name,
        "price_median": _percentile(rates, 0.5),
        "price_p10": _percentile(rates, 0.1),
        "price_p25": _percentile(rates, 0.25),
        "price_p75": _percentile(rates, 0.75),
        "price_p90": _percentile(rates, 0.9),
        "price_mean": _mean(rates),
        "price_std": _std(rates),
        "price_min": min(rates),
        "price_max": max(rates),
        "count": len(rates),
        "total": None,
        "available": len(offers),
        "rented": None,
        "usage": None,
        "tflops": _percentile(tflops, 0.5),
        "tflops_per_dollar": _percentile(tflops_per_dollar, 0.5),
    }


def get_gpu_metrics() -> list[dict] | None:
    """Current per-GPU snapshot from the marketplace's available offers.

    Returns a list of {gpu_name, price_median, price_p10, price_p25, price_p75,
    price_p90, total, available, rented, usage, tflops, tflops_per_dollar} or
    None when the feed is unavailable. `total`/`rented`/`usage` are always
    None — search offers exposes only available offers.

    One request per basket GPU (a per-GPU filter defeats the API's ~128-offer
    response cap and gives each GPU a full sample).
    """
    from data.cache import ttl_cache

    @ttl_cache(ttl_seconds=900)
    def _fetch():
        global _last_failure, _last_offers
        now = time.time()
        if now - _last_failure < _COOLDOWN_AFTER_FAILURE:
            log.warning("GPU fetch skipped: cooldown after failed run (%.0fs left)",
                        _COOLDOWN_AFTER_FAILURE - (now - _last_failure))
            return None
        out = []
        raw: list[dict] = []
        for name in GPU_BASKET:
            data = _post("/bundles", {
                "limit": 10000,
                "type": "ondemand",
                "verified": {"eq": True},
                "gpu_name": {"eq": name},
            })
            if not data:
                _last_failure = time.time()
                return None
            offers = data.get("offers", [])
            for o in offers:
                o = dict(o)
                o["gpu_name"] = name
                raw.append(o)
            metric = _offers_to_metric(name, offers)
            if metric:
                out.append(metric)
        _last_offers = raw
        return out or None

    return _fetch()


def get_last_offers() -> list[dict]:
    """Raw offers captured during the most recent get_gpu_metrics fetch.

    The scheduler persists these via record_offers so the raw source of truth
    is never lost. Empty when no fetch has succeeded yet.
    """
    return list(_last_offers)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def record_snapshot(metrics: list[dict]) -> str | None:
    """Persist a metrics snapshot. Returns the timestamp written, or None.

    Best-effort — never raises into the render path.
    """
    if not metrics:
        return None
    ts = _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()
    try:
        with _connect() as conn:
            for m in metrics:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO gpu_snapshots
                        (ts, gpu_name, price_median, price_p10, price_p25,
                         price_p75, price_p90, total, available, rented,
                         usage, tflops, tflops_per_dollar)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (ts, m.get("gpu_name"), m.get("price_median"), m.get("price_p10"),
                     m.get("price_p25"), m.get("price_p75"), m.get("price_p90"),
                     m.get("total"), m.get("available"), m.get("rented"),
                     m.get("usage"), m.get("tflops"), m.get("tflops_per_dollar")),
                )
            conn.commit()
        return ts
    except Exception as e:
        log.warning("GPU snapshot write failed: %s", e)
        return None


def record_offers(offers: list[dict]) -> str | None:
    """Persist raw search-offer rows for a snapshot. Returns the timestamp, or None.

    Best-effort — never raises into the render path. Rows are the raw Vast.ai
    offer fields (dph_total, num_gpus, total_flops, verification, geolocation,
    reliability, rented, is_bid, start_date, end_date) so downstream analysis
    can re-derive any statistic from the source of truth.
    """
    if not offers:
        return None
    ts = _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()
    try:
        with _connect() as conn:
            for o in offers:
                dph = o.get("dph_total")
                ng = o.get("num_gpus") or 1
                conn.execute(
                    """
                    INSERT INTO gpu_offers
                        (ts, gpu_name, dph_total, num_gpus, dph_per_gpu,
                         total_flops, verification, geolocation, reliability,
                         rented, is_bid, start_date, end_date)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (ts, o.get("gpu_name"), dph, ng,
                     (dph / ng) if dph and ng else None,
                     o.get("total_flops"), o.get("verification"),
                     o.get("geolocation"), o.get("reliability"),
                     o.get("rented"), o.get("is_bid"),
                     o.get("start_date"), o.get("end_date")),
                )
            conn.commit()
        return ts
    except Exception as e:
        log.warning("GPU offers write failed: %s", e)
        return None


def load_offers(days: int | None = None, gpu: str | None = None) -> pd.DataFrame:
    """Load raw offer rows as a DataFrame, oldest first.

    Columns: ts, gpu_name, dph_total, num_gpus, dph_per_gpu, total_flops,
    verification, geolocation, reliability, rented, is_bid, start_date, end_date.
    """
    try:
        with _connect() as conn:
            sql = "SELECT * FROM gpu_offers"
            params: list = []
            clauses = []
            if days is not None:
                cutoff = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=days)).isoformat()
                clauses.append("ts >= ?")
                params.append(cutoff)
            if gpu:
                clauses.append("gpu_name = ?")
                params.append(gpu)
            if clauses:
                sql += " WHERE " + " AND ".join(clauses)
            sql += " ORDER BY ts"
            df = pd.read_sql(sql, conn, params=params)
    except Exception as e:
        log.warning("GPU offers read failed: %s", e)
        return pd.DataFrame()
    if df.empty:
        return df
    df["ts"] = pd.to_datetime(df["ts"], utc=True, errors="coerce")
    return df.dropna(subset=["ts"]).reset_index(drop=True)


def load_history(days: int | None = None) -> pd.DataFrame:
    """Load persisted GPU snapshots as a DataFrame, oldest first.

    Columns: ts, gpu_name, price_median, price_p10, price_p25, price_p75,
    price_p90, total, available, rented, usage, tflops, tflops_per_dollar.
    """
    try:
        with _connect() as conn:
            if days is not None:
                cutoff = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=days)).isoformat()
                df = pd.read_sql(
                    "SELECT * FROM gpu_snapshots WHERE ts >= ? ORDER BY ts",
                    conn, params=(cutoff,),
                )
            else:
                df = pd.read_sql("SELECT * FROM gpu_snapshots ORDER BY ts", conn)
    except Exception as e:
        log.warning("GPU history read failed: %s", e)
        return pd.DataFrame()
    if df.empty:
        return df
    df["ts"] = pd.to_datetime(df["ts"], utc=True, errors="coerce")
    return df.dropna(subset=["ts"]).reset_index(drop=True)


def earliest_ts() -> str | None:
    """Earliest persisted snapshot timestamp, or None."""
    try:
        with _connect() as conn:
            row = conn.execute("SELECT MIN(ts) FROM gpu_snapshots").fetchone()
        return row[0] if row and row[0] else None
    except Exception as e:
        log.warning("GPU earliest-ts read failed: %s", e)
        return None


def snapshot_count() -> int:
    try:
        with _connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM gpu_snapshots").fetchone()[0])
    except Exception as e:
        log.warning("GPU snapshot count failed: %s", e)
        return 0
