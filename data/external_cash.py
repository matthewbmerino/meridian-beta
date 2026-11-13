"""External balances held at other institutions.

The operator holds balances at institutions that don't provide a holdings CSV
(banks, savings accounts, other brokerages) and at operators where a balance
is committed and illiquid (sportsbook stakes on open bets). This store records
those balances so the Overview can feature them and the totals can reflect
true total wealth.

Storage: encrypted JSON at ``data/external_cash.json`` (single-operator,
private). Mirrors the ``data/watchlist.py`` pattern — atomic tmp-rename
writes through ``data/crypto.py`` so the store is encrypted at rest.

``kind`` divides the entries by liquidity:

  - ``bank``       — liquid cash at a bank/brokerage. Rolls into
                     ``cash_balance`` and ``net_liquidation``.
  - ``sportsbook`` — funds at a sportsbook: withdrawable balance plus open
                     bets at stake (cost basis). Binary-outcome, illiquid
                     between placement and settlement. Rolls into
                     ``net_liquidation`` (NAV) but NOT ``cash_balance``. See
                     ``get_cash_totals``. Settled results flow through the
                     sportsbook statement import (``extract_schemas.py``)
                     into realized P&L — NAV is honest at cost, P&L at
                     settlement.

Missing/unknown ``kind`` on legacy entries is ``bank`` (old rows behaved
exactly this way). See ``data/account.py get_combined_summary``:
  - external balances roll into ``net_liquidation``,
  - ``bank`` kind also rolls into ``cash_balance``,
  - nothing here rolls into ``buying_power`` (not deployable at the
    brokerage),
  - when ``cash_override`` is set, it wins over both imports cash and
    external balances (no double counting).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path

from data.crypto import load_json_store, write_json
from data.session import data_root

# Institution names are free-form; cap length so a stray paste can't bloat the
# store. Amounts must be positive finite dollars.
_MAX_INSTITUTION_LEN = 60

# Entry kinds. ``bank`` is liquid cash; ``sportsbook`` is illiquid at stake.
# Adding a kind: extend this tuple, then wire the liquidity classification in
# get_cash_totals() and the account breakdown / summary consumers.
KINDS: tuple[str, ...] = ("bank", "sportsbook")

# Kinds counted as liquid cash (roll into cash_balance).
LIQUID_KINDS: frozenset[str] = frozenset({"bank"})


def _file() -> Path:
    return data_root() / "external_cash.json"


def _read() -> list[dict]:
    data = load_json_store(_file(), "external_cash", [])
    return data if isinstance(data, list) else []


def _write(entries: list[dict]):
    try:
        write_json(_file(), "external_cash", entries)
    except OSError:
        pass


def _normalize_kind(kind: str | None) -> str:
    """Normalize a kind string; None/unknown falls back to ``bank``.

    Legacy entries (pre-kind) have no field and are liquid bank balances —
    defaulting to ``bank`` preserves their old behavior exactly.
    """
    k = str(kind or "").strip().lower()
    return k if k in KINDS else "bank"


def normalize_kind(kind: str | None) -> str:
    """Public wrapper — cross-layer consumers must not import underscore names."""
    return _normalize_kind(kind)


def list_external_cash() -> list[dict]:
    """Return all external-balance entries (newest first)."""
    return list(reversed(_read()))


def _validate(
    institution: str, amount: float, kind: str | None = None
) -> tuple[str, float, str]:
    """Validate + normalize an (institution, amount, kind) triple.

    Raises ValueError on an empty institution, a non-positive/non-finite
    amount, or an unrecognized kind — a wrong value must never be accepted
    silently (the unknown→bank fallback exists ONLY for reading legacy rows,
    never for writes). Shared by add and update so the two can never drift.
    """
    name = " ".join(str(institution).split())
    if not name:
        raise ValueError("institution must not be empty")
    if len(name) > _MAX_INSTITUTION_LEN:
        raise ValueError(f"institution must be ≤ {_MAX_INSTITUTION_LEN} characters")
    amt = float(amount)
    if not (amt > 0) or amt != amt or amt == float("inf"):
        raise ValueError("amount must be a positive finite number")
    if kind is not None:
        k = str(kind).strip().lower()
        if k not in KINDS:
            raise ValueError(f"kind must be one of: {', '.join(KINDS)}")
    else:
        k = "bank"
    return name, round(amt, 2), k


def add_external_cash(institution: str, amount: float, kind: str = "bank") -> dict:
    """Record a balance held at *institution*.

    ``kind`` selects the liquidity classification — ``bank`` (liquid cash,
    counts toward cash_balance) or ``sportsbook`` (illiquid: withdrawable
    balance + open bets at stake, NAV only). Raises ValueError on an empty
    institution, a non-positive/non-finite amount, or an unrecognized kind —
    a wrong number or wrong kind must never be accepted silently.
    """
    name, amt, k = _validate(institution, amount, kind)

    entry = {
        "id": uuid.uuid4().hex[:12],
        "institution": name,
        "kind": k,
        "amount": amt,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    entries = _read()
    entries.append(entry)
    _write(entries)
    return entry


def update_external_cash(
    doc_id: str, institution: str, amount: float, kind: str | None = None
) -> dict | None:
    """Update an entry's institution + amount (+ kind) in place.

    Preserves ``created_at`` (the record's provenance). ``kind=None`` keeps
    the entry's existing kind (legacy callers unchanged). Returns the updated
    entry, or None when *doc_id* doesn't exist. Raises ValueError on invalid
    input — same rules as add.
    """
    name, amt, k = _validate(institution, amount, kind if kind is not None else "bank")
    entries = _read()
    for entry in entries:
        if entry.get("id") == doc_id:
            entry["institution"] = name
            entry["amount"] = amt
            if kind is not None:
                entry["kind"] = k
            _write(entries)
            return entry
    return None


def remove_external_cash(doc_id: str) -> bool:
    """Remove an entry by id. Returns True when removed (idempotent)."""
    entries = _read()
    remaining = [e for e in entries if e.get("id") != doc_id]
    if len(remaining) == len(entries):
        return False
    _write(remaining)
    return True


def get_external_cash_total() -> float:
    """Sum of ALL recorded external balances, every kind (NAV figure)."""
    total = 0.0
    for e in _read():
        try:
            amt = float(e.get("amount", 0))
        except (TypeError, ValueError):
            continue
        if amt > 0:
            total += amt
    return round(total, 2)


def get_cash_totals() -> dict:
    """Split external balances by liquidity classification.

    Returns ``{"total": ..., "liquid": ..., "illiquid": ...}`` where
    ``liquid`` sums the LIQUID_KINDS (bank — rolls into cash_balance) and
    ``illiquid`` the rest (sportsbook stakes — NAV only). ``total`` is the
    NAV figure, identical to ``get_external_cash_total()``.
    """
    liquid = 0.0
    illiquid = 0.0
    for e in _read():
        try:
            amt = float(e.get("amount", 0))
        except (TypeError, ValueError):
            continue
        if amt <= 0:
            continue
        if _normalize_kind(e.get("kind")) in LIQUID_KINDS:
            liquid += amt
        else:
            illiquid += amt
    return {
        "total": round(liquid + illiquid, 2),
        "liquid": round(liquid, 2),
        "illiquid": round(illiquid, 2),
    }
