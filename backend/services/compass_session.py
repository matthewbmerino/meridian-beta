"""Per-request Compass session context.

Usage in the compass router:

    with compass_context(chat_id):
        for event in agent_loop.run(prompt, extended):
            ...
"""

import threading
from contextlib import contextmanager

_local = threading.local()


def set_context(chat_id: str):
    _local.chat_id = chat_id
    _local.history = []
    _local.display = []


def clear_context():
    for attr in ("chat_id", "history", "display"):
        try:
            delattr(_local, attr)
        except AttributeError:
            pass


@contextmanager
def compass_context(chat_id: str):
    set_context(chat_id)
    try:
        yield
    finally:
        clear_context()


def get_chat_id() -> str:
    return getattr(_local, "chat_id", "")


def get_history() -> list[dict]:
    if not hasattr(_local, "history"):
        _local.history = []
    return _local.history


def set_history(history: list[dict]):
    _local.history = history


def get_display() -> list[dict]:
    if not hasattr(_local, "display"):
        _local.display = []
    return _local.display


def append_display(msg: dict):
    if not hasattr(_local, "display"):
        _local.display = []
    _local.display.append(msg)
