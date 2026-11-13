"""Start, stop, and inspect the two local processes without systemd/launchd."""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import home, net, ui


def _pid_path(name: str) -> Path:
    return home.RUN / f"{name}.pid"


def _log_path(name: str) -> Path:
    return home.LOGS / f"{name}.log"


def read_pid(name: str) -> int | None:
    try:
        raw = _pid_path(name).read_text().strip()
        return int(raw) if raw else None
    except (OSError, ValueError):
        return None


def write_pid(name: str, pid: int) -> None:
    home.RUN.mkdir(parents=True, exist_ok=True)
    _pid_path(name).write_text(str(pid), encoding="utf-8")


def clear_pid(name: str) -> None:
    try:
        _pid_path(name).unlink()
    except OSError:
        pass


def alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def running(name: str) -> bool:
    return alive(read_pid(name))


def _base_env() -> dict[str, str]:
    env = os.environ.copy()
    env.update(home.read_env())
    env["MERIDIAN_ENV_FILE"] = str(home.ENV_PATH)
    env["PYTHONUNBUFFERED"] = "1"
    env.pop("MERIDIAN_DEMO", None)
    return env


def _spawn(name: str, cmd: list[str], cwd: Path) -> int:
    home.LOGS.mkdir(parents=True, exist_ok=True)
    log = (_log_path(name)).open("ab")
    log.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} starting: {' '.join(cmd)}\n".encode())
    log.flush()
    proc = subprocess.Popen(
        cmd,
        cwd=str(cwd),
        env=_base_env(),
        stdout=log,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    write_pid(name, proc.pid)
    return proc.pid


def start_api(cfg: dict) -> int:
    home.require_app()
    python = home.venv_python()
    if not python.exists():
        ui.die(f"No virtualenv at {home.VENV}. Reinstall with the installer.")
    port = home.port(cfg, "api_port")
    if not net.port_free(port):
        if net.health(cfg):
            ui.warn(f"API already answering on {home.api_url(cfg)}")
            return read_pid("api") or 0
        ui.die(f"Port {port} is in use by another process. Stop it or change server.api_port.")
    cmd = [
        str(python), "-m", "uvicorn", "backend.main:app",
        "--host", str(home.get_cfg(cfg, "server.bind", "127.0.0.1")),
        "--port", str(port),
        "--workers", "1",
    ]
    return _spawn("api", cmd, home.APP)


def web_bundle() -> Path:
    return home.APP / "frontend" / "web"


def start_web(cfg: dict) -> int:
    home.require_app()
    bundle = web_bundle()
    server = bundle / "server.js"
    if not server.exists():
        ui.die("No web bundle installed. Reinstall with the dashboard, or run API-only.")
    node = home.which_node()
    if not node:
        ui.die("Node.js 20+ is required for the dashboard. Install Node, or run API-only.")
    assert node
    port = home.port(cfg, "web_port")
    if not net.port_free(port):
        ui.warn(f"Dashboard port {port} is already in use.")
        return read_pid("web") or 0
    env_port = str(port)
    # server.js reads PORT/HOSTNAME from the environment (_base_env + these).
    os.environ["PORT"] = env_port
    os.environ["HOSTNAME"] = str(home.get_cfg(cfg, "server.bind", "127.0.0.1"))
    try:
        return _spawn("web", [node, "server.js"], server.parent)
    finally:
        os.environ.pop("PORT", None)
        os.environ.pop("HOSTNAME", None)


def wait_api(cfg: dict, seconds: float = 45.0) -> dict:
    deadline = time.time() + seconds
    last: Exception | None = None
    while time.time() < deadline:
        try:
            result = net.health(cfg, timeout=2.0)
            if result:
                return result
        except Exception as exc:  # pragma: no cover - transient
            last = exc
        time.sleep(0.6)
    tail = log_tail("api", lines=12)
    msg = f"API did not become healthy within {seconds:.0f}s."
    if last:
        msg += f" Last error: {last}"
    if tail:
        msg += "\n\n" + tail
    ui.die(msg)


def wait_web(cfg: dict, seconds: float = 30.0) -> bool:
    import urllib.request

    deadline = time.time() + seconds
    url = home.web_url(cfg) + "/"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2.0) as resp:
                if resp.status < 500:
                    return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def stop_one(name: str, timeout: float = 12.0) -> bool:
    pid = read_pid(name)
    if not pid or not alive(pid):
        clear_pid(name)
        return False
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            clear_pid(name)
            return False
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not alive(pid):
            clear_pid(name)
            return True
        time.sleep(0.3)
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    clear_pid(name)
    return True


def stop(cfg: dict, include_web: bool = True) -> None:
    stopped = []
    if include_web and stop_one("web"):
        stopped.append("web")
    if stop_one("api"):
        stopped.append("api")
    if stopped:
        ui.ok(f"Stopped: {', '.join(stopped)}")
    else:
        ui.info("Nothing was running.")


def start(cfg: dict, ui_enabled: bool | None = None) -> None:
    home.ensure_dirs()
    api_pid = start_api(cfg)
    info = wait_api(cfg)
    ui.ok(f"API healthy on {home.api_url(cfg)} (pid {api_pid})")
    use_ui = home.get_cfg(cfg, "server.ui", False) if ui_enabled is None else ui_enabled
    if use_ui and not web_bundle().joinpath("server.js").exists():
        use_ui = False
    if use_ui:
        web_pid = start_web(cfg)
        if wait_web(cfg):
            ui.ok(f"Dashboard on {home.web_url(cfg)} (pid {web_pid})")
        else:
            ui.warn("Dashboard did not answer yet — check `meridian logs web`.")
    version = (info or {}).get("version") or home.read_version()
    ui.info(f"Meridian {version}")


def log_tail(name: str, lines: int = 40) -> str:
    path = _log_path(name)
    if not path.exists():
        return ""
    try:
        content = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(content[-lines:])
    except OSError:
        return ""


def follow(name: str) -> None:
    path = _log_path(name)
    if not path.exists():
        ui.die(f"No log at {path}")
    ui.info(f"Tailing {path} (Ctrl-C to stop)")
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        fh.seek(0, os.SEEK_END)
        try:
            while True:
                chunk = fh.readline()
                if chunk:
                    sys.stdout.write(chunk)
                    sys.stdout.flush()
                else:
                    time.sleep(0.5)
        except KeyboardInterrupt:
            return
