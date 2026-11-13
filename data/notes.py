"""Developer note capture — a git-tracked inbox for the operator's ad-hoc thoughts.

Deliberate deviation from the encrypted PII stores: notes are written to
`research/notes.md`, a plaintext file inside the repo, so agents and the
operator's git history can see them. They are quick scratch notes ("add X",
"change Y", "look into Z"), not private data — the briefs hold the spec, the
ledger holds open work, and this file is the raw capture inbox.

Format: a `## YYYY-MM-DD` section per day, notes appended newest-first within
the day. New notes are read by agents, then promoted into `research/plans/` or
`research/backlog.md` when they crystallize into work.
"""

from __future__ import annotations

import datetime as _dt
import re
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_NOTES_FILE = _PROJECT_ROOT / "research" / "notes.md"

_HEADER = """# Developer Notes

Quick capture inbox — press ⌘/ (Ctrl+/ on Windows/Linux) inside Meridian or
add notes directly here. Promoted to `research/plans/` or `research/backlog.md`
when a note crystallizes into work.
"""

_MAX_LENGTH = 2000


def notes_file() -> Path:
    """Path to the notes file (public for tests/diagnostics)."""
    return _NOTES_FILE


def _read() -> str:
    if not _NOTES_FILE.exists():
        return ""
    try:
        return _NOTES_FILE.read_text(encoding="utf-8")
    except OSError:
        return ""


def list_notes() -> list[dict]:
    """Parse the notes file into `[{date, text, context}]`, newest date first."""
    raw = _read()
    notes: list[dict] = []
    current: dict | None = None
    for line in raw.splitlines():
        m = re.match(r"^## (\d{4}-\d{2}-\d{2})$", line)
        if m:
            current = {"date": m.group(1), "texts": []}
            notes.append(current)
        elif current is not None:
            stripped = line.strip()
            if stripped:
                if stripped.startswith("- "):
                    stripped = stripped[2:]
                current["texts"].append(stripped)
    result = []
    for section in reversed(notes):
        for text in section["texts"]:
            context = None
            cm = re.match(r"^\*\*\[([^\]]+)\]\*\*\s?(.*)$", text)
            if cm:
                context, text = cm.group(1), cm.group(2)
            result.append({"date": section["date"], "text": text, "context": context})
    return result


def add_note(text: str, context: str | None = None) -> dict:
    """Append a note under today's `## YYYY-MM-DD` heading, newest-first.

    `context` (e.g. the page the note was captured on) is rendered as a bold
    bracket prefix: `- **[Context]** text`. Creates the file with a header
    banner on first use. Empty/whitespace-only notes are rejected (callers
    must validate before calling). Returns the saved `{date, text, context}`
    dict.
    """
    cleaned = text.strip()
    if not cleaned:
        raise ValueError("note text must not be empty")

    today = _dt.date.today().isoformat()
    heading = f"## {today}"
    existing = _read()

    bullet = f"- {cleaned}"
    if context:
        ctx = context.strip()
        if ctx:
            bullet = f"- **[{ctx}]** {cleaned}"

    if not existing:
        content = f"{_HEADER}\n\n{heading}\n\n{bullet}\n"
    elif heading in existing:
        # Insert newest-first: place the new note right under the heading's
        # blank line. Find the heading's end (the blank line after it).
        lines = existing.splitlines()
        insert_at = 0
        for i, line in enumerate(lines):
            if line == heading:
                insert_at = i + 1
                break
        while insert_at < len(lines) and lines[insert_at] == "":
            insert_at += 1
        lines.insert(insert_at, bullet)
        content = "\n".join(lines) + "\n"
    else:
        # New day — append the heading at the end (notes are reverse-chron by
        # day when read via list_notes, so physical order is least important).
        if not existing.endswith("\n"):
            existing += "\n"
        content = f"{existing}\n{heading}\n\n{bullet}\n"

    try:
        _NOTES_FILE.write_text(content, encoding="utf-8")
    except OSError:
        raise
    return {"date": today, "text": cleaned, "context": (context.strip() if context and context.strip() else None)}
