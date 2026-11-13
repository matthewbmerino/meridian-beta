"""Home directory, config file, and secret environment for Meridian Desk.

Layout (default ``~/.meridian``)::

    meridian.toml      operator config (0600)
    .env               secrets (0600)
    app/               the application tree (code + data/)
    venv/              Python environment
    logs/              api.log, web.log
    run/               pid files
    backups/           meridian-backup-*.tar.gz
    downloads/         release tarballs

The app tree keeps its stores in ``app/data`` because the data layer treats
that directory as a Python package. Updates preserve every non-Python entry
under it; see ``update.py``.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import sys
from pathlib import Path

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - the installer requires 3.11+
    tomllib = None


def _home_default() -> Path:
    return Path(os.environ.get("MERIDIAN_HOME") or (Path.home() / ".meridian")).expanduser()


HOME = _home_default()
APP = HOME / "app"
VENV = HOME / "venv"
LOGS = HOME / "logs"
RUN = HOME / "run"
BACKUPS = HOME / "backups"
DOWNLOADS = HOME / "downloads"
CONFIG_PATH = HOME / "meridian.toml"
ENV_PATH = HOME / ".env"
VERSION_PATH = APP / "VERSION"
CHANNEL_PATH = APP / "CHANNEL"

DEFAULT_MANIFEST = "https://meridian.matthewmerino.com/desk/manifest.json"

DEFAULTS: dict[str, dict] = {
    "book": {
        "source": "empty",  # demo | csv | empty
        "csv": "",
    },
    "server": {
        "api_port": 8001,
        "web_port": 3000,
        "bind": "127.0.0.1",
        "ui": False,
    },
    "model": {
        "provider": "none",  # none | ollama-cloud | ollama-local
        "model": "",
        "base_url": "",
    },
    "market_data": {
        "provider": "yfinance",  # yfinance | massive | polygon | none
    },
    "broker": {
        "provider": "csv",  # csv | schwab
    },
    "agent": {
        "runtime": "hermes",  # hermes | claude-code | codex | none
        "skills_installed": False,
    },
    "updates": {
        "channel": "stable",
        "check_on_start": True,
        "manifest_url": DEFAULT_MANIFEST,
    },
    "reports": {
        "endpoint": "https://meridian.matthewmerino.com/api/desk/report",
    },
}

SECRET_KEYS = {
    "MERIDIAN_API_TOKEN",
    "OLLAMA_API_KEY",
    "MASSIVE_API_KEY",
    "POLYGON_API_KEY",
    "FRED_API_KEY",
    "PERPLEXITY_API_KEY",
    "FIRECRAWL_API_KEY",
    "SCHWAB_APP_KEY",
    "SCHWAB_APP_SECRET",
}


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _deep_merge(base: dict, override: dict) -> dict:
    out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in base.items()}
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config() -> dict:
    cfg = {k: dict(v) for k, v in DEFAULTS.items()}
    if CONFIG_PATH.exists():
        if tomllib is None:  # pragma: no cover
            raise RuntimeError("Python 3.11+ is required to read meridian.toml")
        try:
            with CONFIG_PATH.open("rb") as fh:
                cfg = _deep_merge(cfg, tomllib.load(fh))
        except Exception as exc:
            print(f"warning: could not read {CONFIG_PATH}: {exc}", file=sys.stderr)
    return cfg


def _toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(str(value), ensure_ascii=False)


def save_config(cfg: dict) -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    for section in ("book", "server", "model", "market_data", "broker", "agent", "updates", "reports"):
        values = cfg.get(section, {})
        lines.append(f"[{section}]")
        for key, value in values.items():
            lines.append(f"{key} = {_toml_value(value)}")
        lines.append("")
    tmp = CONFIG_PATH.with_suffix(".toml.tmp")
    tmp.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(CONFIG_PATH)


def get_cfg(cfg: dict, dotted: str, default=None):
    node = cfg
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def set_cfg(cfg: dict, dotted: str, value) -> None:
    parts = dotted.split(".")
    node = cfg
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value


def port(cfg: dict, which: str) -> int:
    default = 8001 if which == "api_port" else 3000
    value = get_cfg(cfg, f"server.{which}", default)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def api_url(cfg: dict) -> str:
    return f"http://{get_cfg(cfg, 'server.bind', '127.0.0.1')}:{port(cfg, 'api_port')}"


def web_url(cfg: dict) -> str:
    return f"http://{get_cfg(cfg, 'server.bind', '127.0.0.1')}:{port(cfg, 'web_port')}"


# ---------------------------------------------------------------------------
# Secrets (.env)
# ---------------------------------------------------------------------------

def read_env(path: Path | None = None) -> dict[str, str]:
    path = path or ENV_PATH
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        out[key.strip()] = value
    return out


def write_env(values: dict[str, str]) -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    existing = read_env()
    existing.update({k: v for k, v in values.items() if v is not None})
    lines = ["# Meridian Desk secrets — keep this file private (0600)."]
    for key in sorted(existing):
        lines.append(f"{key}={existing[key]}")
    tmp = ENV_PATH.with_suffix(".env.tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(ENV_PATH)


def redact_env(env: dict[str, str]) -> dict[str, str]:
    return {
        key: ("<set>" if value else "")
        if key in SECRET_KEYS or "KEY" in key or "SECRET" in key or "TOKEN" in key
        else value
        for key, value in env.items()
    }


# ---------------------------------------------------------------------------
# Versions
# ---------------------------------------------------------------------------

def read_version() -> str:
    try:
        return VERSION_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return "0.0.0"


def read_channel() -> str:
    """Release channel: `beta` on every early build, `stable` once it earns it."""
    try:
        value = CHANNEL_PATH.read_text(encoding="utf-8").strip().lower()
    except OSError:
        return "stable"
    return value if value in ("beta", "stable") else "stable"


def parse_version(value: str) -> tuple[int, ...]:
    parts: list[int] = []
    for chunk in str(value).lstrip("v").split("."):
        digits = "".join(ch for ch in chunk if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def version_gt(a: str, b: str) -> bool:
    left, right = parse_version(a), parse_version(b)
    length = max(len(left), len(right))
    left += (0,) * (length - len(left))
    right += (0,) * (length - len(right))
    return left > right


# ---------------------------------------------------------------------------
# Filesystem helpers
# ---------------------------------------------------------------------------

def ensure_dirs() -> None:
    for path in (HOME, LOGS, RUN, BACKUPS, DOWNLOADS, HOME / "reports"):
        path.mkdir(parents=True, exist_ok=True)
    if APP.exists():
        data = APP / "data"
        if data.exists():
            try:
                os.chmod(data, 0o700)
            except OSError:
                pass


def data_dir() -> Path:
    return APP / "data"


def human_size(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num < 1024 or unit == "TB":
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} TB"


def dir_size(path: Path) -> int:
    total = 0
    if not path.exists():
        return 0
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                pass
    return total


def require_app() -> None:
    if not APP.exists():
        from . import ui
        ui.die(f"No app found at {APP}. Run the installer first: curl -fsSL https://meridian.matthewmerino.com/install.sh | sh")


def venv_python() -> Path:
    candidate = VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    return candidate


def is_file_private(path: Path) -> bool:
    try:
        return not (path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO))
    except OSError:
        return False


def which_node() -> str | None:
    return shutil.which("node")
