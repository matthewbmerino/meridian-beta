"""Developer note capture endpoints.

Notes are appended to `research/notes.md` (git-tracked) so agents see the
operator's ad-hoc capture inbox. GET lists parsed notes, POST appends one.
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from data.notes import _MAX_LENGTH

router = APIRouter(prefix="/api/notes", tags=["notes"])


class NoteBody(BaseModel):
    text: str
    context: str | None = None


@router.get("")
def get_notes():
    from data.notes import list_notes
    return {"notes": list_notes()}


@router.post("")
def add_note(body: NoteBody):
    text = body.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Note text must not be empty")
    if len(text) > _MAX_LENGTH:
        raise HTTPException(status_code=400, detail=f"Note must be ≤ {_MAX_LENGTH} characters")
    from data.notes import add_note
    return {"note": add_note(text, context=body.context)}
