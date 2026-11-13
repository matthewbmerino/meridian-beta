"""Manual external-P&L entries — gains/losses known by operator assertion.

Interest from banks without downloadable statements (Marcus savings), referral
bonuses, cashback — money events the operator knows but has no document for.
The document-extraction path (``add_external_import``) is for statements; this
store is for *asserted* numbers, always provenance-tagged: source + as_of are
mandatory so the entry can be audited later (which statement cycle, whose
number, when).

Merged into the external-P&L ledger by ``data.imports.get_all_external_pl``
(so it flows into realized totals + the by-source breakdown) and listed by
the agents via ``list_manual_external_pl``.

Storage: encrypted JSON at ``data/external_pl_manual.json`` (single-operator,
private) — same crypto pattern as ``data/theses.py``.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from pathlib import Path

from data.crypto import load_json_store, write_json

_MAX_TEXT = 200

_MAX_AMOUNT = 10_000_000.0  # sanity bound — a bank statement is never this


def _file() -> Path:
    from data.session import data_root
    return data_root() / "external_pl_manual.json"


def _read() -> list[dict]:
    data = load_json_store(_file(), "external_pl_manual", [])
    return data if isinstance(data, list) else []


def _write(entries: list[dict]) -> None:
    try:
        write_json(_file(), "external_pl_manual", entries)
    except OSError:
        pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean_text(v, label: str, required: bool = False) -> str:
    s = " ".join(str(v).split()) if v is not None else ""
    if required and not s:
        raise ValueError(f"{label} must not be empty")
    if len(s) > _MAX_TEXT:
        raise ValueError(f"{label} must be ≤ {_MAX_TEXT} characters")
    return s


def _clean_amount(v) -> float:
    """Signed money: P&L can be negative. But never zero (a no-op event has
    nothing to reflect) and never absurd."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise ValueError("amount must be a finite number")
    if f != f or f in (float("inf"), float("-inf")):
        raise ValueError("amount must be a finite number")
    if f == 0:
        raise ValueError("amount must be non-zero")
    if abs(f) > _MAX_AMOUNT:
        raise ValueError("amount exceeds sanity bound — check the number")
    return round(f, 2)


def _clean_date(v, label: str, required: bool) -> str | None:
    if v is None or v == "":
        if required:
            raise ValueError(f"{label} must be a YYYY-MM-DD date")
        return None
    s = str(v).strip()
    try:
        return date.fromisoformat(s).isoformat()
    except ValueError:
        raise ValueError(f"{label} must be a YYYY-MM-DD date")


def list_manual_external_pl() -> list[dict]:
    """All manual entries, newest event first."""
    return sorted(_read(), key=lambda e: str(e.get("as_of") or ""), reverse=True)


def add_manual_external_pl(
    amount: float,
    source: str,
    description: str,
    as_of: str,
    note: str | None = None,
) -> dict:
    """Record an asserted P&L event.

    ``source`` is the source FAMILY (e.g. ``bank_interest`` — see
    ``extract_schemas._SOURCE_HINTS``/frontend ``EXTERNAL_SOURCE_LABELS`` for
    the label map), ``description`` names the payer/payer account, ``as_of``
    the event date (statement month-end for interest). Raises ValueError on
    any invalid field.
    """
    entry = {
        "id": uuid.uuid4().hex[:12],
        "source": _clean_text(source, "source", required=True),
        "description": _clean_text(description, "description", required=True),
        "amount": _clean_amount(amount),
        "as_of": _clean_date(as_of, "as_of", required=True),
        "note": _clean_text(note, "note"),
        "created_at": _now_iso(),
    }
    entries = _read()
    entries.append(entry)
    _write(entries)
    return entry


def update_manual_external_pl(entry_id: str, amount: float, source: str,
                              description: str, as_of: str,
                              note: str | None = None) -> dict | None:
    """Edit an entry in place — amount, source, description, as_of, note.

    The recurring-entry workflow: this month's YTD edit is "change the
    amount, bump the date" on the same row — provenance stays one entry
    instead of a delete+recreate that churns the id. ``created_at`` is
    preserved (when it was first asserted); ``updated_at`` re-stamps.
    Raises ValueError on any invalid field; returns None when *entry_id*
    doesn't exist.
    """
    # Validate EVERYTHING up front, then mutate a fresh copy: data.crypto's
    # JSON read cache hands the caller a SHARED object, so a partial in-place
    # edit followed by a failed validation would leak the half-mutation into
    # every later read (observed live: a rejected amount=100 persisted).
    amt = _clean_amount(amount)
    src = _clean_text(source, "source", required=True)
    desc = _clean_text(description, "description", required=True)
    when = _clean_date(as_of, "as_of", required=True)
    note_clean = _clean_text(note, "note")
    entries = _read()
    updated = None
    new_entries = []
    for entry in entries:
        if entry.get("id") == entry_id:
            updated = {**entry, "amount": amt, "source": src, "description": desc,
                       "as_of": when, "note": note_clean, "updated_at": _now_iso()}
            new_entries.append(updated)
        else:
            new_entries.append(entry)
    if updated is not None:
        _write(new_entries)
    return updated


def remove_manual_external_pl(entry_id: str) -> bool:
    entries = _read()
    remaining = [e for e in entries if e.get("id") != entry_id]
    if len(remaining) == len(entries):
        return False
    _write(remaining)
    return True


def manual_pl_rows() -> tuple[list[dict], float]:
    """Rows shaped like get_all_external_pl's output + their total.

    ``filename`` is empty (no document), ``schema_name`` carries the
    description — the label fallback chain in externalSourceLabel renders it.
    """
    rows = []
    total = 0.0
    for e in list_manual_external_pl():
        amt = float(e["amount"])
        total += amt
        rows.append({
            "source": e["source"],
            "schema_name": e["description"],
            "total_net": amt,
            "needs_review": False,   # asserted by the operator, not extracted
            "filename": "",
            "period_start": e["as_of"],
            "period_end": e["as_of"],
            "id": e["id"],
            "manual": True,
        })
    return rows, round(total, 2)
