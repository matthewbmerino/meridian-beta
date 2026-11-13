"""Pure aggregation over cash movements — deployed capital, derived balances,
reconciliation against the asserted balances.

Rows in, totals out: no store, no network, no imports from ``data/`` (the
``analysis/*`` invariant). Rounding happens ONCE, at emission, so a headline
total and the figures printed beside it cannot disagree.
"""

from __future__ import annotations

from collections.abc import Iterable


def summarize(rows: list[dict], as_of: str | None = None) -> dict:
    """Totals by tag and direction, optionally bounded by an inclusive date."""
    window = [r for r in rows if not as_of or str(r.get("as_of", "")) <= str(as_of)[:10]]
    by_tag: dict[str, dict] = {}
    inflow = outflow = 0.0
    for r in window:
        try:
            amt = float(r.get("amount") or 0)
        except (TypeError, ValueError):
            continue
        tag = str(r.get("tag") or "other")
        b = by_tag.setdefault(tag, {"tag": tag, "count": 0, "in": 0.0, "out": 0.0, "net": 0.0})
        b["count"] += 1
        b["net"] += amt
        if amt >= 0:
            b["in"] += amt
            inflow += amt
        else:
            b["out"] += amt
            outflow += amt
    tags = [
        {"tag": b["tag"], "count": b["count"], "in": round(b["in"], 2),
         "out": round(b["out"], 2), "net": round(b["net"], 2)}
        for b in by_tag.values()
    ]
    tags.sort(key=lambda x: x["tag"])
    days = [str(r.get("as_of")) for r in window if r.get("as_of")]
    return {
        "as_of": as_of,
        "count": len(window),
        "inflow": round(inflow, 2),
        "outflow": round(outflow, 2),
        "net": round(inflow + outflow, 2),
        "first": min(days) if days else None,
        "last": max(days) if days else None,
        "by_tag": tags,
    }


def balance_by_institution(rows: Iterable[dict]) -> dict[str, float]:
    """opening + Σ movements per institution — rounded once, at emission."""
    out: dict[str, float] = {}
    for r in rows:
        inst = " ".join(str(r.get("institution") or "").split())
        if not inst:
            continue
        try:
            out[inst] = out.get(inst, 0.0) + float(r.get("amount") or 0)
        except (TypeError, ValueError):
            continue
    return {k: round(v, 2) for k, v in out.items()}


def reconcile(rows: list[dict], balances: dict[str, float], tolerance: float = 0.01) -> dict:
    """Compare derived (opening + movements) against the asserted balances.

    Four states, never one verdict: institutions that MATCH; those that DRIFT
    (with the delta, so the operator can see which side is short); institutions
    with movements but NO ``opening`` row (cannot be reconciled — named, not
    assumed to be zero, because assuming zero would invent a number the size of
    the balance); and movements for an institution the balance store does not
    carry at all (orphans). ``ok`` is true only when all four are clean.
    """
    derived = balance_by_institution(rows)
    opened = {str(r.get("institution")) for r in rows if str(r.get("tag")) == "opening"}
    matched: list[dict] = []
    drift: list[dict] = []
    unopened: list[dict] = []
    for inst, asserted in (balances or {}).items():
        try:
            asserted_f = round(float(asserted), 2)
        except (TypeError, ValueError):
            continue
        if inst not in opened:
            unopened.append({"institution": inst, "asserted": asserted_f})
            continue
        d = round(derived.get(inst, 0.0), 2)
        delta = round(asserted_f - d, 2)
        if abs(delta) <= tolerance:
            matched.append({"institution": inst, "balance": asserted_f})
        else:
            drift.append({"institution": inst, "asserted": asserted_f, "derived": d, "drift": delta})
    orphans = [
        {"institution": i, "derived": v}
        for i, v in derived.items()
        if i not in (balances or {})
    ]
    return {
        "matched": matched,
        "drift": drift,
        "unopened": unopened,
        "orphans": orphans,
        "tolerance": tolerance,
        "ok": not drift and not orphans and not unopened,
    }


def deployed(rows: list[dict], tags: tuple[str, ...] = ("renovation", "sale_cost")) -> dict:
    """Capital deployed into the things the operator itemizes, by month.

    Only the named tags are rolled up; everything else stays in the general
    ledger. A tag with no rows reports zero WITH its count, so "nothing spent"
    and "nothing recorded" are distinguishable at a glance.
    """
    out: dict[str, dict] = {}
    for t in tags:
        sel = [r for r in rows if str(r.get("tag")) == t]
        months: dict[str, float] = {}
        for r in sel:
            m = str(r.get("as_of"))[:7]
            try:
                months[m] = months.get(m, 0.0) + float(r.get("amount") or 0)
            except (TypeError, ValueError):
                continue
        out[t] = {
            "tag": t,
            "count": len(sel),
            "net": round(sum(months.values()), 2),
            "by_month": [{"month": m, "net": round(v, 2)} for m, v in sorted(months.items())],
        }
    return {"tags": out, "net": round(sum(v["net"] for v in out.values()), 2)}
