"""Chaining a card's statements through the charge ledger — a check that can fail.

The identity, for two adjacent statements on one card:

    prev.statement_balance + charges(prev.period_end < d <= cur.period_end)
        - cur.payments_credited
        == cur.statement_balance

Every term comes from a different place: the balances and payments from what the
issuer reported, the charges from the operator's own expense rows. So the check is
NOT a tautology of a construction — it goes red when a charge is missing, doubled,
misdated or booked to the wrong card, which is exactly what it is for.

Deliberately NOT done here: comparing the last statement against the live
liability balance. Payments since the last statement are not recorded anywhere
(the cash ledger has no link to a card), so any such figure would be invented. The
honest output is "reconciled as of <period_end>", not a current balance.

Pure functions: no I/O, no storage.
"""

from __future__ import annotations

TOLERANCE = 0.01


def _r(x: float) -> float:
    return round(float(x), 2)


def _next_day(day: str) -> str:
    """The day after a YYYY-MM-DD. Statements are contiguous when start == this."""
    from datetime import date, timedelta

    return (date.fromisoformat(day) + timedelta(days=1)).isoformat()


def chain(statements: list[dict], expenses: list[dict], account: str | None = None) -> dict:
    """Reconcile adjacent statements for one card through its charges.

    ``statements`` are that card's rows (any order — sorted here). ``expenses``
    may include other cards; rows are filtered by account.

    Returns a report rather than raising. Three states, kept distinct because
    collapsing them is how a gap gets presented as a clean result:

      ok                 every adjacent pair reconciles within tolerance
      drift              a pair did not reconcile; each entry carries both the
                         reported balance and what the ledger implies
      unattributable     a pair that cannot be checked at all — the periods do not
                         touch, or no charges exist to span them. Reported with a
                         reason, never silently skipped.
    """
    rows = sorted(statements, key=lambda r: str(r.get("period_end", "")))
    accounts = {str(r.get("account")) for r in rows}
    if account is not None:
        accounts = accounts or {account}

    report: dict = {
        "ok": True,
        "accounts": sorted(a for a in accounts if a),
        "pairs_checked": 0,
        "drift": [],
        "unattributable": [],
        "as_of": str(rows[-1].get("period_end", "")) if rows else "",
        "tolerance": TOLERANCE,
    }

    if len(rows) < 2:
        report["unattributable"].append({
            "reason": "fewer_than_two_statements",
            "note": (
                "A chain needs two statements. One statement cannot be checked "
                "against itself — that would be a tautology, not a reconciliation."
            ),
        })
        report["ok"] = False
        return report

    charges_by_account: dict[str, list[dict]] = {}
    for e in expenses:
        charges_by_account.setdefault(str(e.get("account")), []).append(e)

    for prev, cur in zip(rows, rows[1:]):
        acct = str(cur.get("account"))
        if str(prev.get("account")) != acct:
            report["unattributable"].append({
                "account": acct,
                "period_end": str(cur.get("period_end")),
                "reason": "different_account",
                "note": "Adjacent statements belong to different cards.",
            })
            continue

        prev_end = str(prev.get("period_end", ""))
        cur_end = str(cur.get("period_end", ""))
        cur_start = str(cur.get("period_start", ""))

        # Periods must actually touch. A gap means charges inside it belong to
        # neither statement, so the arithmetic would be wrong in a way that looks
        # like drift. Contiguous means start == the day after the previous end.
        if cur_start and prev_end and cur_start > _next_day(prev_end):
            report["unattributable"].append({
                "account": acct,
                "period_end": cur_end,
                "reason": "period_gap",
                "note": (
                    f"Statement starting {cur_start} does not continue from "
                    f"{prev_end}; charges in the gap belong to neither."
                ),
            })
            continue

        charges = [
            e for e in charges_by_account.get(acct, [])
            if prev_end < str(e.get("charge_date", "")) <= cur_end
        ]
        if not charges:
            report["unattributable"].append({
                "account": acct,
                "period_end": cur_end,
                "reason": "no_charges_in_window",
                "note": (
                    "No expense rows fall between these statements, so the "
                    "balance difference cannot be attributed. That is a coverage "
                    "gap, not a zero."
                ),
            })
            continue

        charged = sum(float(e.get("amount") or 0) for e in charges)
        expected = _r(float(prev.get("statement_balance") or 0) + charged
                      - float(cur.get("payments_credited") or 0))
        reported = _r(cur.get("statement_balance") or 0)
        delta = _r(reported - expected)

        report["pairs_checked"] += 1
        if abs(delta) > TOLERANCE:
            report["drift"].append({
                "account": acct,
                "from_period_end": prev_end,
                "to_period_end": cur_end,
                "prev_balance": _r(prev.get("statement_balance") or 0),
                "charges": _r(charged),
                "payments_credited": _r(cur.get("payments_credited") or 0),
                "expected": expected,
                "reported": reported,
                "drift": delta,
                "charge_rows": len(charges),
            })

    report["ok"] = not report["drift"] and not report["unattributable"]
    return report
