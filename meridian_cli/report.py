"""Report a problem and a recommended solution.

The report is deliberately two fields plus a minimal environment fingerprint.
It never includes the book, positions, theses, decisions, liabilities, imports,
logs, file paths, hostnames, usernames, or credentials. Nothing leaves the
machine unless `--send` is passed.
"""

from __future__ import annotations

import json
import os
import platform
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import home, net, ui

CONTEXTS = ("setup", "start", "update", "import", "ui", "api", "docs", "other")
KINDS = ("bug", "missing-flow", "architecture", "docs", "other")
MIN_LEN = 10
MAX_LEN = 2000

_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_BEARER = re.compile(r"(?i)\bbearer\s+\S+")
_KEYLIKE = re.compile(r"\b(?:sk|ghp|gho|ghu|ghs|xox[baprs]|AKIA)[A-Za-z0-9_\-]{10,}\b")
_HEX = re.compile(r"\b[0-9a-fA-F]{32,}\b")
_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_USERPATH = re.compile(r"/(?:Users|home)/[^/\s]+")


def scrub(text: str) -> str:
    """Remove the things a report must never carry."""
    text = str(text or "")
    try:
        text = text.replace(str(Path.home()), "~")
    except Exception:
        pass
    text = _USERPATH.sub("~", text)
    text = _EMAIL.sub("[email]", text)
    text = _BEARER.sub("Bearer [redacted]", text)
    text = _KEYLIKE.sub("[redacted]", text)
    text = _HEX.sub("[redacted]", text)
    text = _IPV4.sub("[ip]", text)
    # Drop control characters except newline and tab.
    text = "".join(ch for ch in text if ch in "\n\t" or ch >= " ")
    return text.strip()


def _check_field(label: str, value: str) -> str:
    value = scrub(value)
    if len(value) < MIN_LEN:
        ui.die(f"{label} is too short — write at least {MIN_LEN} characters.")
    if len(value) > MAX_LEN:
        ui.die(f"{label} is too long — keep it under {MAX_LEN} characters.")
    return value


def environment(cfg: dict, context: str, kind: str = "other") -> dict:
    node = ""
    node_bin = home.which_node()
    if node_bin:
        try:
            node = subprocess.run([node_bin, "-v"], capture_output=True, text=True, timeout=5).stdout.strip().lstrip("v")
        except Exception:
            node = ""
    return {
        "context": context if context in CONTEXTS else "other",
        "kind": kind if kind in KINDS else "other",
        "version": home.read_version(),
        "channel": home.read_channel(),
        "os": platform.system().lower() or "",
        "arch": platform.machine() or "",
        "python": platform.python_version(),
        "node": node,
        "agent": str(home.get_cfg(cfg, "agent.runtime", "none")),
    }


def _reports_dir() -> Path:
    path = home.HOME / "reports"
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


def _path(report_id: str) -> Path:
    return _reports_dir() / f"{report_id}.json"


def _write(record: dict) -> Path:
    path = _path(record["id"])
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    return path


def _read(report_id: str) -> dict:
    path = _path(report_id)
    if not path.exists():
        ui.die(f"No local report with id {report_id}.")
    return json.loads(path.read_text(encoding="utf-8"))


def _all() -> list[dict]:
    records = []
    for path in sorted(_reports_dir().glob("*.json"), reverse=True):
        try:
            records.append(json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            continue
    records.sort(key=lambda r: r.get("created_at", ""), reverse=True)
    return records


def _new_id(problem: str, solution: str) -> str:
    import hashlib

    stamp = datetime.now(timezone.utc).isoformat()
    return hashlib.sha256(f"{problem}\n{solution}\n{stamp}".encode()).hexdigest()[:12]


def _payload(problem: str, solution: str, env: dict) -> dict:
    return {"problem": problem, "solution": solution, **env}


def _send(cfg: dict, record: dict) -> dict:
    endpoint = str(home.get_cfg(cfg, "reports.endpoint", "") or "")
    if not endpoint:
        raise RuntimeError("No reports.endpoint configured.")
    keys = ("problem", "solution", "context", "kind", "version", "channel",
            "os", "arch", "python", "node", "agent")
    payload = {key: record.get(key, "") for key in keys}
    response = net.post_public(endpoint, payload)
    if not response or not response.get("ok"):
        raise RuntimeError((response or {}).get("error") or "the report endpoint refused the report")
    record["sent"] = True
    record["remote_id"] = response.get("id", "")
    if response.get("issue_url"):
        record["issue_url"] = response["issue_url"]
    record["sent_at"] = datetime.now(timezone.utc).isoformat()
    _write(record)
    return response


def _list() -> None:
    records = _all()
    if not records:
        ui.info("No local reports.")
        return
    rows = []
    for record in records:
        problem = str(record.get("problem", "")).replace("\n", " ")
        rows.append([
            record.get("id", ""),
            record.get("kind", "-"),
            str(record.get("created_at", ""))[:16].replace("T", " "),
            "yes" if record.get("sent") else "no",
            problem[:64],
        ])
    ui.table(["ID", "KIND", "CREATED", "SENT", "PROBLEM"], rows)


def run(args) -> int:
    cfg = home.load_config()

    if args.list:
        if args.json:
            ui.json_out({"ok": True, "reports": _all()})
        else:
            _list()
        return 0

    if args.show:
        record = _read(args.show)
        ui.json_out(record)
        return 0

    if args.send_all:
        pending = [r for r in _all() if not r.get("sent")]
        if not pending:
            ui.ok("No unsent reports.")
            return 0
        sent = 0
        failed = 0
        for record in pending:
            try:
                _send(cfg, record)
                sent += 1
                ui.ok(f"Sent {record['id']}")
            except Exception as exc:
                failed += 1
                ui.warn(f"Could not send {record['id']}: {exc}")
        if args.json:
            ui.json_out({"ok": failed == 0, "sent": sent, "failed": failed})
        return 0 if failed == 0 else 1

    if args.id:
        record = _read(args.id)
        if not args.send:
            ui.json_out(record)
            return 0
        try:
            response = _send(cfg, record)
        except Exception as exc:
            ui.fail(f"Could not send: {exc}")
            return 1
        if args.json:
            ui.json_out({"ok": True, "id": record["id"], "remote_id": response.get("id"), "sent": True})
        else:
            ui.ok(f"Sent report {response.get('id') or record['id']}")
        return 0

    interactive = sys.stdin.isatty()
    problem = args.problem
    solution = args.solution
    if not problem:
        if not interactive:
            ui.die('Pass --problem "what went wrong" (and --solution) when not on a terminal.')
        ui.banner("Report a problem", "Two fields. No book data.")
        problem = ui.ask("What went wrong?")
    if not solution:
        if not interactive:
            ui.die('Pass --solution "what you would change".')
        solution = ui.ask("What would you change?")

    problem = _check_field("Problem", problem)
    solution = _check_field("Solution", solution)
    kind = args.kind if args.kind in KINDS else "other"
    env = environment(cfg, args.context, kind)
    payload = _payload(problem, solution, env)
    report_id = _new_id(problem, solution)
    record = {
        "id": report_id,
        **payload,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "sent": False,
        "remote_id": None,
    }

    if args.dry_run:
        ui.json_out({"ok": True, "dry_run": True, "payload": payload})
        return 0

    path = _write(record)

    if not args.yes:
        print()
        ui.kv([
            ("Problem", problem),
            ("Solution", solution),
            ("Context", env["context"]),
            ("Kind", env["kind"]),
            ("Environment", f"Meridian {env['version']} · {env['os']}/{env['arch']} · "
                            f"Python {env['python']}" + (f" · Node {env['node']}" if env["node"] else "") +
                            f" · agent {env['agent']}"),
        ])
        print()
        print(ui.dim("  Only the text above and the environment line are sent."))
        print(ui.dim("  Never your book, positions, theses, decisions, logs, paths, or keys."))
        print()
        if not ui.confirm("Send this report?", default=False):
            ui.ok(f"Saved locally: {path}")
            ui.info("Send later with `meridian report --send-all`.")
            if args.json:
                ui.json_out({"ok": True, "id": report_id, "sent": False, "path": str(path)})
            return 0

    if args.send:
        try:
            response = _send(cfg, record)
            report_id = response.get("id") or report_id
            ui.ok(f"Report sent: {report_id}")
            if response.get("issue_url"):
                ui.info(f"GitHub issue: {response['issue_url']}")
        except Exception as exc:
            ui.warn(f"Could not send: {exc}")
            ui.info(f"Saved locally: {path}. Retry with `meridian report --send-all`.")
            if args.json:
                ui.json_out({"ok": False, "id": record["id"], "sent": False, "error": str(exc)})
            return 1
    else:
        ui.ok(f"Saved locally: {path}")
        ui.info("Nothing was sent. Add `--send` to submit it.")

    if args.json:
        ui.json_out({"ok": True, "id": record["id"], "remote_id": record.get("remote_id"),
                     "sent": bool(record.get("sent")), "issue_url": record.get("issue_url"),
                     "path": str(path)})
    return 0
