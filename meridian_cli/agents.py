"""Install the Meridian skills into the operator's agent runtime."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from . import ui

SKILLS = ("meridian-setup", "meridian-desk")

RUNTIME_LABELS = {
    "hermes": "Hermes",
    "claude-code": "Claude Code",
    "codex": "Codex",
    "none": "No agent yet",
}

PROMPTS = {
    "hermes": 'Open Hermes and say: "Use the meridian-setup skill to set up Meridian Desk on this machine."',
    "claude-code": 'Open Claude Code and say: "Use the meridian-setup skill to set up Meridian Desk on this machine."',
    "codex": 'Open Codex and say: "Read the Meridian Desk brief and set it up on this machine."',
    "none": "Run `meridian setup` when you are ready.",
}


def skills_src() -> Path:
    return Path(__file__).resolve().parent.parent / "skills"


def target_dir(runtime: str) -> Path | None:
    if runtime == "hermes":
        return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")) / "skills"
    if runtime == "claude-code":
        return Path.home() / ".claude" / "skills"
    if runtime == "codex":
        return Path.home() / ".codex" / "skills"
    return None


def _codex_brief() -> str:
    return """<!-- meridian-desk:start -->
# Meridian Desk

Meridian is a private portfolio desk installed on this machine. Its CLI is
`meridian`; its skills live in `~/.codex/skills/meridian-desk` and
`~/.codex/skills/meridian-setup`.

- Read `meridian-desk/SKILL.md` before answering questions about the book.
- Never invent a number. Read it from the local API or the CLI.
- Writes are limited to the record and the manual ledgers. Nothing sends an order.
- `meridian status --json` and `meridian doctor --json` are the state probes.
- Run `meridian qa --json` before answering, after setup, and after an update.
- Report every defect or missing flow with `meridian report --kind bug|missing-flow|architecture|docs`.
  The intake files it as a GitHub issue; never patch the installed app in place.
<!-- meridian-desk:end -->
"""


def install(runtime: str) -> list[Path]:
    src = skills_src()
    if not src.exists():
        raise FileNotFoundError(f"skills directory missing: {src}")
    if runtime == "none":
        return []
    destination = target_dir(runtime)
    if destination is None:
        return []
    destination.mkdir(parents=True, exist_ok=True)
    installed: list[Path] = []
    for name in SKILLS:
        source = src / name
        if not source.exists():
            ui.warn(f"skill missing from package: {name}")
            continue
        target = destination / name
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(source, target)
        installed.append(target)
    if runtime == "codex":
        brief = Path.home() / ".codex" / "AGENTS.md"
        brief.parent.mkdir(parents=True, exist_ok=True)
        existing = brief.read_text(encoding="utf-8") if brief.exists() else ""
        block = _codex_brief()
        if "<!-- meridian-desk:start -->" in existing:
            start = existing.index("<!-- meridian-desk:start -->")
            end = existing.index("<!-- meridian-desk:end -->") + len("<!-- meridian-desk:end -->")
            existing = existing[:start] + block + existing[end:]
        else:
            existing = existing.rstrip() + "\n\n" + block
        brief.write_text(existing, encoding="utf-8")
        installed.append(brief)
    return installed


def prompt(runtime: str) -> str:
    return PROMPTS.get(runtime, PROMPTS["none"])


def status(runtime: str) -> list[dict]:
    destination = target_dir(runtime)
    if destination is None:
        return []
    out = []
    for name in SKILLS:
        path = destination / name / "SKILL.md"
        out.append({"skill": name, "path": str(path), "installed": path.exists()})
    return out
