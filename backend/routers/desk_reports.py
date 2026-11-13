"""Public intake for Meridian Desk product reports.

A report is exactly two things — a problem and a recommended solution — plus a
small environment fingerprint (version, OS, arch, Python, Node, agent). It
never carries the book, positions, theses, decisions, liabilities, imports,
logs, file paths, hostnames, usernames, or credentials. The submitter's IP is
used only for in-memory rate limiting and is never stored.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from backend.auth import require_auth
from data.session import data_root

router = APIRouter(prefix="/api/desk", tags=["desk"])
log = logging.getLogger("meridian.desk_reports")

MAX_FIELD = 2000
MIN_FIELD = 10
CONTEXTS = ("setup", "start", "update", "import", "ui", "api", "docs", "other")
KINDS = ("bug", "missing-flow", "visual", "architecture", "docs", "other")

_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_BEARER = re.compile(r"(?i)\bbearer\s+\S+")
_KEYLIKE = re.compile(r"\b(?:sk|ghp|gho|ghu|ghs|xox[baprs]|AKIA)[A-Za-z0-9_\-]{10,}\b")
_HEX = re.compile(r"\b[0-9a-fA-F]{32,}\b")
_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_USERPATH = re.compile(r"/(?:Users|home)/[^/\s]+")

_HITS: dict[str, list[float]] = {}
_LOCK = threading.Lock()
_WINDOW_SECONDS = 3600
_MAX_PER_WINDOW = 20


class ReportBody(BaseModel):
    problem: str
    solution: str
    context: str = "other"
    kind: str = "other"
    version: str = ""
    channel: str = ""
    os: str = ""
    arch: str = ""
    python: str = ""
    node: str = ""
    agent: str = ""


def _redact(text: str) -> str:
    text = str(text or "")
    text = _USERPATH.sub("~", text)
    text = _EMAIL.sub("[email]", text)
    text = _BEARER.sub("Bearer [redacted]", text)
    text = _KEYLIKE.sub("[redacted]", text)
    text = _HEX.sub("[redacted]", text)
    text = _IPV4.sub("[ip]", text)
    text = "".join(ch for ch in text if ch in "\n\t" or ch >= " ")
    return text.strip()


def _clean(value: str, limit: int = MAX_FIELD) -> str:
    return _redact(value)[:limit]


def _short(value: str) -> str:
    return _clean(value, 40)


def _throttle(client: str) -> None:
    now = time.time()
    with _LOCK:
        recent = [t for t in _HITS.get(client, []) if now - t < _WINDOW_SECONDS]
        if len(recent) >= _MAX_PER_WINDOW:
            _HITS[client] = recent
            raise HTTPException(status_code=429, detail="Too many reports from this address; try later.")
        recent.append(now)
        _HITS[client] = recent
        if len(_HITS) > 2000:  # bound memory on a long-lived process
            _HITS.clear()
            _HITS[client] = recent


def _path() -> Path:
    return data_root() / "desk_reports.jsonl"


def _append(record: dict) -> None:
    path = _path()
    # Public endpoint: bound the active file. One rotation is enough for a v1
    # intake; the maintainer drains it into the repo.
    try:
        if path.exists() and path.stat().st_size > 5 * 1024 * 1024:
            path.replace(path.with_name("desk_reports.1.jsonl"))
    except OSError:
        pass
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
    fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)


def _load(limit: int = 50) -> list[dict]:
    path = _path()
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    out: list[dict] = []
    for line in reversed(lines):
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            out.append(record)
        if len(out) >= limit:
            break
    return out


def _github_issue(record: dict) -> str:
    """File the report as a GitHub issue in the desk repository.

    The token lives only on the intake host. A missing token is not an error:
    the report is still stored and the CLI still gets its id.
    """
    token = os.getenv("MERIDIAN_GITHUB_TOKEN") or os.getenv("GITHUB_TOKEN")
    repo = os.getenv("MERIDIAN_REPORT_REPO", "matthewbmerino/meridian-desk")
    if not token:
        return ""
    kind = record.get("kind", "other")
    title = f"[{kind}] {record.get('problem', '')[:72]}"
    body = (
        f"**Problem**\n\n{record.get('problem', '')}\n\n"
        f"**Recommended solution**\n\n{record.get('solution', '')}\n\n"
        f"**Environment**\n\n"
        f"- kind: `{kind}`\n"
        f"- context: `{record.get('context', '')}`\n"
        f"- version: `{record.get('version', '')}`\n"
        f"- channel: `{record.get('channel', '')}`\n"
        f"- os/arch: `{record.get('os', '')}/{record.get('arch', '')}`\n"
        f"- python/node: `{record.get('python', '')}` / `{record.get('node', '')}`\n"
        f"- agent: `{record.get('agent', '')}`\n"
        f"- report id: `{record.get('id', '')}`\n"
    )
    payload = json.dumps({
        "title": title,
        "body": body,
        "labels": ["report", kind],
    }).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/issues",
        data=payload,
        method="POST",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "meridian-desk-report/1",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            issue = json.loads(response.read())
        return str(issue.get("html_url") or "")
    except Exception as exc:  # noqa: BLE001 - intake must survive GitHub outages
        log.warning("could not file GitHub issue: %s", _redact(str(exc)))
        return ""


@router.post("/report")
def submit_report(body: ReportBody, request: Request):
    client = request.client.host if request.client else "unknown"
    _throttle(client)

    problem = _clean(body.problem)
    solution = _clean(body.solution)
    if len(problem) < MIN_FIELD:
        raise HTTPException(status_code=422, detail=f"problem must be at least {MIN_FIELD} characters")
    if len(solution) < MIN_FIELD:
        raise HTTPException(status_code=422, detail=f"solution must be at least {MIN_FIELD} characters")

    received = datetime.now(timezone.utc).isoformat()
    context = body.context if body.context in CONTEXTS else "other"
    kind = body.kind if body.kind in KINDS else "other"
    digest = hashlib.sha256(f"{problem}\n{solution}\n{received}".encode()).hexdigest()[:12]
    record = {
        "id": digest,
        "received_at": received,
        "problem": problem,
        "solution": solution,
        "context": context,
        "kind": kind,
        "version": _short(body.version),
        "channel": _short(body.channel),
        "os": _short(body.os),
        "arch": _short(body.arch),
        "python": _short(body.python),
        "node": _short(body.node),
        "agent": _short(body.agent),
    }
    issue_url = _github_issue(record)
    if issue_url:
        record["issue_url"] = issue_url
    _append(record)
    log.info("desk report %s kind=%s context=%s agent=%s version=%s",
             digest, kind, context, record["agent"], record["version"])
    return {"ok": True, "id": digest, "issue_url": issue_url}


@router.get("/reports")
def list_reports(limit: int = 50, kind: str | None = None, _=Depends(require_auth)):
    count = max(1, min(int(limit), 200))
    reports = _load(200)
    if kind:
        reports = [report for report in reports if report.get("kind") == kind]
    return {"reports": reports[:count]}
