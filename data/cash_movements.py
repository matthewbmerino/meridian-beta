"""Cash movements — the movement history the balance stores cannot keep.

``external_cash.json`` records one BALANCE per institution and edits it in
place, so a payment lands as a silent change: no date, no delta, no reason.
This store is the opposite by construction — append-only rows with signed
amounts, a closed tag set and mandatory provenance, so a balance is *explained*
by its movements instead of replaced by them.

Rules (mirroring the append-only marks in ``data/alternatives.py``):

  - APPEND-ONLY. There is no update and no delete: a movement is a fact about
    the past. A wrong row is corrected by adding a reversing row (same
    institution, opposite sign, description naming the original id).
  - Signed amounts: negative = money OUT, positive = money IN. Zero is refused
    — a zero movement is not a movement.
  - Provenance on every row: ``as_of`` (when the money moved), ``source``
    (``statement`` — read off the institution's own record — or ``operator``),
    and ``created_at`` (when the row was written). A balance figure is only as
    good as these three.
  - ``tag`` is a CLOSED set so the deployed-capital rollup can group it.
  - Reconciliation is REPORTED, never enforced: ``external_cash`` stays the
    asserted truth, and a drift between (opening + movements) and the balance
    comes back with the institution's name on it.

Storage: encrypted JSON at ``data/cash_movements.json`` via ``data/crypto.py``
(the ``data/external_cash.py`` pattern), the path resolved through
``data.session.data_root()``.
"""

from __future__ import annotations

import math
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

from data.crypto import load_json_store, write_json
from data.session import data_root

MAX_INSTITUTION_LEN = 60
MAX_DESCRIPTION_LEN = 120
MAX_NOTE_LEN = 280

# Closed tag set. ``opening`` seeds reconciliation (the balance a ledger starts
# from — never invented, always the asserted balance on a stated date);
# ``renovation`` and ``sale_cost`` are the two the operator itemizes; the rest
# catch everything else without inventing a category for it.
TAGS: tuple[str, ...] = ("opening", "renovation", "sale_cost", "income", "interest", "transfer", "other")

# Where the number came from. ``statement`` = the institution's own record,
# ``operator`` = the operator asserted it. Both are legal; neither is optional.
SOURCES: tuple[str, ...] = ("statement", "operator")


def _file() -> Path:
    return data_root() / "cash_movements.json"


def store_path() -> Path:
    """Public accessor — cross-layer consumers must not import the underscore name."""
    return _file()


def _read() -> list[dict]:
    data = load_json_store(_file(), "cash_movements", [])
    return data if isinstance(data, list) else []


def _write(rows: list[dict]) -> None:
    try:
        write_json(_file(), "cash_movements", rows)
    except OSError:
        pass


def _norm_date(value) -> str:
    """Normalize an as_of to YYYY-MM-DD. A bad date is REFUSED, never guessed."""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value or "").strip()
    if not text:
        raise ValueError("as_of is required (YYYY-MM-DD)")
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").date().isoformat()
    except ValueError as e:
        raise ValueError("as_of must be a YYYY-MM-DD date") from e


def _validate(
    as_of, institution: str, amount, description: str, tag: str, source: str, note: str | None
) -> tuple[str, str, float, str, str, str, str]:
    day = _norm_date(as_of)
    name = " ".join(str(institution).split())
    if not name:
        raise ValueError("institution must not be empty")
    if len(name) > MAX_INSTITUTION_LEN:
        raise ValueError(f"institution must be ≤ {MAX_INSTITUTION_LEN} characters")
    try:
        amt = float(amount)
    except (TypeError, ValueError) as e:
        raise ValueError("amount must be a number") from e
    if not math.isfinite(amt) or amt == 0:
        raise ValueError("amount must be a non-zero finite number (negative = money out)")
    desc = " ".join(str(description).split())
    if not desc:
        raise ValueError("description must not be empty")
    if len(desc) > MAX_DESCRIPTION_LEN:
        # REFUSED, never truncated: a description that silently loses its tail
        # still looks correct, and the operator's own words are the provenance.
        raise ValueError(f"description must be ≤ {MAX_DESCRIPTION_LEN} characters")
    t = str(tag or "").strip().lower()
    if t not in TAGS:
        raise ValueError(f"tag must be one of: {', '.join(TAGS)}")
    src = str(source or "operator").strip().lower()
    if src not in SOURCES:
        raise ValueError(f"source must be one of: {', '.join(SOURCES)}")
    n = " ".join(str(note).split()) if note else ""
    if len(n) > MAX_NOTE_LEN:
        raise ValueError(f"note must be ≤ {MAX_NOTE_LEN} characters")
    return day, name, round(amt, 2), desc, t, src, n


def add_movement(
    as_of,
    institution: str,
    amount,
    description: str,
    tag: str = "other",
    source: str = "operator",
    note: str | None = None,
) -> dict:
    """Append one movement. Raises ValueError on anything invalid.

    Nothing is normalized silently beyond whitespace and sign-preserving
    rounding: a wrong tag, a bad date, a zero amount or an over-long
    description must never be accepted, and must never be trimmed into shape.
    """
    day, name, amt, desc, t, src, n = _validate(as_of, institution, amount, description, tag, source, note)
    row = {
        "id": uuid.uuid4().hex[:12],
        "as_of": day,
        "institution": name,
        "amount": amt,
        "description": desc,
        "tag": t,
        "source": src,
        "note": n,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    rows = _read()
    rows.append(row)
    _write(rows)
    return row


def list_movements(
    institution: str | None = None,
    tag: str | None = None,
    since: str | None = None,
    limit: int | None = None,
) -> list[dict]:
    """Movements newest first (by as_of, then write time), optionally filtered.

    ``since`` is an inclusive YYYY-MM-DD lower bound; an unknown tag simply
    matches nothing rather than erroring — a filter that cannot match is empty,
    not a failure.
    """
    rows = _read()
    if institution:
        want = " ".join(str(institution).split())
        rows = [r for r in rows if str(r.get("institution")) == want]
    if tag:
        want_tag = str(tag).strip().lower()
        rows = [r for r in rows if str(r.get("tag")) == want_tag]
    if since:
        bound = _norm_date(since)
        rows = [r for r in rows if str(r.get("as_of", "")) >= bound]
    rows.sort(key=lambda r: (str(r.get("as_of", "")), str(r.get("created_at", ""))), reverse=True)
    return rows[:limit] if limit else rows
