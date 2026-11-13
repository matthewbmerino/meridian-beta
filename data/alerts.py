"""Operator alert store for Meridian.

Alerts are stored globally at ``data/alerts.json`` — single operator, no
session isolation. The scheduler writes alerts directly (it also reads global
data). Alerts are deduplicated by a stable key (type + symbol + date) so
repeated scans don't spam the feed.
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path

from data.crypto import load_json_store, write_json
from data.session import data_root

MAX_ALERTS = 200
DEDUP_WINDOW_HOURS = 24


def _alerts_path() -> Path:
    return data_root() / "alerts.json"


def _read() -> dict:
    data = load_json_store(_alerts_path(), "alerts", {"alerts": [], "last_scan": None})
    return data if isinstance(data, dict) else {"alerts": [], "last_scan": None}


def _write(data: dict):
    try:
        write_json(_alerts_path(), "alerts", data)
    except OSError:
        pass


def _dedup_key(alert: dict) -> str:
    return f"{alert.get('type')}:{alert.get('symbol', '')}:{alert.get('date', '')}"


def add_alert(alert_type: str, title: str, message: str,
              severity: str = "info", symbol: str | None = None,
              link: str | None = None) -> bool:
    """Add an alert if a duplicate hasn't fired in the dedup window.

    Returns True if added, False if it was a duplicate.
    """
    data = _read()
    now = time.time()
    date = time.strftime("%Y-%m-%d", time.gmtime(now))
    key = _dedup_key({"type": alert_type, "symbol": symbol, "date": date})

    for a in data["alerts"]:
        if _dedup_key(a) == key:
            return False

    data["alerts"].insert(0, {
        "id": uuid.uuid4().hex[:12],
        "type": alert_type,
        "title": title,
        "message": message,
        "severity": severity,
        "symbol": symbol,
        "link": link,
        "ts": now,
        "date": date,
        "read": False,
    })
    data["alerts"] = data["alerts"][:MAX_ALERTS]
    data["last_scan"] = now
    _write(data)
    return True


# Stable severity ranking: critical > warning > info. Unknown severities sort
# lowest rather than being dropped, so a malformed alert is still visible.
_SEVERITY_ORDER = {"critical": 3, "warning": 2, "info": 1}

# Routine housekeeping types excluded by `signals_only`. These are not
# actionable: a stale-review nag and a "cannot score this name" notice are
# bookkeeping, whereas an entry signal, a thesis invalidation, or an STBR warning
# is something to act on.
# NOTE: a thesis invalidation/target-hit is a SIGNAL, not housekeeping, and
# must never appear here. Only the routine review nag and the "cannot score
# this name yet" notice are bookkeeping.
HOUSEKEEPING_TYPES = frozenset({"thesis_stale_review", "entry_data"})


def list_alerts(limit: int = 50, signals_only: bool = False) -> list[dict]:
    """Alerts, most actionable first.

    Ordered by severity descending, then most-recent first, so a critical
    invalidation outranks a routine info nag regardless of when it fired.

    ``signals_only`` drops the housekeeping types above. NOTE the defaults here
    are plain Python values, NOT FastAPI ``Query`` objects: this function is
    called from scripts and tests as well as the router, and a ``Query`` default
    is truthy when called directly, which would silently enable the filter.
    """
    data = _read()
    entries = list(data.get("alerts", []))

    if signals_only:
        # Keep the housekeeping type available for the default view; only the
        # explicit signals view excludes it.
        entries = [a for a in entries if a.get("type") not in HOUSEKEEPING_TYPES]

    entries.sort(
        key=lambda a: (_SEVERITY_ORDER.get(a.get("severity", "info"), 0), a.get("ts", 0) or 0),
        reverse=True,
    )
    return entries[:limit]


def unread_count() -> int:
    data = _read()
    return sum(1 for a in data["alerts"] if not a.get("read"))


def mark_read(alert_id: str | None = None) -> int:
    """Mark one alert (or all if alert_id is None) as read. Returns count marked."""
    data = _read()
    marked = 0
    for a in data["alerts"]:
        if alert_id is None or a["id"] == alert_id:
            if not a.get("read"):
                a["read"] = True
                marked += 1
    if marked:
        _write(data)
    return marked


def last_scan() -> float | None:
    return _read().get("last_scan")
