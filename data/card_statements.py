"""Card statements — the anchor points a charge ledger chains through.

An expense row says what was charged and when. A statement says what the card
issuer claimed the balance was at a point in time, and how much of it they
credited as paid. Two adjacent statements are therefore a closed book: everything
between them must be explained by charges plus payments.

That is why the settlement lives HERE and not in ``cash_movements``. A link field
on the cash ledger could never be backfilled — rows are append-only — so a
tie-out built from it would work only from the day it shipped, and would silently
claim the past was reconciled. Carrying the figures on the statement means the
check works retroactively and needs no link at all.

Rules (mirroring ``data/cash_movements.py``):

  - APPEND-ONLY. A wrong statement is corrected by appending a superseding one
    with a note naming the original id; both stay visible.
  - ``amount`` fields are stored as reported, positive, in account currency.
  - ``period_start`` ≤ ``period_end`` is enforced, not assumed.
  - Provenance on every row: ``source`` plus an optional issuer reference.

Storage: encrypted JSON at ``data/card_statements.json`` via ``data/crypto.py``,
path resolved through ``data.session.data_root()``.
"""

from __future__ import annotations

import math
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

from data.crypto import load_json_store, write_json
from data.session import data_root

MAX_ACCOUNT_LEN = 60
MAX_NOTE_LEN = 280
MAX_REF_LEN = 120

SOURCES: tuple[str, ...] = ("statement", "operator")


def _file() -> Path:
    return data_root() / "card_statements.json"


def store_path() -> Path:
    """Public accessor — cross-layer consumers must not import the underscore name."""
    return _file()


def _read() -> list[dict]:
    data = load_json_store(_file(), "card_statements", [])
    return data if isinstance(data, list) else []


def _write(rows: list[dict]) -> None:
    try:
        write_json(_file(), "card_statements", rows)
    except OSError:
        pass


def _norm_date(value, field: str) -> str:
    """Normalize to YYYY-MM-DD. A bad date is REFUSED, never guessed."""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field} is required (YYYY-MM-DD)")
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").date().isoformat()
    except ValueError as e:
        raise ValueError(f"{field} must be a YYYY-MM-DD date") from e


def _money(value, field: str, *, allow_negative: bool) -> float:
    try:
        amt = float(value)
    except (TypeError, ValueError) as e:
        raise ValueError(f"{field} must be a number") from e
    if not math.isfinite(amt):
        raise ValueError(f"{field} must be finite")
    if amt < 0 and not allow_negative:
        # A negative balance is a credit, which the model does not represent yet.
        # Refuse rather than coerce it to zero: a silently zeroed balance would
        # reconcile perfectly and be wrong.
        raise ValueError(f"{field} must not be negative")
    return round(amt, 2)


def add_statement(
    account: str,
    period_start,
    period_end,
    statement_balance,
    payments_credited=0.0,
    source: str = "operator",
    issuer_ref: str | None = None,
    note: str | None = None,
) -> dict:
    """Append one statement. Raises ValueError on anything invalid."""
    acct = " ".join(str(account).split())
    if not acct:
        raise ValueError("account must not be empty")
    if len(acct) > MAX_ACCOUNT_LEN:
        raise ValueError(f"account must be ≤ {MAX_ACCOUNT_LEN} characters")

    start = _norm_date(period_start, "period_start")
    end = _norm_date(period_end, "period_end")
    if start > end:
        raise ValueError("period_start must not be after period_end")

    bal = _money(statement_balance, "statement_balance", allow_negative=False)
    paid = _money(payments_credited, "payments_credited", allow_negative=False)

    src = str(source or "operator").strip().lower()
    if src not in SOURCES:
        raise ValueError(f"source must be one of: {', '.join(SOURCES)}")

    ref = " ".join(str(issuer_ref).split()) if issuer_ref else ""
    if len(ref) > MAX_REF_LEN:
        raise ValueError(f"issuer_ref must be ≤ {MAX_REF_LEN} characters")

    n = " ".join(str(note).split()) if note else ""
    if len(n) > MAX_NOTE_LEN:
        raise ValueError(f"note must be ≤ {MAX_NOTE_LEN} characters")

    row = {
        "id": uuid.uuid4().hex[:12],
        "account": acct,
        "period_start": start,
        "period_end": end,
        "statement_balance": bal,
        "payments_credited": paid,
        "source": src,
        "issuer_ref": ref,
        "note": n,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    rows = _read()
    rows.append(row)
    _write(rows)
    return row


def list_statements(account: str | None = None, limit: int | None = None) -> list[dict]:
    """Statements oldest first by period_end — the order a chain is read in."""
    rows = _read()
    if account:
        want = " ".join(str(account).split())
        rows = [r for r in rows if str(r.get("account")) == want]
    rows.sort(key=lambda r: (str(r.get("period_end", "")), str(r.get("created_at", ""))))
    return rows[:limit] if limit else rows
