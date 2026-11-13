"""Compute exposure — the book mapped against the price of compute.

Layer-5 relationship intelligence: how much of the operator's capital depends
on the Compute Index. Answers the question the index alone cannot — "if the
price of compute moves, what in my book moves with it?"

Pure: accepts the Compute Index series, the operator's positions, and the
compute-linked theses already assembled by the caller. No fetching, no LLM.

Honesty caveat (backlog #10, operator decision 2026-09-14): the Compute Index
is an available-offer spot index, not a full-market index — it is
concentration-heavy (B300-dominated) and its history is short. The exposure
panel therefore reports a ``reliability`` block and **never gates capital** on
the index; the drop is informational context, not a signal. A drop computed
from too short a series is reported as ``None`` rather than a confident number.
"""

from __future__ import annotations

# Below this many index snapshots the drop-from-peak is not trustworthy — the
# index is too young for a peak to mean anything. Mirrors backlog #10.
MIN_RELIABLE_SNAPSHOTS = 90


def index_drop(series: list[dict]) -> dict:
    """Drop of the Compute Index from its peak, with a reliability flag.

    ``series`` is ``[{ts, value}]`` oldest-first (``compute_index_series``).
    Returns ``{drop_pct, peak, current, current_value, n, reliable,
    reason}``. ``drop_pct`` is a positive percentage below the peak; when the
    series is empty it is ``None``. When the series is shorter than
    ``MIN_RELIABLE_SNAPSHOTS`` the value is still returned but marked
    ``reliable: False`` with the reason — informational, not a gate.
    """
    values: list[float] = []
    for s in series:
        v = _num(s.get("value"))
        if v is not None:
            values.append(v)
    n = len(values)
    if n == 0:
        return {"drop_pct": None, "peak": None, "current": None,
                "n": 0, "reliable": False, "reason": "No Compute Index history yet"}

    peak = max(values)
    current = values[-1]
    drop = (peak - current) / peak * 100 if peak > 0 else None
    reliable = n >= MIN_RELIABLE_SNAPSHOTS
    reason = None if reliable else (
        f"Index history is {n} snapshots — below {MIN_RELIABLE_SNAPSHOTS}; "
        "the drop is informational, not a capital gate"
    )
    return {
        "drop_pct": round(drop, 2) if drop is not None else None,
        "peak": round(peak, 2),
        "current": round(current, 2),
        "n": n,
        "reliable": reliable,
        "reason": reason,
    }


def compute_exposure(
    positions: list[dict],
    compute_symbols: set[str],
    series: list[dict],
    nav: float,
) -> dict:
    """Map compute-linked holdings to weights and context against the index.

    ``positions`` — ``[{symbol, market_value, asset_type}]``; ``compute_symbols``
    — the uppercase symbols whose recorded thesis is compute-linked;
    ``series`` — the index series; ``nav`` — net liquidation for weights.

    Returns ``{available, positions[], exposure_weight, exposure_value,
    linked_count, drop, reliability, reason}``. ``available`` is False with a
    reason when no holding is compute-linked — an explicit empty state, never
    a silent blank.
    """
    nav = float(nav or 0)
    linked: list[dict] = []
    seen: set[str] = set()
    for p in positions:
        sym = str(p.get("symbol") or "").upper()
        if not sym or sym in seen or p.get("asset_type") == "OPTION":
            continue
        if sym not in compute_symbols:
            continue
        seen.add(sym)
        mv = _num(p.get("market_value")) or 0.0
        linked.append({
            "symbol": sym,
            "market_value": round(mv, 2),
            "weight": round(mv / nav, 4) if nav > 0 else None,
        })

    linked.sort(key=lambda r: r["market_value"], reverse=True)
    exposure_value = round(sum(r["market_value"] for r in linked), 2)
    drop = index_drop(series)

    if not linked:
        return {
            "available": False,
            "positions": [],
            "exposure_weight": 0.0,
            "exposure_value": 0.0,
            "linked_count": 0,
            "drop": drop,
            "reliability": {"reliable": drop["reliable"]},
            "reason": "No compute-linked theses — flag a position's thesis as compute-linked to map it here",
        }

    return {
        "available": True,
        "positions": linked,
        "exposure_weight": round(exposure_value / nav, 4) if nav > 0 else 0.0,
        "exposure_value": exposure_value,
        "linked_count": len(linked),
        "drop": drop,
        "reliability": {"reliable": drop["reliable"]},
        "reason": None,
    }


def _num(v) -> float | None:
    try:
        if v is None:
            return None
        f = float(v)
        return f if f == f else None
    except (TypeError, ValueError):
        return None
