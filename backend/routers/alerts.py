"""Alerts endpoints: list, unread count, mark read, SSE live stream."""

import asyncio
import json

from fastapi import APIRouter, Query
from sse_starlette.sse import EventSourceResponse

router = APIRouter(prefix="/api/alerts", tags=["alerts"])

@router.get("")
def list_alerts(
    limit: int = Query(default=50, le=200),
    signals_only: bool = Query(default=False),
):
    """Alerts, most actionable first.

    Ordering and filtering live in ``data.alerts.list_alerts`` so there is ONE
    implementation of this query. An earlier version reimplemented it here, which
    split the behaviour across two layers and left the data layer returning an
    unsorted list to every other caller.

    Do not call this function directly from a test — route functions keep their
    ``Query`` defaults when invoked without FastAPI's injection, and ``Query(False)``
    is truthy, which silently enables ``signals_only``. Exercise it through the
    TestClient so the real parameter binding applies.
    """
    from data.alerts import list_alerts as _list

    return {"alerts": _list(limit=limit, signals_only=bool(signals_only))}


@router.get("/unread")
def unread():
    from data.alerts import unread_count
    return {"count": unread_count()}


@router.post("/read")
def mark_read(alert_id: str | None = None):
    from data.alerts import mark_read
    return {"marked": mark_read(alert_id)}


@router.get("/stream")
async def stream():
    """SSE stream that pushes new alerts as they arrive."""
    from data.alerts import last_scan, list_alerts

    async def event_generator():
        seen = {a["id"] for a in list_alerts(limit=200)}
        while True:
            await asyncio.sleep(15)
            current = list_alerts(limit=200)
            new = [a for a in current if a["id"] not in seen]
            if new:
                seen.update(a["id"] for a in new)
                yield {"event": "alerts", "data": json.dumps(new, default=str)}
            else:
                yield {"event": "ping", "data": json.dumps({"last_scan": last_scan()})}

    return EventSourceResponse(event_generator())
