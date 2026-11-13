"""Persistence layer for Journalist research reports.

JSON-backed storage under data/reports/. Each report is a standalone file
keyed by report_id with metadata + full content.
"""

from __future__ import annotations

import re
import time
import uuid
from pathlib import Path

from data.crypto import load_json_store, write_json
from data.session import data_root

# Report IDs are server-generated (uuid4 hex, 12 chars) — but the load/delete
# endpoints accept arbitrary strings from the client, so validate before
# interpolating into a path. Without this, a report_id like "../../etc/passwd"
# would escape the data directory (path traversal).
_REPORT_ID_RE = re.compile(r"^[a-f0-9]{12}$")


def _valid_report_id(report_id: str) -> bool:
    return bool(_REPORT_ID_RE.match(report_id or ""))


def _dir() -> Path:
    reports = data_root() / "reports"
    reports.mkdir(exist_ok=True)
    return reports


def save_report(
    report_id: str,
    title: str,
    content: str,
    research_type: str,
    topic: str,
    symbols: list[str] | None = None,
    citations: list[str] | None = None,
    elapsed_ms: float | None = None,
    parent_report_id: str | None = None,
    research_state: dict | None = None,
    artifacts: dict | None = None,
    writer_model: str | None = None,
    reviewer_model: str | None = None,
    corrections: list | None = None,
    actions: list | None = None,
    review: dict | None = None,
    status: str = "open",
) -> dict:
    d = _dir()
    path = d / f"{report_id}.json"
    data = {
        "report_id": report_id,
        "title": title,
        "content": content,
        "research_type": research_type,
        "topic": topic,
        "symbols": symbols or [],
        "citations": citations or [],
        "elapsed_ms": elapsed_ms,
        "parent_report_id": parent_report_id,
        "research_state": research_state or {},
        "artifacts": artifacts or {},
        # Provenance + desk state. writer/reviewer name the models that
        # produced and checked the report; corrections keep the reviewer's
        # findings instead of streaming them once and losing them; actions
        # are the parsed action blocks; status tracks the report's thesis.
        "writer_model": writer_model,
        "reviewer_model": reviewer_model,
        "corrections": corrections or [],
        "actions": actions or [],
        # Panel review — the seats, the chief's ruling and the deterministic
        # figure lint. Absent on reports written before the panel existed.
        "review": review or {},
        "status": status,
        "created_at": time.time(),
    }
    try:
        write_json(path, f"report:{report_id}", data)
    except OSError:
        pass
    return data


def load_report(report_id: str) -> dict | None:
    if not _valid_report_id(report_id):
        return None
    d = _dir()
    path = d / f"{report_id}.json"
    if not path.exists():
        return None
    try:
        data = load_json_store(path, f"report:{report_id}", {})
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def list_reports() -> list[dict]:
    d = _dir()
    reports = []
    for path in sorted(d.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            data = load_json_store(path, f"report:{path.stem}", {})
            if not isinstance(data, dict) or "report_id" not in data:
                continue
            reports.append({
                "report_id": data["report_id"],
                "title": data.get("title", "Untitled"),
                "research_type": data.get("research_type", "custom"),
                "topic": data.get("topic", ""),
                "created_at": data.get("created_at"),
                "elapsed_ms": data.get("elapsed_ms"),
                "parent_report_id": data.get("parent_report_id"),
                "symbols": data.get("symbols", []),
                "status": data.get("status", "open"),
                "has_actions": bool(data.get("actions")),
                "review_verdict": ((data.get("review") or {}).get("chief") or {}).get("verdict"),
                "review_score": (data.get("review") or {}).get("composite"),
            })
        except (OSError, KeyError, ValueError):
            continue
    return reports


def delete_report(report_id: str) -> bool:
    if not _valid_report_id(report_id):
        return False
    d = _dir()
    path = d / f"{report_id}.json"
    if path.exists():
        path.unlink()
        return True
    return False


def new_report_id() -> str:
    return uuid.uuid4().hex[:12]


# Fields the API is allowed to change after the fact. Everything else a
# report earned at run time (content, artifacts, corrections) is immutable.
_UPDATABLE = {"status", "title", "writer_model", "reviewer_model", "corrections", "actions", "review"}


def update_report(report_id: str, **fields) -> dict | None:
    """Patch a stored report in place. Returns the updated report or None.

    Unknown fields are ignored rather than written — the store allowlist is
    the last line of defence against a typo inventing a new field nobody reads.
    """
    if not _valid_report_id(report_id):
        return None
    report = load_report(report_id)
    if report is None:
        return None
    changed = False
    for k, v in fields.items():
        if k in _UPDATABLE:
            report[k] = v
            changed = True
    if not changed:
        return report
    try:
        write_json(_dir() / f"{report_id}.json", f"report:{report_id}", report)
    except OSError:
        return None
    return report
