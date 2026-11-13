"""Leaderboard — who is winning, and where the dollars are going.

The page's question: **"who is winning, over which window, and is capital
arriving or leaving?"** Flows answers the same question at SECTOR level from a
sampled universe; this answers it at SYMBOL level from the whole panel and adds
the multi-window comparison the operator asked for.

Reads ``data/market_panel`` (the market-wide daily panel) and computes through
``analysis/leaderboard`` (pure). Nothing here fetches per symbol, so a 503-name
board costs two filtered Parquet reads rather than 503 network calls.

The assembled board is TTL-cached for 15 minutes at the composition layer: the
panel only changes once a day, so recomputing it per request would trade idle
CPU for nothing, while a shorter TTL would still be stale in exactly the same
way. Every response carries the panel's ``as_of`` so a stale panel greys the
surface instead of quietly ranking yesterday's tape.
"""

import logging

import numpy as np
import pandas as pd
from fastapi import APIRouter, Depends, Query

from backend.auth import require_auth
from data.cache import ttl_cache

log = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/leaderboard", tags=["leaderboard"], dependencies=[Depends(require_auth)]
)

# The board changes once a day (one new session); 15 minutes keeps a page
# reload instant without ever serving a different session than a cold compute.
_BOARD_TTL = 900

# Trailing window read. It must CONTAIN the longest window any measure slices:
# the 252-session flow baseline / drawdown window, and ~273 sessions for thirteen
# month-ends of consistency history. Sized to the requirement, not to a round
# number — and every consumer below slices its OWN window off it, so this number
# can change without moving a published figure.
_WINDOW_SESSIONS = 300

# "All US" without a liquidity floor ranks thousands of OTC shells. $1M of
# average daily dollar volume is the floor, and the response publishes it.
_DEFAULT_MIN_DOLLAR_VOLUME = 1_000_000.0

UNIVERSES: tuple[str, ...] = ("S&P 500", "Nasdaq 100", "Dow 30", "All US")


def _universe(universe: str) -> tuple[list[str] | None, dict[str, str], dict]:
    """Resolve a universe to (symbols | None for all, sector map, coverage block).

    A universe whose upstream list fails resolves to an EMPTY list with an
    explicit reason — never to "everything", which would silently widen the
    board to the whole market and call it the S&P 500.
    """
    if universe == "All US":
        return None, {}, {"requested": None, "resolved": None, "degraded": False,
                          "note": "Whole panel, filtered to common stock with a liquidity floor."}
    try:
        from data.universe import UNIVERSE_SECTORS
        from data.universe import UNIVERSES as LOADERS
    except Exception as e:  # pragma: no cover - import guard
        return [], {}, {"requested": None, "resolved": 0, "degraded": True,
                        "note": f"Universe registry unavailable: {e}"}
    loader = LOADERS.get(universe)
    if loader is None:
        return [], {}, {"requested": None, "resolved": 0, "degraded": True,
                        "note": f"Unknown universe {universe!r}. Known: {', '.join(UNIVERSES)}."}
    try:
        symbols = loader()
    except Exception as e:
        return [], {}, {"requested": None, "resolved": 0, "degraded": True,
                        "note": f"Universe {universe!r} failed to load from its upstream list: {e}"}
    if not symbols or len(symbols) < 20:
        return [], {}, {"requested": None, "resolved": len(symbols or []), "degraded": True,
                        "note": (f"Universe {universe!r} resolved to {len(symbols or [])} symbols — "
                                 "the upstream constituent list is unavailable; not ranking a partial board.")}
    # The sector map is ENRICHMENT, read in its OWN try: a dead sector source
    # degrades the Sector column, never the universe — one shared try-block
    # meant either failing blanked the whole board.
    sectors: dict[str, str] = {}
    sector_note = ""
    try:
        sectors = dict(UNIVERSE_SECTORS.get(universe, lambda: {})() or {})
    except Exception as e:
        log.warning("Universe %s sector map unavailable: %s", universe, e)
        sector_note = " Sector map unavailable — rows carry no sector."
    if sectors and len(sectors) < len(symbols):
        sector_note = f" Sectors for {len(sectors)} of {len(symbols)} names; the rest render blank."
    return list(symbols), sectors, {
        "requested": None, "resolved": len(symbols), "degraded": False,
        "note": "Constituent list loaded from the published index table." + sector_note,
    }


def _needed_dates(sessions: pd.DatetimeIndex) -> list[pd.Timestamp]:
    """Session dates worth reading for the multi-window return block.

    One read covers every period anchor, every calendar-year base and end, and
    the monthly closes the consistency measure needs. ~35 dates.
    """
    from analysis import leaderboard as lb

    dates: set[pd.Timestamp] = {sessions[-1]}
    anchors = lb.session_anchors(sessions)
    for a in anchors.values():
        if a["available"] and a["base_session"]:
            dates.add(pd.Timestamp(a["base_session"]))

    for year in sorted({d.year for d in sessions}):
        in_year = sessions[sessions.year == year]
        if len(in_year) == 0:
            continue
        prior = sessions[sessions < pd.Timestamp(year=year, month=1, day=1)]
        if len(prior):
            dates.add(prior[-1])
        dates.add(in_year[-1])
    return sorted(dates)


def _window_start(sessions: pd.DatetimeIndex) -> pd.Timestamp:
    return sessions[max(0, len(sessions) - _WINDOW_SESSIONS)]


def _assemble(universe: str, period: str, min_dollar_volume: float | None) -> dict:
    """Compose the whole board once. Cached by the caller."""
    from data import market_panel

    try:
        from analysis import leaderboard as lb
    except Exception as e:  # pragma: no cover
        return {"available": False, "note": f"Analytics layer unavailable: {e}"}

    meta = market_panel.panel_meta()
    if not meta.get("available"):
        return {
            "available": False,
            "note": meta.get("note", "Market panel is not built."),
            "universe": universe, "period": period,
        }

    sessions = market_panel.session_index()
    if len(sessions) < lb.MIN_SESSIONS_FOR_RANK:
        return {
            "available": False, "universe": universe, "period": period,
            "note": f"Panel holds {len(sessions)} sessions — not enough to rank.",
        }

    anchors = lb.session_anchors(sessions)
    if period not in anchors or not anchors[period]["available"]:
        return {
            "available": False, "universe": universe, "period": period,
            "anchors": anchors,
            "note": (anchors.get(period, {}).get("note")
                     or f"Period {period!r} is not measurable on this panel."),
        }

    # ── Window read: flow, drawdown, monthly consistency ────────────
    win_start = _window_start(sessions)
    window = market_panel.load_panel(
        start=win_start, columns=["date", "symbol", "close", "volume"]
    )
    if window.empty:
        return {
            "available": False, "universe": universe, "period": period,
            "note": "Panel window read returned no rows.",
        }
    window = window.replace([np.inf, -np.inf], np.nan)
    window["dollar_volume"] = window["close"] * window["volume"]

    # Every measure slices its OWN trailing window off the read. Averaging over
    # "whatever the window read happened to hold" made the definition an
    # implementation detail: shrinking the read moved the published numbers.
    baseline_window = window[window["date"] >= sessions[-lb.FLOW_BASELINE_SESSIONS]]
    recent_window = window[window["date"] >= sessions[-21]]

    dv_recent = recent_window.groupby("symbol")["dollar_volume"].mean()
    dv_base = baseline_window.groupby("symbol")["dollar_volume"].mean()
    flow = pd.DataFrame({"dollar_volume_avg": dv_recent})
    flow["dollar_volume_baseline"] = dv_base
    flow["flow_ratio"] = dv_recent / dv_base.replace(0, np.nan)

    hi = baseline_window.groupby("symbol")["close"].max()
    last_close_w = window.sort_values("date").groupby("symbol")["close"].last()
    drawdown = (last_close_w / hi.replace(0, np.nan) - 1.0) * 100.0

    # Reuse the pure month-end measure rather than re-deriving it here — one
    # definition of "consistency", testable without a panel.
    window_close = window.pivot(index="date", columns="symbol", values="close")
    consistency = lb.monthly_returns(window_close, months=12)

    stats = pd.DataFrame({
        "flow_ratio": flow["flow_ratio"],
        "dollar_volume_avg": flow["dollar_volume_avg"],
        "drawdown_pct": drawdown,
        "consistency": consistency,
    })

    # ── Anchor read: every period and calendar-year return ──────────
    dates = _needed_dates(sessions)
    anchor_rows = market_panel.load_panel(dates=dates, columns=["date", "symbol", "close"])
    if anchor_rows.empty:
        return {
            "available": False, "universe": universe, "period": period,
            "note": "Panel anchor read returned no rows.",
        }
    closes = anchor_rows.pivot(index="date", columns="symbol", values="close")

    # The anchor frame holds ~35 dates; the calendar holds 1,251. Anchors must
    # be resolved against the CALENDAR or "21 sessions" silently means "21
    # anchor dates".
    returns = lb.period_returns(closes, sessions=sessions)
    years = lb.calendar_year_returns(closes, sessions=sessions)

    # ── Universe + reference join ───────────────────────────────────
    symbols, sector_map, coverage = _universe(universe)
    reference = {}
    try:
        reference = market_panel.load_reference()
    except Exception as e:
        log.warning("Reference load failed: %s", e)

    candidates = closes.columns
    if symbols is not None:
        wanted = {s.upper() for s in symbols}
        candidates = candidates[candidates.isin(wanted)]
    else:
        common = {s for s, r in reference.items() if (r or {}).get("type") == "CS"}
        if common:
            candidates = candidates[candidates.isin(common)]
        else:
            coverage["degraded"] = True
            coverage["note"] = (
                "Instrument reference unavailable, so the whole panel was ranked "
                "without the common-stock filter — ETFs and warrants may appear."
            )

    if len(candidates) == 0:
        return {
            "available": False, "universe": universe, "period": period,
            "as_of": meta.get("as_of"), "coverage": coverage,
            "note": "No panel symbols matched this universe.",
        }

    rows = _build_rows(candidates, closes, returns, years, stats, sector_map, reference)

    # "All US" defaults the floor; an explicit value always wins.
    default_floor = min_dollar_volume is None and universe == "All US"
    floor = _DEFAULT_MIN_DOLLAR_VOLUME if default_floor else (min_dollar_volume or 0.0)
    before = len(rows)
    if floor > 0:
        rows = rows[rows["dollar_volume_avg"].fillna(0.0) >= floor]
    illiquid_dropped = before - len(rows)

    period_ret = returns.loc[period] if period in returns.index else pd.Series(dtype="float64")
    rows["ret_period"] = rows["symbol"].map(period_ret)
    bench = _benchmark_return(period, anchors)

    rows["rs_period"] = (
        rows["ret_period"] - bench if bench is not None else np.nan
    )
    rows = rows[rows["ret_period"].notna()]

    if rows.empty:
        return {
            "available": True, "universe": universe, "period": period,
            "as_of": meta.get("as_of"), "coverage": coverage, "rows": [],
            "count": 0, "returned": 0, "breadth": lb.breadth(pd.DataFrame()),
            "note": "No symbol in this universe has a usable close at both ends of the period.",
        }

    rows = rows.set_index("symbol")

    # A symbol whose series contains an impossible single-session move cannot
    # be ranked: every period spanning that date is fiction. Marked here,
    # filtered at the endpoint so the cache stays single-purpose.
    flags = {}
    try:
        flags = market_panel.discontinuities()
    except Exception as e:
        log.warning("Discontinuity map unavailable: %s", e)
    rows["suspect"] = [str(s) in flags for s in rows.index]
    rows["suspect_note"] = [_suspect_note(flags.get(str(s))) for s in rows.index]

    scored = lb.score_board(rows, period)
    scored = scored.sort_values("score", ascending=False).reset_index()

    return {
        "available": True,
        "universe": universe,
        "period": period,
        "period_label": lb.PERIODS.get(period, {}).get("label", period),
        "basis": anchors[period]["basis"],
        "base_session": anchors[period]["base_session"],
        "as_of": meta.get("as_of"),
        "expected_session": meta.get("expected_session"),
        "degraded": bool(meta.get("degraded") or coverage.get("degraded")),
        "note": meta.get("note") if meta.get("degraded") else coverage.get("note"),
        "anchors": anchors,
        "benchmark": bench,
        "coverage": {**coverage, "panel_matched": int(len(candidates)),
                     "illiquid_dropped": int(illiquid_dropped),
                     "liquidity_floor": floor},
        "breadth": lb.breadth(rows[["ret_period"]]),
        "weights": lb.SCORE_WEIGHTS,
        "_frame": scored,
    }


def _suspect_note(flag: dict | None) -> str | None:
    """Name the defect and its date. A flagged row is never silently dropped."""
    if not flag:
        return None
    return (
        f"price series contains an impossible {flag.get('move_pct')}% move on "
        f"{flag.get('date')} ({flag.get('from')} -> {flag.get('to')}) — a ticker "
        "reassignment or unadjusted split, not a return"
    )


def _benchmark_return(period: str, anchors: dict) -> float | None:
    """SPY's return over the same window — the relative-strength reference.

    Returns None when SPY is not in the panel, and the board then reports
    relative strength as unavailable rather than benchmarked against nothing.
    """
    from data import market_panel

    a = anchors.get(period) or {}
    if not a.get("available"):
        return None
    dates = [pd.Timestamp(a["base_session"])] if a.get("base_session") else []
    sessions = market_panel.session_index()
    if len(sessions) == 0:
        return None
    dates.append(sessions[-1])
    sub = market_panel.load_panel(dates=dates, columns=["date", "symbol", "close"])
    if sub.empty:
        return None
    sub = sub[sub["symbol"] == "SPY"]
    if sub["date"].nunique() < 2:
        return None
    sub = sub.sort_values("date")
    base = float(sub["close"].iloc[0])
    end = float(sub["close"].iloc[-1])
    if base <= 0:
        return None
    return (end / base - 1.0) * 100.0


def _build_rows(
    candidates: pd.Index,
    closes: pd.DataFrame,
    returns: pd.DataFrame,
    years: pd.DataFrame,
    stats: pd.DataFrame,
    sector_map: dict[str, str],
    reference: dict[str, dict],
) -> pd.DataFrame:
    """One row per candidate symbol, carrying every period and year return."""
    syms = pd.Index(candidates)
    out = pd.DataFrame(index=syms)
    out.index.name = "symbol"
    out["close"] = closes.iloc[-1].reindex(syms)

    for name in returns.index:
        out[f"ret_{name}"] = returns.loc[name].reindex(syms)
    for year in years.index:
        out[f"year_{year}"] = years.loc[year].reindex(syms)

    for col in ("flow_ratio", "dollar_volume_avg", "drawdown_pct", "consistency"):
        out[col] = stats[col].reindex(syms) if col in stats else np.nan

    out["sector"] = [sector_map.get(str(s).upper()) for s in syms]
    out["name"] = [
        ((reference.get(str(s).upper()) or {}).get("name")) for s in syms
    ]
    return out.reset_index()


def _row_payload(frame: pd.DataFrame, extension: dict[str, float | None]) -> list[dict]:
    """Serializable rows. NaN becomes None so JSON never carries a bare NaN."""
    # `ret_period` is already a top-level field; emitting it again as
    # returns["period"] would give the same number two names.
    period_cols = [c for c in frame.columns if c.startswith("ret_") and c != "ret_period"]
    year_cols = [c for c in frame.columns if c.startswith("year_")]
    rows: list[dict] = []
    for _, r in frame.iterrows():
        rows.append({
            "symbol": r["symbol"],
            "name": None if pd.isna(r.get("name")) else r.get("name"),
            "sector": None if pd.isna(r.get("sector")) else r.get("sector"),
            "close": _num(r.get("close")),
            "score": _num(r.get("score")),
            "rank": int(r["rank"]) if "rank" in r and not pd.isna(r.get("rank")) else None,
            "signal": r.get("signal"),
            "signal_note": r.get("signal_note"),
            "reasons": list(r.get("reasons") or []),
            "ret_period": _num(r.get("ret_period")),
            "rs_period": _num(r.get("rs_period")),
            "flow_ratio": _num(r.get("flow_ratio")),
            "dollar_volume_avg": _num(r.get("dollar_volume_avg")),
            "consistency": _num(r.get("consistency")),
            "drawdown_pct": _num(r.get("drawdown_pct")),
            "extension_z": extension.get(str(r["symbol"])),
            "returns": {c[4:]: _num(r.get(c)) for c in period_cols},
            "years": {c[5:]: _num(r.get(c)) for c in year_cols},
        })
    return rows


def _num(v):
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if not np.isfinite(f) else round(f, 4)


# The TTL wrapper is built ONCE at import. Building it inside the endpoint
# returned a brand-new wrapper per request, so the cache never retained
# anything and "cached for 15 minutes" was a comment that did nothing.
# copy=False: the board payload is large and every endpoint treats it as
# read-only (shallow-copying the outer dict before adding keys). The old
# deep copy cost ~700ms per request on hits — more than the compute saved.
assemble_cached = ttl_cache(ttl_seconds=_BOARD_TTL, copy=False)(_assemble)


def _extension_for(symbols: list[str], sessions: pd.DatetimeIndex) -> dict[str, float | None]:
    """Overstretch for the rows actually being returned.

    Computed AFTER ranking and only for the page's symbols: the measure needs a
    126-session moving average per name, which is cheap for 100 names and
    wasteful for 13,000 when the ranking does not use it.
    """
    if not symbols:
        return {}
    from analysis import leaderboard as lb
    from data import market_panel

    start = sessions[max(0, len(sessions) - lb.EXTENSION_MA_SESSIONS - 260)]
    panel = market_panel.load_panel(start=start, columns=["date", "symbol", "close"])
    if panel.empty:
        return {}
    sub = panel[panel["symbol"].isin(set(symbols))]
    if sub.empty:
        return {}
    closes = sub.pivot(index="date", columns="symbol", values="close")
    z = lb.extension_z(closes)
    return {str(k): _num(v) for k, v in z.items() if _num(v) is not None}


@router.get("/meta")
def leaderboard_meta():
    """Panel coverage, available periods and universes. Never a bare default."""
    return _cached_leaderboard_meta()


def _leaderboard_meta_compute():
    from analysis import leaderboard as lb
    from data import market_panel

    meta = market_panel.panel_meta()
    sessions = market_panel.session_index()
    anchors = lb.session_anchors(sessions) if len(sessions) else {}
    return {
        "available": bool(meta.get("available")),
        "panel": meta,
        "reference": market_panel.reference_meta(),
        "sessions": int(len(sessions)),
        "universes": list(UNIVERSES),
        "periods": [
            {
                "id": pid,
                "label": lb.PERIODS[pid]["label"],
                "basis": spec.get("basis"),
                "base_session": spec.get("base_session"),
                "available": spec.get("available"),
                "note": spec.get("note"),
            }
            for pid, spec in anchors.items()
        ],
        "weights": lb.SCORE_WEIGHTS,
        "signals": lb.SIGNALS,
        "liquidity_floor": _DEFAULT_MIN_DOLLAR_VOLUME,
    }


# TTL-cached: universe metadata (deterministic).
_cached_leaderboard_meta = ttl_cache(ttl_seconds=600)(_leaderboard_meta_compute)


@router.get("/board")
def leaderboard_board(
    universe: str = Query("S&P 500"),
    period: str = Query("YTD"),
    limit: int = Query(default=100, ge=1, le=1000),
    q: str = Query(""),
    sector: str = Query(""),
    signal: str = Query(""),
    sort: str = Query("score"),
    asc: bool = Query(False),
    include_suspect: bool = Query(False),
):
    """The ranked board. One row per symbol, every window on the row."""
    return _cached_board_response(universe, period, limit, q, sector, signal, sort, asc, include_suspect)


def _board_response_compute(
    universe: str = Query("S&P 500"),
    period: str = Query("YTD"),
    limit: int = Query(default=100, ge=1, le=1000),
    q: str = Query(""),
    sector: str = Query(""),
    signal: str = Query(""),
    sort: str = Query("score"),
    asc: bool = Query(False),
    include_suspect: bool = Query(False),
):
    payload = assemble_cached(universe, period, None)
    payload = {**payload}  # shallow: never mutate the cached dict
    if not payload.get("available"):
        return payload

    frame = payload.pop("_frame")
    frame = frame.copy()

    # Set aside rows whose own price history cannot produce a real return, and
    # SAY so with the count and the reason — a filter that acts silently would
    # just look like the name was never in the index.
    excluded_suspect: list[dict] = []
    if "suspect" in frame.columns:
        bad = frame[frame["suspect"].fillna(False)]
        excluded_suspect = [
            {"symbol": str(r["symbol"]), "note": r.get("suspect_note")}
            for _, r in bad.iterrows()
        ]
        if not include_suspect:
            frame = frame[~frame["suspect"].fillna(False)]
    frame = frame.drop(columns=[c for c in ("suspect", "suspect_note") if c in frame.columns])

    frame["rank"] = np.arange(1, len(frame) + 1)

    query = q.strip().upper()
    if query:
        name_series = frame["name"].fillna("").astype(str).str.upper()
        sector_series = frame["sector"].fillna("").astype(str).str.upper()
        sym_series = frame["symbol"].astype(str).str.upper()
        frame = frame[
            sym_series.str.contains(query, regex=False)
            | name_series.str.contains(query, regex=False)
            | sector_series.str.contains(query, regex=False)
        ]
    if sector.strip():
        frame = frame[frame["sector"].fillna("").str.lower() == sector.strip().lower()]
    if signal.strip():
        frame = frame[frame["signal"].fillna("") == signal.strip().upper()]

    total_matched = int(len(frame))

    if sort in frame.columns:
        frame = frame.sort_values(sort, ascending=asc, na_position="last")
    else:
        payload["sort_note"] = (
            f"{sort!r} is not a sortable column; kept the score order. "
            f"Sortable: {', '.join(sorted(frame.columns))}"
        )

    page = frame.head(limit)
    ordered = [str(s) for s in page["symbol"].tolist()]
    extension = _extension_for(ordered, pd.DatetimeIndex(sorted(_sessions_safe())))

    coverage = dict(payload.get("coverage") or {})
    coverage["excluded_suspect"] = len(excluded_suspect)
    coverage["suspect_symbols"] = [e["symbol"] for e in excluded_suspect][:50]
    return {
        **payload,
        "coverage": coverage,
        "count": total_matched,
        "returned": int(len(page)),
        "truncated": total_matched > len(page),
        "excluded_suspect": excluded_suspect[:50],
        "suspect_included": bool(include_suspect),
        "rows": _row_payload(page, extension),
    }


# The response is cached (300s, copy=False): the per-request build — frame
# copy, rank, filter, sort, and row dicts for up to `limit` rows — cost more
# than the compose it followed. Deterministic per param set; consumers only
# serialize it, so it is handed out uncopied.
# 300s > the 5-minute warmer interval, so the periodic warm in
# backend/services/scheduler.py keeps this entry permanently hot; a 60s TTL
# lapsed between warms and the next visit paid the build again.
_cached_board_response = ttl_cache(ttl_seconds=300, copy=False)(_board_response_compute)

# ── Movers: the two tails of the same board ──────────────────────────
#
# The board is ranked by COMPOSITE SCORE, so the biggest decliner of a window
# routinely sits far down the order — a -25% week is exactly what the score
# punishes, and the collapse the operator wants to see is often past the page
# limit entirely. The two tails are therefore taken from the WHOLE composed
# frame BEFORE any limit, not from the rows the board page happened to carry.


def _movers_from_frame(frame: pd.DataFrame, n: int, include_suspect: bool = False) -> dict:
    """The *n* largest gainers and decliners in a scored board frame.

    A row whose price series contains an impossible move cannot be a "mover",
    so suspects are handled exactly as on the board: set aside, and every
    set-aside name that WOULD have placed is named in ``excluded_suspect`` —
    a list the operator will read must never hide its exclusions.
    """
    miss = {"available": False, "gainers": pd.DataFrame(), "losers": pd.DataFrame(),
            "excluded_suspect": [], "rankable": 0}
    if frame is None or frame.empty or "ret_period" not in frame.columns:
        return {**miss, "note": "No rankable rows to move."}

    raw = frame.copy().reset_index(drop=True)
    # _assemble already drops rows without a usable return; the guard is for any
    # other caller — a NaN sorted to either end of a movers list is a fiction.
    raw = raw[raw["ret_period"].notna()].reset_index(drop=True)
    if raw.empty:
        return {**miss, "note": "No rankable rows to move."}

    if "suspect" in raw.columns:
        suspect = raw["suspect"].fillna(False).astype(bool)
    else:
        suspect = pd.Series(False, index=raw.index, dtype="bool")

    work = (raw if include_suspect else raw[~suspect].reset_index(drop=True)).copy()
    # The frame already arrives in score order; rank it here (rather than
    # re-sorting by score) so each row's rank is the number the board prints.
    work["rank"] = np.arange(1, len(work) + 1)

    gainers = work.sort_values(["ret_period", "symbol"], ascending=[False, True]).head(n)
    losers = work.sort_values(["ret_period", "symbol"], ascending=[True, True]).head(n)

    excluded: list[dict] = []
    if not include_suspect and bool(suspect.any()):
        top = pd.concat([
            raw.sort_values(["ret_period", "symbol"], ascending=[False, True]).head(n),
            raw.sort_values(["ret_period", "symbol"], ascending=[True, True]).head(n),
        ]).drop_duplicates(subset="symbol")
        for _, r in top[suspect.reindex(top.index).fillna(False)].iterrows():
            excluded.append({
                "symbol": str(r["symbol"]),
                "side": "gainers" if float(r["ret_period"]) >= 0 else "losers",
                "ret_period": _num(r.get("ret_period")),
                "note": _optional_text(r.get("suspect_note")),
            })
    return {"available": True, "gainers": gainers, "losers": losers,
            "excluded_suspect": excluded, "rankable": int(len(work))}


def _optional_text(value) -> str | None:
    """A stored note for JSON: absent stays absent, NaN never travels."""
    if value is None:
        return None
    if pd.isna(value):
        return None
    return str(value)


def _movers_compute(
    universe: str = "S&P 500",
    period: str = "YTD",
    n: int = 8,
    include_suspect: bool = False,
) -> dict:
    """Both tails of the composed board. Cached by the caller."""
    payload = assemble_cached(universe, period, None)
    payload = {**payload}  # shallow: never mutate the cached dict
    if not payload.get("available"):
        return payload

    frame = payload.pop("_frame")
    movers = _movers_from_frame(frame, n, include_suspect)
    if not movers["available"]:
        return {
            **payload, "n": n, "rankable": 0, "suspect_included": bool(include_suspect),
            "excluded_suspect": [], "gainers": [], "losers": [],
            "note": movers.get("note"),
        }

    suspect_map: dict[str, bool] = {}
    note_map: dict[str, str | None] = {}
    if "suspect" in frame.columns:
        suspect_map = {str(s): bool(v) for s, v in zip(frame["symbol"], frame["suspect"].fillna(False))}
    if "suspect_note" in frame.columns:
        note_map = {str(s): _optional_text(v) for s, v in zip(frame["symbol"], frame["suspect_note"])}

    def tag(rows: list[dict]) -> list[dict]:
        # Only reachable with include_suspect=true — the rows must then carry
        # the flag so a broken series cannot read as a real move.
        for r in rows:
            r["suspect"] = suspect_map.get(r["symbol"], False)
            r["suspect_note"] = note_map.get(r["symbol"])
        return rows

    return {
        **payload,
        "n": n,
        "rankable": movers["rankable"],
        "suspect_included": bool(include_suspect),
        "excluded_suspect": movers["excluded_suspect"],
        "gainers": tag(_row_payload(movers["gainers"], {})),
        "losers": tag(_row_payload(movers["losers"], {})),
    }


# Same cached shape as the board response: deterministic per param set, handed
# out uncopied. The strip is on the page's critical path, so a warm hit beats
# rebuilding row dicts on every period switch.
_cached_movers = ttl_cache(ttl_seconds=300, copy=False)(_movers_compute)


@router.get("/movers")
def leaderboard_movers(
    universe: str = Query("S&P 500"),
    period: str = Query("YTD"),
    n: int = Query(default=8, ge=1, le=50),
    include_suspect: bool = Query(False),
):
    """The board's two tails: the biggest gainers and decliners over the window.

    Read from the whole frame rather than the board page — the board's own
    worst decliner is normally past the page edge, because the score punishes
    precisely the move this list exists to surface.
    """
    return _cached_movers(universe, period, n, include_suspect)


def _sessions_safe() -> list:
    from data import market_panel
    try:
        return list(market_panel.session_index())
    except Exception:
        return []


@router.get("/sectors")
def leaderboard_sectors(
    universe: str = Query("S&P 500"),
    period: str = Query("YTD"),
    limit: int = Query(default=30, ge=1, le=200),
):
    """Sector roll-up for the period — where the money went, by group."""
    payload = assemble_cached(universe, period, None)
    payload = {**payload}  # shallow: never mutate the cached dict
    if not payload.get("available"):
        return payload

    from analysis import leaderboard as lb

    frame = payload.pop("_frame").set_index("symbol")
    frame = frame[frame["ret_period"].notna()]
    if frame.empty:
        return {"available": False, "note": "No rankable rows for this period."}

    _, sector_map, _ = _universe(universe)
    rolled = lb.rollup(frame, sector_map if sector_map else _sector_from_frame(frame))
    rolled = rolled.reset_index()
    rows = [
        {
            "sector": r["sector"],
            "n": int(r["n"]),
            "median_return": _num(r["median_return"]),
            "median_flow_ratio": _num(r["median_flow_ratio"]),
            "advancers_pct": _num(r["advancers_pct"]),
        }
        for _, r in rolled.head(limit).iterrows()
    ]
    return {
        "available": True,
        "universe": universe,
        "period": period,
        "basis": payload.get("basis"),
        "base_session": payload.get("base_session"),
        "as_of": payload.get("as_of"),
        "count": int(len(rolled)),
        "rows": rows,
    }


def _sector_label(row) -> str:
    sector = row.get("sector")
    return "Unclassified" if sector is None or pd.isna(sector) else str(sector)


def _sector_from_frame(frame: pd.DataFrame) -> dict[str, str]:
    """Fall back to the sector the row already carries when the universe has no map."""
    return {
        str(sym): _sector_label(row) for sym, row in frame.iterrows()
    }


@router.get("/years")
def leaderboard_years(
    universe: str = Query("S&P 500"),
    period: str = Query("YTD"),
    limit: int = Query(default=30, ge=1, le=200),
):
    """Calendar-year matrix by sector — the multi-year comparison frame.

    Rows are groups, columns are calendar years, cells are the median return of
    that group's members in that year. Median, not mean: one 400% mover must not
    define a sector's year.
    """
    payload = assemble_cached(universe, period, None)
    payload = {**payload}  # shallow: never mutate the cached dict
    if not payload.get("available"):
        return payload

    frame = payload.pop("_frame").set_index("symbol")
    year_cols = [c for c in frame.columns if c.startswith("year_")]
    if not year_cols:
        return {"available": False, "note": "No calendar-year returns on this panel."}

    _, sector_map, _ = _universe(universe)
    groups = sector_map if sector_map else _sector_from_frame(frame)
    frame = frame.assign(sector=[groups.get(str(s).upper(), "Unclassified") for s in frame.index])

    by_sector = frame.groupby("sector")[sorted(year_cols)].median()
    counts = frame.groupby("sector").size()

    # Year columns are "year_YYYY" — sort them CHRONOLOGICALLY. Ordering them by
    # median return (they have none) or by insertion would silently reshuffle the
    # columns the operator is comparing across.
    ordered = sorted(year_cols)
    latest = ordered[-1] if ordered else None
    if latest:
        by_sector = by_sector.sort_values(latest, ascending=False)

    rows = []
    for sector, r in by_sector.head(limit).iterrows():
        rows.append({
            "sector": sector,
            "n": int(counts.get(sector, 0)),
            "years": {c[5:]: _num(r[c]) for c in ordered},
        })

    years = [c[5:] for c in ordered]
    market = {c[5:]: _num(frame[c].median()) for c in ordered}
    return {
        "available": True,
        "universe": universe,
        "as_of": payload.get("as_of"),
        "years": years,
        "market_median": market,
        "count": int(len(by_sector)),
        "rows": rows,
    }
