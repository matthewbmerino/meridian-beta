"""User preferences endpoints."""

import logging

from fastapi import APIRouter, Request

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/prefs", tags=["prefs"])


@router.get("")
def get_preferences():
    from data.prefs import get_prefs
    return get_prefs()


@router.patch("")
async def update_preferences(request: Request):
    from data.prefs import update_prefs
    body = await request.json()
    if not isinstance(body, dict):
        return {"error": "Body must be a JSON object"}
    try:
        return update_prefs(**body)
    except ValueError as e:
        return {"error": str(e)}
