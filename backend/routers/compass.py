"""Compass chat endpoint — SSE streaming + history CRUD.

PRIVATE — Compass is portfolio-aware: the proactive briefing injects the
GLOBAL snapshot store (NAV, weather) into the system prompt, and its tools
read operator positions. An anonymous user could extract that through chat.
Gated by require_auth (fail-closed).
"""

import asyncio
import json
import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from backend.auth import require_auth

router = APIRouter(
    prefix="/api/compass",
    tags=["compass"],
    dependencies=[Depends(require_auth)],
)


class ChatBody(BaseModel):
    message: str
    chat_id: str | None = None


class SaveBody(BaseModel):
    chat_id: str
    title: str
    messages: list[dict]
    display_messages: list[dict]


@router.post("/chat")
async def chat(body: ChatBody):
    chat_id = body.chat_id or uuid.uuid4().hex[:12]

    async def event_generator():
        yield {"event": "meta", "data": json.dumps({"chat_id": chat_id})}

        def _run_sync():
            from agent import loop as agent_loop
            from agent.history import init_history
            from agent.prompts import get_system_prompt
            from backend.services.compass_session import compass_context
            from data.compass_chats import load_chat

            events = []
            with compass_context(chat_id):
                existing = load_chat(chat_id)
                if existing and existing.get("messages"):
                    from backend.services.compass_session import set_history
                    set_history(existing["messages"])
                else:
                    # Fresh conversation — inject the proactive briefing so
                    # Compass leads with what changed since last check.
                    init_history(get_system_prompt(briefing=True))

                for event in agent_loop.run(body.message):
                    events.append(event)
            return events

        # to_thread copies contextvars (run_in_executor does NOT), so the
        # per-request context is visible inside the thread (chat history,
        # briefing).
        events = await asyncio.to_thread(_run_sync)

        for event in events:
            etype = event.get("type", "")
            yield {"event": etype, "data": json.dumps(event, default=str)}

        yield {"event": "done", "data": json.dumps({"chat_id": chat_id})}

    return EventSourceResponse(event_generator())


@router.get("/chats")
def list_chats():
    from data.compass_chats import list_chats
    return list_chats()


@router.get("/chats/{chat_id}")
def get_chat(chat_id: str):
    from data.compass_chats import load_chat
    try:
        chat = load_chat(chat_id)
    except ValueError:
        return {"error": "Chat not found"}
    if chat is None:
        return {"error": "Chat not found"}
    return chat


@router.post("/chats/save")
def save_chat(body: SaveBody):
    from data.compass_chats import save_chat
    try:
        save_chat(body.chat_id, body.title, body.messages, body.display_messages)
    except ValueError:
        return {"ok": False, "error": "Invalid chat_id"}
    return {"ok": True}


@router.delete("/chats/{chat_id}")
def delete_chat(chat_id: str):
    from data.compass_chats import delete_chat
    try:
        existed = delete_chat(chat_id)
    except ValueError:
        return {"ok": True, "existed": False}
    return {"ok": True, "existed": existed}
