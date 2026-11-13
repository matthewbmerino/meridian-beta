"""Panel review for research reports — a hierarchy, not a single check.

A finished report is reviewed by four ANALYST seats, each with one job, then
adjudicated by a CHIEF who sees their findings and issues the publish verdict.
The seats are deliberately adversarial to each other: the Quant only cares that
the numbers reproduce, the Visual only cares whether the figure set makes the
argument, the Qualitative only cares whether the prose earns its claims, and the
Risk seat argues the other side.

Underneath the model panel sits `lint_figures` — deterministic checks over the
artifact manifest. It matters because a model asked "are these charts good?"
will compliment them; a rule set that knows a bar chart of ranges is the wrong
type, or that a chart with no `value_format` renders an unlabelled axis, will
not. The Visual seat receives the lint output as evidence rather than opinion.
"""

from __future__ import annotations

import json
import logging
import re
import time

log = logging.getLogger(__name__)

ANALYST_SEATS: list[dict] = [
    {
        "id": "quant",
        "name": "Quant",
        "weight": 0.30,
        "brief": (
            "You are the QUANT seat. You do not care how the report reads; you care whether "
            "the numbers reproduce. Check every figure, percentage, date, level and unit "
            "against the verified data and against arithmetic. A number that cannot be traced "
            "to a source is a finding. A percentage that does not follow from the two prices "
            "quoted beside it is a finding. State the number you checked and the number it "
            "should be."
        ),
    },
    {
        "id": "visual",
        "name": "Visual",
        "weight": 0.25,
        "brief": (
            "You are the VISUAL seat. Judge the figure set, not the prose. For each figure ask: "
            "is this the right chart TYPE for the question (a trend over time is a line, a "
            "calendar is a timeline, a scenario range is a floating zone, a grid of numbers is a "
            "heatmap)? Does it carry its unit? Would a reader learn the argument from the "
            "picture alone? A solid-bar chart standing in for a range, a chart with no axis "
            "caption, or a figure that decorates rather than argues is a finding."
        ),
    },
    {
        "id": "qual",
        "name": "Qualitative",
        "weight": 0.25,
        "brief": (
            "You are the QUALITATIVE seat. Judge whether the argument earns its conclusion. "
            "Is the thesis stated in the first paragraph? Is every load-bearing claim supported "
            "by evidence in the report rather than assertion? Are the probabilities given a "
            "basis? Is the language proportionate to the evidence — no 'clearly', no 'obviously', "
            "no hedge that hides a missing number? Is there an explicit falsifier?"
        ),
    },
    {
        "id": "risk",
        "name": "Risk",
        "weight": 0.20,
        "brief": (
            "You are the RISK seat and you are paid to argue the other side. What is the "
            "strongest case that this report is wrong? Is the downside sized, or only named? "
            "Does the plan survive its own bear case — if the base case fails, what does the "
            "reader own and at what price? Does the report distinguish a test position from a "
            "conviction position?"
        ),
    },
]

CHIEF = {
    "id": "chief",
    "name": "Desk Chief",
    "brief": (
        "You are the DESK CHIEF. You have your analysts' findings and the report. You do not "
        "re-do their work — you adjudicate: which findings are material, which are pedantry, "
        "and whether this note goes out over your name. Publish means a client could act on it "
        "tomorrow. Revise means the thesis is sound but something breaks before it ships. "
        "Reject means the argument does not hold."
    ),
}

_SEVERITIES = ("major", "minor")


# --------------------------------------------------------------------------- #
# Deterministic figure lint — the evidence the Visual seat is given
# --------------------------------------------------------------------------- #

def lint_figures(artifacts: dict) -> list[dict]:
    """Rule-based checks over the artifact manifest. Returns finding dicts."""
    findings: list[dict] = []
    charts = artifacts.get("charts") or []
    tables = artifacts.get("tables") or []
    metrics = artifacts.get("metrics") or []

    def add(sev: str, code: str, claim: str, note: str) -> None:
        findings.append({"severity": sev, "code": code, "claim": claim, "note": note})

    if not charts and not tables:
        add("major", "no-figures", "The report ships with no figures",
            "A research note without a single chart or table cannot be read at a glance.")

    for c in charts:
        if not isinstance(c, dict):
            continue
        title = str(c.get("title") or "Untitled chart")
        ctype = str(c.get("chart_type") or "")
        data = c.get("data") or []
        series = [s for s in data if isinstance(s, dict)]
        if not c.get("x_label"):
            add("minor", "no-x-label", title, "No x_label — the reader has to infer what the axis measures.")
        if not c.get("y_label"):
            add("minor", "no-y-label", title, "No y_label — the unit is missing from the axis.")
        if not c.get("value_format") and ctype != "timeline":
            # A timeline's axis is a date range, not a value — there is nothing for
            # value_format to format, so flagging it is noise a seat would repeat.
            add("minor", "no-format", title,
                "No value_format — ticks render as bare numbers instead of $, % or days.")
        if len(series) > 6:
            add("major", "too-many-series", title,
                f"{len(series)} series on one plot — the lines are indistinguishable past six.")
        if ctype == "bar" and len(series) == 1:
            pts = series[0].get("values") or []
            if len(pts) >= 3 and re.search(r"range|band|scenario|ladder|zone", title, re.I):
                add("major", "bar-for-range", title,
                    "A bar chart is standing in for a range comparison — use chart_type 'range' "
                    "with a low and a high series so the uncertainty is visible.")
        if ctype == "timeline":
            for s in series:
                pts = s.get("values") or []
                if len(pts) != 2:
                    add("major", "timeline-row", f"{title} — {s.get('label')}",
                        f"Timeline rows need exactly two points (start, end); this row has {len(pts)}.")
        if ctype == "range" and len(series) < 2:
            add("major", "range-missing-bound", title,
                "A range chart needs TWO series — the low bound and the high bound.")
        if ctype == "line" and len(series) == 0:
            add("major", "empty-chart", title, "Chart has no series.")

    titles = [str(c.get("title")) for c in charts if isinstance(c, dict)]
    dupes = {t for t in titles if titles.count(t) > 1}
    for t in dupes:
        add("minor", "duplicate-title", t, "Two figures share a title, so annotations and legend cannot resolve.")

    for t in tables:
        if not isinstance(t, dict):
            continue
        cols = t.get("columns") or []
        if len(cols) > 9:
            add("minor", "wide-table", str(t.get("title") or "Table"),
                f"{len(cols)} columns — the last ones will scroll out of view on a laptop.")

    if len(metrics) > 8:
        add("minor", "metric-overflow", "Metrics strip",
            f"{len(metrics)} metrics pinned above the report — the strip stops having a hierarchy.")

    return findings


# --------------------------------------------------------------------------- #
# Manifest the seats read
# --------------------------------------------------------------------------- #

def figure_manifest(artifacts: dict) -> str:
    """Compact, model-readable description of the figure set."""
    lines: list[str] = []
    for c in artifacts.get("charts") or []:
        if not isinstance(c, dict):
            continue
        series = c.get("data") or []
        labels = ", ".join(str(s.get("label")) for s in series[:6] if isinstance(s, dict))
        bits = [
            f"- CHART [{c.get('chart_type')}] \"{c.get('title')}\"",
            f"series: {len(series)} ({labels})",
            f"x_label: {c.get('x_label') or 'MISSING'}",
            f"y_label: {c.get('y_label') or 'MISSING'}",
            f"value_format: {c.get('value_format') or 'MISSING'}",
        ]
        if c.get("bands"):
            # Print the labels, not just the count: a seat cannot judge whether a
            # shaded window is self-explanatory from "bands: 1".
            bits.append(
                "bands: "
                + "; ".join(
                    f"{b.get('label') or '(unlabelled)'} {b.get('from')}-{b.get('to')}"
                    + (f" [{b.get('tone')}]" if b.get("tone") else "")
                    for b in c["bands"]
                    if isinstance(b, dict)
                )
            )
        if c.get("markers"):
            bits.append(
                "markers: "
                + "; ".join(
                    f"{m.get('label') or '(unlabelled)'}@{m.get('x')}={m.get('y')}"
                    for m in c["markers"]
                    if isinstance(m, dict) and not str(m.get("x", "")).startswith("__")
                )
            )
        if c.get("scale"):
            bits.append(f"scale: {c['scale']}")
        if c.get("note"):
            bits.append(f"note: {str(c['note'])[:140]}")
        if c.get("chart_type") in ("range", "timeline", "heatmap", "bar", "grouped_bar"):
            # Categorical figures carry their meaning in the row labels — a seat
            # told only "series: 2 (Zone low, Zone high)" cannot tell which
            # scenario is the base case.
            cats = [str(pt.get("x")) for s2 in series if isinstance(s2, dict) for pt in (s2.get("values") or [])]
            uniq = list(dict.fromkeys(cats))[:10]
            if uniq:
                bits.append("rows: " + ", ".join(uniq))
            if c.get("chart_type") == "range" and len(series) >= 2:
                lows = {pt.get("x"): pt.get("y") for pt in (series[0].get("values") or [])}
                highs = {pt.get("x"): pt.get("y") for pt in (series[1].get("values") or [])}
                zones = "; ".join(
                    f"{k} {float(v):,.0f}-{float(highs.get(k, v)):,.0f}"
                    for k, v in lows.items()
                    if isinstance(v, (int, float))
                )
                if zones:
                    bits.append("zones: " + zones)
        lines.append(" · ".join(bits))
    for t in artifacts.get("tables") or []:
        if not isinstance(t, dict):
            continue
        cols = ", ".join(str(col.get("label")) for col in (t.get("columns") or [])[:10])
        lines.append(f"- TABLE \"{t.get('title')}\" · {len(t.get('columns') or [])} cols ({cols})")
    for m in artifacts.get("metrics") or []:
        if isinstance(m, dict):
            lines.append(f"- METRIC {m.get('label')}: {m.get('value')}")
    for k in artifacts.get("callouts") or []:
        if isinstance(k, dict):
            lines.append(f"- CALLOUT [{k.get('severity')}] {k.get('title')}")
    return "\n".join(lines) if lines else "(no figures)"


# --------------------------------------------------------------------------- #
# Model plumbing
# --------------------------------------------------------------------------- #

def _ask(system: str, user: str) -> str:
    from agent.compute import get_client_for_research

    client, model = get_client_for_research()
    response = client.chat(
        model=model,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        stream=False,
        role="research",
    )
    return ((response.get("message") or {}).get("content") or "").strip()


def _json_obj(text: str) -> dict:
    """Pull the first JSON object out of a model response (fences tolerated)."""
    if not text:
        return {}
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    raw = fence.group(1) if fence else None
    if raw is None:
        start = text.find("{")
        end = text.rfind("}")
        raw = text[start : end + 1] if start >= 0 and end > start else ""
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else {}
    except (ValueError, TypeError):
        return {}


def _norm_findings(items) -> list[dict]:
    out: list[dict] = []
    for it in items or []:
        if isinstance(it, str):
            out.append({"severity": "minor", "claim": "", "note": it})
            continue
        if not isinstance(it, dict):
            continue
        sev = str(it.get("severity") or "minor").lower()
        out.append({
            "severity": sev if sev in _SEVERITIES else "minor",
            "claim": str(it.get("claim") or "")[:240],
            "note": str(it.get("note") or it.get("correction") or "")[:400],
        })
    return out


def _norm_score(value, default: float | None = None) -> float | None:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(10.0, n))


def _norm_verdict(value: str, score: float | None) -> str:
    v = (value or "").strip().lower()
    if v in ("pass", "concerns", "fail"):
        return v
    if score is None:
        return "concerns"
    return "pass" if score >= 7.5 else "concerns" if score >= 5 else "fail"


# --------------------------------------------------------------------------- #
# The panel
# --------------------------------------------------------------------------- #

def run_panel(report: dict, gathered: dict | None = None) -> dict:
    """Run the four seats + the chief. Returns the persisted review block."""
    from agent.journalist import _preformat_gathered

    artifacts = report.get("artifacts") or {}
    content = report.get("content") or ""
    topic = report.get("topic") or report.get("title") or ""
    rtype = report.get("research_type") or "custom"

    manifest = figure_manifest(artifacts)
    lint = lint_figures(artifacts)

    # A note is ~13k chars; a 7k window cut the middle, which is exactly where the
    # derivation sections live, and a seat then reports "asserted, not shown" about
    # arithmetic that IS shown. Give the model the whole note when it fits and name
    # the sections otherwise, so a truncated read is never mistaken for a missing one.
    sections = [ln[3:].strip() for ln in content.splitlines() if ln.startswith("## ")]
    body = content if len(content) <= 12000 else content[:9000] + "\n\n[… middle truncated …]\n\n" + content[-3000:]

    verified = _preformat_gathered(gathered) if gathered else "(no gathered data attached)"
    verified = verified[:6000]

    lint_block = "\n".join(
        f"- [{f['severity']}] {f['claim']}: {f['note']}" for f in lint
    ) or "(no rule violations)"

    base_context = f"""## BRIEF
Topic: {topic}
Type: {rtype}

## FIGURE SET
{manifest}

## DETERMINISTIC FIGURE LINT
{lint_block}

## SECTIONS PRESENT
{chr(10).join("- " + x for x in sections) if sections else "(no sections)"}

## REPORT
{body}
"""

    started = time.time()
    panel: list[dict] = []
    llm_ok = True

    for seat in ANALYST_SEATS:
        system = (
            "You are a reviewing seat on an investment research desk. Respond with ONE JSON "
            "object and nothing else. Keys: score (0-10 number), verdict (pass|concerns|fail), "
            "findings (array of {severity: major|minor, claim, note}), strengths (array of "
            "strings, max 3). Be specific and terse; a finding must name the sentence or figure "
            "it is about."
        )
        user = f"{seat['brief']}\n\n"
        if seat["id"] == "quant":
            user += f"## VERIFIED DATA\n{verified}\n\n"
        user += base_context
        try:
            seat_obj = _json_obj(_ask(system, user))
        except Exception as e:
            log.warning("Review seat %s failed: %s", seat["id"], e)
            llm_ok = False
            seat_obj = {}

        score = _norm_score(seat_obj.get("score"))
        findings = _norm_findings(seat_obj.get("findings"))
        if seat["id"] == "visual":
            # The linter's findings are evidence, not an opinion — always merge.
            findings = _norm_findings(lint) + findings
        if seat["id"] == "visual" and score is not None and any(f["severity"] == "major" for f in lint):
            score = min(score, 6.0)
        panel.append({
            "id": seat["id"],
            "name": seat["name"],
            "seat": "analyst",
            "weight": seat["weight"],
            "score": score,
            "verdict": _norm_verdict(str(seat_obj.get("verdict") or ""), score),
            "findings": findings,
            "strengths": [str(x)[:200] for x in (seat_obj.get("strengths") or [])][:3],
        })

    scored = [s for s in panel if s["score"] is not None]
    if scored:
        tw = sum(s["weight"] for s in scored)
        analyst_score = sum(s["score"] * s["weight"] for s in scored) / tw if tw else None
    else:
        analyst_score = None

    # The chief reads the seats, not the raw data — that is the hierarchy.
    seat_digest = "\n\n".join(
        f"### {s['name']} — score {s['score'] if s['score'] is not None else 'n/a'}, verdict {s['verdict']}\n"
        + ("\n".join(f"- [{f['severity']}] {f['claim']}: {f['note']}" for f in s["findings"]) or "- (no findings)")
        for s in panel
    )

    chief_system = (
        "You are the desk chief adjudicating a research note. Respond with ONE JSON object: "
        "score (0-10), verdict (publish|revise|reject), summary (<=2 sentences, the reason in "
        "plain language), required_changes (array of strings — empty when publish). Adjudicate "
        "your analysts; do not repeat their work."
    )
    chief_user = f"{CHIEF['brief']}\n\n## ANALYST FINDINGS\n{seat_digest}\n\n{base_context}"
    try:
        chief_obj = _json_obj(_ask(chief_system, chief_user))
    except Exception as e:
        log.warning("Chief review failed: %s", e)
        llm_ok = False
        chief_obj = {}

    chief_score = _norm_score(chief_obj.get("score"))
    chief_verdict = str(chief_obj.get("verdict") or "").strip().lower()
    if chief_verdict not in ("publish", "revise", "reject"):
        # A dead LLM must never read as approval.
        chief_verdict = "revise" if llm_ok else "unreviewed"
    if chief_score is None and analyst_score is not None:
        chief_score = analyst_score

    composite = None
    if analyst_score is not None and chief_score is not None:
        composite = round(0.75 * analyst_score + 0.25 * chief_score, 2)
    elif analyst_score is not None:
        composite = round(analyst_score, 2)

    return {
        "run_at": time.time(),
        "elapsed_s": round(time.time() - started, 1),
        "llm": llm_ok,
        "panel": panel,
        "chief": {
            "name": CHIEF["name"],
            "seat": "chief",
            "score": chief_score,
            "verdict": chief_verdict,
            "summary": str(chief_obj.get("summary") or "")[:600],
            "required_changes": [str(x)[:300] for x in (chief_obj.get("required_changes") or [])][:8],
        },
        "composite": composite,
        "lint": lint,
    }


def review_to_corrections(review: dict) -> list[str]:
    """Flatten a panel run into the reviewer-correction strings the reader renders."""
    out: list[str] = []
    for seat in review.get("panel") or []:
        for f in seat.get("findings") or []:
            if f.get("severity") != "major":
                continue
            claim = f.get("claim") or seat.get("name")
            out.append(f"[error] {claim}: {f.get('note')}" if f.get("note") else f"[error] {claim}")
    for change in (review.get("chief") or {}).get("required_changes") or []:
        out.append(f"[error] {change}")
    return out
