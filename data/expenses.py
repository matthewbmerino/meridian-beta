"""Expenses — spend by charge date, on an accrual basis.

``cash_movements`` records when money MOVED. This store records when the expense
was INCURRED. The two are deliberately different stores because conflating them
double-counts: the $11,632.48 "Chase Freedom Unlimited statement paid in full" is
a movement, not spending — it settles spending that was already recorded here. Sum
both and the month is overstated by the payment.

Rules (mirroring ``data/cash_movements.py``):

  - APPEND-ONLY. No update, no delete. A wrong row is corrected by appending a
    reversing row (opposite sign, description naming the original id) and, if
    needed, the corrected row. Both stay visible — that is the point.
  - Signed amounts: positive = spend, negative = refund/credit. Zero is refused.
  - ``charge_date`` is the date the card was charged, never the payment date.
  - Provenance on every row: ``source`` (``statement`` — read off the card's own
    record — or ``operator``) plus ``statement_id`` when it came from one.
  - ``category`` is a CLOSED set so the monthly rollup can group it.

TWO THINGS THIS STORE MUST NOT DO:

  1. Record a card payment. A payment is a transfer to a liability; the expense
     was recorded when the charge landed. Payments belong in ``cash_movements``.
  2. Record the whole of a loan instalment. The Tesla $620 is $575.60 principal
     (a balance reduction, not an expense) and $44.40 interest (an expense). Book
     the interest only, or every month is inflated by the principal.

Storage: encrypted JSON at ``data/expenses.json`` via ``data/crypto.py``, path
resolved through ``data.session.data_root()``.
"""

from __future__ import annotations

import math
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

from data.crypto import load_json_store, write_json
from data.session import data_root

MAX_ACCOUNT_LEN = 60
MAX_MERCHANT_LEN = 80
MAX_DESCRIPTION_LEN = 160
MAX_NOTE_LEN = 280

# Closed set. Deliberately says ``interest`` and not ``debt service``: a
# "debt service" bucket invites booking the full instalment, which buries the
# principal inside spending. Only interest and fees are expenses.
CATEGORIES: tuple[str, ...] = (
    "housing",
    "utilities",
    "food",
    "transport",
    "health",
    "insurance",
    "interest",
    "fees",
    "discretionary",
    "one-off",
)

# Where the number came from. Neither is optional.
SOURCES: tuple[str, ...] = ("statement", "operator")


def _file() -> Path:
    return data_root() / "expenses.json"


def store_path() -> Path:
    """Public accessor — cross-layer consumers must not import the underscore name."""
    return _file()


def _read() -> list[dict]:
    data = load_json_store(_file(), "expenses", [])
    return data if isinstance(data, list) else []


def _write(rows: list[dict]) -> None:
    try:
        write_json(_file(), "expenses", rows)
    except OSError:
        pass


def _norm_date(value) -> str:
    """Normalize a charge_date to YYYY-MM-DD. A bad date is REFUSED, never guessed."""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value or "").strip()
    if not text:
        raise ValueError("charge_date is required (YYYY-MM-DD)")
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").date().isoformat()
    except ValueError as e:
        raise ValueError("charge_date must be a YYYY-MM-DD date") from e


def _validate(
    charge_date,
    account: str,
    merchant: str,
    amount,
    category: str,
    description: str | None,
    source: str,
    statement_id: str | None,
    note: str | None,
) -> tuple[str, str, str, float, str, str, str, str, str]:
    day = _norm_date(charge_date)

    acct = " ".join(str(account).split())
    if not acct:
        raise ValueError("account must not be empty")
    if len(acct) > MAX_ACCOUNT_LEN:
        raise ValueError(f"account must be ≤ {MAX_ACCOUNT_LEN} characters")

    merch = " ".join(str(merchant).split())
    if not merch:
        raise ValueError("merchant must not be empty")
    if len(merch) > MAX_MERCHANT_LEN:
        raise ValueError(f"merchant must be ≤ {MAX_MERCHANT_LEN} characters")

    try:
        amt = float(amount)
    except (TypeError, ValueError) as e:
        raise ValueError("amount must be a number") from e
    if not math.isfinite(amt) or amt == 0:
        raise ValueError("amount must be a non-zero finite number (positive = spend)")

    cat = str(category or "").strip().lower()
    if cat not in CATEGORIES:
        raise ValueError(f"category must be one of: {', '.join(CATEGORIES)}")

    desc = " ".join(str(description).split()) if description else ""
    if len(desc) > MAX_DESCRIPTION_LEN:
        # REFUSED, never truncated: a description that silently loses its tail
        # still looks correct, and the operator's own words are the provenance.
        raise ValueError(f"description must be ≤ {MAX_DESCRIPTION_LEN} characters")

    src = str(source or "operator").strip().lower()
    if src not in SOURCES:
        raise ValueError(f"source must be one of: {', '.join(SOURCES)}")

    stmt = " ".join(str(statement_id).split()) if statement_id else ""

    n = " ".join(str(note).split()) if note else ""
    if len(n) > MAX_NOTE_LEN:
        raise ValueError(f"note must be ≤ {MAX_NOTE_LEN} characters")

    return day, acct, merch, round(amt, 2), cat, desc, src, stmt, n


def add_expense(
    charge_date,
    account: str,
    merchant: str,
    amount,
    category: str,
    description: str | None = None,
    source: str = "operator",
    statement_id: str | None = None,
    note: str | None = None,
) -> dict:
    """Append one expense. Raises ValueError on anything invalid.

    Nothing is normalized silently beyond whitespace and sign-preserving
    rounding: a wrong category, a bad date or a zero amount must never be
    accepted, and must never be trimmed into shape.
    """
    day, acct, merch, amt, cat, desc, src, stmt, n = _validate(
        charge_date, account, merchant, amount, category, description, source, statement_id, note
    )
    row = {
        "id": uuid.uuid4().hex[:12],
        "charge_date": day,
        "account": acct,
        "merchant": merch,
        "amount": amt,
        "category": cat,
        "description": desc,
        "source": src,
        "statement_id": stmt,
        "note": n,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    rows = _read()
    rows.append(row)
    _write(rows)
    return row


def list_expenses(
    account: str | None = None,
    category: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int | None = None,
) -> list[dict]:
    """Expenses newest first (by charge_date, then write time), optionally filtered.

    ``since``/``until`` are inclusive YYYY-MM-DD bounds. An unknown category
    simply matches nothing rather than erroring — a filter that cannot match is
    empty, not a failure.
    """
    rows = _read()
    if account:
        want = " ".join(str(account).split())
        rows = [r for r in rows if str(r.get("account")) == want]
    if category:
        want_cat = str(category).strip().lower()
        rows = [r for r in rows if str(r.get("category")) == want_cat]
    if since:
        lo = _norm_date(since)
        rows = [r for r in rows if str(r.get("charge_date", "")) >= lo]
    if until:
        hi = _norm_date(until)
        rows = [r for r in rows if str(r.get("charge_date", "")) <= hi]
    rows.sort(key=lambda r: (str(r.get("charge_date", "")), str(r.get("created_at", ""))), reverse=True)
    return rows[:limit] if limit else rows


def total(rows: list[dict]) -> float:
    """Signed sum. Refunds subtract, because a refund is not negative spending."""
    return round(sum(float(r.get("amount") or 0) for r in rows), 2)


def by_category(rows: list[dict]) -> dict[str, float]:
    """Category -> signed total, sorted high to low. Empty categories are omitted."""
    out: dict[str, float] = {}
    for r in rows:
        cat = str(r.get("category") or "")
        out[cat] = out.get(cat, 0.0) + float(r.get("amount") or 0)
    return {k: round(v, 2) for k, v in sorted(out.items(), key=lambda kv: -kv[1])}


def by_month(rows: list[dict]) -> dict[str, float]:
    """``YYYY-MM`` -> signed total, oldest first. The trend series."""
    out: dict[str, float] = {}
    for r in rows:
        month = str(r.get("charge_date") or "")[:7]
        if len(month) != 7:
            continue
        out[month] = out.get(month, 0.0) + float(r.get("amount") or 0)
    return {k: round(v, 2) for k, v in sorted(out.items())}
