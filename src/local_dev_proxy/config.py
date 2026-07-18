from __future__ import annotations

from dataclasses import dataclass
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path
import os
import shutil
import sys
from collections.abc import Mapping

from filelock import FileLock, Timeout
from PySide6.QtCore import QCoreApplication, QStandardPaths

_APP_NAME = "local-dev-proxy"
_ORGANIZATION_NAME = "andrewtheguy"
_ORGANIZATION_DOMAIN = "andrewtheguy.com"
_ICON_RESOURCE = "assets/tray-icon.png"
_MACOS_ICON_RESOURCE = "assets/tray-icon-macos.png"
_DOCK_ICON_RESOURCE = "assets/dock-icon.png"


@dataclass(frozen=True)
class ProjectPaths:
    """All application paths derived from one canonical profile root."""

    root: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", self.root.expanduser().resolve())

    @property
    def services_file(self) -> Path:
        return self.root / "services.toml"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    @property
    def instance_lock(self) -> Path:
        return self.root / ".instance.lock"


class AlreadyRunningError(RuntimeError):
    """Raised when the single-instance lock is already held by another manager."""


def acquire_instance_lock(paths: ProjectPaths) -> FileLock:
    """Take and return the single-instance lock for the manager's lifetime.

    The lock is scoped to the selected configuration directory, so isolated
    development/test profiles do not contend with the normal application.

    Raises :class:`AlreadyRunningError` if another manager already holds it.
    """
    lock = FileLock(paths.instance_lock)
    try:
        lock.acquire(timeout=0)
    except Timeout as exc:
        raise AlreadyRunningError("local-dev-proxy is already running.") from exc
    return lock


def release_instance_lock(lock: FileLock) -> None:
    """Release the single-instance guard."""
    lock.release()


def configure_application_identity() -> None:
    """Set stable Qt application metadata used by platform integrations."""
    QCoreApplication.setApplicationName(_APP_NAME)
    QCoreApplication.setOrganizationName(_ORGANIZATION_NAME)
    QCoreApplication.setOrganizationDomain(_ORGANIZATION_DOMAIN)


def user_config_dir() -> Path:
    """Return the platform-standard per-user application config directory.

    ``LOCAL_DEV_PROXY_CONFIG_DIR`` is an explicit development/test override.
    Normal launches use Qt's ``AppConfigLocation``, which follows the native
    convention on macOS, Windows, and Linux (including XDG on Linux).
    """
    override = os.environ.get("LOCAL_DEV_PROXY_CONFIG_DIR")
    if override:
        return Path(override).expanduser().resolve()

    configure_application_identity()
    location = QStandardPaths.writableLocation(
        QStandardPaths.StandardLocation.AppConfigLocation
    )
    if not location:
        raise RuntimeError("The operating system did not provide a config directory")
    return Path(location).expanduser().resolve()


def get_paths(config_root: Path | None = None) -> ProjectPaths:
    """Resolve all runtime paths, optionally for an isolated config profile."""
    return ProjectPaths(config_root or user_config_dir())


def bundled_resource(name: str) -> Traversable:
    """Return a Traversable for a resource shipped inside the package."""
    resource = resources.files("local_dev_proxy")
    for part in name.split("/"):
        resource = resource / part
    return resource


def ensure_profile(paths: ProjectPaths | None = None) -> ProjectPaths:
    """Create the profile and log directories without creating configuration.

    Passing paths makes startup and tests independent of the user's real
    platform config directory. The operation is idempotent.
    """
    resolved = paths or get_paths()
    resolved.root.mkdir(parents=True, exist_ok=True)
    resolved.logs_dir.mkdir(parents=True, exist_ok=True)
    return resolved


def _cached_icon(
    resource_name: str,
    cache_name: str,
    paths: ProjectPaths | None = None,
) -> Path | None:
    """Copy a bundled icon into the config dir and return its path, or None.

    Using the config dir as a stable cache means the icon works even when the
    package is loaded from a zip/frozen bundle (where resources have no real path).
    """
    resource = bundled_resource(resource_name)
    if not resource.is_file():
        return None

    cache = (paths or get_paths()).root / cache_name
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        with resources.as_file(resource) as src:
            shutil.copyfile(src, cache)
    except OSError:
        return None
    return cache


def icon_path(paths: ProjectPaths | None = None) -> Path | None:
    """Return a filesystem path to the bundled system-tray icon, or None."""
    if sys.platform == "darwin":
        return _cached_icon(
            _MACOS_ICON_RESOURCE,
            "tray-icon-macos.png",
            paths,
        )
    return _cached_icon(_ICON_RESOURCE, "tray-icon.png", paths)


def dock_icon_path(paths: ProjectPaths | None = None) -> Path | None:
    """Return a filesystem path to the bundled Dock icon, or None."""
    return _cached_icon(_DOCK_ICON_RESOURCE, "dock-icon.png", paths)


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


def require_socket_path(env: Mapping[str, str], key: str) -> str:
    raw = env.get(key)
    if raw is None:
        raise ValueError(f"Missing required socket variable: {key}")
    if not raw:
        raise ValueError(f"{key} must be a non-empty socket path")
    return raw
