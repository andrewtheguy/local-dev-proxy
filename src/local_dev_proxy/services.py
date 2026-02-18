from __future__ import annotations

import threading
import urllib.error
import urllib.request

from .config import ProjectPaths, get_paths
from .process_manager import ServiceManager
from .proxy import ADMIN_PORT, ProxyServer
from .routes import RouteConfigError, RoutesManifest, load_routes


class ServiceError(RuntimeError):
    pass


_sync_routes_lock = threading.Lock()


def sync_all_routes() -> None:
    with _sync_routes_lock:
        admin_url = f"http://127.0.0.1:{ADMIN_PORT}/reload"
        req = urllib.request.Request(admin_url, method="POST", data=b"")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                if resp.status >= 400:
                    raise ServiceError(f"Reload failed: HTTP {resp.status}")
        except urllib.error.URLError as exc:
            raise ServiceError(f"Failed to reach proxy admin: {exc}") from exc


def start_services_managed(paths: ProjectPaths | None = None) -> ServiceManager:
    resolved_paths = paths or get_paths()
    manifest = _load_manifest(resolved_paths)
    log_dir = resolved_paths.root / "logs"
    return ServiceManager(manifest, log_dir=log_dir, cwd=resolved_paths.root)


def start_proxy(
    paths: ProjectPaths | None = None,
    service_manager: ServiceManager | None = None,
) -> ProxyServer:
    resolved_paths = paths or get_paths()
    manifest = _load_manifest(resolved_paths)

    server = ProxyServer(
        services_file=resolved_paths.services_file,
        http_port=manifest.http_port,
        bind=manifest.bind,
        service_manager=service_manager,
    )
    server.start()
    return server


def _load_manifest(paths: ProjectPaths) -> RoutesManifest:
    try:
        return load_routes(paths.services_file)
    except RouteConfigError as exc:
        raise ServiceError(str(exc)) from exc
