"""agent/hermes_bridge.py — Meridian's window into the Linode's local Hermes agent.

Hermes (v0.21.5+) is installed at ~/.hermes with the operator's full skill set
(investment-thesis-vetting + meridian-portfolio fact banks, web tools, memory).
This bridge runs it as a SUBPROCESS (never HTTP): no gateway session recording,
no Meridian API surface touched, no cross-session contamination.

The tool's role in research: deep multi-source verification ON DEMAND —
consensus dispersion with named firms, primary-release extraction, source
conflict surfacing. It is NOT a number source: data_guard still verifies every
meridian-side value, and the reviewer pass still checks the final text.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess

_HERMES_CANDIDATES = (
    os.path.expanduser("~/.local/bin/hermes"),
    "/usr/local/bin/hermes",
    shutil.which("hermes") or "",
)
_MAX_QUESTION_CHARS = 4000
_DEFAULT_TIMEOUT = 300


def _hermes_bin() -> str | None:
    for cand in _HERMES_CANDIDATES:
        if cand and os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


def _extract_answer(output: str) -> str:
    """Pull the agent's reply out of the CLI chat rendering.

    The CLI prints the reply inside a unicode box (rxvt-style corners). Take the
    LAST box in the output; fall back to the tail of the raw output when the
    box marker is absent so the tool degrades to prose, never to nothing.
    """
    starts = [m.start() for m in re.finditer(r"╭─", output)]
    if starts:
        seg = output[starts[-1]:]
        end = seg.find("╰─")
        if end != -1:
            lines = []
            for ln in seg[:end].splitlines():
                clean = ln.strip(" \u256d\u256e\u256f\u2570\u256e\u256f\u2500\u2502\u256e\u2624|t").strip()
                if clean and clean not in ("Hermes", "Compass", "Journalist"):  # box label artifacts
                    lines.append(clean)
            return "\n".join(lines).strip()
    return output[-3000:].strip()


def hermes_research(question: str, timeout_s: int = _DEFAULT_TIMEOUT) -> dict:
    """Ask the local Hermes agent a research question; return its sourced answer.

    Args: question (what to research), timeout_s (cap, default 300; Hermes runs
    5-30s for quick lookups, may take minutes for deep multi-source work).
    Returns: {answer, session, duration_s} or {error, recoverable}.
    """
    if not question or not question.strip():
        return {"error": "empty question", "recoverable": False}
    question = question.strip()[:_MAX_QUESTION_CHARS]
    binary = _hermes_bin()
    if binary is None:
        return {"error": "hermes CLI not found on this host", "recoverable": False}
    try:
        r = subprocess.run(
            [binary, "chat", "-q", question],
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return {
            "error": f"hermes_research timed out after {timeout_s}s — ask a narrower question or raise timeout_s",
            "recoverable": True,
        }
    except Exception as e:
        return {"error": f"hermes_research failed: {str(e)[:200]}", "recoverable": True}

    output = (r.stdout or "") + (r.stderr or "")
    if not output.strip():
        return {"error": "hermes returned no output", "recoverable": True}

    answer = _extract_answer(output)
    session_m = re.search(r"Session:\s+(\S+)", output)
    duration_m = re.search(r"Duration:\s+([\dms\.]+)", output)
    return {
        "answer": answer,
        "session": session_m.group(1) if session_m else None,
        "duration": duration_m.group(1) if duration_m else None,
        "note": "Answer comes from the local Hermes agent (skills + web). Numbers still require source lines + your own tool verification before use.",
    }


HERMES_TOOLS = {
    "hermes_research": {
        "fn": hermes_research,
        "schema": {
            "type": "function",
            "function": {
                "name": "hermes_research",
                "description": "Deep research escalation: asks the local Hermes research agent (skill-grounded, full web access) to investigate and return a sourced answer. Use when: consensus/analyst dispersion with named firms is needed, a primary release/8-K must be extracted verbatim, sources conflict and need arbitration, or multiple independent checks are required before a claim enters the report. Also the route for ALTERNATIVE-ASSET valuation (watches, graded cards): ask with the item description verbatim; the agent resolves the reference/cert, pulls market comps, and returns {value, as_of, source, confidence}. Returns the answer plus a session id. NOT a substitute for Meridian's own data tools — every number still needs a source line, and anything portfolio-side must come from your own tools.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "question": {
                            "type": "string",
                            "description": "The research question, stated precisely and self-contained (Hermes sees none of this conversation). Example: 'Extract VRT's Q3 2026 guidance band and FY26 adjusted-EPS guidance verbatim from Vertiv's primary Q2 2026 release, then fetch stockanalysis.com/stocks/vrt/forecast for consensus average/median/low/high + analyst count.'",
                        },
                        "timeout_s": {
                            "type": "integer",
                            "description": "Max seconds to wait (default 300). Deep multi-source questions may need 300-600.",
                        },
                    },
                    "required": ["question"],
                },
            },
        },
    },
}
