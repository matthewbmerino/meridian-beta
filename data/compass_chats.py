"""Persistent Compass chat storage.

Each conversation is a JSON file in data/compass_chats/. An index file
(index.json) keeps lightweight metadata for fast listing without reading
every chat file.

Public API:
    list_chats()                -> list of {id, title, created_at, updated_at, message_count}
    load_chat(chat_id)          -> full chat dict or None
    save_chat(chat_id, title, messages, display_messages) -> chat_id
    create_chat(title)          -> chat_id
    delete_chat(chat_id)        -> bool
    auto_title(display_messages) -> str
"""

from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path

from data.crypto import load_json_store, write_json
from data.session import data_root


def _dir() -> Path:
    chats = data_root() / "compass_chats"
    chats.mkdir(parents=True, exist_ok=True)
    return chats


def _index_path() -> Path:
    return _dir() / "index.json"


def _read_index() -> list[dict]:
    data = load_json_store(_index_path(), "compass_index", [])
    return data if isinstance(data, list) else []


def _write_index(entries: list[dict]):
    try:
        write_json(_index_path(), "compass_index", entries)
    except OSError:
        pass


def _chat_path(chat_id: str) -> Path:
    import re
    d = _dir()
    if not re.match(r'^[a-f0-9\-]{12,36}$', chat_id):
        raise ValueError("Invalid chat_id")
    return d / f"{chat_id}.json"


def list_chats() -> list[dict]:
    """Return lightweight metadata for all saved chats, newest first."""
    entries = _read_index()
    entries.sort(key=lambda e: e.get("updated_at", ""), reverse=True)
    return entries


def load_chat(chat_id: str) -> dict | None:
    """Load a full chat (messages + display_messages) by ID."""
    path = _chat_path(chat_id)
    if not path.exists():
        return None
    try:
        data = load_json_store(path, f"chat:{chat_id}", {})
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def save_chat(
    chat_id: str,
    title: str,
    messages: list[dict],
    display_messages: list[dict],
) -> str:
    """Save or update a chat. Returns the chat_id."""
    path = _chat_path(chat_id)
    now = datetime.now().isoformat()

    existing = load_chat(chat_id)
    chat = {
        "id": chat_id,
        "title": title,
        "messages": messages,
        "display_messages": display_messages,
        "created_at": existing["created_at"] if existing else now,
        "updated_at": now,
    }

    try:
        write_json(path, f"chat:{chat_id}", chat)
    except OSError:
        pass

    index = _read_index()
    index = [e for e in index if e.get("id") != chat_id]
    index.append({
        "id": chat_id,
        "title": title,
        "created_at": chat["created_at"],
        "updated_at": now,
        "message_count": len([m for m in display_messages if m.get("role") == "user"]),
    })
    _write_index(index)
    return chat_id


def create_chat(title: str = "New conversation") -> str:
    """Create a new empty chat and return its ID."""
    chat_id = uuid.uuid4().hex[:12]
    save_chat(chat_id, title, [], [])
    return chat_id


def delete_chat(chat_id: str) -> bool:
    """Delete a chat by ID. Returns True if it existed."""
    path = _chat_path(chat_id)
    existed = path.exists()
    if existed:
        path.unlink()
    index = _read_index()
    index = [e for e in index if e.get("id") != chat_id]
    _write_index(index)
    return existed
def auto_title(display_messages: list[dict]) -> str:
    """Generate a title from the first user message."""
    for msg in display_messages:
        if msg.get("role") == "user":
            text = (msg.get("content") or "").strip()
            if text:
                return text[:60] + ("..." if len(text) > 60 else "")
    return "New conversation"
