"""Settings endpoints — user-supplied AI provider configuration.

The Ollama API key a user supplies here is used for a single probe request
and is never stored, logged, or written to disk. The key itself lives only
in the user's browser (localStorage) and is sent per-request as the
X-Ollama-Key header; this endpoint exists so the user can validate it before
saving.
"""

import logging

from fastapi import APIRouter
from pydantic import BaseModel

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/settings", tags=["settings"])


class OllamaProbeBody(BaseModel):
    api_key: str


@router.post("/ollama-probe")
def ollama_probe(body: OllamaProbeBody):
    """Validate a user-supplied Ollama API key.

    The key is used for this probe only and is never persisted. Returns
    {ok, error?, latency_ms, model}.
    """
    from agent import compute
    result = compute.probe_with(body.api_key)
    return {
        "ok": result.ok,
        "error": result.error,
        "latency_ms": result.latency_ms,
        "model": result.model,
    }
