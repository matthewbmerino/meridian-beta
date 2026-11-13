"""Compute backend for Compass — provider-agnostic LLM client lifecycle.

Supports two providers, switchable via the ``llm_provider`` pref:
  - "ollama" (default): Ollama cloud open models (deepseek-v4.1-flash for chat,
    deepseek-v4-pro:0813 for research/viz). OpenAI-compatible /v1/chat/completions.
    (deepseek-v4-flash was RETIRED by Ollama Cloud 2026-09-25 - HTTP 410.)
  - "perplexity": Perplexity sonar-pro chat completions.

The Journalist report writer keeps the Perplexity Agent API (web_search +
finance_search + citations) regardless of provider — that's the research moat.

Public API:
    get_client()               -> (client, model)
    recycle()                   -> drop + close cached client
    probe(force)               -> ProbeResult (TTL-cached)
    classify_error(exc)        -> one of the ERROR_KIND constants
    current_model()            -> model name
    current_provider()         -> "ollama" | "perplexity"
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Iterator
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

import httpx

import config as _config
from data import prefs as _prefs

log = logging.getLogger(__name__)

PERPLEXITY_URL = "https://api.perplexity.ai/chat/completions"
OLLAMA_URL = "https://ollama.com/v1/chat/completions"

# ---------------------------------------------------------------------------
# Per-request credentials (set by the SessionMiddleware from the X-Ollama-Key
# header). A user-supplied Ollama key is used for the current request only —
# it is never persisted, logged, or written to disk. When unset, the server's
# own env key is used. The base URL is always the server's configured endpoint
# (OLLAMA_BASE_URL or the Ollama Cloud default) — users only supply a key.
# ---------------------------------------------------------------------------

current_ollama_key: ContextVar[str | None] = ContextVar("current_ollama_key", default=None)

# ---------------------------------------------------------------------------
# Timeouts & constants
# ---------------------------------------------------------------------------

CHAT_TIMEOUT = 600
PROBE_TIMEOUT = 2
PROBE_TTL = 60

# Error kinds used by UI routing
UNREACHABLE = "unreachable"
TIMEOUT = "timeout"
MODEL_NOT_FOUND = "model_not_found"
SERVER_ERROR = "server_error"
TOOL_ERROR = "tool_error"
UNKNOWN = "unknown"

# Provider names
OLLAMA = "ollama"
PERPLEXITY = "perplexity"


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class ProbeResult:
    ok: bool
    error: str | None = None
    error_kind: str | None = None
    latency_ms: float | None = None
    checked_at: float = field(default_factory=time.time)
    model: str | None = None
    host: str | None = None

    def is_fresh(self, ttl: float = PROBE_TTL) -> bool:
        return (time.time() - self.checked_at) < ttl


# ---------------------------------------------------------------------------
# Module state (process-local, guarded by _lock)
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_client_cache: OpenAICompatClient | None = None
_probe_cache_entry: ProbeResult | None = None

# Client cache keyed by api_key so a user-supplied key never reuses a client
# built for the server's env key.
_client_cache_by_creds: dict[str, OpenAICompatClient] = {}


def _ollama_creds() -> tuple[str, str]:
    """Resolve the active Ollama credentials: request ContextVar first, then
    the server's env key. Returns (api_key, base_url) — the base URL is always
    the server's configured endpoint."""
    key = current_ollama_key.get()
    base_url = _config.OLLAMA_BASE_URL or OLLAMA_URL
    return (key or _config.OLLAMA_API_KEY), base_url


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def current_provider() -> str:
    """Return the active LLM provider: "ollama" (default) or "perplexity"."""
    return _prefs.get("llm_provider") or OLLAMA


def current_model() -> str:
    """Return the model for the active provider's chat client."""
    provider = current_provider()
    if provider == OLLAMA:
        return _prefs.get("ollama_model") or "deepseek-v4.1-flash"
    return _prefs.get("perplexity_model") or "sonar-pro"


def research_model() -> str:
    """Return the model for the agentic viz agent (Ollama research tier)."""
    return _prefs.get("ollama_research_model") or "deepseek-v4-pro"


def writer_model() -> str:
    """Return the model for the Journalist report writer (Ollama research tier).

    Independently tunable from the viz agent so research prose can be pointed
    at a stronger/cheaper model without affecting the canvas.
    """
    return _prefs.get("ollama_writer_model") or "deepseek-v4-pro"


# ---------------------------------------------------------------------------
# Client lifecycle
# ---------------------------------------------------------------------------

def get_client() -> tuple[Any, str]:
    """Return (client, model) for the active provider.

    Both providers speak OpenAI-compatible chat completions, so a single
    OpenAICompatClient class serves both — only the base URL and key differ.
    Ollama credentials resolve per-request (user-supplied key via ContextVar,
    else the server env key); the client cache is keyed by credentials so a
    user key never reuses a server-key client.
    """
    global _client_cache
    provider = current_provider()
    model = current_model()

    if provider == OLLAMA:
        api_key, base_url = _ollama_creds()
        if not api_key:
            raise RuntimeError("OLLAMA_API_KEY is not set")
    else:
        api_key = _config.PERPLEXITY_API_KEY
        base_url = PERPLEXITY_URL
        if not api_key:
            raise RuntimeError("PERPLEXITY_API_KEY is not set")

    with _lock:
        if provider == OLLAMA:
            client = _client_cache_by_creds.get(api_key)
            if client is None:
                client = OpenAICompatClient(
                    api_key=api_key, base_url=base_url, timeout=CHAT_TIMEOUT
                )
                _client_cache_by_creds[api_key] = client
        else:
            if _client_cache is None:
                _client_cache = OpenAICompatClient(
                    api_key=api_key, base_url=base_url, timeout=CHAT_TIMEOUT
                )
            client = _client_cache
    return client, model


def recycle() -> None:
    """Drop the cached clients. Next call rebuilds them."""
    global _client_cache
    with _lock:
        client = _client_cache
        _client_cache = None
        clients = list(_client_cache_by_creds.values())
        _client_cache_by_creds.clear()
    for c in [client, *clients]:
        if c is not None:
            c.close()


def get_client_for_research() -> tuple[Any, str]:
    """Return (client, model) for the agentic viz agent (Ollama research tier).

    Uses a separate cached client so the chat model and research model can
    coexist. Falls back to the chat client if Ollama is unavailable.
    """
    provider = current_provider()
    model = research_model()

    if provider == OLLAMA:
        api_key, base_url = _ollama_creds()
        if not api_key:
            raise RuntimeError("OLLAMA_API_KEY is not set")
        with _lock:
            client = _client_cache_by_creds.get(api_key)
            if client is None:
                client = OpenAICompatClient(
                    api_key=api_key, base_url=base_url, timeout=CHAT_TIMEOUT
                )
                _client_cache_by_creds[api_key] = client
        return client, model

    # Perplexity provider: research model falls back to the chat client
    return get_client()


def get_client_for_writer() -> tuple[Any, str]:
    """Return (client, model) for the Journalist report writer.

    Same cached research client as the viz agent (same provider, same
    credentials) but the independently-tunable writer model. This is the
    Ollama-native writer path — it is only reached when llm_provider is
    "ollama"; the Perplexity path uses the AgentClient instead.
    """
    provider = current_provider()
    model = writer_model()

    if provider == OLLAMA:
        api_key, base_url = _ollama_creds()
        if not api_key:
            raise RuntimeError("OLLAMA_API_KEY is not set")
        with _lock:
            client = _client_cache_by_creds.get(api_key)
            if client is None:
                client = OpenAICompatClient(
                    api_key=api_key, base_url=base_url, timeout=CHAT_TIMEOUT
                )
                _client_cache_by_creds[api_key] = client
        return client, model

    # Perplexity provider: the writer uses the Agent API, not this client.
    raise RuntimeError("get_client_for_writer is only valid for the ollama provider")


# ---------------------------------------------------------------------------
# Probe (TTL-cached)
# ---------------------------------------------------------------------------

def probe(force: bool = False) -> ProbeResult:
    """Check that the active provider's API is reachable and the key is valid."""
    global _probe_cache_entry
    provider = current_provider()
    model = current_model()

    with _lock:
        cached = _probe_cache_entry
    if cached and not force and cached.is_fresh():
        return cached

    if provider == OLLAMA:
        api_key, _ = _ollama_creds()
        host = "ollama"
        url = "https://ollama.com/api/tags"
    else:
        api_key = _config.PERPLEXITY_API_KEY
        host = "perplexity"
        url = "https://api.perplexity.ai/"

    if not api_key:
        result = ProbeResult(
            ok=False,
            error=f"{host.upper()}_API_KEY not set",
            error_kind=UNREACHABLE,
            model=model,
            host=host,
        )
    else:
        try:
            t0 = time.perf_counter()
            with httpx.Client(timeout=PROBE_TIMEOUT) as hc:
                r = hc.get(url, headers={"Authorization": f"Bearer {api_key}"})
            latency = (time.perf_counter() - t0) * 1000.0
            if r.status_code in (401, 403):
                result = ProbeResult(
                    ok=False,
                    error=f"auth failed ({r.status_code})",
                    error_kind=SERVER_ERROR,
                    latency_ms=latency,
                    model=model,
                    host=host,
                )
            else:
                result = ProbeResult(
                    ok=True, latency_ms=latency, model=model, host=host,
                )
        except Exception as e:
            result = ProbeResult(
                ok=False,
                error=f"{type(e).__name__}: {e}",
                error_kind=classify_error(e),
                model=model,
                host=host,
            )

    with _lock:
        _probe_cache_entry = result
    return result


def probe_with(api_key: str) -> ProbeResult:
    """Validate a user-supplied Ollama key against the server's endpoint.

    Used by the settings "test connection" endpoint. The key is used for this
    probe only and is never stored. Returns a ProbeResult (not cached — the
    user's key is transient).

    Validation hits the chat-completions endpoint with a minimal request: the
    /api/tags endpoint returns 200 for any key, so it can't distinguish a
    valid key from an invalid one.
    """
    model = current_model()
    host = "ollama"
    url = (_config.OLLAMA_BASE_URL or OLLAMA_URL).rstrip("/")
    if not url.endswith("/chat/completions"):
        url += "/chat/completions"
    if not api_key:
        return ProbeResult(ok=False, error="OLLAMA_API_KEY not set", error_kind=UNREACHABLE, model=model, host=host)
    try:
        t0 = time.perf_counter()
        with httpx.Client(timeout=PROBE_TIMEOUT) as hc:
            r = hc.post(
                url,
                json={"model": model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 1},
                headers={"Authorization": f"Bearer {api_key}"},
            )
        latency = (time.perf_counter() - t0) * 1000.0
        if r.status_code in (401, 403):
            return ProbeResult(
                ok=False, error=f"auth failed ({r.status_code})", error_kind=SERVER_ERROR,
                latency_ms=latency, model=model, host=host,
            )
        if r.status_code >= 400:
            return ProbeResult(
                ok=False, error=f"API error ({r.status_code})", error_kind=SERVER_ERROR,
                latency_ms=latency, model=model, host=host,
            )
        return ProbeResult(ok=True, latency_ms=latency, model=model, host=host)
    except Exception as e:
        return ProbeResult(
            ok=False, error=f"{type(e).__name__}: {e}", error_kind=classify_error(e), model=model, host=host,
        )


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------

def classify_error(exc: Exception) -> str:
    name = type(exc).__name__
    msg = str(exc).lower()

    if name in {"ConnectError", "ConnectTimeout"} or "connection refused" in msg or "getaddrinfo" in msg:
        return UNREACHABLE
    if "timed out" in msg or name in {"ReadTimeout", "WriteTimeout", "PoolTimeout"}:
        return TIMEOUT
    if "model" in msg and "not found" in msg:
        return MODEL_NOT_FOUND
    if "401" in msg or "403" in msg:
        return SERVER_ERROR
    if "500" in msg or "5xx" in msg or "internal server error" in msg or "502" in msg or "503" in msg:
        return SERVER_ERROR
    return UNKNOWN


# ---------------------------------------------------------------------------
# OpenAI-compatible client — serves both Ollama cloud and Perplexity
# ---------------------------------------------------------------------------

# Model catalog with per-role fallbacks. Ollama Cloud retires models over
# time (deepseek-v3.x → deepseek-v4.1-flash per the retirement schedule; the
    # prior chat default deepseek-v4-flash went 410 on 2026-09-25 — a
# retired/renamed model must degrade to a working fallback, not a red screen.
# Fallbacks are per role: chat (Compass), research (viz), writer (Journalist).
_OLLAMA_CHAT_FALLBACKS = ("deepseek-v4.1-flash", "glm-5.3-flash", "qwen3.5")
_OLLAMA_RESEARCH_FALLBACKS = ("deepseek-v4-pro:0813", "deepseek-v4.1-flash", "qwen3.5:397b")
_OLLAMA_WRITER_FALLBACKS = ("deepseek-v4-pro", "deepseek-v4-flash", "qwen3.5:397b")


def _model_fallbacks(role: str) -> tuple[str, ...]:
    """Return the ordered fallback chain for a model role."""
    if role == "chat":
        return _OLLAMA_CHAT_FALLBACKS
    if role == "research":
        return _OLLAMA_RESEARCH_FALLBACKS
    if role == "writer":
        return _OLLAMA_WRITER_FALLBACKS
    return ()


class OpenAICompatClient:
    """HTTP client for OpenAI-compatible chat completions with tool calling.

    Serves both providers:
      - Ollama cloud: https://ollama.com/v1/chat/completions (deepseek-v4-*)
      - Perplexity:   https://api.perplexity.ai/chat/completions (sonar-pro)

    Supports streaming, tool calling (OpenAI function-calling format),
    citation extraction, and reasoning-effort control (Ollama thinking).
    On a model-not-found / server error, falls back to the next model in the
    role's catalog chain so a retired model degrades to a working one.
    """

    def __init__(self, api_key: str, base_url: str, timeout: float = 600.0):
        self._api_key = api_key
        # Accept either the base (https://ollama.com/v1) or the full
        # chat-completions endpoint; normalize to the full endpoint.
        if not base_url.endswith("/chat/completions"):
            base_url = base_url.rstrip("/") + "/chat/completions"
        self._base_url = base_url
        self._client = httpx.Client(timeout=timeout)

    def close(self):
        try:
            self._client.close()
        except Exception as e:
            log.debug("LLM client close failed: %s", e)

    def _is_retryable_model_error(self, exc: Exception) -> bool:
        return classify_error(exc) in (MODEL_NOT_FOUND, SERVER_ERROR, UNREACHABLE, TIMEOUT)

    def chat(self, *, model: str, messages: list[dict], stream: bool = False,
             tools: list | None = None, reasoning_effort: str | None = None,
             role: str | None = None, **_ignored) -> Any:
        fallbacks = _model_fallbacks(role) if role else ()
        candidates = [model] + [m for m in fallbacks if m != model]
        if not stream:
            return self._chat_with_fallback(
                candidates, messages=messages, stream=False,
                tools=tools, reasoning_effort=reasoning_effort,
            )
        return self._stream_with_fallback(
            candidates, messages=messages, tools=tools,
            reasoning_effort=reasoning_effort,
        )

    def _chat_with_fallback(self, candidates: list[str], *, messages: list[dict],
                            stream: bool, tools: list | None,
                            reasoning_effort: str | None) -> Any:
        last_exc: Exception | None = None
        for i, candidate in enumerate(candidates):
            try:
                return self._chat_once(
                    model=candidate, messages=messages, stream=stream,
                    tools=tools, reasoning_effort=reasoning_effort,
                )
            except Exception as e:
                last_exc = e
                if not self._is_retryable_model_error(e):
                    raise
                nxt = candidates[i + 1] if i + 1 < len(candidates) else "none"
                log.warning("Model %s failed (%s); trying fallback %s",
                            candidate, classify_error(e), nxt)
        raise last_exc if last_exc is not None else RuntimeError("no model candidates")

    def _stream_with_fallback(self, candidates: list[str], *, messages: list[dict],
                              tools: list | None, reasoning_effort: str | None) -> Iterator[dict]:
        """Stream with model fallback. Errors surface during iteration, so the
        fallback chain is driven from inside the generator."""
        last_exc: Exception | None = None
        for i, candidate in enumerate(candidates):
            try:
                payload = self._build_payload(
                    candidate, messages, stream=True, tools=tools,
                    reasoning_effort=reasoning_effort,
                )
                yield from self._stream(payload, self._headers(stream=True))
                return
            except Exception as e:
                last_exc = e
                if not self._is_retryable_model_error(e):
                    raise
                nxt = candidates[i + 1] if i + 1 < len(candidates) else "none"
                log.warning("Stream model %s failed (%s); trying fallback %s",
                            candidate, classify_error(e), nxt)
        if last_exc is not None:
            raise last_exc

    def _build_payload(self, model: str, messages: list[dict], *, stream: bool,
                       tools: list | None, reasoning_effort: str | None) -> dict:
        payload: dict[str, Any] = {
            "model": model,
            "messages": _sanitize_messages(messages),
            "stream": bool(stream),
        }
        if tools:
            payload["tools"] = tools
        if reasoning_effort:
            payload["reasoning_effort"] = reasoning_effort
        return payload

    def _headers(self, *, stream: bool) -> dict:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if stream else "application/json",
        }

    def _chat_once(self, *, model: str, messages: list[dict], stream: bool,
                   tools: list | None, reasoning_effort: str | None) -> Any:
        payload = self._build_payload(
            model, messages, stream=stream, tools=tools,
            reasoning_effort=reasoning_effort,
        )
        headers = self._headers(stream=stream)
        if stream:
            return self._stream(payload, headers)

        r = self._client.post(self._base_url, json=payload, headers=headers)
        r.raise_for_status()
        body = r.json()
        choice = (body.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        return {
            "message": {
                "content": msg.get("content", ""),
                "tool_calls": msg.get("tool_calls"),
            },
            "done": True,
        }

    def _stream(self, payload: dict, headers: dict) -> Iterator[dict]:
        """Yield chunks in a normalized format.

        Handles both text streaming and tool call streaming. Perplexity
        returns citations and search_results at the top level of chunks.
        Tool calls arrive as delta.tool_calls in OpenAI format.
        """
        citations: list[str] = []
        search_results: list[dict] = []
        # Accumulate tool calls across deltas — OpenAI streams them in pieces
        tool_call_accum: dict[int, dict] = {}

        with self._client.stream("POST", self._base_url, json=payload,
                                 headers=headers) as r:
            if r.status_code >= 400:
                body = b"".join(r.iter_bytes()).decode("utf-8", "replace")
                raise RuntimeError(f"LLM API {r.status_code}: {body[:300]}")

            for line in r.iter_lines():
                if not line:
                    continue
                if line.startswith("data: "):
                    data = line[6:]
                elif line.startswith("data:"):
                    data = line[5:]
                else:
                    continue
                data = data.strip()
                if not data:
                    continue
                if data == "[DONE]":
                    # Emit final chunk with accumulated tool calls
                    final_tool_calls = None
                    if tool_call_accum:
                        final_tool_calls = _finalize_tool_calls(tool_call_accum)
                    yield {
                        "message": {
                            "content": "",
                            "tool_calls": final_tool_calls,
                        },
                        "done": True,
                        "citations": citations,
                        "search_results": search_results,
                    }
                    continue

                try:
                    obj = json.loads(data)
                except json.JSONDecodeError:
                    continue

                if isinstance(obj.get("citations"), list):
                    citations = obj["citations"]
                if isinstance(obj.get("search_results"), list):
                    search_results = obj["search_results"]

                choice = (obj.get("choices") or [{}])[0]
                delta = choice.get("delta") or {}
                content = delta.get("content") or ""
                finish = choice.get("finish_reason")

                # Accumulate streamed tool call deltas
                delta_tool_calls = delta.get("tool_calls")
                if delta_tool_calls:
                    for tc in delta_tool_calls:
                        idx = tc.get("index", 0)
                        if idx not in tool_call_accum:
                            tool_call_accum[idx] = {
                                "id": tc.get("id", ""),
                                "type": "function",
                                "function": {"name": "", "arguments": ""},
                            }
                        entry = tool_call_accum[idx]
                        if tc.get("id"):
                            entry["id"] = tc["id"]
                        fn = tc.get("function", {})
                        if fn.get("name"):
                            entry["function"]["name"] = fn["name"]
                        if fn.get("arguments"):
                            entry["function"]["arguments"] += fn["arguments"]

                # Yield content tokens as they arrive
                tool_calls_snapshot = None
                if finish and tool_call_accum:
                    tool_calls_snapshot = _finalize_tool_calls(tool_call_accum)

                chunk = {
                    "message": {
                        "content": content,
                        "tool_calls": tool_calls_snapshot,
                    },
                    "done": bool(finish),
                }
                if finish:
                    chunk["citations"] = citations
                    chunk["search_results"] = search_results
                yield chunk


def _finalize_tool_calls(accum: dict[int, dict]) -> list[dict]:
    """Convert accumulated tool call deltas into final tool call dicts."""
    result = []
    for idx in sorted(accum.keys()):
        tc = accum[idx]
        args_str = tc["function"]["arguments"]
        try:
            args = json.loads(args_str) if args_str else {}
        except json.JSONDecodeError:
            args = {}
        result.append({
            "id": tc.get("id", f"call_{idx}"),
            "function": {
                "name": tc["function"]["name"],
                "arguments": args,
            },
        })
    return result


def _sanitize_messages(messages: list[dict]) -> list[dict]:
    """Convert internal message history to OpenAI-compatible format.

    Handles tool role messages by converting them to the OpenAI tool
    response format, and ensures assistant messages with tool_calls
    are properly structured.
    """
    out: list[dict] = []
    for m in messages:
        role = m.get("role")
        content = m.get("content", "") or ""

        if role == "tool":
            # OpenAI format: tool messages need a tool_call_id
            tool_call_id = m.get("tool_call_id", "")
            if tool_call_id:
                out.append({
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": content,
                })
            else:
                # Fallback: inject as user context if no tool_call_id. This
                # converts a tool result into user text — the model can't
                # distinguish it from actual user input. Log it so a pattern
                # of these is visible rather than silently corrupting history.
                log.warning("Tool message without tool_call_id converted to user role")
                out.append({
                    "role": "user",
                    "content": f"[tool result]\n{content}",
                })
        elif role == "assistant":
            msg: dict[str, Any] = {"role": "assistant", "content": content}
            tc = m.get("tool_calls")
            if tc:
                # Re-serialize tool_calls in OpenAI format
                openai_tcs = []
                for t in tc:
                    func = t.get("function", {})
                    args = func.get("arguments", {})
                    if isinstance(args, dict):
                        args = json.dumps(args)
                    openai_tcs.append({
                        "id": t.get("id", ""),
                        "type": "function",
                        "function": {
                            "name": func.get("name", ""),
                            "arguments": args,
                        },
                    })
                msg["tool_calls"] = openai_tcs
            out.append(msg)
        elif role in ("system", "user"):
            out.append({"role": role, "content": content})

    return out


# ---------------------------------------------------------------------------
# Agent API client — for Journalist (sonar-reasoning-pro, built-in tools)
# ---------------------------------------------------------------------------

AGENT_URL = "https://api.perplexity.ai/v1/agent"
AGENT_TIMEOUT = 300


class AgentClient:
    """Client for Perplexity's Agent API (/v1/agent).

    Provides access to sonar-reasoning-pro with built-in web_search,
    finance_search, and custom function calling. Non-streaming.
    """

    def __init__(self, api_key: str, timeout: float = AGENT_TIMEOUT):
        self._api_key = api_key
        self._client = httpx.Client(timeout=timeout)

    def close(self):
        try:
            self._client.close()
        except Exception as e:
            log.debug("LLM client close failed: %s", e)

    def run(
        self,
        *,
        model: str,
        input_items: list[dict],
        tools: list[dict] | None = None,
        instructions: str | None = None,
        max_output_tokens: int = 8192,
        tool_choice: str | None = None,
    ) -> dict:
        """Single Agent API call. Returns the full response dict.

        Returns:
            {
                "output": [...],       # message + function_call items
                "output_text": "...",  # aggregated text
                "usage": {...},
                "status": "completed",
            }
        """
        payload: dict[str, Any] = {
            "model": model,
            "input": input_items,
            "max_output_tokens": max_output_tokens,
        }
        if tools:
            payload["tools"] = tools
        if instructions:
            payload["instructions"] = instructions
        if tool_choice:
            payload["tool_choice"] = tool_choice

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        r = self._client.post(AGENT_URL, json=payload, headers=headers)
        if r.status_code >= 400:
            body = r.text[:500]
            raise RuntimeError(f"Agent API {r.status_code}: {body}")

        return r.json()

    def extract_function_calls(self, response: dict) -> list[dict]:
        """Extract function_call items from an Agent API response."""
        calls = []
        for item in response.get("output", []):
            if item.get("type") == "function_call":
                args_str = item.get("arguments", "{}")
                try:
                    args = json.loads(args_str) if isinstance(args_str, str) else args_str
                except json.JSONDecodeError:
                    args = {}
                calls.append({
                    "call_id": item.get("call_id", item.get("id", "")),
                    "name": item.get("name", ""),
                    "arguments": args,
                    "raw": item,
                })
        return calls

    def extract_text(self, response: dict) -> str:
        """Extract the text content from an Agent API response."""
        parts: list[str] = []
        for item in response.get("output", []):
            if item.get("type") == "message":
                for block in item.get("content", []):
                    if block.get("type") == "output_text":
                        parts.append(block.get("text", ""))
        return "".join(parts)

    def extract_citations(self, response: dict) -> list[str]:
        """Extract citation URLs from an Agent API response."""
        urls: list[str] = []
        for item in response.get("output", []):
            if item.get("type") == "message":
                for block in item.get("content", []):
                    for ann in block.get("annotations", []):
                        if ann.get("type") == "url_citation":
                            url = ann.get("url", "")
                            if url and url not in urls:
                                urls.append(url)
        return urls


_agent_cache: AgentClient | None = None


def get_agent_client() -> AgentClient:
    """Return a cached AgentClient instance."""
    global _agent_cache
    api_key = _config.PERPLEXITY_API_KEY
    if not api_key:
        raise RuntimeError("PERPLEXITY_API_KEY is not set")
    with _lock:
        if _agent_cache is None:
            _agent_cache = AgentClient(api_key=api_key, timeout=AGENT_TIMEOUT)
    return _agent_cache
