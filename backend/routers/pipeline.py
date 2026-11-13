"""Idea pipeline endpoints — the Desk Loop spine.

PRIVATE — the pipeline is GLOBAL (the operator's hunt is their own). Gated by
require_auth (fail-closed, matches the private-data-gate audit check).
"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from backend.auth import require_auth

router = APIRouter(prefix="/api", tags=["pipeline"], dependencies=[Depends(require_auth)])


class PipelineBody(BaseModel):
    symbol: str
    stage: str | None = None
    origin: str | None = None
    notes: str | None = None
    conviction: int | None = None
    target_price: float | None = None
    tags: list[str] | None = None


class StageBody(BaseModel):
    stage: str


class LinkBody(BaseModel):
    decision_id: str | None = None
    report_id: str | None = None


class CommitBody(BaseModel):
    action: str = "buy"
    rationale: str | None = None
    size: float | None = None          # target weight (fraction) or dollars
    conviction: int | None = None      # defaults to the item's conviction
    horizon: str | None = None
    direction: str | None = None
    thesis: str | None = None          # optional thesis text recorded on commit
    target_price: float | None = None
    theme: str | None = None
    book: str = "alpha"


@router.get("/pipeline")
def list_pipeline():
    from data.pipeline import get_pipeline
    return {"items": get_pipeline()}


@router.get("/pipeline/{symbol}")
def get_pipeline_item(symbol: str):
    from data.pipeline import get_item
    return {"item": get_item(symbol)}


@router.post("/pipeline")
def upsert_pipeline_item(body: PipelineBody):
    """Create or update a pipeline item. Body carries the symbol."""
    from data.pipeline import upsert_item
    record = upsert_item(body.symbol, body.model_dump(exclude={"symbol"}))
    return {"item": record}


@router.post("/pipeline/{symbol}/stage")
def advance_pipeline_stage(symbol: str, body: StageBody):
    from data.pipeline import advance_stage
    record = advance_stage(symbol, body.stage)
    if record is None:
        return {"available": False, "error": f"No pipeline item for {symbol}"}
    return {"item": record}


@router.post("/pipeline/{symbol}/link")
def link_pipeline_artifact(symbol: str, body: LinkBody):
    """Attach a decision and/or report id to the item."""
    from data.pipeline import link_decision, link_report
    record = None
    if body.decision_id:
        record = link_decision(symbol, body.decision_id)
    if body.report_id:
        record = link_report(symbol, body.report_id) or record
    if record is None:
        return {"available": False, "error": f"No pipeline item for {symbol}"}
    return {"item": record}


@router.post("/pipeline/{symbol}/commit")
def commit_pipeline_item(symbol: str, body: CommitBody):
    """Commit a picked idea: log a decision, record the thesis, advance stage.

    The Desk Loop's closing motion — turns a sized candidate into a
    pre-registered decision (which arms an outcome) and links it back to the
    pipeline item so the dossier shows the whole arc. Requires an existing
    item; if the thesis text is provided, the thesis is recorded and
    ``thesis_ref`` is set on the item.
    """
    from data.decisions import log_decision
    from data.pipeline import get_item, link_decision, link_thesis, upsert_item
    from data.theses import get_thesis, upsert_thesis

    sym = symbol.upper()
    existing_item = get_item(sym)
    if existing_item is None:
        return {"available": False, "error": f"No pipeline item for {sym}"}
    # Conviction defaults to the item's recorded conviction (never invented).
    conviction = body.conviction if body.conviction is not None else existing_item.get("conviction")
    item_data = {"stage": "committed"}
    if body.conviction is not None:
        item_data["conviction"] = body.conviction
    if body.target_price is not None:
        item_data["target_price"] = body.target_price
    if body.thesis:
        item_data["notes"] = body.thesis[:2000]

    upsert_item(sym, item_data)

    # Record the thesis first so decision conviction can resolve from it.
    thesis_record = None
    if body.thesis and body.thesis.strip():
        existing = get_thesis(sym) or {}
        thesis_record = upsert_thesis(sym, {
            "thesis": body.thesis.strip(),
            "conviction": conviction or existing.get("conviction", 3),
            "theme": body.theme if body.theme is not None else existing.get("theme"),
            "target_price": body.target_price if body.target_price is not None else existing.get("target_price"),
            "invalidation_triggers": existing.get("invalidation_triggers", []),
            "compute_exposed": existing.get("compute_exposed", existing.get("compute_linked", False)),
            "compute_index_gate": False,
        })
        link_thesis(sym, sym)

    entry = log_decision(
        type="thesis" if body.thesis else "manual",
        symbol=sym,
        action=body.action,
        rationale=body.rationale,
        book=body.book,
        direction=body.direction,
        size=body.size,
        conviction=conviction,
        horizon=body.horizon,
        source="ui",
    )
    item = link_decision(sym, entry["id"])

    return {"item": item, "decision": entry, "thesis": thesis_record}


@router.delete("/pipeline/{symbol}")
def delete_pipeline_item(symbol: str):
    from data.pipeline import remove_item
    return {"deleted": remove_item(symbol)}
