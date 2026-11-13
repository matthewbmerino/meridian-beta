"""WebSocket endpoint for live quote streaming."""

import asyncio
import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from data import auth

log = logging.getLogger(__name__)

router = APIRouter()

#: A client may not subscribe to more symbols than this in one message. The
#: list is unvalidated input that drives operator-credentialed vendor fetches,
#: so an uncapped list is an uncapped spend on the operator's API key.
_MAX_SYMBOLS = 200

#: Cadence by session. A closed market has nothing new to say and should not
#: spend the same calls a minute as the open.
_POLL_INTERVAL = 5           # regular session
_PREPOST_INTERVAL = 15       # pre-market / after-hours
_CLOSED_INTERVAL = 60        # closed

#: Quotes must be at most this old when the stream pushes them. The page-load
#: cache is 30s and the stream used to inherit it, which is why a "live" table
#: showed the same number for half a minute.
_STREAM_MAX_AGE = 2.0


def _stream_interval() -> float:
    """Poll cadence for the current US session."""
    try:
        from data.massive import market_session

        session = market_session()
    except Exception:
        return _POLL_INTERVAL
    if session == "regular":
        return _POLL_INTERVAL
    if session in ("pre-market", "after-hours"):
        return _PREPOST_INTERVAL
    return _CLOSED_INTERVAL


def _authorized(ws: WebSocket) -> bool:
    """A stream connection must carry the operator cookie or the service token.

    The socket used to accept before checking anything: anyone who could reach
    the port could subscribe, and each subscription drives a quote fetch made
    with the operator's vendor credentials.
    """
    try:
        cookie = ws.cookies.get("meridian_auth")
    except Exception:
        cookie = None
    if cookie and auth.validate_cookie(cookie):
        return True
    header = ws.headers.get("authorization") or ""
    if header.lower().startswith("bearer "):
        return auth.verify_api_token(header[7:].strip())
    return False


@router.websocket("/ws/quotes")
async def quote_stream(ws: WebSocket):
    # Complete the handshake first, then refuse: closing before accept() leaves
    # a browser (and Starlette's TestClient) waiting on a half-open upgrade, and
    # the close code never reaches the client. Nothing is emitted either way.
    await ws.accept()
    if not _authorized(ws):
        await ws.close(code=1008)
        return

    subscribed: set[str] = set()
    last_snapshot: dict = {}
    # A new subscription pushes a snapshot IMMEDIATELY -- not on the next poll
    # tick. The old loop consumed the subscribe message, then waited out a
    # fresh 5-second timeout before its first fetch, which is what left
    # change/% blank on first paint.
    force = False

    try:
        while True:
            try:
                msg = await asyncio.wait_for(ws.receive_text(), timeout=_stream_interval())
                try:
                    data = json.loads(msg)
                except (TypeError, ValueError):
                    data = {}
                if "subscribe" in data:
                    requested = [s.upper() for s in data["subscribe"] if s]
                    if len(requested) > _MAX_SYMBOLS:
                        log.warning(
                            "ws subscribe capped: %d -> %d symbols from %s",
                            len(requested), _MAX_SYMBOLS, ws.client.host if ws.client else "?",
                        )
                        requested = requested[:_MAX_SYMBOLS]
                    new = set(requested)
                    if new == subscribed:
                        continue  # duplicate subscribe (the hook re-sends) -- no fetch
                    subscribed = new
                    force = True
                elif "unsubscribe" in data:
                    before = len(subscribed)
                    subscribed -= {s.upper() for s in data["unsubscribe"]}
                    if len(subscribed) == before:
                        continue
                    force = True
                else:
                    continue
            except asyncio.TimeoutError:
                pass

            if not subscribed:
                continue

            try:
                from data.market import get_quotes, quote_view

                # Blocking upstream fetch, moved OFF the event loop: with
                # several open connections (desktop + mobile + audit windows)
                # the old inline call serialized every subscriber behind each
                # other's network wait.
                symbols = tuple(sorted(subscribed))
                quotes = await asyncio.to_thread(
                    lambda: get_quotes(symbols, max_age=_STREAM_MAX_AGE)
                )
            except Exception as e:
                # A transient quote-fetch failure shouldn't drop the stream —
                # retry on the next poll. Log it so silent quote gaps are
                # distinguishable from real outages.
                log.warning("Quote fetch failed for %s: %s", sorted(subscribed), e)
                continue

            changes = {}
            for sym, qdata in quotes.items():
                current = quote_view(qdata)
                if force or current != last_snapshot.get(sym):
                    changes[sym] = current
                    last_snapshot[sym] = current

            if force:
                # Drop symbols that are no longer subscribed so a stale entry
                # can't suppress a real change later.
                last_snapshot = {s: v for s, v in last_snapshot.items() if s in subscribed}
                force = False

            if changes:
                await ws.send_text(json.dumps(changes))

    except WebSocketDisconnect:
        pass
