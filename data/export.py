"""Export formatters for the decision journal.

Pure functions: records in, text out. No IO, no network — so the export a fund
would receive is testable byte-for-byte.

The markdown shape is deliberate: one decision per section, plan before
rationale (because the plan is the part that can be tested later), then the
stage history, then the forecast record. The JSON shape is the store verbatim.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone

HORIZONS = ("1d", "5d", "20d", "60d")


def _ts(v) -> str:
    if not v:
        return "—"
    try:
        return datetime.fromtimestamp(float(v), tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return "—"


def _cell_block(cell: dict) -> list[str]:
    """One horizon cell as a markdown line. 'not due' and 'no data' are distinct."""
    status = str(cell.get("status") or "resolved")
    ret = cell.get("ret")
    bench = cell.get("bench_ret")
    win = cell.get("win")
    if status == "pending":
        return []
    if status == "no_data":
        return [f"  - **{cell.get('horizon', '?')}** no data — {cell.get('reason') or 'unavailable'}"]
    if status == "unclassified":
        if ret is not None and bench is not None:
            return [f"  - **{cell.get('horizon', '?')}** unclassified — intent not classifiable "
                    f"(ret {ret:+.2%}, bench {bench:+.2%})"]
        return [f"  - **{cell.get('horizon', '?')}** unclassified"]
    verdict = "win" if win else "loss"
    parts = [f"  - **{cell.get('horizon', '?')}** {verdict}: ret {ret:+.2%}"
             if isinstance(ret, (int, float)) else f"  - **{cell.get('horizon', '?')}** {verdict}"]
    if isinstance(bench, (int, float)):
        parts.append(f" vs SPY {bench:+.2%}")
    return ["".join(parts)]


def decision_to_markdown(entry: dict, outcome: dict | None = None) -> str:
    """One decision as a self-contained markdown section."""
    sym = entry.get("symbol", "?")
    lines = [f"### {sym} — {entry.get('action', '')}"]
    lines.append("")
    meta = [
        f"**stage** {entry.get('status', 'open')}",
        f"**direction** {entry.get('direction') or '—'}",
        f"**size** {entry.get('size') if entry.get('size') is not None else '—'}",
        f"**conviction** {entry.get('conviction') if entry.get('conviction') is not None else '—'}",
        f"**type** {entry.get('type')}",
        f"**book** {entry.get('book')}",
        f"**source** {entry.get('source')}",
        f"**actor** {entry.get('actor') or '—'}",
    ]
    if entry.get("agent_model"):
        meta.append(f"**model** {entry['agent_model']}")
    lines.append(" · ".join(meta))
    lines.append("")
    lines.append(f"- logged {_ts(entry.get('ts'))} · id `{entry.get('id')}`")
    if entry.get("supersedes"):
        lines.append(f"- supersedes `{entry['supersedes']}`")
    if entry.get("superseded_by"):
        lines.append(f"- superseded by `{entry['superseded_by']}`")
    lines.append("")

    legs = entry.get("legs")
    if legs:
        lines.append("**Legs**")
        lines.append("")
        for leg in legs:
            cond = f" — {leg['condition']}" if leg.get("condition") else ""
            limit = f" @ {leg['limit']}" if leg.get("limit") is not None else ""
            lines.append(f"- {leg.get('shares')} sh{limit} ({leg.get('status', 'planned')}){cond}")
        lines.append("")

    if entry.get("constraint"):
        lines.append("**Plan / constraint**")
        lines.append("")
        lines.append(str(entry["constraint"]))
        lines.append("")
    if entry.get("rationale"):
        lines.append("**Why**")
        lines.append("")
        lines.append(str(entry["rationale"]))
        lines.append("")

    events = entry.get("status_events") or []
    if events:
        lines.append("**Stage history**")
        lines.append("")
        for ev in sorted(events, key=lambda e: e.get("ts", 0)):
            reason = f" — {ev['reason']}" if ev.get("reason") else ""
            lines.append(f"- {_ts(ev.get('ts'))}: {ev.get('from')} → {ev.get('to')}{reason}")
        lines.append("")

    if outcome:
        cells = []
        for h in HORIZONS:
            cell = (outcome.get("horizons") or {}).get(h)
            if isinstance(cell, dict):
                c = dict(cell)
                c.setdefault("horizon", h)
                cells.extend(_cell_block(c))
        if cells:
            lines.append("**Forecast record**")
            lines.append("")
            lines.extend(cells)
            lines.append("")
    return "\n".join(lines)


def decisions_to_markdown(entries: list[dict], outcomes: list[dict] | None = None) -> str:
    """The whole journal as one markdown document, oldest first."""
    by_decision = {o.get("decision_id"): o for o in (outcomes or [])}
    ordered = sorted(entries, key=lambda e: e.get("ts", 0))
    head = [
        "# Decision Journal",
        "",
        f"Exported {_ts(time.time())} · {len(ordered)} entries",
        "",
        "---",
        "",
    ]
    body = [decision_to_markdown(e, by_decision.get(e.get("id"))) for e in ordered]
    return "\n".join(head + body)


def decisions_to_json(entries: list[dict]) -> str:
    """The journal verbatim, as JSON."""
    return json.dumps({"decisions": entries, "n": len(entries)}, indent=2, default=str)
