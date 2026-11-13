"""Account endpoints: summary, positions, realized P&L."""

from fastapi import APIRouter

from backend.routers.analysis._common import _safe_float
from data.cache import ttl_cache

router = APIRouter(prefix="/api/accounts", tags=["accounts"])


def total_pl_pct(row) -> float | None:
    """Return percent change on cost basis for a position.

    Anchored to total_pl so the sign is correct for shorts: the denominator is
    the absolute cost basis (avg_cost × qty × multiplier). Returns None when
    the basis is unknown (zero cost) — never a fabricated 0%.
    """
    total_pl = _safe_float(row.get("total_pl", 0))
    avg_cost = _safe_float(row.get("avg_cost", 0))
    qty = _safe_float(row.get("quantity", 0))
    mult = _safe_float(row.get("option_multiplier", 1)) or 1
    basis = abs(avg_cost * qty * mult)
    if basis <= 0:
        return None
    return (total_pl / basis) * 100


def _clean_str(value) -> str:
    """A missing pandas field arrives as the string \"nan\"; the UI must not show it."""
    text = str(value if value is not None else "")
    return "" if text.strip().lower() in ("nan", "none", "null") else text


def _pos_to_dict(row) -> dict:
    return {
        "symbol": str(row.get("symbol", "") or ""),
        "description": str(row.get("description", "") or ""),
        "asset_type": str(row.get("asset_type", "") or ""),
        "quantity": _safe_float(row.get("quantity", 0)),
        "avg_cost": _safe_float(row.get("avg_cost", 0)),
        "current_price": _safe_float(row.get("current_price", 0)),
        "market_value": _safe_float(row.get("market_value", 0)),
        "day_pl": _safe_float(row.get("day_pl", 0)),
        "day_pl_pct": _safe_float(row.get("day_pl_pct", 0)),
        "total_pl": _safe_float(row.get("total_pl", 0)),
        "total_pl_pct": total_pl_pct(row),
        "underlying": _clean_str(row.get("underlying")),
        "put_call": _clean_str(row.get("put_call")),
        "strike": _safe_float(row.get("strike", 0)),
        "expiration": _clean_str(row.get("expiration")),
        "source": str(row.get("source", "") or ""),
        "stale": bool(row.get("stale", False)),
        "session": str(row.get("session", "regular") or "regular"),
    }


@router.get("/summary")
def account_summary():
    from data.account import get_combined_summary
    from data.snapshots import maybe_snapshot
    raw = get_combined_summary()
    maybe_snapshot(min_interval_seconds=300)
    return {k: _safe_float(v) if isinstance(v, (int, float)) else v for k, v in raw.items()}


@router.get("/positions")
def positions():
    from data.account import get_combined_positions
    df = get_combined_positions()
    if df.empty:
        return []
    return [_pos_to_dict(row) for _, row in df.iterrows()]


@ttl_cache(ttl_seconds=300)
def _realized_tax_block(reported_securities_total: float) -> dict:
    """Tax reads over the per-lot tape: short/long split and OPEN wash-sale
    windows, plus a tie-out of the lots against the reported total.

    `as_of` is America/New_York — the box runs UTC, and a window that "expires
    today" must expire on the operator's today.
    """
    import datetime
    from zoneinfo import ZoneInfo

    from analysis.tax import summarize
    from data.imports import get_all_activity_lots

    df = get_all_activity_lots()
    lots = df.to_dict(orient="records") if not df.empty else []
    as_of = datetime.datetime.now(ZoneInfo("America/New_York")).date()
    return summarize(lots, reported_securities_total, as_of)


@router.get("/realized")
def realized_pl():
    from data.imports import get_all_external_pl
    from data.realized import get_combined_realized_pl

    summary_df, sec_total = get_combined_realized_pl()
    ext_rows, ext_total = get_all_external_pl()  # excludes needs_review
    rows = summary_df.to_dict(orient="records") if not summary_df.empty else []
    return {
        # Aggregate merge: total realized includes external P&L. Per-symbol
        # rows stay securities-only — external has no symbol/lot/benchmark.
        "total": round(sec_total + ext_total, 2),
        "by_symbol": rows,
        "external": {"total": ext_total, "by_source": ext_rows},
        # Tax reads run against the SECURITIES total only: the lot tape has no
        # external rows, so the tie-out compares like with like.
        "tax": _realized_tax_block(round(sec_total, 2)),
    }


@router.get("/external")
def external_pl():
    """External P&L (LLM-extracted: gambling, prediction markets, staking).

    A parallel ledger to securities realized P&L — never merged into
    attribution, per-symbol P&L, or the equity curve. Rolls up into the
    Overview External P&L line only. Manual asserted entries (bank interest
    without a statement) merge into the same by_source rows.
    """
    from data.external_pl_manual import list_manual_external_pl
    from data.imports import get_all_external_pl
    rows, total = get_all_external_pl()
    return {
        "total": total,
        "by_source": rows,
        "manual_entries": list_manual_external_pl(),
    }
