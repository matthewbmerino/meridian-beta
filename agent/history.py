"""Conversation history management for Compass with token-aware pruning."""

import json

from backend.services.compass_session import (
    append_display as _append_thread_display,
)
from backend.services.compass_session import (
    get_display as _get_thread_display,
)
from backend.services.compass_session import (
    get_history as _get_thread_history,
)
from backend.services.compass_session import (
    set_history as _set_thread_history,
)

MAX_HISTORY_CHARS = 24_000  # ~6K tokens at 4 chars/token
MAX_TOOL_RESULT_CHARS = 2_000
KEEP_RECENT_TURNS = 6  # user+assistant pairs to keep verbatim


def get_history() -> list[dict]:
    """Return the current conversation history from session state."""
    return _get_thread_history()


def init_history(system_prompt: str) -> list[dict]:
    """Initialize conversation history with system prompt."""
    history = [{"role": "system", "content": system_prompt}]
    _set_thread_history(history)
    return history


def append(message: dict):
    """Append a message to history, pruning if needed."""
    history = get_history()
    if message.get("role") == "tool":
        content = message.get("content", "")
        if len(content) > MAX_TOOL_RESULT_CHARS:
            message = {**message, "content": content[:MAX_TOOL_RESULT_CHARS] + "\n...[truncated]"}
    history.append(message)
    _prune_if_needed(history)
    _set_thread_history(history)


def _total_chars(history: list[dict]) -> int:
    total = 0
    for msg in history:
        content = msg.get("content", "")
        if isinstance(content, str):
            total += len(content)
        # tool_calls are dicts, estimate their size
        if msg.get("tool_calls"):
            total += len(json.dumps(msg["tool_calls"], default=str))
    return total


def _prune_if_needed(history: list[dict]):
    """Remove older messages when approaching context limit."""
    if _total_chars(history) <= MAX_HISTORY_CHARS:
        return

    # Keep: system prompt (index 0) + last N messages
    if len(history) <= KEEP_RECENT_TURNS * 2 + 1:
        return

    system = history[0]
    keep_count = KEEP_RECENT_TURNS * 2  # each turn = user + assistant (+ tool messages)

    # Walk from the end to find enough turns
    recent = history[-keep_count:]

    # Summarize what was removed
    removed = history[1:-keep_count]
    symbols_mentioned = set()
    for msg in removed:
        content = msg.get("content", "")
        if isinstance(content, str):
            # Extract any uppercase symbols that look like tickers
            words = content.split()
            for w in words:
                clean = w.strip(".,()[]{}\"'")
                if clean.isupper() and 1 <= len(clean) <= 5 and clean.isalpha():
                    symbols_mentioned.add(clean)

    summary = "Earlier in this conversation we discussed"
    if symbols_mentioned:
        summary += f": {', '.join(sorted(symbols_mentioned))}."
    else:
        summary += " various topics."

    history.clear()
    history.append(system)
    history.append({"role": "user", "content": summary})
    history.append({"role": "assistant", "content": "Understood, I have that context."})
    history.extend(recent)


def get_display_messages() -> list[dict]:
    """Return messages formatted for display (excludes system, tool messages)."""
    return _get_thread_display()


def append_display(message: dict):
    """Append a message to the display list."""
    _append_thread_display(message)
