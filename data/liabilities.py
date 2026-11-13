"""Liability balances held by the operator — debt, not assets.

The operator carries debt the brokerage book cannot see: revolving credit card
statements paid out of cash holdings, an installment car loan at a low rate,
and 0%-APR promotional furniture financing. This store records those balances
so the operator can see short-term vs long-term obligations and how much of
their cash is already committed.

Storage: encrypted JSON at ``data/liabilities.json`` (single-operator,
private). Mirrors the ``data/external_cash.py`` pattern — atomic tmp-rename
writes through ``data/crypto.py`` so the store is encrypted at rest.

``kind`` divides the entries by structure:

  - ``revolving``   — a credit card. ``balance`` is the CURRENT statement
                      balance owed. ``payment`` is what actually leaves cash
                      each cycle; when ``autopay_full`` is set the statement is
                      paid in full, so the service amount is ``balance`` by
                      definition and no interest is carried. Revolving
                      balances are short-term in their entirety.
  - ``installment`` — an amortizing loan (car loan, 0% furniture financing).
                      ``balance`` is remaining principal, ``apr`` the annual
                      percentage rate, ``payment`` the fixed monthly payment.
                      The principal falls due on a schedule, so the balance
                      splits into a current portion (principal due within the
                      next 12 months) and a long-term remainder.
  - ``recurring``   — a repeating commitment that is NOT a balance owed (HOA
                      dues, a subscription, insurance). ``balance`` holds the
                      MONTHLY amount. It claims cash every month and never
                      amortizes, so it carries no APR, term or payoff.
  - ``oneoff``      — a single payment that is NOT a balance owed (a
                      contractor invoice). ``balance`` holds the payment
                      amount. It claims cash once and is not a rate or a
                      schedule.

The first two are DEBT: a balance owed, repaid over time, netted out of
``net_worth``. The last two are COMMITMENTS: money that will leave the
operator's cash without any balance being carried, so they reserve cash but
are never subtracted from net worth. Recording an HOA bill as debt would
understate net worth by its balance FOREVER — it is payable every month and
never repaid. Keeping them in this ledger is still right, because the question
"how much of my cash is actually available?" has the same answer for both.

``is_debt`` on the read contract distinguishes them, so a consumer never has to
know the kind list. For debt rows ``balance`` is owed; for commitment rows it
is the claim.

Legacy/unknown ``kind`` on an existing row normalizes to ``installment`` at
READ time (see ``normalize_kind``) — the conservative classification, because
an unknown debt is assumed to be amortizing rather than assumed to self-clear.
Writes never accept an unknown kind.

This is a PARALLEL LEDGER. Nothing here is subtracted from ``net_liquidation``
— that figure stays the market value of assets (see ``data/account.py``).
Debt is surfaced separately, with ``net_worth = net_liquidation − total debt``
as its own labeled number so a liability can never silently shrink an asset
total. Commitments do not enter that equation at all.
"""

from __future__ import annotations

import math
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

from data.crypto import load_json_store, write_json
from data.session import data_root

# Names are free-form; cap length so a stray paste can't bloat the store.
_MAX_NAME_LEN = 60

# Entry kinds. DEBT kinds carry a BALANCE that is owed and repaid over time;
# COMMITMENT kinds carry a cash CLAIM that is not a balance — it reduces
# available cash without ever being repaid. Adding a kind: extend the right
# tuple, then wire the classification in analysis/liabilities.py and consumers.
DEBT_KINDS: tuple[str, ...] = ("revolving", "installment")
COMMITMENT_KINDS: tuple[str, ...] = ("recurring", "oneoff")
KINDS: tuple[str, ...] = DEBT_KINDS + COMMITMENT_KINDS

# Read-time fallback for a row with a missing/unknown kind. Conservative:
# assume it amortizes (has a real schedule) rather than assume it self-clears.
_DEFAULT_KIND = "installment"

# APR is a percentage, not a fraction. 100% is a sane ceiling; anything above
# is a unit error (someone typed a fraction where a percent belongs).
_MAX_APR_PCT = 100.0

# Due day is capped at 28 so every month has one — a 31 in a 30-day month is
# an ambiguity the store refuses to hold, not something it silently clamps.
_MAX_DUE_DAY = 28

# A term longer than 50 years is a typo, not a mortgage.
_MAX_TERM_MONTHS = 600

# Manual-order slot. The operator arranges their own list; this is the sort key
# for that arrangement, not a date.
_MAX_POSITION = 10_000


def _file() -> Path:
    return data_root() / "liabilities.json"


def store_path() -> Path:
    """Public path accessor — consumers must not import the underscore name
    (data/AGENTS.md invariant 5)."""
    return _file()


def _read() -> list[dict]:
    data = load_json_store(_file(), "liabilities", [])
    return data if isinstance(data, list) else []


def _write(entries: list[dict]) -> None:
    try:
        write_json(_file(), "liabilities", entries)
    except OSError:
        pass


def _normalize_kind(kind: str | None) -> str:
    """Normalize a kind string; None/unknown reads as ``installment``.

    Only ever used on READ. ``_validate`` rejects an unknown kind on write so
    a wrong kind is never accepted silently.
    """
    k = str(kind or "").strip().lower()
    return k if k in KINDS else _DEFAULT_KIND


def normalize_kind(kind: str | None) -> str:
    """Public wrapper — cross-layer consumers must not import underscore names."""
    return _normalize_kind(kind)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean_name(name: str) -> str:
    """Collapse whitespace and bound length. Raises ValueError when empty."""
    cleaned = " ".join(str(name or "").split())
    if not cleaned:
        raise ValueError("name must not be empty")
    if len(cleaned) > _MAX_NAME_LEN:
        raise ValueError(f"name must be ≤ {_MAX_NAME_LEN} characters")
    return cleaned


def _money(v, field: str, *, allow_zero: bool = False) -> float:
    """Coerce a money field to a finite float. Raises ValueError otherwise."""
    try:
        amt = float(v)
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be a number")
    if amt != amt or amt in (float("inf"), float("-inf")):
        raise ValueError(f"{field} must be a finite number")
    if allow_zero:
        if amt < 0:
            raise ValueError(f"{field} must not be negative")
    elif amt <= 0:
        raise ValueError(f"{field} must be a positive number")
    return round(amt, 2)


def _optional_money(v, field: str) -> float | None:
    if v is None or v == "":
        return None
    return _money(v, field)


def _optional_int(v, field: str, *, low: int, high: int) -> int | None:
    if v is None or v == "":
        return None
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be an integer")
    if not (low <= n <= high):
        raise ValueError(f"{field} must be between {low} and {high}")
    return n


def _optional_date(v, field: str) -> str | None:
    """Validate an ISO date (YYYY-MM-DD) and return it normalized."""
    if v is None or v == "":
        return None
    try:
        return date.fromisoformat(str(v).strip()).isoformat()
    except ValueError:
        raise ValueError(f"{field} must be an ISO date (YYYY-MM-DD)")


def _optional_note(v) -> str | None:
    if v is None:
        return None
    note = " ".join(str(v).split())
    return note[:280] or None


def _validate(
    *,
    kind: str,
    name: str,
    balance: float,
    apr: float = 0.0,
    payment: float | None = None,
    due_day: int | None = None,
    autopay_full: bool = False,
    term_months: int | None = None,
    originated_at: str | None = None,
    note: str | None = None,
    position: int | None = None,
    credit_limit: float | None = None,
) -> dict:
    """Validate every field and return a FRESH normalized dict.

    Returns a new dict rather than mutating a caller's — ``data/crypto``'s JSON
    read cache hands out a SHARED object, so a partial in-place edit followed by
    a failed validation would leak the half-mutation into every later reader.

    Raises ValueError on any invalid field. An unknown ``kind`` is rejected
    (the unknown→installment fallback exists ONLY for reading old rows, never
    for writes).
    """
    k = str(kind or "").strip().lower()
    if k not in KINDS:
        raise ValueError(f"kind must be one of: {', '.join(KINDS)}")

    validated = {
        "kind": k,
        "name": _clean_name(name),
        "balance": _money(balance, "balance"),
        "apr": round(_money(apr, "apr", allow_zero=True), 4),
        "payment": _optional_money(payment, "payment"),
        "due_day": _optional_int(due_day, "due_day", low=1, high=_MAX_DUE_DAY),
        "term_months": _optional_int(term_months, "term_months", low=1, high=_MAX_TERM_MONTHS),
        "originated_at": _optional_date(originated_at, "originated_at"),
        "note": _optional_note(note),
        "position": _optional_int(position, "position", low=0, high=_MAX_POSITION),
        "credit_limit": _optional_money(credit_limit, "credit_limit"),
    }

    if validated["apr"] > _MAX_APR_PCT:
        raise ValueError(f"apr must be ≤ {_MAX_APR_PCT} (a percentage, e.g. 4.9)")

    # autopay_full is revolving-only: an amortizing loan is never "paid in full
    # each cycle", so accepting the flag there would create a contradictory row.
    if k == "installment":
        if payment is None or validated["payment"] is None:
            raise ValueError("payment is required for an installment liability")
        validated["autopay_full"] = False
    elif k == "revolving":
        validated["autopay_full"] = bool(autopay_full)
        # A full-pay card needs no separate service amount — paying the
        # statement IS the service, so it is derived in analysis, never stored.
        if validated["autopay_full"]:
            validated["payment"] = None
    else:
        # recurring / oneoff are commitments, not debt: no rate, no schedule,
        # no "paid in full" cycle. Accepting any of those would persist a row
        # that contradicts its own kind, so they are refused rather than
        # silently dropped.
        if validated["apr"]:
            raise ValueError(f"apr does not apply to a {k} entry (it is not a balance)")
        if validated["term_months"] is not None:
            raise ValueError(f"term_months does not apply to a {k} entry (it is not a balance)")
        if validated["originated_at"] is not None:
            raise ValueError(f"originated_at does not apply to a {k} entry (it does not amortize)")
        validated["payment"] = None
        validated["autopay_full"] = False

    # A credit limit only means something on a balance that can be drawn against.
    # On a loan it would be a contradictory row (a loan's "limit" is its ORIGINAL
    # amount, a different field); on a commitment there is no balance at all. Both
    # are refused rather than silently dropped.
    if k != "revolving" and validated["credit_limit"] is not None:
        raise ValueError(f"credit_limit applies only to a revolving balance, not a {k} entry")

    return validated


def _entry_from(validated: dict, *, entry_id: str, created_at: str, updated_at: str) -> dict:
    """Assemble the persisted record from validated fields + provenance."""
    return {
        "id": entry_id,
        "created_at": created_at,
        "updated_at": updated_at,
        **validated,
    }


def _entry_position(entry: dict) -> int | None:
    """The entry's manual-order slot, or None when it has never been arranged."""
    pos = entry.get("position")
    if isinstance(pos, bool) or not isinstance(pos, int):
        return None
    return pos


def _ordered(entries: list[dict]) -> list[dict]:
    """Apply the operator's manual order.

    Rows carrying a ``position`` come first, in position order. Rows without one
    (legacy rows, or a store the operator has never arranged) follow in the
    historical newest-first order — an unarranged list must look exactly as it
    did before this field existed.
    """
    newest_first = list(reversed(entries))
    arranged = [e for e in newest_first if _entry_position(e) is not None]
    arranged.sort(key=lambda e: _entry_position(e) or 0)
    unarranged = [e for e in newest_first if _entry_position(e) is None]
    return arranged + unarranged


def is_debt_kind(kind: str | None) -> bool:
    """Whether this kind is a balance owed (vs a cash commitment)."""
    return _normalize_kind(kind) in DEBT_KINDS


def public_entry(entry: dict) -> dict:
    """The read-contract view of one entry (see ``_public``).

    Write endpoints return entries too, and a consumer that reads
    ``entry["is_debt"]`` off a POST response must get the same shape a GET
    gives it — otherwise the flag exists only until the next round trip.
    """
    return _public(entry)


def _public(entry: dict) -> dict:
    """Normalize an entry for consumers: kind resolved, position always present.

    Legacy rows (written before ``position`` existed) have NO key at all, and a
    consumer reading ``entry["position"]`` would fail on exactly the rows the
    operator cares about. Normalize at read — the same rule as ``kind``.

    ``is_debt`` is derived here too, so every consumer reads one flag instead of
    re-deriving the debt/commitment split from a kind list that will grow.
    """
    kind = _normalize_kind(entry.get("kind"))
    return {
        **entry,
        "kind": kind,
        "is_debt": kind in DEBT_KINDS,
        "position": _entry_position(entry),
        # A row written before credit_limit existed has NO key at all: normalize at
        # read so a consumer never KeyErrors — or silently drops the card from a
        # utilization denominator — on exactly the rows the operator cares about.
        "credit_limit": entry.get("credit_limit"),
    }


def list_liabilities() -> list[dict]:
    """Return all liability entries in the operator's order, kind normalized."""
    return [_public(e) for e in _ordered(_read())]


def reorder_liabilities(ordered_ids: list[str]) -> list[dict]:
    """Persist a manual order for the liability list.

    ``ordered_ids`` is the full desired order. Any existing entry omitted from
    it keeps its relative order and is appended after the listed ones, so a
    partial list from a stale client degrades instead of dropping rows. An id
    that does not exist raises ValueError — a bad order must not be accepted
    silently, and it must not half-apply either: every position is computed
    before anything is written.
    """
    if not isinstance(ordered_ids, list) or not ordered_ids:
        raise ValueError("ids must be a non-empty list")
    ids = [str(i) for i in ordered_ids]

    entries = _read()
    known = {e.get("id") for e in entries}
    unknown = [i for i in ids if i not in known]
    if unknown:
        raise ValueError(f"unknown liability id(s): {', '.join(unknown[:5])}")

    # Preserve order, drop duplicates, then append whatever wasn't listed.
    seen: set[str] = set()
    final: list[str] = []
    for i in ids:
        if i not in seen:
            seen.add(i)
            final.append(i)
    for e in _ordered(entries):
        doc_id = e.get("id")
        if isinstance(doc_id, str) and doc_id not in seen:
            seen.add(doc_id)
            final.append(doc_id)

    slot = {doc_id: idx for idx, doc_id in enumerate(final)}
    now = _now_iso()
    updated = [
        {**e, "position": slot[e["id"]], "updated_at": now} if e.get("id") in slot else e
        for e in entries
    ]
    _write(updated)
    return [_public(e) for e in _ordered(updated)]


def add_liability(**fields) -> dict:
    """Record a liability. See ``_validate`` for the accepted fields.

    A new entry is appended to the end of the operator's list (it gets the next
    manual-order slot) rather than silently taking a spot in the middle, so
    arranging the list by hand is not undone by the next addition. An explicit
    ``position`` is honoured when supplied.

    Raises ValueError on any invalid field — a wrong number or wrong kind must
    never be accepted silently.
    """
    validated = _validate(**fields)
    entries = _read()
    if validated["position"] is None:
        arranged = [p for p in (_entry_position(e) for e in entries) if p is not None]
        validated["position"] = (max(arranged) + 1) if arranged else len(entries)
    now = _now_iso()
    entry = _entry_from(validated, entry_id=uuid.uuid4().hex[:12], created_at=now, updated_at=now)
    entries.append(entry)
    _write(entries)
    return entry


def update_liability(doc_id: str, **fields) -> dict | None:
    """Replace an entry's fields in place, preserving ``created_at`` provenance.

    The whole record is rewritten from validated values (never a partial
    in-place patch). Returns the updated entry, or None when *doc_id* is
    unknown. Raises ValueError on invalid input — same rules as add.

    The entry's manual-order ``position`` is PRESERVED when the incoming
    position is None. An edit form manages a liability's numbers, not the order
    of the list; without this, saving any edit would quietly drop the row back
    into the unarranged block and undo the operator's arrangement.
    """
    validated = _validate(**fields)
    entries = _read()
    for idx, entry in enumerate(entries):
        if entry.get("id") == doc_id:
            if validated["position"] is None:
                validated["position"] = _entry_position(entry)
            updated = _entry_from(
                validated,
                entry_id=doc_id,
                created_at=entry.get("created_at") or _now_iso(),
                updated_at=_now_iso(),
            )
            entries[idx] = updated
            _write(entries)
            return updated
    return None


def remove_liability(doc_id: str) -> bool:
    """Remove an entry by id. Returns True when removed (idempotent)."""
    entries = _read()
    remaining = [e for e in entries if e.get("id") != doc_id]
    if len(remaining) == len(entries):
        return False
    _write(remaining)
    return True


def get_liabilities_total() -> float:
    """Sum of every recorded liability balance — the headline debt figure.

    An unreadable store is a FAILURE, not an empty store: ``load_json_store``
    raises rather than letting a corrupt file read as zero debt.
    """
    total = 0.0
    for e in _read():
        try:
            balance = float(e.get("balance", 0))
        except (TypeError, ValueError):
            continue
        if balance > 0 and math.isfinite(balance):
            total += balance
    return round(total, 2)
