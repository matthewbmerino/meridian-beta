"""Import file upload and management endpoints."""

import asyncio
import io
import logging

from fastapi import APIRouter, File, UploadFile
from pydantic import BaseModel

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/imports", tags=["imports"])

# Upload guardrails: a brokerage CSV is a few hundred KB at most; PDFs can be
# larger. Reject oversized uploads (memory exhaustion) and unsupported types.
_MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # 20 MB
_ALLOWED_CONTENT_TYPES = {
    "text/csv",
    "text/plain",
    "application/csv",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/octet-stream",  # browsers often send this for .csv
    "application/pdf",
    "application/x-pdf",
}


@router.get("/schwab-sync")
def schwab_sync_status():
    """State of the automated Schwab position pull (never an account number)."""
    from data.schwab_sync import status

    return status()


@router.post("/schwab-sync")
def schwab_sync_now():
    """Pull now. Returns the same shape as /status, plus whether it wrote."""
    from data.schwab_sync import sync

    result = sync(reason="operator")
    return result


@router.get("/list")
def list_imports():
    from data.imports import list_activity_imports, list_external_imports, list_imports
    return {
        "positions": list_imports(),
        "activity": list_activity_imports(),
        "external": list_external_imports(),
    }


@router.post("/upload")
async def upload_file(file: UploadFile = File(...)):
    from data.cache import invalidate
    from data.chase import parse_chase_csv, parse_schwab_positions
    from data.imports import add_activity_import, add_external_import, add_import
    from data.realized import parse_realized_csv

    content = await file.read()
    if len(content) > _MAX_UPLOAD_BYTES:
        return {"ok": False, "error": (
            f"File too large ({len(content) / 1e6:.1f} MB) — "
            f"max {_MAX_UPLOAD_BYTES / 1e6:.0f} MB"
        )}
    ctype = (file.content_type or "").lower()
    if ctype and ctype not in _ALLOWED_CONTENT_TYPES:
        return {"ok": False, "error": f"Unsupported file type: {ctype}"}
    filename = file.filename or "upload.csv"
    # Sanitize the stored filename — never trust the client's path.
    filename = filename.replace("\\", "/").split("/")[-1][:120]

    parsed = _try_parse(lambda: parse_realized_csv(content, filename))
    if parsed is not None:
        lot_df, summary_df, total = parsed
    else:
        lot_df = summary_df = None
        total = 0.0
    if lot_df is not None and not lot_df.empty:
        doc_id = add_activity_import(filename, lot_df, summary_df, total)
        invalidate("get_combined_positions")
        invalidate("get_combined_summary")
        return {"ok": True, "id": doc_id, "type": "activity", "total_pl": total}

    positions_df, summary = _try_parse(lambda: parse_chase_csv(io.BytesIO(content))) or (None, None)
    if positions_df is not None and not positions_df.empty:
        doc_id = add_import(filename, positions_df, summary)
        invalidate("get_combined_positions")
        invalidate("get_combined_summary")
        return {"ok": True, "id": doc_id, "type": "positions", "count": len(positions_df)}

    positions_df, summary = _try_parse(lambda: parse_schwab_positions(io.BytesIO(content))) or (None, None)
    if positions_df is not None and not positions_df.empty:
        doc_id = add_import(filename, positions_df, summary)
        invalidate("get_combined_positions")
        invalidate("get_combined_summary")
        return {"ok": True, "id": doc_id, "type": "positions", "count": len(positions_df)}

    # Non-brokerage file (PDF, unrecognized CSV, statement): LLM extraction.
    # to_thread copies contextvars (run_in_executor does NOT), so the session
    # ContextVar is visible inside the thread — the extraction's LLM call and
    # storage stay session-scoped (same pattern as compass/research).
    record = await asyncio.to_thread(
        _extract_document, content, filename
    )
    if record is None:
        return {"ok": False, "error": "Could not parse file"}

    needs_review = bool(record.get("needs_review"))
    if "error" in record:
        return {
            "ok": False,
            "error": record["error"],
            "needs_review": needs_review,
            "type": "external",
        }

    doc_id = add_external_import(filename, record)
    invalidate("get_combined_positions")
    invalidate("get_combined_summary")
    return {
        "ok": True,
        "id": doc_id,
        "type": "external",
        "total_net": record.get("total_net"),
        "source": record.get("source", "external_other"),
        "needs_review": needs_review,
        "entry_count": len(record.get("entries") or []),
    }


def _try_parse(fn):
    """Run a deterministic parser; any failure falls through to LLM extraction.

    A degenerate file (single-token text, malformed CSV) must never 500 — it
    becomes a candidate for extraction instead.
    """
    try:
        return fn()
    except Exception as e:
        log.warning("Deterministic import parse failed; falling through: %s", e)
        return None


def _extract_document(content: bytes, filename: str) -> dict | None:
    """LLM-extract a non-brokerage file. Returns the record or None."""
    from data.document_extract import extract_financial_document
    try:
        return extract_financial_document(content, filename)
    except Exception as e:
        log.warning("Document extraction failed for %s: %s", filename, e)
        return None


@router.patch("/toggle/{doc_id}")
def toggle_visibility(doc_id: str):
    from data.cache import invalidate
    from data.imports import toggle_visibility
    new_state = toggle_visibility(doc_id)
    invalidate("get_combined_positions")
    invalidate("get_combined_summary")
    return {"visible": new_state}


@router.delete("/{doc_id}")
def delete_import(doc_id: str):
    from data.cache import invalidate
    from data.imports import remove_import
    remove_import(doc_id)
    invalidate("get_combined_positions")
    invalidate("get_combined_summary")
    return {"ok": True}

# ---------------------------------------------------------------------------
# Manual external P&L (no document — operator-asserted, provenance kept)
# ---------------------------------------------------------------------------


class ManualExternalPLBody(BaseModel):
    amount: float
    source: str
    description: str
    as_of: str
    note: str | None = None


@router.post("/manual-external")
def add_manual_external(body: ManualExternalPLBody):
    from data.cache import invalidate
    from data.external_pl_manual import add_manual_external_pl
    try:
        entry = add_manual_external_pl(
            amount=body.amount,
            source=body.source,
            description=body.description,
            as_of=body.as_of,
            note=body.note,
        )
    except ValueError as e:
        return {"error": str(e)}
    invalidate("get_combined_summary")
    return {"entry": entry}


@router.patch("/manual-external/{entry_id}")
def update_manual_external(entry_id: str, body: ManualExternalPLBody):
    """Edit in place — the recurring-entry path: bump the amount + date on
    the same YTD row instead of delete+recreate."""
    from data.cache import invalidate
    from data.external_pl_manual import update_manual_external_pl
    try:
        entry = update_manual_external_pl(
            entry_id,
            amount=body.amount,
            source=body.source,
            description=body.description,
            as_of=body.as_of,
            note=body.note,
        )
    except ValueError as e:
        return {"error": str(e)}
    if entry is None:
        return {"error": "entry not found"}
    invalidate("get_combined_summary")
    return {"entry": entry}


@router.delete("/manual-external/{entry_id}")
def remove_manual_external(entry_id: str):
    from data.cache import invalidate
    from data.external_pl_manual import remove_manual_external_pl
    ok = remove_manual_external_pl(entry_id)
    if ok:
        invalidate("get_combined_summary")
    return {"ok": ok}
