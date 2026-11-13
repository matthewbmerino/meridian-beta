"""Per-symbol chart state — scale mode, overlays, and drawings.

GLOBAL store (single operator), mirroring the theses/decisions precedent: a
fixed path under ``data/``, encrypted at rest. Keyed by symbol so the Search
page chart restores its log/lin scale, active macro overlays, and drawing
tools when the operator returns to a symbol.

Two chart surfaces persist independently, namespaced by *chart* to avoid
collisions: the price chart (``chart="price"``, stored under the bare symbol —
the original key shape) and the STBR chart (``chart="stbr"``, stored under
``SYMBOL:STBR``). The price key is intentionally left unchanged so existing
operator state is preserved.

Storage: JSON at ``data/chart_state.json``, atomic tmp-rename writes (same
pattern as ``data/theses.py``).
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from data.crypto import StoreUnreadable, load_json_store, write_json

log = logging.getLogger(__name__)

_CHART_STATE_PATH = Path(__file__).resolve().parent / "chart_state.json"

# A corrupt store must not 500 an otherwise healthy chart page: the read path
# degrades to "nothing saved yet" so the symbol chart opens on its defaults.
# Writes deliberately DO refuse — silently overwriting an unreadable store would
# destroy drawings that are still recoverable from chart_state.json.bak.
_STORE_UNREADABLE = False
_WARNED = False


class ChartStateUnavailable(RuntimeError):
    """The chart-state store cannot be decrypted; writes are refused."""

# Allowlisted fields — a client can't inject arbitrary keys.
_FIELDS = ("scale_mode", "overlays", "drawings", "analyst")

# Allowlisted chart namespaces. "price" keeps the bare-symbol key shape.
_CHARTS = ("price", "stbr")


def _store_key(symbol: str, chart: str) -> str:
    """Map (symbol, chart) to its store key, preserving the price-chart shape."""
    sym = symbol.upper()
    name = (chart or "price").lower()
    return sym if name == "price" else f"{sym}:{name.upper()}"


def _read() -> dict[str, dict]:
    global _STORE_UNREADABLE, _WARNED
    try:
        data = load_json_store(_CHART_STATE_PATH, "chart_state", {})
        _STORE_UNREADABLE = False
    except StoreUnreadable as exc:
        _STORE_UNREADABLE = True
        if not _WARNED:
            log.warning(
                "chart_state store is unreadable (%s) — reads degrade to defaults, "
                "writes refused until data/chart_state.json is restored", exc,
            )
            _WARNED = True
        return {}
    return data if isinstance(data, dict) else {}


def _write(data: dict[str, dict]) -> None:
    try:
        write_json(_CHART_STATE_PATH, "chart_state", data)
    except OSError:
        pass


def _now() -> float:
    return time.time()


def _normalize(record: dict) -> dict:
    """Fill every allowlisted field on read.

    A row written before a field existed is missing the KEY entirely, not holding
    ``None`` — so a consumer reading ``state["analyst"]`` would raise KeyError on
    exactly the symbols the operator has been charting longest. Normalizing here means
    the read shape always matches what the frontend type declares.
    """
    return {
        "symbol": record.get("symbol"),
        "chart": record.get("chart", "price"),
        "scale_mode": record.get("scale_mode") or "linear",
        "overlays": record.get("overlays") or [],
        "drawings": record.get("drawings") or [],
        "analyst": bool(record.get("analyst", False)),
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
    }


def get_chart_state(symbol: str, chart: str = "price") -> dict | None:
    """Return the chart state for *symbol*/*chart*, or None if never saved."""
    record = _read().get(_store_key(symbol, chart))
    return None if record is None else _normalize(record)


def save_chart_state(symbol: str, state: dict, chart: str = "price") -> dict:
    """Create or update the chart state for *symbol*/*chart*.

    Only known fields are persisted (allowlist). ``updated_at`` is stamped on
    every write; ``created_at`` is preserved on update.
    """
    sym = symbol.upper()
    name = (chart or "price").lower()
    if name not in _CHARTS:
        name = "price"
    key = _store_key(sym, name)
    data = _read()
    if _STORE_UNREADABLE:
        raise ChartStateUnavailable(
            "chart state store is unreadable; restore data/chart_state.json "
            "(its .bak decrypts) before saving"
        )
    existing = data.get(key, {})
    now = _now()

    record = {
        "symbol": sym,
        "chart": name,
        "scale_mode": str(state.get("scale_mode", "linear")),
        "analyst": bool(state.get("analyst", False)),
        "overlays": [str(s).upper() for s in state.get("overlays", []) if str(s).strip()],
        "drawings": state.get("drawings", []),
        "created_at": existing.get("created_at", now),
        "updated_at": now,
    }
    if record["scale_mode"] not in ("linear", "log"):
        record["scale_mode"] = "linear"
    if not isinstance(record["drawings"], list):
        record["drawings"] = []

    data[key] = record
    _write(data)
    return record
