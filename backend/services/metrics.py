"""In-process request metrics for operational visibility.

The RequestLogMiddleware records per-route request counts and latency
percentiles; GET /api/meta/metrics serves them. This is a lightweight
operational surface — not a full APM — enough to answer "is the system
degrading?" over time.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

_MAX_SAMPLES_PER_ROUTE = 2000

_lock = threading.Lock()
_counts: dict[str, int] = defaultdict(int)
_status: dict[str, dict[int, int]] = defaultdict(lambda: defaultdict(int))
_latency: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=_MAX_SAMPLES_PER_ROUTE))
_started_at = time.time()


def record(method: str, path: str, status_code: int, duration_ms: float):
    """Record one request against a route key."""
    key = f"{method} {path}"
    with _lock:
        _counts[key] += 1
        _status[key][status_code] += 1
        _latency[key].append(duration_ms)


def _pctile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    idx = min(len(values) - 1, int(len(values) * p))
    return round(values[idx], 1)


def snapshot() -> dict:
    """Return a point-in-time metrics snapshot (no lock held during read)."""
    with _lock:
        routes = []
        for key in sorted(_counts):
            lat = list(_latency[key])
            routes.append({
                "route": key,
                "count": _counts[key],
                "status": dict(_status[key]),
                "p50_ms": _pctile(lat, 0.5),
                "p95_ms": _pctile(lat, 0.95),
                "p99_ms": _pctile(lat, 0.99),
            })
        total = sum(_counts.values())
        return {
            "uptime_seconds": round(time.time() - _started_at, 1),
            "total_requests": total,
            "routes": routes,
        }
