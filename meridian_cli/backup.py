"""Back up and restore the operator's data, config, and secrets."""

from __future__ import annotations

import os
import shutil
import tarfile
import time
from pathlib import Path

from . import home, process, ui


def _data_members(root: Path):
    for entry in root.iterdir():
        if entry.name == "__pycache__" or entry.suffix == ".py" or entry.name.endswith(".pyc"):
            continue
        yield entry


def create(cfg: dict, quiet: bool = False) -> Path:
    home.ensure_dirs()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    archive = home.BACKUPS / f"meridian-backup-{stamp}.tar.gz"
    data = home.data_dir()
    if not data.exists():
        ui.die(f"No data directory at {data}. Run `meridian setup` first.")
    count = 0
    with tarfile.open(archive, "w:gz") as tar:
        for entry in _data_members(data):
            tar.add(entry, arcname=f"data/{entry.name}")
            count += 1
        if home.CONFIG_PATH.exists():
            tar.add(home.CONFIG_PATH, arcname="meridian.toml")
        if home.ENV_PATH.exists():
            tar.add(home.ENV_PATH, arcname=".env")
        tar.add(home.VERSION_PATH, arcname="VERSION")
    os.chmod(archive, 0o600)
    if not quiet:
        ui.ok(f"Backup written: {archive} ({home.human_size(archive.stat().st_size)}, {count} entries)")
        ui.warn("The backup contains the key that decrypts it. Store it somewhere private.")
    return archive


def restore(cfg: dict, archive: Path) -> None:
    archive = Path(archive).expanduser()
    if not archive.exists():
        ui.die(f"No backup at {archive}")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    staging = home.HOME / f"restore-{stamp}"
    staging.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(archive, "r:gz") as tar:
            for member in tar.getmembers():
                if member.name.startswith("/") or ".." in Path(member.name).parts:
                    ui.die(f"Unsafe path in backup: {member.name}")
            tar.extractall(staging, filter="data")
    except Exception as exc:
        ui.die(f"Could not read the backup: {exc}")
    if not (staging / "data").exists():
        ui.die("The backup has no data/ directory.")

    process.stop(cfg)
    current = home.data_dir()
    moved = None
    if current.exists():
        moved = current.with_name(f"data.before-restore-{stamp}")
        current.rename(moved)
        # The data directory is also a Python package. Start from the current
        # tree (code + old stores), then overlay the backup's store entries.
        shutil.copytree(moved, current)
    else:
        current.mkdir(parents=True)

    for entry in (staging / "data").iterdir():
        target = current / entry.name
        if target.exists():
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
        if entry.is_dir():
            shutil.copytree(entry, target)
        else:
            shutil.copy2(entry, target)
    try:
        os.chmod(current, 0o700)
    except OSError:
        pass
    for name in ("meridian.toml", ".env"):
        source = staging / name
        if source.exists():
            target = home.CONFIG_PATH if name == "meridian.toml" else home.ENV_PATH
            shutil.copy2(source, target)
            os.chmod(target, 0o600)
    shutil.rmtree(staging, ignore_errors=True)

    process.start_api(cfg)
    process.wait_api(cfg, seconds=60)
    if home.get_cfg(cfg, "server.ui", False) and (home.APP / "frontend" / "web" / "server.js").exists():
        process.start_web(cfg)
        process.wait_web(cfg, seconds=30)
    ui.ok(f"Restored {archive.name}")
    if moved:
        ui.info(f"Previous data kept at {moved}")
