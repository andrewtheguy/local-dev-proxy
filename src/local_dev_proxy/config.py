from __future__ import annotations

from dataclasses import dataclass
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path
import os
import shutil
import sys
import tempfile
from collections.abc import Mapping

from filelock import FileLock, Timeout

_APP_NAME = "local-dev-proxy"
_SAMPLE_RESOURCE = "services.toml.sample"
_ICON_RESOURCE = "assets/tray-icon.png"
_MACOS_ICON_RESOURCE = "assets/tray-icon-macos.png"
_DOCK_ICON_RESOURCE = "assets/dock-icon.png"

# Single-instance lock for the running manager (proxy + service manager). Backed
# by ``filelock``, whose OS-level advisory lock is released automatically when the
# holding process dies, so the lock never goes stale. A sidecar pidfile records
# the holder's PID (the OS lock alone can't tell us who holds it cross-platform).
LOCK_PATH = os.path.join(tempfile.gettempdir(), "local-dev-proxy.lock")
PID_PATH = os.path.join(tempfile.gettempdir(), "local-dev-proxy.pid")

# Cross-platform "raise the running window" request. A second launch drops this
# marker; the running manager polls for it and un-hides its window. Replaces the
# Unix-only SIGUSR1 path so the same mechanism works on Windows.
RAISE_REQUEST_PATH = os.path.join(tempfile.gettempdir(), "local-dev-proxy.raise")


class AlreadyRunningError(RuntimeError):
    """Raised when the single-instance lock is already held by another manager."""


def acquire_instance_lock() -> FileLock:
    """Take the single-instance lock and record our PID; hold the returned lock
    for the manager's lifetime and pass it to :func:`release_instance_lock`.

    Raises :class:`AlreadyRunningError` if another manager already holds it.
    """
    lock = FileLock(LOCK_PATH)
    try:
        lock.acquire(timeout=0)
    except Timeout as exc:
        raise AlreadyRunningError("local-dev-proxy is already running.") from exc
    Path(PID_PATH).write_text(str(os.getpid()))
    return lock


def release_instance_lock(lock: FileLock) -> None:
    """Release the single-instance lock and remove the sidecar pidfile."""
    lock.release()
    try:
        os.remove(PID_PATH)
    except OSError:
        pass  # already gone (or another instance owns it now)


def manager_pid() -> int | None:
    """Return the PID of the running manager, or None if not running.

    Probed via the single-instance lock: if we can take it (non-blocking) the
    manager is not running; otherwise the sidecar pidfile names the holder.
    """
    lock = FileLock(LOCK_PATH)
    try:
        lock.acquire(timeout=0)
    except Timeout:
        pass  # held -> manager is running
    else:
        lock.release()
        return None  # lock was free -> nobody running

    try:
        return int(Path(PID_PATH).read_text().strip())
    except (OSError, ValueError):
        return None  # pidfile missing/corrupt (e.g. mid-startup race)


def manager_running() -> bool:
    return manager_pid() is not None


def request_raise() -> None:
    """Ask the running manager to raise its window (cross-platform SIGUSR1)."""
    Path(RAISE_REQUEST_PATH).write_text(str(os.getpid()))


def consume_raise_request() -> bool:
    """Return True (clearing the request) if a raise was requested since last call."""
    try:
        os.remove(RAISE_REQUEST_PATH)
    except OSError:
        return False
    return True


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


def _cached_icon(resource_name: str, cache_name: str) -> Path | None:
    """Copy a bundled icon into the config dir and return its path, or None.

    Using the config dir as a stable cache means the icon works even when the
    package is loaded from a zip/frozen bundle (where resources have no real path).
    """
    resource = bundled_resource(resource_name)
    if not resource.is_file():
        return None

    cache = user_config_dir() / cache_name
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        with resources.as_file(resource) as src:
            shutil.copyfile(src, cache)
    except OSError:
        return None
    return cache


def icon_path() -> Path | None:
    """Return a filesystem path to the bundled system-tray icon, or None."""
    if sys.platform == "darwin":
        return _cached_icon(_MACOS_ICON_RESOURCE, "tray-icon-macos.png")
    return _cached_icon(_ICON_RESOURCE, "tray-icon.png")


def dock_icon_path() -> Path | None:
    """Return a filesystem path to the bundled Dock icon, or None."""
    return _cached_icon(_DOCK_ICON_RESOURCE, "dock-icon.png")


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
