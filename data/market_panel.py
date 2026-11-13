"""Market-wide daily price panel — the cross-sectional raw material.

The problem this solves: every question of the form "who is up the most this
year / this quarter / since 2022?" needs a *panel* — one close per
(session, symbol) for a whole universe. The app had no panel. ``analysis/
sectors.py`` takes returns as arguments, and its only cross-sectional consumer
(``backend/routers/intelligence.py``) had to SAMPLE ten names per sector to keep
latency sane. A leaderboard cannot be built by sampling.

The primitive is Massive's **grouped daily** endpoint: every US-listed ticker's
OHLCV for ONE date in a single call (~11k-12.6k rows). So N sessions cost N
calls, not N x universe calls.

Measured entitlement and behaviour (probed live 2026-10-14):
  * Entitlement floor is **2021-10-15** — exactly five years. Earlier dates
    return HTTP 403 NOT_AUTHORIZED, and so does a future date. A 403 is
    therefore OUT-OF-WINDOW, never "no data".
  * A market HOLIDAY returns HTTP 200 with zero rows. An empty 200 is
    therefore genuinely a closed market, and is reported as ``closed_sessions``
    rather than silently dropped.
  * ``data/massive.get_price_history()`` is per-symbol and the plan caps it at
    1,254 daily rows — one call per symbol per period is 503 x N calls. This
    module exists to avoid that.

Storage: long-form Parquet at ``data/market_panel.parquet`` (plaintext,
GITIGNORED — licensed third-party market data, not operator balances, and
always rebuildable from the feed; precedent is ``data/sonar_cache.json``). It is
a warm cache, never a source of truth: every payload built on it carries the
panel's ``as_of`` so a stale panel greys the surface instead of quietly ranking
yesterday's tape.

Memory: the long frame is ~14.7M rows (~470 MB in RAM). Consumers that need
cross-sectional math use ``wide()``, which pivots once into session x symbol
float32 frames (~60 MB each) and caches them for the process. A 5-year backfill
never accumulates raw dicts for the whole window — it writes parts in batches
and compacts, so a crash at minute four keeps the first four minutes.

Degrade, never fabricate:
  * ``load_panel()`` returns an EMPTY frame when the file is absent, and
    ``panel_meta()`` says the panel was never built — consumers render "panel
    not built", never an empty board that reads as "nothing is up".
"""

from __future__ import annotations

import datetime
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

log = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")

# The grouped endpoint's entitlement floor, verified live on the PAID tier:
# 2021-10-14 -> 403, 2021-10-15 -> 200. Asking for anything earlier wastes a call
# and returns a 403 that must never be confused with a closed market.
#
# THIS IS A PAID-TIER FLOOR. On Stocks Basic the aggregates cap at two years,
# so a from-scratch rebuild could only reach ~two years back and every session
# before that would come back 403 and be retried forever. The stored panel is
# complete from the floor (a backup lives at
# ~/backups/market_panel_20261105.tgz) and update() skips covered sessions, so
# the normal hourly run is unaffected: it fetches only the new session.
ENTITLEMENT_FLOOR = datetime.date(2021, 10, 15)

# A session that has not settled should not be requested. The US close is
# 16:00 ET; 30 minutes of slack covers the aggregate's own publication lag.
_SETTLE = datetime.time(16, 30)

# Sessions per part file. Kept small on purpose: each batch becomes its own
# Parquet part, so peak memory is ~300k rows rather than 14.7M.
_BATCH_SESSIONS = 25

_WORKERS = 8

_COLUMNS = ("date", "symbol", "close", "volume", "dollar_volume")

# A single session that moves more than this cannot be a real return for a
# listed common stock. Measured live: BNY printed +1,262% on 2026-05-21 (a
# ticker reassignment — the pre-date prices belong to a different instrument),
# while the largest genuine one-day gap in the same universe was a biotech
# trial-result move of +177%. 400% separates the two with room to spare, and
# anything past it poisons every multi-period return that spans it.
_DISCONTINUITY_PCT = 400.0


class NotEntitled(Exception):
    """Raised when the feed refuses a date as out of the licensed window."""


# ── Paths ────────────────────────────────────────────────────────────


def _path() -> Path:
    from data.session import data_root
    return data_root() / "market_panel.parquet"


def _meta_path() -> Path:
    from data.session import data_root
    return data_root() / "market_panel.meta.json"


def _parts_dir() -> Path:
    from data.session import data_root
    return data_root() / "market_panel_parts"


def _ref_path() -> Path:
    from data.session import data_root
    return data_root() / "market_reference.json"


# ── Calendar ─────────────────────────────────────────────────────────


def last_completed_session(now: datetime.datetime | None = None) -> datetime.date:
    """The most recent US session whose aggregates should exist.

    Evaluated in America/New_York, never server-local: the box runs UTC, so
    ``date.today()`` there is already tomorrow during the US evening.
    """
    n = now.astimezone(_ET) if now is not None else datetime.datetime.now(_ET)
    d = n.date()
    if n.weekday() >= 5 or n.time() < _SETTLE:
        d -= datetime.timedelta(days=1)
    while d.weekday() >= 5:
        d -= datetime.timedelta(days=1)
    return d


def _weekdays(start: datetime.date, end: datetime.date) -> list[datetime.date]:
    """Every weekday in [start, end]. Holidays are discovered, not assumed."""
    out: list[datetime.date] = []
    d = start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += datetime.timedelta(days=1)
    return out


# ── Fetch ────────────────────────────────────────────────────────────


def fetch_session(day: datetime.date | str) -> dict[str, tuple[float, float]]:
    """One session's close+volume for the whole US market.

    Returns ``{symbol: (close, volume)}``. An EMPTY dict means the market was
    closed — the feed answered 200 with zero rows. Raises :class:`NotEntitled`
    when the feed refuses the date, which means out-of-window, NOT closed.
    """
    import requests

    from config import MASSIVE_API_KEY

    ds = day.isoformat() if isinstance(day, datetime.date) else str(day)
    resp = requests.get(
        f"https://api.massive.com/v2/aggs/grouped/locale/us/market/stocks/{ds}",
        params={"adjusted": "true", "apiKey": MASSIVE_API_KEY},
        timeout=20,
    )
    if resp.status_code in (401, 403):
        raise NotEntitled(f"{ds}: {resp.status_code} out of licensed window")
    resp.raise_for_status()
    rows = resp.json().get("results") or []

    out: dict[str, tuple[float, float]] = {}
    for r in rows:
        sym = r.get("T")
        close = r.get("c")
        vol = r.get("v")
        if not sym or close is None or vol is None:
            continue
        try:
            c = float(close)
            v = float(vol)
        except (TypeError, ValueError):
            continue
        if c <= 0:
            continue
        out[str(sym).upper()] = (c, v)
    return out


def _fetch_batch(days: list[datetime.date]) -> tuple[list[dict], list[str], list[str]]:
    """Fetch a batch in parallel.

    Returns ``(rows, closed_sessions, errors)``. A session is CLOSED when the
    feed answered 200-empty; it is an ERROR when the fetch itself failed. The
    two are different instructions to the operator and are never merged.
    """
    rows: list[dict] = []
    closed: list[str] = []
    errors: list[str] = []
    with ThreadPoolExecutor(max_workers=_WORKERS) as pool:
        futures = {pool.submit(fetch_session, d): d for d in days}
        for future, day in futures.items():
            ds = day.isoformat()
            try:
                result = future.result()
            except NotEntitled as e:
                log.warning("Market panel: %s", e)
                errors.append(ds)
                continue
            except Exception as e:
                log.warning("Market panel fetch failed for %s: %s", ds, e)
                errors.append(ds)
                continue
            if not result:
                closed.append(ds)
                continue
            stamp = pd.Timestamp(day)
            for sym, (c, v) in result.items():
                rows.append(
                    {
                        "date": stamp,
                        "symbol": sym,
                        "close": c,
                        "volume": v,
                        "dollar_volume": c * v,
                    }
                )
    return rows, closed, errors


# ── Instrument reference (names + security type) ────────────────────
#
# The grouped-daily endpoint returns tickers, never names, and it returns
# EVERYTHING listed: ETFs, warrants, units, preferreds, rights. A leaderboard
# built on it unfiltered would rank a 3x leveraged ETF against a common stock.
# Massive's ticker reference lists the whole market 1,000 at a time (measured
# 0.14s/page, ~14 pages) and carries `name` and `type` ("CS" = common stock),
# so this is one cheap daily call set that buys both a searchable name column
# and a defensible common-stock filter.

_REF_PAGE = 1000


def fetch_reference() -> dict[str, dict]:
    """Every active US-listed ticker with its name and security type.

    Returns ``{symbol: {"name", "type", "exchange"}}``. An empty dict on
    failure — the caller keeps the previous snapshot rather than blanking a
    name column that was working.
    """
    import requests

    from config import MASSIVE_API_KEY

    out: dict[str, dict] = {}
    url = "https://api.massive.com/v3/reference/tickers"
    params: dict | None = {
        "market": "stocks",
        "active": "true",
        "limit": _REF_PAGE,
        "apiKey": MASSIVE_API_KEY,
    }
    for _ in range(40):  # hard stop: 40k instruments is far past the real market
        try:
            resp = requests.get(url, params=params, timeout=25)
            resp.raise_for_status()
            payload = resp.json()
        except Exception as e:
            log.warning("Market reference fetch failed: %s", e)
            break
        for r in payload.get("results") or []:
            sym = r.get("ticker")
            if not sym:
                continue
            out[str(sym).upper()] = {
                "name": r.get("name"),
                "type": r.get("type"),
                "exchange": r.get("primary_exchange"),
            }
        next_url = payload.get("next_url")
        if not next_url:
            break
        # next_url already carries the cursor; only the key must be re-attached.
        url, params = next_url, {"apiKey": MASSIVE_API_KEY}
    return out


def refresh_reference() -> int:
    """Fetch and persist the instrument reference. Returns the count written."""
    ref = fetch_reference()
    if not ref:
        return 0
    path = _ref_path()
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({
        "fetched_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "count": len(ref),
        "instruments": ref,
    }))
    tmp.replace(path)
    return len(ref)


def load_reference() -> dict[str, dict]:
    """Instrument reference snapshot. Empty dict when never fetched.

    Consumers must treat a missing name as UNKNOWN and render the symbol —
    never invent a name from the ticker.
    """
    path = _ref_path()
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        log.warning("Market reference read failed: %s", e)
        return {}
    instruments = payload.get("instruments")
    return instruments if isinstance(instruments, dict) else {}


def reference_meta() -> dict:
    path = _ref_path()
    if not path.exists():
        return {"available": False, "note": "Instrument reference never fetched."}
    try:
        payload = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        return {"available": False, "note": f"Instrument reference unreadable: {e}"}
    return {
        "available": True,
        "count": payload.get("count", 0),
        "fetched_at": payload.get("fetched_at"),
    }


def _tidy(df: pd.DataFrame) -> pd.DataFrame:
    """Normalize dtypes and drop unusable rows. Never reindex or interpolate."""
    if df.empty:
        return pd.DataFrame(columns=list(_COLUMNS))
    out = df.copy()
    out["date"] = pd.to_datetime(out["date"]).dt.normalize()
    out["symbol"] = out["symbol"].astype(str).str.upper()
    out["close"] = pd.to_numeric(out["close"], errors="coerce").astype("float32")
    out["volume"] = pd.to_numeric(out["volume"], errors="coerce")
    out["dollar_volume"] = pd.to_numeric(out["dollar_volume"], errors="coerce")
    out = out.dropna(subset=["close", "volume"])
    out = out[out["close"] > 0]
    out = out.drop_duplicates(subset=["date", "symbol"], keep="last")
    return out.reset_index(drop=True)


def _to_frame(rows: list[dict]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=list(_COLUMNS))
    return _tidy(pd.DataFrame(rows, columns=list(_COLUMNS)))


# ── Parts + compaction ───────────────────────────────────────────────


def _write_part(df: pd.DataFrame, index: int) -> Path:
    parts = _parts_dir()
    parts.mkdir(parents=True, exist_ok=True)
    path = parts / f"part-{index:05d}.parquet"
    tmp = path.with_suffix(".parquet.tmp")
    df.to_parquet(tmp, index=False, compression="zstd")
    tmp.replace(path)
    return path


def _load_parts() -> pd.DataFrame:
    parts = _parts_dir()
    if not parts.exists():
        return pd.DataFrame(columns=list(_COLUMNS))
    files = sorted(parts.glob("part-*.parquet"))
    if not files:
        return pd.DataFrame(columns=list(_COLUMNS))
    frames = []
    for f in files:
        try:
            frames.append(pd.read_parquet(f))
        except Exception as e:
            log.warning("Market panel: unreadable part %s (%s) — skipped", f.name, e)
    if not frames:
        return pd.DataFrame(columns=list(_COLUMNS))
    return _tidy(pd.concat(frames, ignore_index=True))


def _covered_sessions() -> set[str]:
    """Sessions already resolved, across the panel, open parts, AND closed dates.

    A holiday returns 200-with-zero-rows, so it never lands in the panel — but it
    IS resolved. Leaving it out means re-asking the feed for all ~50 market
    holidays on every single update, and the run can never report "already
    current".
    """
    seen: set[str] = set(_read_meta().get("closed_sessions") or [])
    panel = load_panel(columns=["date"])
    if not panel.empty:
        seen |= {d.date().isoformat() for d in panel["date"].unique()}
    parts = _parts_dir()
    if parts.exists():
        for f in parts.glob("part-*.parquet"):
            try:
                d = pd.read_parquet(f, columns=["date"])
                seen |= {x.date().isoformat() for x in d["date"].unique()}
            except Exception as e:
                log.warning("Market panel: cannot read covered dates from %s (%s)", f.name, e)
    return seen


def refresh_meta() -> dict:
    """Rewrite the coverage sidecar so it MATCHES the panel on disk.

    This is a reconciliation, not just a field patch: sessions, rows, symbols,
    the first and last session, and the error list are all recomputed from the
    file itself. A sidecar that disagrees with the data it describes is worse
    than a missing one — it reports a defect the panel has already healed.

    Only the columns the scan needs are read, so adding a coverage field never
    requires a five-minute refetch.
    """
    if not _path().exists():
        return panel_meta()
    df = load_panel(columns=["date", "symbol", "close"])
    if df.empty:
        return panel_meta()
    prior = _read_meta()
    closed_all = sorted(set(prior.get("closed_sessions") or []))
    dates = {pd.Timestamp(d).date().isoformat() for d in df["date"].unique()}
    resolved = dates | set(closed_all)
    meta = {
        **prior,
        "as_of": max(dates) if dates else None,
        "first_session": min(dates) if dates else None,
        "sessions": len(dates),
        "symbols": int(df["symbol"].nunique()),
        "rows": int(len(df)),
        "closed_sessions": closed_all,
        "errors": sorted({e for e in (prior.get("errors") or []) if e not in resolved}),
        "discontinuities": scan_discontinuities(df),
        "meta_refreshed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    tmp = _meta_path().with_suffix(".json.tmp")
    tmp.write_text(json.dumps(meta, indent=1))
    tmp.replace(_meta_path())
    return meta


def compact() -> dict:
    """Merge all open parts into the single panel, then remove the parts.

    The panel is the read path; parts exist only so an interrupted backfill
    keeps its work. Compaction is idempotent.
    """
    parts = _load_parts()
    existing = load_panel()
    frames = [f for f in (existing, parts) if not f.empty]
    if not frames:
        return panel_meta()
    merged = _tidy(pd.concat(frames, ignore_index=True))
    merged = merged.drop_duplicates(subset=["date", "symbol"], keep="last")
    merged = merged.sort_values(["date", "symbol"]).reset_index(drop=True)

    path = _path()
    tmp = path.with_suffix(".parquet.tmp")
    merged.to_parquet(tmp, index=False, compression="zstd")
    tmp.replace(path)

    # Only now is it safe to drop the parts.
    pdir = _parts_dir()
    if pdir.exists():
        for f in pdir.glob("part-*.parquet"):
            try:
                f.unlink()
            except OSError as e:
                log.warning("Market panel: could not remove part %s (%s)", f.name, e)

    meta = _write_meta(merged, [], [])
    return meta


def scan_discontinuities(df: pd.DataFrame, threshold: float = _DISCONTINUITY_PCT) -> dict:
    """Find symbols whose price series contains a move no real session produced.

    A ticker reassignment, an unadjusted split, or a bad adjustment shows up as
    one enormous single-session return, and it corrupts EVERY period return
    spanning it. This is a property of the PANEL, not of a request, so it is
    computed once at build and stored in the coverage sidecar.

    Returns ``{symbol: {"date", "move_pct", "from", "to"}}``. Detection only —
    the feed's history is never silently rewritten.
    """
    if df.empty or len(df) < 2:
        return {}
    # Normalize here rather than trusting the caller: this runs on a frame that
    # may come straight from a fixture or a CSV (string dates) as well as from
    # the compacted panel (timestamps).
    work = df[["date", "symbol", "close"]].copy()
    work["date"] = pd.to_datetime(work["date"])
    work = work.sort_values(["symbol", "date"])
    work = work.assign(move=work.groupby("symbol")["close"].pct_change() * 100.0)
    work = work.assign(prev=work.groupby("symbol")["close"].shift())
    hits = work[work["move"].abs() > threshold]
    out: dict[str, dict] = {}
    for _, r in hits.iterrows():
        sym = str(r["symbol"])
        prior = out.get(sym)
        # Keep the LARGEST move per symbol — that is the one that matters.
        if prior is None or abs(float(r["move"])) > abs(float(prior["move_pct"])):
            out[sym] = {
                "date": pd.Timestamp(r["date"]).date().isoformat(),
                "move_pct": round(float(r["move"]), 1),
                "from": round(float(r["prev"]), 4) if pd.notna(r["prev"]) else None,
                "to": round(float(r["close"]), 4),
            }
    return out


def discontinuities() -> dict:
    """The discontinuity map from the last build. Empty when none/never built."""
    return dict(_read_meta().get("discontinuities") or {})


def _write_meta(df: pd.DataFrame, closed: list[str], errors: list[str]) -> dict:
    prior = _read_meta()
    closed_all = sorted(set(prior.get("closed_sessions") or []) | set(closed))
    errors_all = sorted(set(prior.get("errors") or []) | set(errors))
    meta = {
        "as_of": df["date"].max().date().isoformat() if not df.empty else None,
        "first_session": df["date"].min().date().isoformat() if not df.empty else None,
        "sessions": int(df["date"].nunique()) if not df.empty else 0,
        "symbols": int(df["symbol"].nunique()) if not df.empty else 0,
        "rows": int(len(df)),
        "entitlement_floor": ENTITLEMENT_FLOOR.isoformat(),
        "discontinuities": scan_discontinuities(df),
        "closed_sessions": closed_all,
        "errors": errors_all,
        "built_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "source": "Massive grouped daily (v2/aggs/grouped/locale/us/market/stocks)",
    }
    mtmp = _meta_path().with_suffix(".json.tmp")
    mtmp.write_text(json.dumps(meta, indent=1))
    mtmp.replace(_meta_path())
    return meta


def _read_meta() -> dict:
    p = _meta_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


# ── Read ─────────────────────────────────────────────────────────────


def load_panel(
    start: datetime.date | str | None = None,
    end: datetime.date | str | None = None,
    dates: list | None = None,
    columns: list[str] | None = None,
) -> pd.DataFrame:
    """Read the compacted panel, filtered at the Parquet layer.

    The full panel is ~1,300 sessions x ~13,400 symbols (~16M rows, ~450 MB in
    RAM). Nothing should ever need all of it: every cross-sectional question
    reads a handful of ANCHOR dates or one trailing window. The panel is
    written sorted by date, so pyarrow's row-group pruning means a filtered read
    touches only the matching row groups.

    Returns an EMPTY frame when the panel is absent — callers must render
    "panel not built" rather than an empty board that reads as "nothing is up".
    """
    path = _path()
    if not path.exists():
        return pd.DataFrame(columns=list(columns or _COLUMNS))
    filters = []
    if dates is not None:
        stamps = [pd.Timestamp(d) for d in dates]
        filters.append(("date", "in", stamps))
    else:
        if start is not None:
            filters.append(("date", ">=", pd.Timestamp(start)))
        if end is not None:
            filters.append(("date", "<=", pd.Timestamp(end)))
    try:
        df = pd.read_parquet(path, columns=columns, filters=filters or None)
    except Exception as e:
        log.warning("Market panel read failed: %s", e)
        return pd.DataFrame(columns=list(columns or _COLUMNS))
    return df


def frames(
    start: datetime.date | str | None = None,
    end: datetime.date | str | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Pivot a WINDOW of the panel into (close, volume) session x symbol frames.

    Scoped to a window on purpose. Called with the last ~380 sessions this is
    ~14 MB per frame; called with the whole panel it is ~210 MB and a slow
    pivot. Callers should never want the whole panel.
    """
    panel = load_panel(start=start, end=end, columns=["date", "symbol", "close", "volume"])
    if panel.empty:
        empty: pd.DataFrame = pd.DataFrame()
        return empty, empty
    close = panel.pivot(index="date", columns="symbol", values="close").astype("float32")
    volume = panel.pivot(index="date", columns="symbol", values="volume").astype("float64")
    close.index.name = "date"
    volume.index.name = "date"
    return close, volume


def session_index() -> pd.DatetimeIndex:
    """Every session the panel holds, ascending. The app's own trading calendar."""
    panel = load_panel(columns=["date"])
    if panel.empty:
        return pd.DatetimeIndex([])
    return pd.DatetimeIndex(sorted(panel["date"].unique()))


# ── Build ─────────────────────────────────────────────────────────────


def update(target: datetime.date | None = None, compact_when_done: bool = True) -> dict:
    """Extend the panel through the latest completed session.

    Resumable: sessions already covered by the compacted panel or by an open
    part are never refetched, so an interrupted backfill continues where it
    stopped. Returns the coverage metadata.
    """
    end = target or last_completed_session()
    if end < ENTITLEMENT_FLOOR:
        return {
            "updated": False,
            **_read_meta(),
            "note": f"{end.isoformat()} predates the entitlement floor.",
        }

    wanted = _weekdays(ENTITLEMENT_FLOOR, end)
    covered = _covered_sessions()
    todo = [d for d in wanted if d.isoformat() not in covered]

    if not todo:
        if compact_when_done and _parts_dir().exists():
            return compact()
        meta = panel_meta()
        meta["updated"] = False
        meta["note"] = "Panel already current through the latest completed session."
        return meta

    log.info("Market panel: %d session(s) to fetch (%d already held)", len(todo), len(covered))
    closed: list[str] = []
    errors: list[str] = []
    part_index = len(list(_parts_dir().glob("part-*.parquet"))) if _parts_dir().exists() else 0

    for i in range(0, len(todo), _BATCH_SESSIONS):
        batch = todo[i : i + _BATCH_SESSIONS]
        rows, closed_b, errors_b = _fetch_batch(batch)
        closed.extend(closed_b)
        errors.extend(errors_b)
        if rows:
            _write_part(_to_frame(rows), part_index)
            part_index += 1
        log.info(
            "Market panel: %d/%d sessions done (closed=%d errors=%d)",
            min(i + _BATCH_SESSIONS, len(todo)), len(todo), len(closed), len(errors),
        )

    if compact_when_done:
        meta = compact()
    else:
        meta = _read_meta()
    closed_all = sorted(set(list(meta.get("closed_sessions") or []) + closed))
    # A session that failed once and succeeded on a later run is FIXED, not
    # still broken. Union the old errors in, then drop every date that is now
    # present in the panel or recorded as closed — otherwise the panel
    # advertises a permanent defect it has already healed.
    resolved = _covered_sessions() | set(closed_all)
    errors_all = sorted(
        {e for e in list(meta.get("errors") or []) + errors if e not in resolved}
    )
    meta = {
        "updated": True,
        **meta,
        "as_of": meta.get("as_of"),
        "closed_sessions": closed_all,
        "errors": errors_all,
    }
    path = _meta_path()
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(meta, indent=1))
    tmp.replace(path)

    # Names/security types ride along: ~14 calls, and the board's search box
    # and its common-stock filter both depend on them.
    try:
        written = refresh_reference()
        if written:
            meta["reference_count"] = written
    except Exception as e:
        log.warning("Market reference refresh skipped: %s", e)
    return meta


def panel_meta() -> dict:
    """Coverage sidecar, or an explicit unbuilt state. Never a silent default."""
    if not _path().exists() and not _parts_dir().exists():
        return {
            "available": False,
            "note": (
                "Market panel has never been built. Run "
                "`venv/bin/python -m data.market_panel` on the server "
                "(~5 min for the full five-year window)."
            ),
        }
    meta = _read_meta()
    open_parts = 0
    pdir = _parts_dir()
    if pdir.exists():
        open_parts = len(list(pdir.glob("part-*.parquet")))

    expected = last_completed_session()
    closed = set(meta.get("closed_sessions") or [])
    errors = set(meta.get("errors") or [])
    as_of = meta.get("as_of")

    degraded = False
    notes: list[str] = []
    if open_parts:
        degraded = True
        notes.append(
            f"{open_parts} uncompacted part(s) — a backfill did not finish; "
            "the panel is still usable and will complete on the next run."
        )
    if as_of and as_of < expected.isoformat() and expected.isoformat() not in closed:
        degraded = True
        notes.append(
            f"Newest session is {as_of} but {expected.isoformat()} should exist; "
            "the board is behind the tape."
        )
    if errors:
        degraded = True
        notes.append(f"{len(errors)} session(s) failed to fetch: {', '.join(sorted(errors)[:5])}.")

    return {
        "available": True,
        "degraded": degraded,
        "note": " ".join(notes) if notes else "Panel covers the full window.",
        "expected_session": expected.isoformat(),
        "open_parts": open_parts,
        **meta,
    }


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Build/refresh the market panel.")
    parser.add_argument("--through", help="Last session to fetch (YYYY-MM-DD).")
    parser.add_argument("--no-compact", action="store_true", help="Leave parts open.")
    parser.add_argument("--status", action="store_true", help="Print coverage and exit.")
    parser.add_argument(
        "--refresh-meta",
        action="store_true",
        help="Recompute the coverage sidecar (incl. the discontinuity scan) without refetching.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.status:
        print(json.dumps(panel_meta(), indent=1, default=str))
        return 0
    if args.refresh_meta:
        meta = refresh_meta()
        print(json.dumps({k: v for k, v in meta.items() if k != "discontinuities"}, indent=1, default=str))
        print(f"discontinuities: {len(meta.get('discontinuities') or {})}")
        return 0
    target = datetime.date.fromisoformat(args.through) if args.through else None
    meta = update(target=target, compact_when_done=not args.no_compact)
    print(json.dumps(meta, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
