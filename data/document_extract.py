"""LLM-based financial document extraction (the Data page's generic importer).

When a dropped file isn't a recognizable brokerage CSV, this module converts
its content to text and asks the LLM (Ollama-first, deepseek-v4-flash) to
produce a structured record against a schema from ``extract_schemas.py``.

Division of labor:
- pdfplumber does the *mechanical* extraction (text + table rows) — the LLM
  never sees raw PDF bytes.
- The LLM *interprets* the extracted text into the structured JSON.

The output is validated, not trusted: JSON parse, numeric coercion, NaN
rejection, and the schema's reconciliation rule (line-item sum ≈ reported
total). Any failure returns ``{"error": ..., "needs_review": True}`` — a wrong
number is never accepted silently. Same privacy posture as Compass: Ollama
first, prompts processed transiently, never stored.
"""

from __future__ import annotations

import io
import json
import logging
import math
from typing import Any

log = logging.getLogger(__name__)

_MAX_TEXT_CHARS = 12_000  # token budget guard for the LLM call


# ---------------------------------------------------------------------------
# Text conversion (deterministic, no LLM)
# ---------------------------------------------------------------------------


def _pdf_to_text(content: bytes) -> str:
    """Extract text + flattened table rows from a PDF (pdfplumber)."""
    import pdfplumber

    chunks: list[str] = []
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            if text.strip():
                chunks.append(text)
            for table in page.extract_tables() or []:
                for row in table:
                    cells = [c.strip() if c else "" for c in row]
                    chunks.append(" | ".join(cells))
    return "\n".join(chunks)


def _tabular_to_text(content: bytes) -> str:
    """Read a CSV/TSV/XLSX into row-wise text (existing format detector)."""
    from data.chase import _read_any_format

    try:
        df = _read_any_format(content)
    except Exception as e:
        log.warning("Tabular text conversion failed: %s", e)
        return content.decode("utf-8", errors="replace")
    if df.empty:
        return content.decode("utf-8", errors="replace")
    return "\n".join(
        df.astype(str).apply(lambda r: " | ".join(r.values), axis=1).tolist()
    )


def content_to_text(content: bytes, filename: str = "") -> str:
    """Convert an uploaded file's bytes to plain text for the LLM.

    PDF → pdfplumber (text + tables). CSV/TSV/XLSX → row-wise text. Otherwise
    assume plain text (TXT/HTML) and decode as-is. Truncated to _MAX_TEXT_CHARS.
    """
    lower = filename.lower()
    if lower.endswith(".pdf") or content[:5] == b"%PDF-":
        text = _pdf_to_text(content)
    elif lower.endswith((".csv", ".tsv", ".xls", ".xlsx")):
        text = _tabular_to_text(content)
    else:
        text = content.decode("utf-8", errors="replace")
    return text[:_MAX_TEXT_CHARS]


# ---------------------------------------------------------------------------
# LLM output validation
# ---------------------------------------------------------------------------


def _coerce_number(v: Any) -> float | None:
    """Coerce a value to float; None for missing/non-numeric."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        f = float(v)
        return None if (math.isnan(f) or math.isinf(f)) else f
    s = str(v).replace(",", "").replace("$", "").strip()
    if s in ("", "-", "n/a", "null", "None"):
        return None
    try:
        f = float(s)
        return None if (math.isnan(f) or math.isinf(f)) else f
    except ValueError:
        return None


def _clean_record(record: dict, schema: dict) -> dict:
    """Coerce numbers in a parsed record against the schema's field types."""
    fields = schema.get("fields", {})
    out: dict[str, Any] = {}
    for key, spec in fields.items():
        if spec.get("type") == "array":
            items = record.get(key) or []
            out[key] = [
                _clean_record(e, {"fields": spec.get("items", {})})
                for e in items if isinstance(e, dict)
            ]
            continue
        if spec.get("type") == "number":
            out[key] = _coerce_number(record.get(key))
        else:
            v = record.get(key)
            out[key] = v if isinstance(v, str) else (str(v) if v is not None else None)
    return out


def _validate_extraction(raw_text: str, schema: dict) -> dict:
    """Parse + coerce + reconcile the LLM output. Never fabricates.

    Returns a clean record on success, or {"error", "needs_review": True}.
    """
    # Strip markdown fences the model sometimes adds.
    text = raw_text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        text = text.rsplit("```", 1)[0]
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as e:
        return {"error": f"LLM returned malformed JSON: {e}", "needs_review": True}
    if not isinstance(parsed, dict):
        return {"error": "LLM output was not an object", "needs_review": True}

    cleaned = _clean_record(parsed, schema)
    required = [k for k, spec in schema.get("fields", {}).items()
                if spec.get("required")]
    missing = [k for k in required if cleaned.get(k) is None]
    if missing:
        return {"error": f"Missing required fields: {', '.join(missing)}",
                "needs_review": True}

    reconcile = schema.get("reconcile")
    if reconcile and not reconcile(cleaned):
        return {"error": "Line-item totals do not reconcile with the reported total",
                "needs_review": True}

    return cleaned


# ---------------------------------------------------------------------------
# Extraction entry point
# ---------------------------------------------------------------------------


def _build_prompt(schema: dict) -> list[dict]:
    from data.extract_schemas import _SCHEMA_PROMPT

    fields_json = json.dumps(schema.get("fields", {}), indent=2)
    return [
        {
            "role": "system",
            "content": (
                f"{_SCHEMA_PROMPT}\n\n"
                f"Target JSON schema:\n{fields_json}"
            ),
        },
        {
            "role": "user",
            "content": (
                f"The document is {schema.get('prompt_name', 'a financial statement')}.\n"
                f"{schema.get('prompt', '')}\n"
                "Statement text:\n{document_text}\n\n"
                "Return ONLY the JSON object."
            ),
        },
    ]


def extract_financial_document(content: bytes, filename: str = "") -> dict:
    """Extract a structured financial record from a file via the LLM.

    Returns the validated record dict on success, or
    {"error": str, "needs_review": True} on any failure. Never fabricates.
    """
    from agent.compute import get_client
    from data.extract_schemas import get_schema, infer_schema

    text = content_to_text(content, filename)
    if not text.strip():
        return {"error": "No readable text in file", "needs_review": True}

    schema_key = infer_schema(filename, text)
    schema = get_schema(schema_key)
    if schema is None:
        return {"error": f"Unknown schema: {schema_key}", "needs_review": True}

    try:
        client, model = get_client()
    except Exception as e:
        return {"error": f"LLM unavailable: {e}", "needs_review": True}

    messages = _build_prompt(schema)
    messages[1]["content"] = messages[1]["content"].replace(
        "{document_text}", text[: _MAX_TEXT_CHARS]
    )

    try:
        resp = client.chat(model=model, messages=messages, stream=False, role="chat")
        content_out = resp.get("message", {}).get("content", "")
    except Exception as e:
        log.warning("LLM extraction call failed: %s", e)
        return {"error": f"LLM extraction failed: {e}", "needs_review": True}

    record = _validate_extraction(content_out, schema)
    if "error" in record:
        return record
    record["source"] = schema_key
    record["schema_name"] = schema.get("name", schema_key)
    return record
