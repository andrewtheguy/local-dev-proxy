from __future__ import annotations

from .config import ProjectPaths
from .process_manager import ServiceManager
from .proxy import ProxyServer
from .routes import load_routes


def start_services_managed(paths: ProjectPaths) -> ServiceManager:
    manifest = load_routes(paths.services_file)
    return ServiceManager(
        manifest,
        log_dir=paths.logs_dir,
        cwd=paths.config_dir,
    )


def start_proxy(paths: ProjectPaths) -> ProxyServer:
    manifest = load_routes(paths.services_file)

    server = ProxyServer(
        services_file=paths.services_file,
        http_port=manifest.http_port,
        bind=manifest.bind,
    )
    try:
        server.start()
    except Exception:
        server.stop()
        raise
    return server
