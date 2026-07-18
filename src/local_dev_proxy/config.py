from __future__ import annotations

from dataclasses import dataclass
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path
import fcntl
import os
import shutil
import subprocess
from typing import Mapping

_APP_NAME = "local-dev-proxy"
_SAMPLE_RESOURCE = "services.toml.sample"
_ICON_RESOURCE = "assets/tray-icon.png"

# Single-instance lock for the running manager (proxy + service manager).
LOCK_PATH = os.path.join(os.environ.get("TMPDIR", "/tmp"), "local-dev-proxy.lock")


def manager_pid() -> int | None:
    """Return the PID of the running manager, or None if not running.

    Detected via the single-instance flock: if we can take the lock the
    manager is not running; otherwise ``lsof`` tells us who holds it.
    """
    try:
        fd = os.open(LOCK_PATH, os.O_WRONLY)
    except FileNotFoundError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return None  # lock was free -> nobody running
    except OSError:
        pass  # lock held -> manager is running
    finally:
        os.close(fd)

    try:
        out = subprocess.check_output(
            ["lsof", "-t", LOCK_PATH], text=True, stderr=subprocess.DEVNULL
        ).strip()
        if out:
            return int(out.splitlines()[0])
    except (subprocess.CalledProcessError, ValueError):
        pass
    return None


def manager_running() -> bool:
    return manager_pid() is not None


@dataclass(frozen=True)
class ProjectPaths:
    config_dir: Path
    services_file: Path
    logs_dir: Path


def user_config_dir() -> Path:
    """Return the per-user config directory for local-dev-proxy.

    Honors ``LOCAL_DEV_PROXY_CONFIG_DIR`` (mainly for tests/dev), then
    ``$XDG_CONFIG_HOME``, falling back to ``~/.config``.
    """
    override = os.environ.get("LOCAL_DEV_PROXY_CONFIG_DIR")
    if override:
        return Path(override).expanduser().resolve()

    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return (base / _APP_NAME).resolve()


def get_paths(config_dir: Path | None = None) -> ProjectPaths:
    root = (config_dir or user_config_dir()).resolve()
    return ProjectPaths(
        config_dir=root,
        services_file=root / "services.toml",
        logs_dir=root / "logs",
    )


def bundled_resource(name: str) -> Traversable:
    """Return a Traversable for a resource shipped inside the package."""
    resource = resources.files("local_dev_proxy")
    for part in name.split("/"):
        resource = resource / part
    return resource


def ensure_config(paths: ProjectPaths | None = None) -> ProjectPaths:
    """Create the config/log dirs and seed services.toml from the sample.

    Idempotent: safe to call at every entrypoint.
    """
    resolved = paths or get_paths()
    resolved.config_dir.mkdir(parents=True, exist_ok=True)
    resolved.logs_dir.mkdir(parents=True, exist_ok=True)

    if not resolved.services_file.exists():
        sample = bundled_resource(_SAMPLE_RESOURCE)
        with resources.as_file(sample) as sample_path:
            shutil.copyfile(sample_path, resolved.services_file)

    return resolved


def icon_path() -> Path | None:
    """Return a filesystem path to the bundled tray icon, or None if missing.

    Uses the config dir as a stable cache so the icon works even when the
    package is loaded from a zip/frozen bundle.
    """
    resource = bundled_resource(_ICON_RESOURCE)
    if not resource.is_file():
        return None

    cache = user_config_dir() / "tray-icon.png"
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        with resources.as_file(resource) as src:
            shutil.copyfile(src, cache)
    except OSError:
        return None
    return cache


def require_port(env: Mapping[str, str], key: str) -> int:
    raw = env.get(key)
    if raw is None:
        raise ValueError(f"Missing required port variable: {key}")

    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{key} must be an integer, got: {raw!r}") from exc

    if value < 1 or value > 65535:
        raise ValueError(f"{key} must be in range 1-65535, got: {value}")

    return value
