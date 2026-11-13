"""Credit endpoints — score observations from the free bureau apps.

PRIVATE and GLOBAL (the operator's credit file is their own). Gated by
``require_auth`` (fail-closed), same as liabilities.

Read-only view + append. There is deliberately no endpoint that returns "your
credit score": the read contract is one row PER SERIES (model + bureau), because
a FICO 8 from Experian and a VantageScore 3.0 from TransUnion are different
numbers on the same day and any single "score" field would be a fiction.

The router returns validation failures as ``{"error": ...}`` with HTTP 200 (the
existing convention in this codebase), so callers must read the BODY.
"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from backend.auth import require_auth

router = APIRouter(
    prefix="/api/credit",
    tags=["credit"],
    dependencies=[Depends(require_auth)],
)


class ScoreBody(BaseModel):
    score: int
    model: str
    bureau: str
    source: str
    as_of: str
    change: int | None = None
    note: str | None = None


class BulkBody(BaseModel):
    """The backfill path: a list of observations, re-runnable."""

    observations: list[ScoreBody]


@router.get("")
def list_credit():
    """Every observation plus the per-series rollup, the utilization read, the
    recovery clocks, and the integrity/staleness flags."""
    from analysis.credit import project_utilization, recovery_windows, utilization_now
    from data.credit_scores import list_scores, rollup, series_integrity, store_path
    from data.crypto import store_error
    from data.liabilities import list_liabilities

    rows = list_liabilities()
    projection = project_utilization(rows)
    return {
        "observations": list_scores(),
        "rollup": rollup(),
        "integrity": series_integrity(),
        # Utilization is derived from the debt ledger, never stored: a stored ratio
        # drifts from the balances it claims to describe.
        "utilization": utilization_now(rows),
        "recovery": recovery_windows(rows, projection=projection),
        "utilization_projection": projection,
        "error": store_error(store_path()),
    }


@router.post("/scores")
def add_score(body: ScoreBody):
    from data.credit_scores import add_score as _add

    try:
        return {"observation": _add(**body.model_dump())}
    except ValueError as e:
        return {"error": str(e)}


@router.post("/scores/bulk")
def add_scores(body: BulkBody):
    from data.credit_scores import add_scores as _add_many

    try:
        result = _add_many([row.model_dump() for row in body.observations])
    except ValueError as e:
        return {"error": str(e)}
    return {"result": result, "rollup": _rollup()}


@router.delete("/scores/{observation_id}")
def remove_score(observation_id: str):
    from data.credit_scores import remove_score as _remove

    return {"ok": _remove(observation_id)}


def _rollup() -> dict:
    from data.credit_scores import rollup

    return rollup()
