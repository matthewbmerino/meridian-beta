"""Extraction schema registry for LLM-based financial-document parsing.

When a dropped file isn't a recognizable brokerage CSV, the LLM extracts a
structured record from its text. This registry is the contract between the
extraction prompt and the validation gate: each source declares the JSON
schema the LLM must return, the reconciliation rule that must hold, and the
prompt fragment describing the source's statement format.

Adding a new source (prediction market, staking, casino) is one entry here —
no new plumbing. The generic ``external_other`` entry is the fallback for
anything the source sniffer can't classify.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Reconciliation helpers
# ---------------------------------------------------------------------------


def _reconcile_nets(record: dict, tolerance_abs: float = 0.5,
                    tolerance_pct: float = 0.01) -> bool:
    """Σ(entry.net) must equal record.total_net within tolerance.

    The LLM's line-item sum must agree with the reported total, or the import
    is flagged for review — a wrong number is never accepted silently.
    """
    total = record.get("total_net")
    entries = record.get("entries") or []
    if total is None:
        return False
    if not entries:
        return True  # no line items to cross-check
    nets = [e.get("net") for e in entries if isinstance(e.get("net"), (int, float))]
    if len(nets) != len(entries):
        return False  # an entry is missing its net — incomplete
    diff = abs(sum(nets) - total)
    return diff <= max(tolerance_abs, tolerance_pct * abs(total))


def _no_entries_reconcile(record: dict) -> bool:
    """Generic schema without line items — nothing to cross-check."""
    return record.get("total_net") is not None


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------

_SCHEMA_PROMPT = (
    "Extract the requested financial fields from the statement text. "
    "Return ONLY a single valid JSON object, no prose, no markdown fences. "
    "Rules: never invent or guess a number — if a field is not present in the "
    "statement, set it to null. Parse money with $ signs and commas. "
    "Dates as YYYY-MM-DD. Every entry in `entries` must carry all its fields."
)

EXTRACT_SCHEMAS: dict[str, dict] = {
    "sportsbook": {
        "name": "Sportsbook Winnings",
        "prompt_name": "a sportsbook (Fanduel/DraftKings/Fanatics) statement",
        "prompt": (
            "The statement is from a sports betting operator. It lists bets or "
            "a period summary. Extract total_net (net profit/loss for the "
            "period), total_staked (sum of wagers), total_payout (sum of "
            "payouts/winnings before netting), period_start, period_end, and "
            "per-bet entries. A bet entry: date placed, description (the "
            "market/game, e.g. 'Chiefs -7.5'), stake (amount wagered), payout "
            "(amount returned including stake), net (payout minus stake). "
            "If the statement only shows a summary total, set entries to []."
        ),
        "fields": {
            "total_net": {"type": "number", "required": True},
            "total_staked": {"type": "number"},
            "total_payout": {"type": "number"},
            "period_start": {"type": "date"},
            "period_end": {"type": "date"},
            "entries": {"type": "array", "items": {
                "date": {"type": "date"},
                "description": {"type": "string"},
                "stake": {"type": "number"},
                "payout": {"type": "number"},
                "net": {"type": "number"},
            }},
        },
        "reconcile": _reconcile_nets,
    },
    "external_other": {
        "name": "Other External P&L",
        "prompt_name": "a financial statement or payout summary",
        "prompt": (
            "The document is a financial payout/income statement (not a "
            "brokerage). Extract total_net (net profit or loss for the period), "
            "period_start, period_end, a one-line description, and optional "
            "entries if the document lists individual transactions. An entry: "
            "date, description, net. If no line items are shown, set entries to []."
        ),
        "fields": {
            "total_net": {"type": "number", "required": True},
            "description": {"type": "string"},
            "period_start": {"type": "date"},
            "period_end": {"type": "date"},
            "entries": {"type": "array", "items": {
                "date": {"type": "date"},
                "description": {"type": "string"},
                "net": {"type": "number"},
            }},
        },
        "reconcile": _no_entries_reconcile,
    },
}

# ---------------------------------------------------------------------------
# Source inference
# ---------------------------------------------------------------------------

_SOURCE_HINTS: dict[str, tuple[str, ...]] = {
    "sportsbook": (
        "fanduel", "draftkings", "fanatics", "sportsbook", "bet mgm",
        "betmgm", "caesars", "sports betting",
    ),
    "prediction_market": ("polymarket", "kalshi", "predictit"),
    "staking": ("staking", "airdrop", "yield"),
    "casino": ("casino", "slots", "poker"),
}


def infer_schema(filename: str, content: str = "") -> str:
    """Pick a schema key from the filename and content, else the generic one."""
    haystack = f"{filename} {content[:4000]}".lower()
    for schema, hints in _SOURCE_HINTS.items():
        if any(h in haystack for h in hints):
            return schema
    return "external_other"


def get_schema(key: str) -> dict | None:
    return EXTRACT_SCHEMAS.get(key)
