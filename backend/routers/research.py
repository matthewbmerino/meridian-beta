"""Research (Journalist) endpoints — SSE streaming + report CRUD.

PRIVATE — Journalist's tool harness is portfolio-aware (positions, sizing,
briefing from the global snapshot store). Gated by require_auth (fail-closed).
"""

import asyncio
import json
import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from backend.auth import require_auth

log = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/research",
    tags=["research"],
    dependencies=[Depends(require_auth)],
)


class ResearchBody(BaseModel):
    topic: str
    research_type: str = "custom"
    symbols: list[str] | None = None
    parent_report_id: str | None = None


def _classify_artifact(artifacts: dict, event: dict) -> None:
    """Fold a viz tool_call event into the persisted-artifacts structure.

    Each tool kind maps to exactly one slot in *artifacts* (graph/charts/
    tables/metrics/annotations/layout/callouts). Events that aren't canvas
    artifacts (or are empty) are ignored.
    """
    name = event.get("name", "")
    args = event.get("args") or {}
    if name == "visualize_network" and args.get("nodes"):
        artifacts["graph"] = args
    elif name == "visualize_chart" and args.get("data"):
        artifacts["charts"].append(args)
    elif name == "visualize_table" and args.get("rows"):
        artifacts["tables"].append(args)
    elif name == "highlight_metric":
        artifacts["metrics"].append(args)
    elif name == "add_annotation":
        artifacts["annotations"].append(args)
    elif name == "canvas_layout" and args.get("artifacts"):
        artifacts["layout"] = args.get("artifacts", [])
    elif name == "insight_callout":
        artifacts["callouts"].append(args)


@router.post("/run")
async def run_research(body: ResearchBody):
    from data.research_reports import new_report_id
    report_id = new_report_id()

    async def event_generator():
        yield {"event": "meta", "data": json.dumps({"report_id": report_id})}

        def _run_sync():
            from agent import journalist
            from backend.services.compass_session import compass_context

            events = []
            with compass_context(report_id):
                for event in journalist.run_research(
                    topic=body.topic,
                    research_type=body.research_type,
                    symbols=body.symbols,
                    parent_report_id=body.parent_report_id,
                ):
                    events.append(event)
            return events

        # to_thread copies contextvars (run_in_executor does NOT), so the
        # session ContextVar is visible inside the thread — required for
        # session-scoped report/finding writes.
        events = await asyncio.to_thread(_run_sync)

        report_content = None
        report_title = None
        citations = []
        elapsed_ms = None
        report_symbols = body.symbols
        report_state = {}
        artifacts: dict = {
            "graph": None, "charts": [], "tables": [],
            "metrics": [], "annotations": [], "layout": [], "callouts": [],
        }
        corrections: list = []
        writer_model = None
        reviewer_model = None
        actions: list = []

        for event in events:
            etype = event.get("type", "")
            yield {"event": etype, "data": json.dumps(event, default=str)}

            if etype == "report":
                report_content = event.get("content", "")
                report_title = event.get("title", body.topic)
            if etype == "citations":
                citations = event.get("urls", [])
            if etype == "done":
                elapsed_ms = event.get("elapsed_ms")
                if event.get("symbols"):
                    report_symbols = event["symbols"]
                if event.get("research_state"):
                    report_state = event["research_state"]
                corrections = event.get("corrections") or []
                writer_model = event.get("writer_model")
                reviewer_model = event.get("reviewer_model")
                actions = event.get("actions") or []
            if etype == "tool_call":
                _classify_artifact(artifacts, event)

        if report_content:
            from data.research_reports import save_report
            save_report(
                report_id=report_id,
                title=report_title or body.topic,
                content=report_content,
                research_type=body.research_type,
                topic=body.topic,
                symbols=report_symbols,
                citations=citations,
                elapsed_ms=elapsed_ms,
                parent_report_id=body.parent_report_id,
                research_state=report_state,
                artifacts=artifacts,
                writer_model=writer_model,
                reviewer_model=reviewer_model,
                corrections=corrections,
                actions=actions,
            )
            # Record a finding so Compass can reference this research later,
            # and the research memory bank can inject accumulated conviction
            # into follow-up writer prompts.
            try:
                from data.findings import add_finding
                add_finding(
                    report_id=report_id,
                    title=report_title or body.topic,
                    research_type=body.research_type,
                    symbols=body.symbols or report_symbols,
                    summary=report_content[:300],
                    research_state=report_state,
                )
            except Exception as e:
                log.warning("Finding persistence failed for report %s: %s", report_id, e)

        yield {"event": "complete", "data": json.dumps({"report_id": report_id})}

    return EventSourceResponse(event_generator())


@router.get("/reports")
def list_reports():
    from data.research_reports import list_reports
    return list_reports()


@router.get("/reports/{report_id}")
def get_report(report_id: str):
    from data.research_reports import load_report
    report = load_report(report_id)
    if report is None:
        return {"error": "Report not found"}
    return report


@router.delete("/reports/{report_id}")
def delete_report_endpoint(report_id: str):
    from data.research_reports import delete_report
    existed = delete_report(report_id)
    return {"ok": True, "existed": existed}


class ReportPatch(BaseModel):
    status: str | None = None
    title: str | None = None


@router.patch("/reports/{report_id}")
def patch_report(report_id: str, body: ReportPatch):
    """Update a report's desk state. Status is a closed set; a title is trimmed.

    Same failure convention as the rest of this router: validation problems come
    back as {"error": ...} in the body, not an HTTP error status.
    """
    from data.research_reports import update_report

    fields: dict = {}
    if body.status is not None:
        if body.status not in ("open", "confirmed", "invalidated"):
            return {"error": "status must be one of: open, confirmed, invalidated"}
        fields["status"] = body.status
    if body.title is not None:
        title = body.title.strip()
        if not title:
            return {"error": "title cannot be empty"}
        fields["title"] = title[:160]
    if not fields:
        return {"error": "Nothing to update"}
    updated = update_report(report_id, **fields)
    if updated is None:
        return {"error": "Report not found"}
    return {"ok": True, "report_id": report_id,
            "status": updated.get("status"), "title": updated.get("title")}


class ReviewBody(BaseModel):
    """Optional overrides for a panel run. Currently unused — reserved."""

    force: bool = False


def _run_review_panel(report_id: str) -> None:
    """Background worker: run the seats + chief, persist the review block.

    Runs on a thread rather than inline because five model calls will outlive a
    normal proxy read timeout. The report carries ``review.status`` so the reader
    can show a running state and poll — a hung review must not look like a hung
    page.
    """
    from data.research_reports import load_report, update_report

    try:
        report = load_report(report_id)
        if not report:
            return
        from agent.review_panel import review_to_corrections, run_panel

        review = run_panel(report)
        review["status"] = "done"
        existing = [c for c in (report.get("corrections") or []) if isinstance(c, str)]
        merged = existing + [c for c in review_to_corrections(review) if c not in existing]
        update_report(report_id, review=review, corrections=merged)
        log.info("Panel review %s: %s (%.1f)", report_id, review.get("chief", {}).get("verdict"),
                 review.get("composite") or 0)
    except Exception as e:
        log.warning("Panel review failed for %s: %s", report_id, e)
        update_report(report_id, review={"status": "failed", "error": str(e)[:300]})


@router.post("/reports/{report_id}/review")
def review_report(report_id: str, body: ReviewBody | None = None):
    """Start the panel review for a stored report. Returns immediately."""
    import threading
    import time

    from data.research_reports import load_report, update_report

    report = load_report(report_id)
    if report is None:
        return {"error": "Report not found"}
    current = report.get("review") or {}
    started_at = float(current.get("started_at") or 0)
    # A restart (a deploy, a crash) kills the worker thread and leaves the report
    # pinned in "running" forever. Treat a flag older than 15 minutes as dead so a
    # second POST can recover it instead of waiting on a thread that no longer exists.
    stale = (time.time() - started_at) > 900
    if current.get("status") == "running" and not stale and not (body and body.force):
        return {"ok": True, "status": "running", "report_id": report_id}
    update_report(report_id, review={"status": "running", "started_at": time.time()})
    threading.Thread(target=_run_review_panel, args=(report_id,), daemon=True).start()
    return {"ok": True, "status": "running", "report_id": report_id}


@router.get("/proposals")
def research_proposals():
    """Suggested research: deterministic triggers over the book + history.

    A PROPOSAL queue, not a scheduler — nothing here runs a model or spends a
    token; the UI offers a one-tap run. Reads through the same public tool layer
    the research itself uses. A trigger that cannot be evaluated (no snapshot,
    no STBR) contributes no reason: absence is a reason, not a flag.
    """
    import time as _time

    from agent.tools import execute_tool
    from data.research_reports import list_reports

    try:
        positions = execute_tool("get_positions", {})
    except Exception as e:
        return {"proposals": [], "error": f"positions unavailable: {e}"}
    if isinstance(positions, dict) and positions.get("error"):
        return {"proposals": [], "error": f"positions unavailable: {positions['error']}"}
    rows = positions.get("positions") if isinstance(positions, dict) else positions
    if not isinstance(rows, list):
        return {"proposals": [], "error": "positions payload not a list"}

    last_by_symbol: dict[str, float] = {}
    for r in list_reports():
        created = r.get("created_at") or 0
        for s in (r.get("symbols") or []):
            key = str(s).upper()
            if created > last_by_symbol.get(key, 0):
                last_by_symbol[key] = created

    now = _time.time()
    proposals: list[dict] = []
    for p in rows[:20]:
        if not isinstance(p, dict):
            continue
        sym = str(p.get("symbol") or "").upper()
        if not sym:
            continue
        reasons: list[str] = []
        ch = p.get("change_pct")
        if isinstance(ch, (int, float)) and abs(ch) >= 6:
            reasons.append(f"moved {ch:+.1f}% today")
        stbr_val = None
        stbr_meta: dict = {}
        try:
            st = execute_tool("get_stbr", {"symbol": sym})
            if isinstance(st, dict) and not st.get("error") and isinstance(st.get("stbr"), (int, float)):
                stbr_val = float(st["stbr"])
                stbr_meta = st
        except Exception:
            stbr_val = None
        if stbr_val is not None and stbr_val >= 1.0:
            label = stbr_meta.get("risk_label") or "elevated"
            reasons.append(f"STBR {stbr_val:.2f} — {label}")
        last = last_by_symbol.get(sym)
        if last is None:
            reasons.append("no research on file")
        else:
            age = (now - last) / 86400.0
            if age > 30:
                reasons.append(f"last research {int(age)}d old")
        if reasons:
            proposals.append({
                "symbol": sym,
                "reasons": reasons,
                "suggested_type": "stock_deep_dive",
                "stbr": stbr_val,
                "last_research_days": int((now - last) / 86400.0) if last else None,
            })
    proposals.sort(key=lambda x: len(x["reasons"]), reverse=True)
    return {"proposals": proposals[:6], "generated_at": now}


@router.get("/types")
def get_research_types():
    from agent.journalist import RESEARCH_TYPES
    return [{"key": k, "label": v} for k, v in RESEARCH_TYPES.items()]
