"""Desk status — discipline, streak, rank, and earned milestones.

Pure functions over the operator's already-recorded behavior (theses,
decisions, pipeline, research reports) and their resolved skill (the Decision
Book scorecard). No fetching, no LLM, no side effects besides reading the
inputs handed in.

The Desk Loop's contract (research/plans/desk-loop.md): reward **process and
measured skill, never P&L or day-moves**. P&L is luck-contaminated; the rank is
gated on the Wilson lower bound and the information coefficient, with the
existing ``unverified`` honesty contract as the anti-cheat — logging filler
decisions cannot rank you up.

Three discipline components, each 0–100, transparently weighted (defaults
below; tunable in ``analysis/methodology.py`` / prefs):

  1. Review freshness   — are recorded theses reviewed within the interval?
  2. Forecast precision — are decisions pre-registered with a direction AND a
                          horizon, rather than logged as vague actions?
  3. Research discipline — do committed pipeline ideas carry a thesis or a
                          linked research report behind them?

A component with no input of its kind contributes nothing and the score
renormalizes over the components that are present; if none are present the
discipline score is ``None`` (never a fabricated number).
"""

from __future__ import annotations

import time

# Default discipline weights. Sum need not be 1 — the score renormalizes over
# the components actually present. Documented in analysis/methodology.py.
DEFAULT_WEIGHTS = {
    "review_freshness": 0.40,
    "forecast_precision": 0.35,
    "research_discipline": 0.25,
}

# Rank ladder, gated on the Wilson lower bound of the resolved win rate. A rank
# is only reachable when the sample is significant, the IC is positive, and the
# lower bound clears the threshold — activity alone earns nothing.
RANK_LADDER = (
    ("Unrated", 0.0),
    ("Developing", 0.50),
    ("Consistent", 0.55),
    ("Proven", 0.60),
)

# A thesis reviewed more than this many days ago counts against review
# freshness (mirrors data/theses.REVIEW_INTERVAL_DAYS).
REVIEW_INTERVAL_DAYS = 7

# Default minimum resolved sample before any skill claim is certified.
MIN_OBS = 10


def _day(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


def _as_num(v) -> float | None:
    try:
        if v is None:
            return None
        f = float(v)
        return f if f == f else None
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Discipline components
# ---------------------------------------------------------------------------


def review_freshness(theses: list[dict], now: float | None = None) -> dict | None:
    """Fraction of recorded theses reviewed within ``REVIEW_INTERVAL_DAYS``.

    Returns ``None`` when there are no theses — the component is absent, not
    zero (a fresh operator with no positions has no review obligation).
    """
    if not theses:
        return None
    now = now if now is not None else time.time()
    cutoff = now - REVIEW_INTERVAL_DAYS * 86400
    fresh = 0
    for t in theses:
        last = _as_num(t.get("last_reviewed_at"))
        if last is not None and last >= cutoff:
            fresh += 1
    score = round(fresh / len(theses) * 100, 1)
    return {
        "name": "Review Freshness",
        "score": score,
        "detail": f"{fresh}/{len(theses)} theses reviewed in {REVIEW_INTERVAL_DAYS}d",
    }


def forecast_precision(decisions: list[dict]) -> dict | None:
    """Fraction of decisions pre-registered with BOTH a direction and a horizon.

    A decision carrying a direction and a horizon is a *forecast* the resolve
    step can score; a bare action is a note. Returns ``None`` when there are no
    decisions.
    """
    if not decisions:
        return None
    precise = sum(
        1 for d in decisions
        if (d.get("direction") or "").strip() and (d.get("horizon") or "").strip()
    )
    score = round(precise / len(decisions) * 100, 1)
    return {
        "name": "Forecast Precision",
        "score": score,
        "detail": f"{precise}/{len(decisions)} decisions carry direction + horizon",
    }


def research_discipline(pipeline: list[dict]) -> dict | None:
    """Fraction of committed pipeline ideas backed by a thesis or report.

    Only items that reached ``committed`` or ``resolved`` count — an idea still
    being sized has no research obligation yet. Returns ``None`` when no idea
    has committed.
    """
    committed = [p for p in pipeline if p.get("stage") in ("committed", "resolved")]
    if not committed:
        return None
    backed = sum(
        1 for p in committed
        if p.get("thesis_ref") or (p.get("report_ids") or [])
    )
    score = round(backed / len(committed) * 100, 1)
    return {
        "name": "Research Discipline",
        "score": score,
        "detail": f"{backed}/{len(committed)} committed ideas backed by thesis/report",
    }


def discipline_score(
    theses: list[dict],
    decisions: list[dict],
    pipeline: list[dict],
    now: float | None = None,
    weights: dict[str, float] | None = None,
) -> dict:
    """Weighted discipline score from the three process components.

    Returns ``{value, components[], confidence, is_predictive: False, detail}``.
    ``value`` is ``None`` when no component has input. Confidence scales with
    how many components are present.
    """
    w = dict(DEFAULT_WEIGHTS)
    if weights:
        w.update({k: float(v) for k, v in weights.items() if k in w})

    raw_components = [
        ("review_freshness", review_freshness(theses, now)),
        ("forecast_precision", forecast_precision(decisions)),
        ("research_discipline", research_discipline(pipeline)),
    ]
    components = []
    weighted_sum = 0.0
    weight_sum = 0.0
    for key, comp in raw_components:
        if comp is None:
            continue
        weight = w.get(key, 0.0)
        comp = {**comp, "key": key, "weight": weight}
        components.append(comp)
        weighted_sum += comp["score"] * weight
        weight_sum += weight

    if weight_sum <= 0:
        return {
            "value": None,
            "components": components,
            "confidence": 0.0,
            "is_predictive": False,
            "detail": "No process activity recorded yet",
        }

    value = round(weighted_sum / weight_sum, 1)
    confidence = round(min(1.0, len(components) / len(raw_components)), 2)
    return {
        "value": value,
        "components": components,
        "confidence": confidence,
        "is_predictive": False,
        "detail": f"{len(components)}/{len(raw_components)} components present",
    }


# ---------------------------------------------------------------------------
# Streak
# ---------------------------------------------------------------------------


def active_streak(decisions: list[dict], now: float | None = None) -> dict:
    """Consecutive days ending today/yesterday with at least one decision.

    A streak counts as alive if the most recent active day is today or
    yesterday (so an unlogged morning doesn't break it). Returns
    ``{current, last_active}``. ``current`` is 0 when there is no activity.
    """
    now = now if now is not None else time.time()
    if not decisions:
        return {"current": 0, "last_active": None}

    days = sorted({_day(float(d["ts"])) for d in decisions if d.get("ts") is not None})
    if not days:
        return {"current": 0, "last_active": None}

    from datetime import date, timedelta

    def parse(s: str) -> date:
        y, m, d = (int(x) for x in s.split("-"))
        return date(y, m, d)

    active = {parse(s) for s in days}
    today = parse(_day(now))
    anchor = today if today in active else today - timedelta(days=1)
    if anchor not in active:
        return {"current": 0, "last_active": days[-1]}

    streak = 0
    cursor = anchor
    while cursor in active:
        streak += 1
        cursor -= timedelta(days=1)
    return {"current": streak, "last_active": days[-1]}


# ---------------------------------------------------------------------------
# Rank — gated on measured skill
# ---------------------------------------------------------------------------


def _pick_horizon(scorecard: dict, min_obs: int) -> tuple[str | None, dict]:
    """The horizon with the largest significant resolved sample, else the max."""
    best_key = None
    best = None
    for h, sc in (scorecard or {}).items():
        n = sc.get("n", 0) or 0
        if n < min_obs:
            continue
        if best is None or n > (best.get("n") or 0):
            best_key, best = h, sc
    if best is not None:
        return best_key, best
    # No significant horizon — fall back to the largest sample for reporting.
    for h, sc in (scorecard or {}).items():
        if best is None or (sc.get("n") or 0) > (best.get("n") or 0):
            best_key, best = h, sc
    return best_key, (best or {})


def desk_rank(scorecard: dict, min_obs: int = MIN_OBS) -> dict:
    """The operator's desk rank, gated on Wilson lower bound + IC + sample size.

    Returns ``{rank, next_rank, progress, horizon, n, wilson_lower, ic,
    gated_on[]}``. Below ``min_obs`` resolved outcomes the rank is ``Unrated``
    with the gate reason stated — never a fabricated confidence.
    """
    horizon, sc = _pick_horizon(scorecard or {}, min_obs)
    n = (sc.get("n") or 0) if sc else 0
    wilson = _as_num((sc.get("wilson_lower") or {}).get("value")) if sc else None
    ic = _as_num((sc.get("ic") or {}).get("value")) if sc else None

    gated: list[str] = []
    if n < min_obs:
        gated.append(f"need ≥ {min_obs} resolved decisions (have {n})")
    if ic is None or ic <= 0:
        gated.append("information coefficient must be positive")
    if wilson is None:
        gated.append("no resolved win rate yet")

    rank = "Unrated"
    if n >= min_obs and ic is not None and ic > 0 and wilson is not None:
        for name, threshold in RANK_LADDER:
            if name == "Unrated":
                continue
            if wilson >= threshold:
                rank = name

    # Progress toward the next rung (0–1), by Wilson lower bound.
    next_rank = None
    progress = 0.0
    threshold_by_name = dict(RANK_LADDER)
    for name, threshold in RANK_LADDER:
        if threshold > (wilson or 0) and (n >= min_obs and ic is not None and ic > 0):
            next_rank = name
            prev = 0.0
            for pn, pt in RANK_LADDER:
                if pt <= (wilson or 0):
                    prev = pt
            span = threshold - prev
            progress = round(max(0.0, min(1.0, ((wilson or 0) - prev) / span)), 3) if span > 0 else 0.0
            break

    if rank == "Unrated" and next_rank is None:
        next_rank = "Developing"
        progress = round(max(0.0, min(1.0, (wilson or 0) / threshold_by_name["Developing"])), 3) if wilson else 0.0

    return {
        "rank": rank,
        "next_rank": next_rank,
        "progress": progress,
        "horizon": horizon,
        "n": n,
        "wilson_lower": round(wilson, 4) if wilson is not None else None,
        "ic": round(ic, 4) if ic is not None else None,
        "gated_on": gated,
    }


# ---------------------------------------------------------------------------
# Milestones — deterministic conditions; the ledger records what was earned
# ---------------------------------------------------------------------------

MILESTONES = (
    ("first_decision", "First Decision", "Logged the first decision"),
    ("first_commit", "First Commit", "Committed the first pipeline idea to the book"),
    ("ten_resolved", "Ten Resolved", "Ten decisions resolved with outcomes"),
    ("research_backed", "Research-Backed", "Committed an idea with a thesis or report behind it"),
    ("positive_ic", "Positive IC", "The decision score predicts forward returns (IC > 0, significant)"),
    ("skill_sharpened", "Skill Sharpened", "Reached the Consistent desk rank on measured skill"),
    ("streak_5", "Five-Day Streak", "Five consecutive days with a logged decision"),
)


def milestones_due(state: dict) -> list[dict]:
    """Milestone ids whose conditions currently hold.

    ``state`` carries the aggregates the endpoint already computed:
    ``{decision_count, commit_count, resolved_count, research_backed_count,
    rank, ic, ic_significant, streak}``.
    """
    due: list[str] = []
    if state.get("decision_count", 0) >= 1:
        due.append("first_decision")
    if state.get("commit_count", 0) >= 1:
        due.append("first_commit")
    if state.get("resolved_count", 0) >= 10:
        due.append("ten_resolved")
    if state.get("research_backed_count", 0) >= 1:
        due.append("research_backed")
    if state.get("ic_significant") and (_as_num(state.get("ic")) or 0) > 0:
        due.append("positive_ic")
    if state.get("rank") in ("Consistent", "Proven"):
        due.append("skill_sharpened")
    if state.get("streak", 0) >= 5:
        due.append("streak_5")

    label_by_id = {mid: (label, desc) for mid, label, desc in MILESTONES}
    return [
        {"id": mid, "label": label_by_id[mid][0], "description": label_by_id[mid][1]}
        for mid in due
    ]


# ---------------------------------------------------------------------------
# Aggregator
# ---------------------------------------------------------------------------


def desk_status(
    scorecard: dict,
    decisions: list[dict],
    theses: list[dict],
    pipeline: list[dict],
    outcomes: list[dict] | None = None,
    now: float | None = None,
    min_obs: int = MIN_OBS,
    weights: dict[str, float] | None = None,
) -> dict:
    """Assemble the full desk-status payload from the operator's records.

    ``pipeline`` is a list of items (``data.pipeline.get_pipeline().values()``);
    ``theses`` a list (``get_theses().values()``). Everything is pure.
    """
    now = now if now is not None else time.time()
    decisions = decisions or []
    theses = theses or []
    pipeline = list(pipeline or [])
    outcomes = outcomes or []

    discipline = discipline_score(theses, decisions, pipeline, now=now, weights=weights)
    streak = active_streak(decisions, now=now)
    rank = desk_rank(scorecard or {}, min_obs=min_obs)

    commit_count = sum(1 for p in pipeline if p.get("stage") in ("committed", "resolved"))
    research_backed_count = sum(
        1 for p in pipeline
        if p.get("stage") in ("committed", "resolved") and (p.get("thesis_ref") or p.get("report_ids"))
    )
    # Resolved outcomes carry a horizon entry with a non-null return.
    resolved_count = 0
    for o in outcomes:
        horizons = o.get("horizons") or {}
        if any(isinstance(h, dict) and h.get("ret") is not None for h in horizons.values()):
            resolved_count += 1

    ic = rank.get("ic")
    ic_significant = bool(rank.get("n", 0) >= min_obs and ic is not None)

    state = {
        "decision_count": len(decisions),
        "commit_count": commit_count,
        "resolved_count": resolved_count,
        "research_backed_count": research_backed_count,
        "rank": rank["rank"],
        "ic": ic,
        "ic_significant": ic_significant,
        "streak": streak["current"],
    }
    due = milestones_due(state)

    return {
        "available": True,
        "discipline": discipline,
        "streak": streak,
        "rank": rank,
        "counts": {
            "decisions": len(decisions),
            "theses": len(theses),
            "pipeline": len(pipeline),
            "committed": commit_count,
            "resolved": resolved_count,
        },
        "milestones_due": due,
    }
