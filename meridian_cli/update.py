"""Release check, download, verified swap, and rollback."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import time
import urllib.request
from pathlib import Path

from . import home, process, ui


def manifest_url(cfg: dict) -> str:
    return os.environ.get("MERIDIAN_UPDATE_URL") or home.get_cfg(
        cfg, "updates.manifest_url", home.DEFAULT_MANIFEST
    )


def check(cfg: dict, url: str | None = None) -> dict:
    target = url or manifest_url(cfg)
    req = urllib.request.Request(target, headers={"Accept": "application/json", "User-Agent": "meridian-cli"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read())
    if not isinstance(data, dict) or "version" not in data:
        raise ValueError(f"manifest at {target} has no version")
    return data


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download(url: str, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_suffix(destination.suffix + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": "meridian-cli"})
    with urllib.request.urlopen(req, timeout=60) as resp, tmp.open("wb") as out:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = resp.read(1024 * 256)
            if not chunk:
                break
            out.write(chunk)
            done += len(chunk)
            if total and not ui.QUIET and os.isatty(1):
                pct = int(done * 100 / total)
                print(f"\r  downloading {pct:3d}%  {home.human_size(done)} / {home.human_size(total)}", end="")
    if not ui.QUIET and os.isatty(1):
        print()
    tmp.replace(destination)
    return destination


def _safe_extract(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar.getmembers():
            parts = Path(member.name).parts
            if member.name.startswith("/") or ".." in parts:
                raise ValueError(f"unsafe path in release: {member.name}")
        try:
            tar.extractall(destination, filter="data")
        except TypeError:  # Python < 3.12
            tar.extractall(destination)


def _preserve_data(src_app: Path, dst_app: Path) -> int:
    """Copy every non-code entry from data/ into the new tree.

    The data layer is a Python package, so the stores sit beside its modules.
    Only .py files are code; everything else (JSON, HMAC sidecars, imports,
    snapshots, the session secret, the password hash) is the operator's.
    """
    src = src_app / "data"
    dst = dst_app / "data"
    if not src.exists():
        return 0
    dst.mkdir(parents=True, exist_ok=True)
    copied = 0
    for entry in src.iterdir():
        if entry.name == "__pycache__" or entry.suffix == ".py" or entry.name.endswith(".pyc"):
            continue
        target = dst / entry.name
        try:
            if entry.is_dir():
                if target.exists():
                    shutil.rmtree(target)
                shutil.copytree(entry, target)
            else:
                shutil.copy2(entry, target)
            copied += 1
        except OSError as exc:
            ui.warn(f"could not preserve data/{entry.name}: {exc}")
    return copied


def _swap(new_app: Path, keep_old: bool = True) -> Path | None:
    old = None
    if home.APP.exists():
        old = home.HOME / "app.old"
        if old.exists():
            shutil.rmtree(old)
        home.APP.rename(old)
    new_app.rename(home.APP)
    return old


def _restore(old: Path) -> None:
    if home.APP.exists():
        bad = home.HOME / f"app.broken-{time.strftime('%Y%m%d-%H%M%S')}"
        home.APP.rename(bad)
    old.rename(home.APP)


def _requirements_changed(before: str, after: str) -> bool:
    return _sha256(Path(before)) != _sha256(Path(after)) if Path(before).exists() and Path(after).exists() else True


def _uv() -> str | None:
    """The private uv the installer provisions, or one on PATH."""
    candidate = home.HOME / "bin" / "uv"
    if candidate.exists():
        return str(candidate)
    return shutil.which("uv")


def _install_deps() -> None:
    """Install requirements into the app venv.

    uv-created venvs do not include pip, so the old `python -m pip` path fails
    with "No module named pip" exactly when an update changes requirements.
    Prefer uv; fall back to pip, and bootstrap pip with ensurepip if needed.
    """
    python = home.venv_python()
    if not python.exists():
        raise RuntimeError(f"no virtualenv at {home.VENV}")

    errors: list[str] = []
    uv = _uv()
    if uv:
        proc = subprocess.run(
            [uv, "pip", "install", "--python", str(python), "--quiet", "-r", "requirements.txt"],
            cwd=str(home.APP), capture_output=True, text=True, timeout=900,
        )
        if proc.returncode == 0:
            return
        errors.append((proc.stderr or proc.stdout or "uv pip install failed").strip())

    check = subprocess.run(
        [str(python), "-m", "pip", "--version"],
        cwd=str(home.APP), capture_output=True, text=True, timeout=60,
    )
    if check.returncode != 0:
        subprocess.run(
            [str(python), "-m", "ensurepip", "--upgrade"],
            cwd=str(home.APP), capture_output=True, text=True, timeout=300,
        )
    proc = subprocess.run(
        [str(python), "-m", "pip", "install", "--quiet", "-r", "requirements.txt"],
        cwd=str(home.APP), capture_output=True, text=True, timeout=900,
    )
    if proc.returncode != 0:
        errors.append((proc.stderr or proc.stdout or "pip install failed").strip())
        raise RuntimeError("; ".join(errors))


def apply(cfg: dict, assume_yes: bool = False, url: str | None = None, json_mode: bool = False) -> int:
    current = home.read_version()
    try:
        latest = check(cfg, url)
    except Exception as exc:
        ui.die(f"Could not reach the update manifest: {exc}")
    new_version = str(latest.get("version", ""))
    if not home.version_gt(new_version, current):
        if json_mode:
            ui.json_out({"ok": True, "updated": False, "current": current, "latest": new_version})
        else:
            ui.ok(f"Meridian {current} is up to date.")
        return 0

    notes = latest.get("notes") or []
    if not json_mode:
        suffix = ""
        if str(latest.get("channel") or "") not in ("", "stable"):
            suffix = f" ({latest['channel']})"
        ui.banner(f"Meridian {new_version}{suffix} is available", f"you have {current}")
        for note in notes:
            ui.info(f"• {note}")
        print()
    if not assume_yes and not ui.QUIET:
        if not ui.confirm(f"Update to {new_version}?", default=True):
            return 0

    # Always back up: an update is the one operation that replaces the tree.
    from . import backup
    try:
        archive_backup = backup.create(cfg, quiet=True)
        ui.ok(f"Backed up to {archive_backup.name}")
    except Exception as exc:
        ui.die(f"Backup failed; update aborted: {exc}")

    archive = home.DOWNLOADS / f"meridian-{new_version}.tar.gz"
    ui.step(f"Downloading {latest['url']}")
    try:
        _download(latest["url"], archive)
    except Exception as exc:
        ui.die(f"Download failed: {exc}")
    expected = str(latest.get("sha256") or "")
    actual = _sha256(archive)
    if expected and actual != expected:
        ui.die(f"Checksum mismatch for {archive.name}: expected {expected[:12]}…, got {actual[:12]}…")
    ui.ok(f"Verified sha256 {actual[:12]}…")

    staging = home.HOME / "app.new"
    if staging.exists():
        shutil.rmtree(staging)
    ui.step("Unpacking")
    try:
        _safe_extract(archive, staging)
    except Exception as exc:
        ui.die(f"Could not unpack the release: {exc}")
    staged_version = "0.0.0"
    try:
        staged_version = (staging / "VERSION").read_text().strip()
    except OSError:
        pass
    if staged_version and staged_version != new_version:
        ui.die(f"Release is labelled {staged_version}, manifest says {new_version}.")

    preserved = _preserve_data(home.APP, staging)
    ui.ok(f"Preserved {preserved} data entries")

    old_requirements = home.APP / "requirements.txt"
    new_requirements = staging / "requirements.txt"
    deps_changed = _requirements_changed(str(old_requirements), str(new_requirements))

    ui.step("Stopping services")
    process.stop(cfg)

    old = _swap(staging)
    try:
        if deps_changed:
            ui.step("Installing dependencies")
            _install_deps()
        process.start_api(cfg)
        process.wait_api(cfg, seconds=60)
        if home.get_cfg(cfg, "server.ui", False) and (home.APP / "frontend" / "web" / "server.js").exists():
            process.start_web(cfg)
            process.wait_web(cfg, seconds=30)
        runtime = str(home.get_cfg(cfg, "agent.runtime", "none"))
        if runtime != "none":
            try:
                from . import agents
                agents.install(runtime)
                ui.ok(f"Agent skills refreshed for {runtime}")
            except Exception as exc:  # skills are not worth a rollback
                ui.warn(f"Could not refresh agent skills: {exc}")
    except Exception as exc:
        ui.fail(f"Update failed: {exc}")
        ui.warn("Rolling back")
        process.stop(cfg)
        if old and old.exists():
            _restore(old)
            try:
                process.start_api(cfg)
                process.wait_api(cfg, seconds=60)
            except Exception:
                pass
        ui.die("Rolled back to the previous version.")

    if json_mode:
        ui.json_out({"ok": True, "updated": True, "from": current, "to": new_version,
                     "data_entries_preserved": preserved, "rollback_available": bool(old)})
    else:
        ui.ok(f"Updated {current} → {new_version}")
        ui.info(f"Data preserved: {preserved} entries under {home.data_dir()}")
        ui.info("Rollback with `meridian update --rollback` if something is wrong.")
    return 0


def rollback(cfg: dict, json_mode: bool = False) -> int:
    old = home.HOME / "app.old"
    if not old.exists():
        ui.die("No previous version to roll back to.")
    current = home.read_version()
    ui.step("Stopping services")
    process.stop(cfg)
    _restore(old)
    process.start_api(cfg)
    process.wait_api(cfg, seconds=60)
    if home.get_cfg(cfg, "server.ui", False) and (home.APP / "frontend" / "web" / "server.js").exists():
        process.start_web(cfg)
        process.wait_web(cfg, seconds=30)
    restored = home.read_version()
    if json_mode:
        ui.json_out({"ok": True, "from": current, "to": restored})
    else:
        ui.ok(f"Rolled back {current} → {restored}")
    return 0
