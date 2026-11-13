"""Health checks that answer "why is it not working?" before the user asks."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from . import agents, home, net, process, ui


def _ok(name: str, detail: str) -> dict:
    return {"name": name, "status": "ok", "detail": detail}


def _warn(name: str, detail: str, hint: str = "") -> dict:
    return {"name": name, "status": "warn", "detail": detail, "hint": hint}


def _fail(name: str, detail: str, hint: str = "") -> dict:
    return {"name": name, "status": "fail", "detail": detail, "hint": hint}


def _python_version(python: Path) -> str | None:
    if not python.exists():
        return None
    try:
        out = subprocess.run([str(python), "-c", "import sys; print('.'.join(map(str, sys.version_info[:3])))"],
                             capture_output=True, text=True, timeout=10)
        return out.stdout.strip() or None
    except Exception:
        return None


def _node_version() -> str | None:
    node = home.which_node()
    if not node:
        return None
    try:
        out = subprocess.run([node, "-v"], capture_output=True, text=True, timeout=10)
        return out.stdout.strip().lstrip("v") or None
    except Exception:
        return None


def _version_tuple(value: str) -> tuple[int, ...]:
    parts = []
    for chunk in value.split("."):
        digits = "".join(ch for ch in chunk if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def run_checks(cfg: dict, check_updates: bool = False) -> list[dict]:
    checks: list[dict] = []
    version = home.read_version()
    channel = home.read_channel()
    checks.append(_ok("install", f"Meridian {version} at {home.HOME}"))
    if channel == "beta":
        checks.append(_ok("channel", "early beta · report problems with `meridian report`"))

    python = home.venv_python()
    py_version = _python_version(python)
    if not py_version:
        checks.append(_fail("python", f"no interpreter at {python}", "Reinstall with the installer."))
    elif _version_tuple(py_version) < (3, 11):
        checks.append(_fail("python", f"{py_version} is too old", "Meridian needs Python 3.11 or newer."))
    else:
        proc = subprocess.run(
            [str(python), "-c", "import fastapi, pandas, uvicorn"],
            cwd=str(home.APP), capture_output=True, text=True,
        )
        if proc.returncode == 0:
            checks.append(_ok("python", f"{py_version} · fastapi, pandas, uvicorn import"))
        else:
            checks.append(_fail("python", "app dependencies do not import",
                                (proc.stderr or "").strip().splitlines()[-1:] or "Reinstall dependencies."))

    data = home.data_dir()
    if not data.exists():
        checks.append(_fail("data", f"missing {data}", "Run `meridian setup`."))
    else:
        mode = data.stat().st_mode & 0o777
        if mode & 0o077:
            checks.append(_warn("data", f"mode {mode:03o} is not owner-only", "chmod 700 " + str(data)))
        else:
            checks.append(_ok("data", f"{home.human_size(home.dir_size(data))} · mode 700"))

    for label, path in (("config", home.CONFIG_PATH), ("secrets", home.ENV_PATH)):
        if not path.exists():
            checks.append(_warn(label, f"{path.name} missing", "Run `meridian setup`."))
        elif path.stat().st_mode & 0o077:
            checks.append(_warn(label, f"{path.name} is readable by group/other", f"chmod 600 {path}"))
        else:
            checks.append(_ok(label, f"{path.name} 0600"))

    secret = data / ".session_secret"
    if secret.exists() or os.getenv("MERIDIAN_SESSION_SECRET"):
        checks.append(_ok("encryption", "session secret present"))
    else:
        checks.append(_warn("encryption", "no session secret yet", "It is created on first API start."))

    api_port = home.port(cfg, "api_port")
    api_up = net.health(cfg) is not None
    if api_up:
        checks.append(_ok("api", f"healthy on {home.api_url(cfg)}"))
    elif not net.port_free(api_port):
        checks.append(_fail("api", f"port {api_port} is taken but /api/health does not answer",
                            "Stop the other process or change server.api_port."))
    else:
        checks.append(_warn("api", "not running", "Start it with `meridian start`."))

    try:
        status = net.get(cfg, "/api/auth/status", timeout=5)
        if status.get("locked"):
            checks.append(_fail("auth", "API is locked: no password set",
                                "Run `meridian passwd` to set one."))
        elif status.get("authenticated"):
            checks.append(_ok("auth", "password set · bearer accepted"))
        else:
            checks.append(_ok("auth", "password set"))
    except Exception:
        pass

    if api_up:
        try:
            positions = net.get(cfg, "/api/accounts/positions", timeout=10)
            count = len(positions) if isinstance(positions, list) else 0
            if count:
                checks.append(_ok("book", f"{count} positions imported"))
            else:
                checks.append(_warn("book", "no positions yet",
                                    "Import a CSV: `meridian import positions.csv`."))
        except Exception as exc:
            checks.append(_warn("book", f"could not read positions: {exc}", ""))

    model = home.get_cfg(cfg, "model.provider", "none")
    env = home.read_env()
    if model == "none":
        checks.append(_warn("model", "AI features are off", "Add a key later: `meridian config set model.provider ollama-cloud`."))
    elif not env.get("OLLAMA_API_KEY"):
        checks.append(_fail("model", f"{model} selected but OLLAMA_API_KEY is empty",
                            "Run `meridian config` or edit ~/.meridian/.env."))
    else:
        checks.append(_ok("model", f"{model} · key set"))

    market_provider = str(home.get_cfg(cfg, "market_data.provider", "yfinance"))
    if market_provider in ("yfinance", "free"):
        checks.append(_ok("market data", "yfinance · free, delayed"))
    elif market_provider == "none":
        checks.append(_warn("market data", "prices are import-only",
                            "Set market_data.provider to yfinance for the free feed."))
    elif not env.get("MASSIVE_API_KEY"):
        checks.append(_warn("market data", f"{market_provider} selected but MASSIVE_API_KEY is empty",
                            "Add the key in ~/.meridian/.env or switch to yfinance."))
    else:
        checks.append(_ok("market data", f"{market_provider} · key set"))

    runtime = home.get_cfg(cfg, "agent.runtime", "none")
    if runtime == "none":
        checks.append(_warn("agent", "no agent runtime selected", "Run `meridian agent install hermes`."))
    else:
        installed = agents.status(runtime)
        missing = [item["skill"] for item in installed if not item["installed"]]
        if missing:
            checks.append(_warn("agent", f"skills missing for {runtime}: {', '.join(missing)}",
                                "Run `meridian agent install " + runtime + "`."))
        else:
            checks.append(_ok("agent", f"{runtime} · {len(installed)} skills"))

    free = shutil.disk_usage(home.HOME).free
    if free < 1_000_000_000:
        checks.append(_warn("disk", f"{home.human_size(free)} free", "Keep at least 1 GB for snapshots and backups."))
    else:
        checks.append(_ok("disk", f"{home.human_size(free)} free"))

    if check_updates:
        from . import update
        try:
            latest = update.check(cfg)
            if latest and home.version_gt(latest.get("version", "0"), version):
                checks.append(_warn("update", f"{latest['version']} available", "Run `meridian update`."))
            else:
                checks.append(_ok("update", "up to date"))
        except Exception as exc:
            checks.append(_warn("update", f"could not check: {exc}", ""))

    return checks


def run(cfg: dict, check_updates: bool = False, json_mode: bool = False) -> int:
    checks = run_checks(cfg, check_updates=check_updates)
    failures = [c for c in checks if c["status"] == "fail"]
    warnings = [c for c in checks if c["status"] == "warn"]
    if json_mode:
        ui.json_out({"ok": not failures, "checks": checks,
                     "failures": len(failures), "warnings": len(warnings)})
    else:
        ui.banner("Meridian Beta · Doctor", "install health")
        icons = {"ok": ui.green("✓"), "warn": ui.yellow("!"), "fail": ui.red("✗")}
        for check in checks:
            print(f"  {icons[check['status']]} {check['name']:<12} {check['detail']}")
            hint = check.get("hint")
            if hint and check["status"] != "ok":
                print(f"    {ui.dim('→ ' + hint)}")
        print()
        if failures:
            print(ui.red(f"  {len(failures)} check(s) failed."))
        elif warnings:
            print(ui.yellow(f"  {len(warnings)} warning(s). The desk will run."))
        else:
            print(ui.green("  Everything checks out."))
    return 1 if failures else 0
