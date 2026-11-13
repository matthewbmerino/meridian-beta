"""Compass agent loop — streaming Perplexity tool-calling.

Event grammar yielded by `run()`:

    {"type": "turn_start",      "iteration": int, "model": str}
    {"type": "token",           "content": str}
    {"type": "tool_call",       "name": str, "args": dict, "id": str}
    {"type": "tool_result",     "name": str, "result": dict, "id": str}
    {"type": "text",            "content": str}          # full aggregated text
    {"type": "citations",       "urls": list, "results": list}
    {"type": "connection_lost", "kind": str, "detail": str}
    {"type": "error",           "kind": str, "content": str}
    {"type": "aborted"}
    {"type": "done",            "iterations": int, "elapsed_ms": float}
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator

from agent import compute
from agent.data_guard import guard_tool_call
from agent.history import append, get_history
from agent.tools import execute_tool, get_tool_schemas

#: Tools whose results carry content the operator did not author. Once any of
#: them returns text, the turn is "tainted": write tools are refused for the
#: rest of the run, because the next tool call may be an instruction embedded
#: in a page or search result rather than the operator's own intent.
UNTRUSTED_TOOLS = frozenset({"fetch_page", "search_web", "hermes_research"})

MAX_ITERATIONS = 12


def get_current_model() -> str:
    return compute.current_model()


def probe():
    """Probe shim returning (ok, error)."""
    r = compute.probe()
    return r.ok, r.error


def run(
    user_message: str,
    abort_flag: Callable[[], bool] | None = None,
) -> Iterator[dict]:
    """Run one agent turn. Yields event dicts (see module docstring)."""
    started = time.perf_counter()
    try:
        client, model = compute.get_client()
    except Exception as e:
        yield {"type": "error", "kind": compute.classify_error(e), "content": str(e)}
        return

    append({"role": "user", "content": user_message})
    tool_schemas = get_tool_schemas()

    # Running source index for the Data Guard: every data-tool result this
    # conversation has produced, keyed like Journalist's gathered dict
    # (f"{tool}({SYMBOL})"). Viz tool emissions are cross-checked against it
    # so Compass cannot ship a wrong or unverifiable number in a graph/table.
    gathered: dict[str, dict] = {}

    # Set once any untrusted-content tool returns; persists for the whole run.
    tainted = False

    for iteration in range(MAX_ITERATIONS):
        if abort_flag is not None and abort_flag():
            yield {"type": "aborted"}
            return

        yield {
            "type": "turn_start",
            "iteration": iteration,
            "model": model,
            "mode": "perplexity",
        }

        accumulated = ""
        captured_tool_calls: list = []
        citations: list = []
        search_results: list = []
        stream_failed = False
        failure_kind: str | None = None
        failure_detail: str | None = None

        try:
            stream = client.chat(
                model=model,
                messages=get_history(),
                tools=tool_schemas,
                stream=True,
                role="chat",
            )
            for chunk in stream:
                if abort_flag is not None and abort_flag():
                    yield {"type": "aborted"}
                    return

                msg = _chunk_message(chunk)
                delta = msg.get("content") or ""
                if delta:
                    accumulated += delta
                    yield {"type": "token", "content": delta}

                chunk_tool_calls = msg.get("tool_calls")
                if chunk_tool_calls and not captured_tool_calls:
                    captured_tool_calls = list(chunk_tool_calls)

                if isinstance(chunk, dict):
                    if chunk.get("citations"):
                        citations = chunk["citations"]
                    if chunk.get("search_results"):
                        search_results = chunk["search_results"]

                if _chunk_done(chunk):
                    pass
        except Exception as e:
            stream_failed = True
            failure_kind = compute.classify_error(e)
            failure_detail = f"{type(e).__name__}: {e}"

        if stream_failed:
            compute.recycle()
            yield {
                "type": "connection_lost",
                "kind": failure_kind or compute.UNKNOWN,
                "detail": failure_detail or "",
            }
            return

        tool_calls = captured_tool_calls

        if tool_calls:
            append({
                "role": "assistant",
                "content": accumulated,
                "tool_calls": tool_calls,
            })

            for tc in tool_calls:
                if abort_flag is not None and abort_flag():
                    yield {"type": "aborted"}
                    return
                func = tc.get("function", {}) if isinstance(tc, dict) else {}
                name = func.get("name", "unknown")
                args = func.get("arguments", {}) or {}
                tc_id = tc.get("id", "") if isinstance(tc, dict) else ""

                # Data Guard: cross-check emitted visualization payloads against
                # the data this conversation has already gathered, so Compass
                # cannot render a wrong or unverifiable number in a graph/table.
                guarded_args, corrections = guard_tool_call(name, args, gathered)
                if corrections:
                    yield {
                        "type": "tool_result",
                        "name": "data_guard",
                        "result": {"corrections": corrections},
                        "id": f"guard_{tc_id or 'call'}",
                    }
                # A table reduced to nothing by the guard is never emitted.
                # `None` (not falsy `{}`) is the drop sentinel — `{}` is the
                # legitimate args for every zero-argument tool.
                if guarded_args is None:
                    continue

                yield {"type": "tool_call", "name": name, "args": guarded_args, "id": tc_id}

                try:
                    # Once third-party content has entered this conversation, a
                    # write tool call may be an injected instruction rather than
                    # the operator's intent. Reads stay allowed; writes are
                    # refused for the rest of the run. The kwarg is passed ONLY
                    # when tainted so a two-argument stub/monkeypatch of
                    # execute_tool stays valid on the ordinary path.
                    if tainted:
                        result = execute_tool(name, guarded_args, allow_write=False)
                    else:
                        result = execute_tool(name, guarded_args)
                except Exception as e:
                    result = {"error": f"{type(e).__name__}: {e}", "kind": compute.TOOL_ERROR}

                if name in UNTRUSTED_TOOLS and not result.get("error"):
                    tainted = True
                    yield {
                        "type": "tool_result",
                        "name": "taint",
                        "result": {"tainted": True, "source": name},
                        "id": f"taint_{tc_id or 'call'}",
                    }

                # Accumulate data-tool results so later viz calls are verifiable.
                sym = str(args.get("symbol", "")).upper()
                if sym and not result.get("error"):
                    gathered[f"{name}({sym})"] = result

                yield {"type": "tool_result", "name": name, "result": result, "id": tc_id}

                append({
                    "role": "tool",
                    "content": json.dumps(result, default=str),
                    "tool_call_id": tc_id,
                })
            continue

        # No tool calls — final response
        content = accumulated
        append({"role": "assistant", "content": content})
        yield {"type": "text", "content": content}
        if citations or search_results:
            yield {
                "type": "citations",
                "urls": citations,
                "results": search_results,
            }
        yield {
            "type": "done",
            "iterations": iteration + 1,
            "elapsed_ms": (time.perf_counter() - started) * 1000.0,
        }
        return

    yield {
        "type": "error",
        "kind": compute.UNKNOWN,
        "content": "Reached maximum tool call iterations.",
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _chunk_message(chunk) -> dict:
    if isinstance(chunk, dict):
        return chunk.get("message", {})
    msg = getattr(chunk, "message", None)
    if msg is None:
        return {}
    if hasattr(msg, "model_dump"):
        return msg.model_dump()
    return {
        "content": getattr(msg, "content", "") or "",
        "tool_calls": getattr(msg, "tool_calls", None),
    }


def _chunk_done(chunk) -> bool:
    if isinstance(chunk, dict):
        return bool(chunk.get("done"))
    return bool(getattr(chunk, "done", False))
