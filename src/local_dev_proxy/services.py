from __future__ import annotations

from .config import ProjectPaths, get_paths
from .process_manager import ServiceManager
from .proxy import ProxyServer
from .routes import RouteConfigError, RoutesManifest, load_routes


class ServiceError(RuntimeError):
    pass


def start_services_managed(paths: ProjectPaths | None = None) -> ServiceManager:
    resolved_paths = paths or get_paths()
    manifest = _load_manifest(resolved_paths)
    return ServiceManager(
        manifest,
        log_dir=resolved_paths.logs_dir,
        cwd=resolved_paths.config_dir,
    )


def start_proxy(paths: ProjectPaths | None = None) -> ProxyServer:
    resolved_paths = paths or get_paths()
    manifest = _load_manifest(resolved_paths)

    server = ProxyServer(
        services_file=resolved_paths.services_file,
        http_port=manifest.http_port,
        bind=manifest.bind,
    )
    server.start()
    return server


def _load_manifest(paths: ProjectPaths) -> RoutesManifest:
    try:
        return load_routes(paths.services_file)
    except RouteConfigError as exc:
        raise ServiceError(str(exc)) from exc
