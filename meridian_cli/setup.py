"""The onboarding flow: human-guided or agent-guided."""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
from pathlib import Path

from . import agents, home, net, process, seed, ui

WORDS = (
    "amber", "atlas", "beacon", "birch", "cobalt", "copper", "delta", "ember",
    "harbor", "hazel", "indigo", "ivory", "juniper", "kestrel", "lumen", "maple",
    "meridian", "onyx", "orbit", "pepper", "quartz", "raven", "river", "sable",
    "signal", "slate", "summit", "tundra", "velvet", "willow", "cedar", "zephyr",
)


def generate_password() -> str:
    words = secrets.SystemRandom().sample(WORDS, 4)
    return "-".join(words) + f"-{secrets.randbelow(9000) + 1000}"


def plan() -> dict:
    return {
        "version": home.read_version(),
        "channel": home.read_channel(),
        "home": str(home.HOME),
        "questions": [
            {
                "id": "book",
                "prompt": "What should the desk start with?",
                "options": [
                    {"value": "demo", "label": "Demo book — 8 synthetic positions, debt and two theses"},
                    {"value": "csv", "label": "Import my broker's positions CSV"},
                    {"value": "empty", "label": "Start empty"},
                ],
                "default": "demo",
            },
            {
                "id": "model",
                "prompt": "Which model should the agent use?",
                "options": [
                    {"value": "ollama-cloud", "label": "Ollama Cloud — paste an API key"},
                    {"value": "ollama-local", "label": "Local Ollama — http://localhost:11434"},
                    {"value": "none", "label": "No model for now — the app and API still work"},
                ],
                "default": "none",
            },
            {
                "id": "agent",
                "prompt": "Which agent will run the desk?",
                "options": [
                    {"value": "hermes", "label": "Hermes"},
                    {"value": "claude-code", "label": "Claude Code"},
                    {"value": "codex", "label": "Codex"},
                    {"value": "none", "label": "No agent yet"},
                ],
                "default": "hermes",
            },
        ],
        "command_template": (
            "meridian setup --non-interactive --book {book} --model {model} "
            "--agent {agent} --json"
        ),
        "notes": [
            "Ask one question at a time, then run the command once with every answer.",
            "If book=csv, also pass --csv /absolute/path/to/positions.csv.",
            "If model=ollama-cloud, put OLLAMA_API_KEY in the command's environment; never paste the key into chat or argv.",
            "Never invent a password; let the CLI generate one and report it to the operator.",
        ],
    }


def _set_password(password: str) -> None:
    if len(password) < 12:
        ui.die("Password must be at least 12 characters.")
    python = home.venv_python()
    if not python.exists():
        ui.die(f"No virtualenv at {home.VENV}. Run the installer first.")
    code = "import sys; from data.auth import set_password; set_password(sys.stdin.read().strip())"
    proc = subprocess.run(
        [str(python), "-c", code],
        cwd=str(home.APP),
        input=password,
        text=True,
        capture_output=True,
    )
    if proc.returncode != 0:
        ui.die(f"Could not set the password: {proc.stderr.strip() or proc.stdout.strip()}")


def _read_stdin_password() -> str | None:
    if sys.stdin.isatty():
        return None
    line = sys.stdin.readline().strip()
    return line or None


def _ensure_demo_fixture() -> None:
    if not seed.demo_csv().exists():
        ui.die(f"Demo fixture missing from the package: {seed.demo_csv()}")


def _choose(values: dict, args, cfg: dict) -> dict:
    """Fill in any unanswered values interactively."""
    interactive = (not args.non_interactive) and sys.stdin.isatty()

    book = args.book
    if not book:
        current_book = str(home.get_cfg(cfg, "book.source", "demo"))
        book = ui.ask_choice(
            "What should the desk start with?",
            [
                ("demo", "Demo book — 8 synthetic positions, debt and two theses"),
                ("csv", "Import my broker's positions CSV"),
                ("empty", "Start empty"),
            ],
            default={"demo": 1, "csv": 2, "empty": 3}.get(current_book, 1),
        ) if interactive else "demo"

    csv_path = args.csv
    if book == "csv":
        if not csv_path and interactive:
            csv_path = ui.ask("Path to the positions CSV")
        if not csv_path:
            ui.die("--book csv needs --csv /path/to/positions.csv")
        path = Path(csv_path).expanduser()
        if not path.exists():
            ui.die(f"CSV not found: {path}")
        csv_path = str(path.resolve())

    model = args.model
    if not model:
        model = ui.ask_choice(
            "Which model should the agent use?",
            [
                ("ollama-cloud", "Ollama Cloud — paste an API key"),
                ("ollama-local", "Local Ollama — http://localhost:11434"),
                ("none", "No model for now — the app and API still work"),
            ],
            default=1,
        ) if interactive else "none"

    ollama_key = args.ollama_key or os.environ.get("OLLAMA_API_KEY") or home.read_env().get("OLLAMA_API_KEY", "")
    base_url = ""
    if model == "ollama-cloud":
        if not ollama_key and interactive:
            ollama_key = ui.ask_secret("Ollama Cloud API key (input hidden)")
        if not ollama_key:
            ui.die("Ollama Cloud needs an API key: pass --ollama-key or set OLLAMA_API_KEY.")
        base_url = "https://ollama.com/v1"
    elif model == "ollama-local":
        base_url = args.ollama_base or "http://localhost:11434/v1"
        ollama_key = ollama_key or "local"

    agent = args.agent
    if not agent:
        agent = ui.ask_choice(
            "Which agent will run the desk?",
            [
                ("hermes", "Hermes"),
                ("claude-code", "Claude Code"),
                ("codex", "Codex"),
                ("none", "No agent yet"),
            ],
            default=1,
        ) if interactive else "hermes"

    ui_enabled = False

    return {
        "book": book,
        "csv": csv_path or "",
        "model": model,
        "ollama_key": ollama_key,
        "base_url": base_url,
        "agent": agent,
        "ui": bool(ui_enabled),
    }


def _write_config(values: dict, cfg: dict) -> None:
    cfg["book"]["source"] = values["book"]
    cfg["book"]["csv"] = values["csv"]
    cfg["model"]["provider"] = values["model"]
    cfg["model"]["base_url"] = values["base_url"]
    if values["model"] == "ollama-cloud":
        cfg["model"]["model"] = "deepseek-v4-flash"
    elif values["model"] == "ollama-local":
        cfg["model"]["model"] = "deepseek-v4-flash"
    cfg["agent"]["runtime"] = values["agent"]
    cfg["server"]["ui"] = False
    home.save_config(cfg)


def _write_env(values: dict) -> None:
    env: dict[str, str] = {}
    existing = home.read_env()
    if not existing.get("MERIDIAN_API_TOKEN"):
        env["MERIDIAN_API_TOKEN"] = secrets.token_urlsafe(32)
    # The app binds to loopback. A Secure cookie is fine on localhost in
    # Chrome, but Safari and LAN access are not reliable; local-only means
    # plain HTTP is not a downgrade.
    env["MERIDIAN_COOKIE_SECURE"] = "0"
    if values["model"] == "ollama-cloud":
        env["OLLAMA_API_KEY"] = values["ollama_key"]
        env["OLLAMA_BASE_URL"] = values["base_url"]
    elif values["model"] == "ollama-local":
        env["OLLAMA_API_KEY"] = values["ollama_key"] or "local"
        env["OLLAMA_BASE_URL"] = values["base_url"]
    if env:
        home.write_env(env)


def _agent_handoff(runtime: str) -> int:
    try:
        installed = agents.install(runtime)
    except Exception as exc:
        ui.die(f"Could not install the agent skills: {exc}")
    cfg = home.load_config()
    cfg["agent"]["runtime"] = runtime
    cfg["agent"]["skills_installed"] = bool(installed)
    home.save_config(cfg)
    ui.banner("Meridian Beta · Agent setup", "skills installed")
    for path in installed:
        ui.ok(f"Installed {path}")
    print()
    print(ui.bold("Next:"))
    print(f"  {agents.prompt(runtime)}")
    print()
    print(ui.dim("  The agent will ask three questions — book, model, agent runtime —"))
    print(ui.dim("  then run `meridian setup` for you."))
    return 0


def run(args) -> int:
    ui.QUIET = bool(args.json)
    if args.plan:
        ui.json_out(plan())
        return 0

    if not home.APP.exists():
        ui.die(f"No app at {home.APP}. Run the installer first.")

    interactive = (not args.non_interactive) and sys.stdin.isatty()
    cfg = home.load_config()

    if interactive and not args.non_interactive:
        ui.hero("Meridian Beta", "private portfolio desk",
                f"{home.read_version()} · {home.read_channel()} · setup")
        mode = ui.ask_choice(
            "How do you want to set this up?",
            [
                ("here", "Walk me through it here"),
                ("agent", "Guide me with my agent — it asks the questions"),
                ("later", "Just install it; I'll configure later"),
            ],
            default=1,
        )
        if mode == "later":
            print()
            print("Run `meridian setup` when you are ready.")
            return 0
        if mode == "agent":
            runtime = ui.ask_choice(
                "Which agent?",
                [
                    ("hermes", "Hermes"),
                    ("claude-code", "Claude Code"),
                    ("codex", "Codex"),
                ],
                default=1,
            )
            return _agent_handoff(runtime)

    values = _choose({}, args, cfg)
    _write_config(values, cfg)
    _write_env(values)

    home.ensure_dirs()
    data = home.data_dir()
    data.mkdir(parents=True, exist_ok=True)
    try:
        data.chmod(0o700)
    except OSError:
        pass

    # Password: explicit stdin, generated, or interactive choice.
    password: str | None = None
    generated = False
    if args.password_stdin:
        given = _read_stdin_password()
        if given:
            password = given
        else:
            password = generate_password()
            generated = True
    elif interactive and not args.non_interactive:
        if ui.confirm("Generate a strong password for the desk?", default=True):
            password = generate_password()
            generated = True
        else:
            first = ui.ask_secret("Choose a password (12+ characters)")
            second = ui.ask_secret("Repeat the password")
            if first != second:
                ui.die("Passwords did not match.")
            password = first
    else:
        password = generate_password()
        generated = True
    _set_password(password or generate_password())

    if process.running("api") or process.running("web"):
        process.stop(cfg)
    api_pid = process.start_api(cfg)
    process.wait_api(cfg)
    ui.ok(f"API healthy on {home.api_url(cfg)} (pid {api_pid})")

    positions = 0
    try:
        rows = net.get(cfg, "/api/accounts/positions", timeout=10)
        existing = len(rows) if isinstance(rows, list) else 0
    except Exception:
        existing = 0

    if existing and values["book"] in ("demo", "csv"):
        # Setup is safe to re-run: never duplicate a book that is already there.
        positions = existing
        ui.warn(f"{existing} positions already imported; keeping the existing book.")
    elif values["book"] == "demo":
        seeded = seed.seed_demo(cfg)
        positions = seeded["positions"]
    elif values["book"] == "csv":
        seeded = seed.seed_from_csv(cfg, Path(values["csv"]))
        positions = seeded["positions"]

    installed: list[Path] = []
    if values["agent"] != "none":
        try:
            installed = agents.install(values["agent"])
            cfg["agent"]["skills_installed"] = bool(installed)
            home.save_config(cfg)
            ui.ok(f"Installed the desk skills for {agents.RUNTIME_LABELS[values['agent']]}")
        except Exception as exc:
            ui.warn(f"Could not install agent skills: {exc}")

    result = {
        "ok": True,
        "version": home.read_version(),
        "channel": home.read_channel(),
        "home": str(home.HOME),
        "api": home.api_url(cfg),
        "book": values["book"],
        "positions": positions,
        "model": values["model"],
        "agent": values["agent"],
        "skills": [str(p) for p in installed],
        "password": password if generated else None,
        "password_shown_once": bool(password and generated),
        "next": ["meridian status", "meridian doctor", "meridian update --check"],
    }

    if args.json:
        ui.json_out(result)
        return 0

    print()
    ui.banner("Meridian Beta · Ready", "the desk is running")
    pairs = []
    pairs.append(("API", home.api_url(cfg)))
    if password and generated:
        pairs.append(("Password", ui.yellow(password) + ui.dim("  (shown once)")))
    pairs.append(("Book", f"{positions} positions" if positions else "empty"))
    if values["agent"] != "none":
        pairs.append(("Agent", agents.RUNTIME_LABELS[values["agent"]]))
    pairs.append(("Home", str(home.HOME)))
    ui.kv(pairs)
    print()
    if values["agent"] != "none":
        print(f"  {agents.prompt(values['agent'])}")
        print()
    print(ui.dim("  meridian status · meridian doctor · meridian update --check"))
    print(ui.dim("  The first backup is free: meridian backup"))
    print()
    return 0
