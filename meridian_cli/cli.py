"""Command-line entry point for Meridian Desk."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import webbrowser
from pathlib import Path

from . import agents, backup, doctor, home, net, process, qa, report, seed, setup, ui, update


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_setup(args) -> int:
    return setup.run(args)


def cmd_start(args) -> int:
    home.ensure_dirs()
    process.start(home.load_config(), ui_enabled=None if not args.api_only else False)
    return 0


def cmd_stop(args) -> int:
    process.stop(home.load_config(), include_web=True)
    return 0


def cmd_restart(args) -> int:
    cfg = home.load_config()
    process.stop(cfg)
    process.start(cfg, ui_enabled=None if not args.api_only else False)
    return 0


def cmd_status(args) -> int:
    cfg = home.load_config()
    version = home.read_version()
    channel = home.read_channel()
    api_pid = process.read_pid("api")
    web_pid = process.read_pid("web")
    health = net.health(cfg)
    positions = None
    if health:
        try:
            rows = net.get(cfg, "/api/accounts/positions", timeout=8)
            positions = len(rows) if isinstance(rows, list) else 0
        except Exception:
            positions = None
    data_bytes = home.dir_size(home.data_dir())
    runtime = str(home.get_cfg(cfg, "agent.runtime", "none"))
    skills = agents.status(runtime) if runtime != "none" else []
    installed_skills = sum(1 for item in skills if item["installed"])

    latest = None
    if args.updates:
        try:
            latest = update.check(cfg)
        except Exception:
            latest = None

    result = {
        "ok": bool(health),
        "version": version,
        "channel": channel,
        "home": str(home.HOME),
        "api": {"url": home.api_url(cfg), "running": process.alive(api_pid), "pid": api_pid,
                "healthy": bool(health)},
        "web": {"url": home.web_url(cfg), "running": process.alive(web_pid), "pid": web_pid},
        "book": {"positions": positions},
        "data_bytes": data_bytes,
        "model": home.get_cfg(cfg, "model.provider", "none"),
        "agent": {"runtime": runtime, "skills_installed": installed_skills},
        "latest_version": (latest or {}).get("version"),
    }
    if args.json:
        ui.json_out(result)
        return 0 if health else 1

    print()
    print(ui.bold(f"Meridian Desk {version}" + (f" · {channel}" if channel != "stable" else "")))
    pairs = [("home", str(home.HOME))]
    api_state = ui.green(f"running · pid {api_pid}") if process.alive(api_pid) else ui.red("stopped")
    if health:
        api_state += ui.dim(f" · {home.api_url(cfg)}")
    pairs.append(("api", api_state))
    if home.get_cfg(cfg, "server.ui", True):
        web_state = ui.green(f"running · pid {web_pid}") if process.alive(web_pid) else ui.red("stopped")
        if process.alive(web_pid):
            web_state += ui.dim(f" · {home.web_url(cfg)}")
        pairs.append(("web", web_state))
    pairs.append(("book", f"{positions} positions" if positions is not None else "unavailable"))
    pairs.append(("data", f"{home.human_size(data_bytes)} · {home.data_dir()}"))
    pairs.append(("model", str(home.get_cfg(cfg, "model.provider", "none"))))
    if runtime != "none":
        pairs.append(("agent", f"{runtime} · {installed_skills}/{len(skills)} skills"))
    if latest:
        if home.version_gt(str(latest.get("version", "0")), version):
            pairs.append(("update", ui.yellow(f"{latest['version']} available — meridian update")))
        else:
            pairs.append(("update", "up to date"))
    ui.kv(pairs)
    print()
    return 0 if health else 1


def cmd_doctor(args) -> int:
    return doctor.run(home.load_config(), check_updates=args.updates, json_mode=args.json)


def cmd_update(args) -> int:
    cfg = home.load_config()
    if args.check:
        try:
            latest = update.check(cfg, args.url)
        except Exception as exc:
            if args.json:
                ui.json_out({"ok": False, "error": str(exc)})
            else:
                ui.fail(f"Could not check for updates: {exc}")
            return 1
        current = home.read_version()
        available = home.version_gt(str(latest.get("version", "0")), current)
        if args.json:
            ui.json_out({"ok": True, "current": current, "latest": latest.get("version"),
                         "available": available, "notes": latest.get("notes", [])})
        else:
            if available:
                ui.info(f"Meridian {latest.get('version')} is available (you have {current}).")
                for note in latest.get("notes", []):
                    ui.info(f"• {note}")
                ui.info("Run `meridian update` to install it.")
            else:
                ui.ok(f"Meridian {current} is up to date.")
        return 0
    if args.rollback:
        return update.rollback(cfg, json_mode=args.json)
    return update.apply(cfg, assume_yes=args.yes, url=args.url, json_mode=args.json)


def cmd_import(args) -> int:
    cfg = home.load_config()
    try:
        result = seed.import_csv(cfg, Path(args.file))
    except Exception as exc:
        if args.json:
            ui.json_out({"ok": False, "error": str(exc)})
        else:
            ui.fail(str(exc))
        return 1
    if args.json:
        ui.json_out({"ok": True, **result})
    else:
        ui.ok(f"Imported {result.get('count', 0)} positions from {Path(args.file).name}")
        ui.info("Open the Data page to hide or delete the import.")
    return 0


def cmd_backup(args) -> int:
    try:
        archive = backup.create(home.load_config(), quiet=args.quiet)
    except SystemExit:
        raise
    except Exception as exc:
        ui.fail(str(exc))
        return 1
    if args.json:
        ui.json_out({"ok": True, "archive": str(archive), "bytes": archive.stat().st_size})
    return 0


def cmd_restore(args) -> int:
    try:
        backup.restore(home.load_config(), Path(args.file))
    except SystemExit:
        raise
    except Exception as exc:
        ui.fail(str(exc))
        return 1
    if args.json:
        ui.json_out({"ok": True, "archive": str(Path(args.file)), "version": home.read_version()})
    return 0


def cmd_agent(args) -> int:
    cfg = home.load_config()
    runtime = args.runtime or str(home.get_cfg(cfg, "agent.runtime", "hermes"))
    if args.action == "install":
        try:
            installed = agents.install(runtime)
        except Exception as exc:
            ui.fail(str(exc))
            return 1
        cfg["agent"]["runtime"] = runtime
        cfg["agent"]["skills_installed"] = bool(installed)
        home.save_config(cfg)
        if args.json:
            ui.json_out({"ok": True, "runtime": runtime, "installed": [str(p) for p in installed]})
        else:
            for path in installed:
                ui.ok(f"Installed {path}")
            print()
            print(f"  {agents.prompt(runtime)}")
        return 0
    if args.action == "status":
        rows = agents.status(runtime)
        if args.json:
            ui.json_out({"ok": True, "runtime": runtime, "skills": rows})
        else:
            for row in rows:
                mark = ui.green("✓") if row["installed"] else ui.red("✗")
                print(f"  {mark} {row['skill']}  {ui.dim(row['path'])}")
        return 0
    if args.action == "prompt":
        print(agents.prompt(runtime))
        return 0
    return 1


def cmd_passwd(args) -> int:
    from .setup import _set_password
    if args.stdin:
        password = sys.stdin.readline().strip()
    else:
        first = ui.ask_secret("New password (12+ characters)")
        second = ui.ask_secret("Repeat")
        if first != second:
            ui.die("Passwords did not match.")
        password = first
    if not password:
        ui.die("No password was given.")
    _set_password(password)
    ui.ok("Password changed. Existing sessions are still signed with the old secret; restart to rotate them.")
    return 0


def cmd_open(args) -> int:
    cfg = home.load_config()
    url = home.web_url(cfg) if home.get_cfg(cfg, "server.ui", True) else home.api_url(cfg) + "/docs"
    ui.info(url)
    if not args.print_only:
        webbrowser.open(url)
    return 0


def cmd_logs(args) -> int:
    if args.follow:
        process.follow(args.service)
        return 0
    text = process.log_tail(args.service, lines=args.lines)
    if not text:
        ui.info(f"No log output yet for {args.service}.")
        return 0
    print(text)
    return 0


def cmd_config(args) -> int:
    cfg = home.load_config()
    if args.action == "list":
        for section in ("book", "server", "model", "market_data", "broker", "agent", "updates", "reports"):
            print(f"[{section}]")
            for key, value in cfg.get(section, {}).items():
                print(f"{key} = {value}")
            print()
        return 0
    if args.action == "get":
        value = home.get_cfg(cfg, args.key)
        if value is None:
            ui.fail(f"No such key: {args.key}")
            return 1
        print(value)
        return 0
    if args.action == "set":
        raw = args.value
        parsed: object = raw
        if raw.lower() in ("true", "false"):
            parsed = raw.lower() == "true"
        else:
            try:
                parsed = int(raw)
            except ValueError:
                parsed = raw
        home.set_cfg(cfg, args.key, parsed)
        home.save_config(cfg)
        ui.ok(f"{args.key} = {parsed}")
        return 0
    if args.action == "path":
        print(home.HOME)
        return 0
    if args.action == "edit":
        target = home.ENV_PATH if args.secrets else home.CONFIG_PATH
        editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or "vi"
        subprocess.call([editor, str(target)])
        return 0
    return 1


def cmd_api(args) -> int:
    """Escape hatch for agents: call any local API route with the bearer token."""
    cfg = home.load_config()
    body = None
    if args.body:
        try:
            body = json.loads(args.body)
        except json.JSONDecodeError as exc:
            ui.fail(f"--body is not JSON: {exc}")
            return 1
    try:
        result = net.request(cfg, args.method.upper(), args.path, body=body, timeout=args.timeout)
    except Exception as exc:
        ui.fail(str(exc))
        return 1
    print(json.dumps(result, indent=2, default=str))
    return 0


def cmd_qa(args) -> int:
    return qa.run(home.load_config(), json_mode=args.json)


def cmd_report(args) -> int:
    return report.run(args)


def cmd_version(args) -> int:
    version = home.read_version()
    channel = home.read_channel()
    if args.json:
        ui.json_out({"version": version, "channel": channel, "home": str(home.HOME)})
    else:
        print(f"{version} ({channel})" if channel != "stable" else version)
    return 0


def cmd_uninstall(args) -> int:
    cfg = home.load_config()
    if not args.yes and sys.stdin.isatty():
        if not ui.confirm("Stop Meridian and remove the app? Your backup directory is kept.", default=False):
            return 0
    process.stop(cfg)
    if not args.purge:
        try:
            from . import backup as backup_mod
            backup_mod.create(cfg, quiet=True)
            ui.ok(f"Backup kept in {home.BACKUPS}")
        except Exception:
            pass
    for path in (home.APP, home.VENV, home.RUN, home.LOGS):
        if path.exists():
            import shutil
            shutil.rmtree(path, ignore_errors=True)
    shim = Path.home() / ".local" / "bin" / "meridian"
    if shim.exists():
        shim.unlink()
    if args.purge and home.HOME.exists():
        import shutil
        shutil.rmtree(home.HOME, ignore_errors=True)
    ui.ok("Meridian removed." + ("" if args.purge else f" Backups remain in {home.BACKUPS}."))
    return 0


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="meridian",
        description="Meridian Desk — a private portfolio desk for one operator and their agent.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("setup", help="onboarding: book, model, agent, dashboard")
    p.add_argument("--plan", action="store_true", help="print the onboarding questionnaire as JSON")
    p.add_argument("--non-interactive", action="store_true")
    p.add_argument("--book", choices=["demo", "csv", "empty"])
    p.add_argument("--csv")
    p.add_argument("--model", choices=["none", "ollama-cloud", "ollama-local"])
    p.add_argument("--ollama-key")
    p.add_argument("--ollama-base")
    p.add_argument("--agent", choices=["hermes", "claude-code", "codex", "none"])
    p.add_argument("--ui", dest="ui", action="store_true", default=None)
    p.add_argument("--no-ui", dest="ui", action="store_false")
    p.add_argument("--password-stdin", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_setup)

    for name, func, help_text in (
        ("start", cmd_start, "start the API and dashboard"),
        ("restart", cmd_restart, "restart the services"),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--api-only", action="store_true")
        p.set_defaults(func=func)

    p = sub.add_parser("stop", help="stop the API and dashboard")
    p.set_defaults(func=cmd_stop)

    p = sub.add_parser("status", help="what is running, and what the book holds")
    p.add_argument("--json", action="store_true")
    p.add_argument("--updates", action="store_true", help="also check for a new release")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("doctor", help="check the install for common problems")
    p.add_argument("--json", action="store_true")
    p.add_argument("--updates", action="store_true")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("qa", help="run end-to-end checks an agent can act on")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_qa)

    p = sub.add_parser("update", help="update to the latest release")
    p.add_argument("--check", action="store_true")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--rollback", action="store_true")
    p.add_argument("--url", help="override the manifest URL")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_update)

    p = sub.add_parser("import", help="import a positions CSV")
    p.add_argument("file")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("backup", help="archive data, config and secrets")
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_backup)

    p = sub.add_parser("restore", help="restore a backup")
    p.add_argument("file")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_restore)

    p = sub.add_parser("agent", help="install or inspect the agent skills")
    p.add_argument("action", nargs="?", choices=["install", "status", "prompt"], default="status")
    p.add_argument("runtime", nargs="?", choices=["hermes", "claude-code", "codex", "none"])
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_agent)

    p = sub.add_parser("passwd", help="change the dashboard password")
    p.add_argument("--stdin", action="store_true")
    p.set_defaults(func=cmd_passwd)

    p = sub.add_parser("open", help="open the dashboard")
    p.add_argument("--print", dest="print_only", action="store_true")
    p.set_defaults(func=cmd_open)

    p = sub.add_parser("logs", help="show service logs")
    p.add_argument("service", nargs="?", choices=["api", "web"], default="api")
    p.add_argument("-f", "--follow", action="store_true")
    p.add_argument("-n", "--lines", type=int, default=40)
    p.set_defaults(func=cmd_logs)

    p = sub.add_parser("config", help="read or change configuration")
    p.add_argument("action", choices=["list", "get", "set", "edit", "path"])
    p.add_argument("key", nargs="?")
    p.add_argument("value", nargs="?")
    p.add_argument("--secrets", action="store_true", help="edit .env instead of meridian.toml")
    p.set_defaults(func=cmd_config)

    p = sub.add_parser("api", help="call a local API route (agents use this)")
    p.add_argument("method", choices=["GET", "POST", "PUT", "PATCH", "DELETE", "get", "post", "put", "patch", "delete"])
    p.add_argument("path")
    p.add_argument("--body", help="JSON request body")
    p.add_argument("--timeout", type=float, default=30.0)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_api)

    p = sub.add_parser("report", help="report a problem and a recommended solution")
    p.add_argument("--problem", help="what went wrong")
    p.add_argument("--solution", help="what you would change")
    p.add_argument("--context", choices=list(report.CONTEXTS), default="other")
    p.add_argument("--kind", choices=list(report.KINDS), default="bug",
                   help="bug | missing-flow | architecture | docs | other")
    p.add_argument("--send", action="store_true", help="submit the report")
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    p.add_argument("--dry-run", action="store_true", help="print the exact payload, send nothing")
    p.add_argument("--list", action="store_true", help="list local reports")
    p.add_argument("--show", metavar="ID", help="print one local report")
    p.add_argument("--send-all", action="store_true", help="send every unsent local report")
    p.add_argument("--id", metavar="ID", help="send a specific local report with --send")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("version", help="print the installed version")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_version)

    p = sub.add_parser("uninstall", help="stop and remove the app")
    p.add_argument("--purge", action="store_true")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=cmd_uninstall)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "json", False):
        ui.QUIET = True
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        print()
        ui.warn("Interrupted.")
        return 130
    except SystemExit:
        raise
    except Exception as exc:
        if getattr(args, "json", False):
            ui.json_out({"ok": False, "error": str(exc)})
        else:
            ui.fail(str(exc))
        return 1
