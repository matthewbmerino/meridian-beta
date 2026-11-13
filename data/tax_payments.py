"""Quarterly federal estimated-tax payments — append-only, plus the schedule.

A payment is a fact: date, amount, which quarter it settles, a note. Written
once; a mistake is corrected with a reversal row naming the original (the
cash-ledger rule), never an edit. The SCHEDULE is the operator's stated plan —
$2,500 a quarter as stated 2026-10-15 — editable, never inferred from balances.
"""

from __future__ import annotations

import datetime
import uuid
from pathlib import Path

from data.crypto import load_json_store, write_json

DEFAULT_SCHEDULE = {"quarterly_amount": 2500.0, "year": 2026}
# (quarter, month, day, year_offset): Q1-Q3 in-year, Q4 the following January.
DUE_ANCHORS = (("Q1", 4, 15, 0), ("Q2", 6, 15, 0), ("Q3", 9, 15, 0), ("Q4", 1, 15, 1))

#: Store identity for AES-GCM key derivation. Every other data/ store names one;
#: this file was the single exception that wrote plaintext (mode 0644) next to
#: the encryption secret.
_IDENTITY = "tax_payments"


def _file() -> Path:
    return Path(__file__).resolve().parent / "tax_payments.json"


def _read() -> dict:
    data = load_json_store(_file(), _IDENTITY, {"schedule": dict(DEFAULT_SCHEDULE), "payments": []})
    if not isinstance(data, dict):
        data = {}
    data.setdefault("schedule", dict(DEFAULT_SCHEDULE))
    data.setdefault("payments", [])
    return data


def _write(d: dict) -> None:
    write_json(_file(), _IDENTITY, d)


def get_schedule() -> dict:
    return _read()["schedule"]


def set_schedule(quarterly_amount, year=None) -> dict:
    try:
        amt = float(quarterly_amount)
    except (TypeError, ValueError):
        raise ValueError("quarterly_amount must be a number")
    if not (0 < amt <= 1_000_000):
        raise ValueError("quarterly_amount must be between 0 and 1,000,000")
    y = int(year) if year else get_schedule().get("year", datetime.date.today().year)
    if not (2000 <= y <= 2100):
        raise ValueError("year looks wrong")
    d = _read()
    d["schedule"] = {"quarterly_amount": round(amt, 2), "year": y}
    _write(d)
    return d["schedule"]


def _parse_date(v) -> datetime.date:
    if v is None or v == "":
        raise ValueError("paid_date is required (YYYY-MM-DD)")
    s = str(v).strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"unrecognized date: {v!r} — use YYYY-MM-DD")


def infer_quarter(paid: datetime.date, year: int | None = None) -> str:
    """Which quarter a payment settles, by the first due date on or after it.

    Matches IRS practice: a payment made by Apr 15 settles Q1, by Jun 15 Q2,
    by Sep 15 Q3, by Jan 15 of the next year Q4.
    """
    y = year or paid.year
    for q, month, day, offset in DUE_ANCHORS:
        due = datetime.date(y + offset, month, day)
        if paid <= due:
            return f"{y}-{q}"
    return f"{y + 1}-Q1"


def list_payments() -> list[dict]:
    return list(_read()["payments"])


def add_payment(paid_date, amount, quarter: str | None = None,
                note: str = "", correction_of: str | None = None) -> dict:
    paid = _parse_date(paid_date)
    try:
        amt = float(amount)
    except (TypeError, ValueError):
        raise ValueError("amount must be a number")
    if amt == 0:
        raise ValueError("amount must not be zero")
    if amt < 0 and not correction_of:
        raise ValueError("a negative amount is only valid as a correction (correction_of required)")
    if amt > 0 and correction_of:
        raise ValueError("a correction must be negative — it reverses, it does not re-pay")

    d = _read()
    if correction_of and not any(p.get("id") == correction_of for p in d["payments"]):
        raise ValueError(f"no payment with id {correction_of!r} to correct")

    entry = {
        "id": uuid.uuid4().hex[:12],
        "paid_date": paid.isoformat(),
        "amount": round(amt, 2),
        "quarter": quarter or infer_quarter(paid),
        "note": (note or "").strip(),
        "correction_of": correction_of,
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    d["payments"].append(entry)
    _write(d)
    return entry
