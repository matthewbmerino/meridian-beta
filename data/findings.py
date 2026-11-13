"""Shared findings store — bridges Journalist reports into Compass context.

When Journalist produces a report, its key conclusions (title, symbols,
conviction, key numbers) are written here. Compass reads recent findings and
injects them into its system prompt, so research conclusions persist across
sessions and inform future answers.

Storage: global JSON at data/findings.json.
"""

from __future__ import annotations

import time
from pathlib import Path

from data.crypto import load_json_store, write_json
from data.session import data_root

MAX_FINDINGS = 50


def _path() -> Path:
    return data_root() / "findings.json"


def _read() -> list[dict]:
    data = load_json_store(_path(), "findings", [])
    return data if isinstance(data, list) else []


def _write(findings: list[dict]):
    try:
        write_json(_path(), "findings", findings)
    except OSError:
        pass


def add_finding(report_id: str, title: str, research_type: str,
                symbols: list[str] | None = None,
                conviction: str | None = None,
                summary: str | None = None,
                research_state: dict | None = None) -> bool:
    """Record a research conclusion. Returns True if added.

    ``research_state`` carries the structured state the writer extracted
    (thesis, conviction, key_metrics, risks, recommendation) so follow-ups can
    recall the full thesis, not just a text summary.
    """
    findings = _read()
    # Replace any prior finding from the same report
    findings = [f for f in findings if f.get("report_id") != report_id]
    findings.insert(0, {
        "report_id": report_id,
        "title": title,
        "research_type": research_type,
        "symbols": symbols or [],
        "conviction": conviction,
        "summary": summary,
        "research_state": research_state or {},
        "ts": time.time(),
    })
    findings = findings[:MAX_FINDINGS]
    _write(findings)
    return True


def recent_findings(limit: int = 5) -> list[dict]:
    return _read()[:limit]


def findings_context(limit: int = 5) -> str:
    """Compact text block for Compass system-prompt injection."""
    findings = recent_findings(limit)
    if not findings:
        return ""
    lines = ["\nRECENT RESEARCH FINDINGS (from Journalist reports):"]
    for f in findings:
        syms = ", ".join(f.get("symbols", [])[:5]) or "—"
        conv = f.get("conviction") or "—"
        lines.append(f"  • {f.get('title', 'Untitled')} [{f.get('research_type', '')}] "
                     f"symbols={syms} conviction={conv}")
        if f.get("summary"):
            lines.append(f"    {f['summary'][:200]}")
    return "\n".join(lines) + "\n"


def research_memory_context(symbols: list[str] | None = None, limit: int = 8) -> str:
    """Accumulated-conviction memory block for the report writer on follow-ups.

    Unlike findings_context (which is a Compass-facing list), this is a
    research-memory bank for the writer: every prior report's structured state
    (thesis + conviction + verified key metrics + risks + recommendation),
    optionally filtered to symbols relevant to the current brief. This is what
    lets a follow-up build on accumulated conviction across the whole thread,
    not just the immediate parent report.
    """
    findings = _read()
    if symbols:
        wanted = {s.upper() for s in symbols}
        findings = [f for f in findings if wanted & {s.upper() for s in f.get("symbols", [])}]
    findings = findings[:limit]
    if not findings:
        return ""

    lines = ["## RESEARCH MEMORY (accumulated convictions from prior reports)"]
    for f in findings:
        st = f.get("research_state") or {}
        thesis = st.get("thesis") or f.get("title", "Untitled")
        conv = st.get("conviction") or f.get("conviction") or "—"
        syms = ", ".join(f.get("symbols", [])[:5]) or "—"
        lines.append(f"- {thesis} [conviction: {conv}, symbols: {syms}]")
        metrics = st.get("key_metrics") or {}
        if metrics:
            for sym, m in list(metrics.items())[:3]:
                parts = [f"{k}={v:g}" for k, v in m.items() if k in ("price", "stbr", "pe_ratio", "beta")][:4]
                if parts:
                    lines.append(f"    {sym}: " + " · ".join(parts))
        risks = st.get("risks")
        if risks:
            lines.append(f"    risks: {risks[:200]}")
    lines.append(
        "These are the operator's prior research convictions. A follow-up must "
        "build on this state — reference verified metrics verbatim, do NOT "
        "contradict a prior thesis unless new data justifies it."
    )
    return "\n".join(lines) + "\n"
