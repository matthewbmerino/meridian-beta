"""Stated yields and instrument terms per cash position — a settings store, not a ledger.

The run-rate, the blended rate and the liquidity profile are only as honest as
the terms they use, so every term lives here in one place and is labelled as
STATED: the institution's advertised APY plus what the balance actually is (a
checking account, a bill, a term CD), because "3.5%" means different things at
0 days and at 365. Interest actually RECEIVED is a movement tagged ``interest``
in ``cash_movements`` — this store never pretends to know it.

The tax assumption lives here too. It is an INPUT the operator states, never a
rate this module assumes: absent, every after-tax figure degrades to ``None``
and the surface says so.
"""

from __future__ import annotations

import datetime as dt

from data.crypto import load_json_store, write_json
from data.session import data_root

# Instrument vocabulary — ONE table. The store validates against it, the router
# ships it to the frontend so the picker cannot drift from what the analysis
# understands, and ``analysis.cash_position`` classifies liquidity from the same
# spec (passed in, so the analysis layer imports nothing).
#
# ``days`` is days to cash WITHOUT penalty:
#     0     same-day (checking, savings, brokerage sweep)
#     1     T+1 (a money fund, or a bill sold today)
#     7     a no-penalty CD's lock-up window
#     None  a term CD — resolved from its own maturity date
#     999   no defined exit ("other")
# ``state_exempt`` is the tax treatment of the INTEREST, not of the account:
# Treasury obligations are exempt from state and local income tax, bank interest
# is not. That single flag is what lets a 4.21% bill beat a 4.40% CD in New
# York, so it belongs in the vocabulary rather than in a UI toggle.
INSTRUMENTS: dict[str, dict] = {
    "savings": {"label": "High-yield savings", "days": 0, "state_exempt": False},
    "checking": {"label": "Checking", "days": 0, "state_exempt": False},
    "brokerage": {"label": "Brokerage cash", "days": 0, "state_exempt": False},
    "money_market": {"label": "Money market fund", "days": 1, "state_exempt": False},
    "treasury_mmf": {"label": "Treasury money fund", "days": 1, "state_exempt": True},
    "tbill": {"label": "T-bill", "days": 1, "state_exempt": True},
    "cd_no_penalty": {"label": "CD (no penalty)", "days": 7, "state_exempt": False},
    "cd": {"label": "CD (term)", "days": None, "state_exempt": False},
    "other": {"label": "Other", "days": 999, "state_exempt": False},
}

# Legacy rows carry no instrument. A bank balance with no stated type is a
# deposit account, so it reads as savings (0 days) rather than "other" (999) —
# defaulting the other way would file the operator's largest callable balance
# under "locked" and misstate the liquidity profile on the very first load.
DEFAULT_INSTRUMENT = "savings"

_MAX_LABEL_LEN = 80
_UNSET = object()


def _file():
    return data_root() / "cash_yields.json"


def _blank() -> dict:
    return {"version": 2, "assumptions": {}, "positions": {}}


def _clean_label(label: str) -> str:
    out = (label or "").strip()
    if not out:
        raise ValueError("label is required")
    if len(out) > _MAX_LABEL_LEN:
        raise ValueError(f"label must be {_MAX_LABEL_LEN} characters or fewer")
    return out


def _opt_float(v) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def _normalize_maturity(v) -> str | None:
    """ISO date or None. A date-only field — never an instant."""
    if v is None or v == "":
        return None
    text = str(v)[:10]
    try:
        dt.date.fromisoformat(text)
    except ValueError as e:
        raise ValueError("maturity must be an ISO date (YYYY-MM-DD)") from e
    return text


def _record(apy, instrument, maturity) -> dict:
    inst = instrument if instrument in INSTRUMENTS else DEFAULT_INSTRUMENT
    a = _opt_float(apy)
    if a is not None and not (0 <= a <= 25):
        raise ValueError("apy must be between 0 and 25")
    return {
        "apy": round(a, 3) if a is not None else None,
        "instrument": inst,
        "maturity": _normalize_maturity(maturity),
    }


def _normalize(raw: dict) -> dict:
    """Read-shape any on-disk generation into ``{version, assumptions, positions}``.

    v1 wrote a bare ``{label: apy}`` map. A legacy row is missing the keys
    ENTIRELY, so they are filled here at read — a consumer doing
    ``rec["instrument"]`` would raise KeyError on exactly the operator's live
    rows, and a ``.get()`` downstream would silently drop them instead.
    """
    if not isinstance(raw, dict):
        return _blank()
    if "positions" in raw or "assumptions" in raw:
        positions = raw.get("positions") or {}
        assumptions = raw.get("assumptions") or {}
    else:
        positions = {k: {"apy": v} for k, v in raw.items() if isinstance(v, (int, float))}
        assumptions = {}

    clean: dict[str, dict] = {}
    for label, rec in positions.items():
        if not isinstance(label, str) or not label.strip():
            continue
        if isinstance(rec, (int, float)):
            rec = {"apy": rec}
        if not isinstance(rec, dict):
            continue
        try:
            clean[label] = _record(rec.get("apy"), rec.get("instrument"), rec.get("maturity"))
        except ValueError:
            # A corrupt row must not take the whole store down: keep the balance
            # visible with no stated terms rather than raising on every read.
            clean[label] = {"apy": None, "instrument": DEFAULT_INSTRUMENT, "maturity": None}

    return {
        "version": 2,
        "assumptions": {
            "federal": _opt_float(assumptions.get("federal")),
            "state_local": _opt_float(assumptions.get("state_local")),
        },
        "positions": clean,
    }


def _read() -> dict:
    raw = load_json_store(_file(), "cash_yields", {})
    return _normalize(raw)


def _write(d: dict) -> None:
    write_json(_file(), "cash_yields", d)


def list_positions() -> dict[str, dict]:
    """label -> {apy, instrument, maturity}. Every key is always present."""
    return _read()["positions"]


def list_yields() -> dict[str, float]:
    """label -> apy, for callers that only need the rate."""
    return {k: v["apy"] for k, v in _read()["positions"].items() if v.get("apy") is not None}


def get_assumptions() -> dict:
    """Stated marginal tax rates: {federal, state_local}, either may be None."""
    return _read()["assumptions"]


def instrument_catalog() -> list[dict]:
    """The picker payload — ordered so the common bank types lead."""
    return [{"key": k, "label": v["label"], "days": v["days"]} for k, v in INSTRUMENTS.items()]


def upsert_position(label: str, apy=_UNSET, instrument=_UNSET, maturity=_UNSET) -> dict:
    """Set any subset of one position's terms; omitted fields are preserved.

    Everything is validated BEFORE the store is written: ``data.crypto``'s JSON
    read cache returns a shared object, so a half-validated in-place edit would
    leak into every later reader.
    """
    key = _clean_label(label)
    d = _read()
    current = d["positions"].get(key) or _record(None, DEFAULT_INSTRUMENT, None)
    record = _record(
        current["apy"] if apy is _UNSET else apy,
        current["instrument"] if instrument is _UNSET else instrument,
        current["maturity"] if maturity is _UNSET else maturity,
    )
    d["positions"][key] = record
    _write(d)
    return record


def set_assumptions(federal=_UNSET, state_local=_UNSET) -> dict:
    """State the marginal tax rates used for the after-tax view."""
    d = _read()
    current = d["assumptions"]

    def check(v, ceiling: float, name: str):
        if v is _UNSET:
            return current.get(name)
        if v is None:
            return None
        f = _opt_float(v)
        if f is None or not (0 <= f <= ceiling):
            raise ValueError(f"{name} must be between 0 and {ceiling:.0f}")
        return round(f, 3)

    d["assumptions"] = {
        "federal": check(federal, 60, "federal"),
        "state_local": check(state_local, 30, "state_local"),
    }
    _write(d)
    return d["assumptions"]
