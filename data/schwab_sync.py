"""Keep the Schwab holdings current — the CSV upload, automated.

Meridian's book is built from *visible positions imports*, and the operator has
been exporting a Schwab CSV after every trade. The Trader API returns the same
holdings on demand, so this module maintains ONE live Schwab snapshot:

  - ``sync()`` pulls ``/trader/v1/accounts`` and maps the positions onto the
    canonical import schema (``data.chase.build_position_row``), so the
    combined summary, the book, utilization and NAV all work unchanged.
  - A new import is written ONLY when something that matters changed:
    symbol, quantity, average cost, cash. Market value moves every tick and
    the positions endpoint reprices off stored quantities, so a file per tick
    would be pure noise.
  - Writing a snapshot makes the PREVIOUS Schwab positions import invisible.
    ``get_all_visible_positions`` concatenates every visible import, so two
    live snapshots of one account would double the book.

A grant WITHOUT the "Accounts and Trading Production" product answers
``401 Client not authorized`` while every quote call stays healthy; that is
recorded as ``accounts_access: false`` and nothing is touched, because an
entitlement gap must never read as "the account is empty".
"""

from __future__ import annotations

import datetime
import hashlib
import json
from pathlib import Path

from data.crypto import load_json_store, write_json
from data.session import data_root

#: The broker label every snapshot this module writes carries.
SOURCE = "Schwab"

_STATE_FILE = "schwab_sync.json"

#: Status codes that mean "this app lacks the product", not "the call broke".
_ENTITLEMENT_CODES = ("401", "403")


def _state_path() -> Path:
    return data_root() / _STATE_FILE


def _read_state() -> dict:
    state = load_json_store(_state_path(), "schwab_sync", {})
    return state if isinstance(state, dict) else {}


def _write_state(state: dict) -> dict:
    write_json(_state_path(), "schwab_sync", state)
    return state


def fingerprint(positions_df, summary: dict) -> str:
    """Identity of a holdings snapshot — what is HELD, not what it is worth.

    Quantity, average cost and cash decide the ledger. Market value moves every
    tick, so including it would mint an import on every price change.
    """
    rows = []
    if positions_df is not None and len(positions_df):
        for row in positions_df.to_dict("records"):
            rows.append([
                str(row.get("symbol") or "").upper(),
                str(row.get("asset_type") or ""),
                round(float(row.get("quantity") or 0), 6),
                round(abs(float(row.get("avg_cost") or 0)), 4),
                round(float(row.get("strike") or 0), 4),
                str(row.get("expiration") or ""),
                str(row.get("put_call") or ""),
            ])
    rows.sort()
    payload = {"rows": rows, "cash": round(float(summary.get("cash_balance") or 0), 2)}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _commit(state: dict, *, now, reason: str, ok: bool, **fields) -> dict:
    """Persist one run's outcome, keeping the last good fingerprint on failures."""
    new = dict(state)
    new.update({k: v for k, v in fields.items() if v is not None})
    new["last_run_at"] = now.isoformat()
    new["ok"] = bool(ok)
    new["reason"] = reason
    new["runs"] = int(state.get("runs") or 0) + 1
    new["consecutive_failures"] = 0 if ok else int(state.get("consecutive_failures") or 0) + 1
    if fields.get("changed"):
        new["last_change_at"] = now.isoformat()
    return _write_state(new)


def sync(*, reason: str = "scheduler") -> dict:
    """Pull Schwab holdings and persist a snapshot only when they changed."""
    from data import schwab
    from data.imports import add_import, latest_positions_import, set_visible

    now = datetime.datetime.now(datetime.timezone.utc)
    state = _read_state()

    if not schwab.configured():
        return _commit(state, now=now, reason=reason, ok=False,
                       error="Schwab is not configured (SCHWAB_APP_KEY/SECRET missing)")

    try:
        accounts = schwab.get_accounts()
    except schwab.SchwabError as exc:
        message = str(exc)
        entitled = not any(code in message for code in _ENTITLEMENT_CODES)
        return _commit(
            state, now=now, reason=reason, ok=False,
            accounts_access=entitled,
            error=(message if entitled else
                   "Schwab grant lacks the Accounts and Trading product "
                   "(quotes still work; enable it on the app to pull holdings)"),
        )

    positions_df, summary = schwab.positions_from_accounts(accounts)
    marks = fingerprint(positions_df, summary)

    live = latest_positions_import(SOURCE)
    live_id = (live or {}).get("id")

    if marks == state.get("fingerprint") and live_id and state.get("import_id") == live_id:
        return _commit(state, now=now, reason=reason, ok=True, changed=False,
                       accounts_access=True, error=None,
                       positions=int(len(positions_df)),
                       cash=summary.get("cash_balance"),
                       net_liquidation=summary.get("net_liquidation"))

    filename = f"schwab-api-{now:%Y-%m-%dT%H%M%SZ}.csv"
    doc_id = add_import(filename, positions_df, summary)

    if live_id and live_id != doc_id:
        set_visible(live_id, False)

    try:
        from data.cache import invalidate
        invalidate("get_combined_positions")
        invalidate("get_combined_summary")
    except Exception:  # cache is a nicety; a miss must not lose the snapshot
        pass

    return _commit(state, now=now, reason=reason, ok=True, changed=True,
                   accounts_access=True, error=None,
                   import_id=doc_id, fingerprint=marks,
                   positions=int(len(positions_df)),
                   cash=summary.get("cash_balance"),
                   net_liquidation=summary.get("net_liquidation"))


def status() -> dict:
    """Everything the operator needs to judge the pull — no account numbers."""
    from data import schwab

    state = _read_state()
    try:
        grant = schwab.token_status()
    except Exception as exc:  # a broken token store must not hide the pull state
        grant = {"connected": False, "error": str(exc)}
    return {
        "configured": bool(schwab.configured()),
        "grant": grant,
        "accounts_access": state.get("accounts_access"),
        "last_ok": state.get("ok"),
        "last_run_at": state.get("last_run_at"),
        "last_change_at": state.get("last_change_at"),
        "import_id": state.get("import_id"),
        "positions": state.get("positions"),
        "cash": state.get("cash"),
        "net_liquidation": state.get("net_liquidation"),
        "runs": state.get("runs"),
        "consecutive_failures": state.get("consecutive_failures"),
        "error": state.get("error"),
    }
