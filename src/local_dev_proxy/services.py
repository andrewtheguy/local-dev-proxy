from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
from typing import Mapping

from .caddy_api import CaddyAPIError, CaddyAdminClient
from .config import ProjectPaths, get_paths, load_env, require_port
from .routes import RouteConfigError, RoutesManifest, build_routes, load_routes
from .state import locked_active_services, read_active_services


class ServiceError(RuntimeError):
    pass


SERVICE_ROUTE_KEYS = {
    "minio": {"minio", "minioconsole"},
    "s3browser": {"s3browser"},
    "weed": set(),
}


def activate_service(service_name: str, paths: ProjectPaths | None = None) -> list[str]:
    resolved_paths = paths or get_paths()
    manifest, env = _load_manifest_and_env(resolved_paths)

    route_keys = _resolve_route_keys(service_name, manifest)
    if not route_keys:
        return sorted(read_active_services(resolved_paths))

    with locked_active_services(resolved_paths) as active_services:
        active_services.update(route_keys)
        try:
            _sync_caddy(resolved_paths, manifest, env, active_services)
        except (CaddyAPIError, ValueError) as exc:
            raise ServiceError(str(exc)) from exc
        return sorted(active_services)


def deactivate_service(service_name: str, paths: ProjectPaths | None = None) -> list[str]:
    resolved_paths = paths or get_paths()
    manifest, env = _load_manifest_and_env(resolved_paths)

    route_keys = _resolve_route_keys(service_name, manifest)
    if not route_keys:
        return sorted(read_active_services(resolved_paths))

    with locked_active_services(resolved_paths) as active_services:
        active_services.difference_update(route_keys)
        try:
            _sync_caddy(resolved_paths, manifest, env, active_services)
        except (CaddyAPIError, ValueError) as exc:
            raise ServiceError(str(exc)) from exc
        return sorted(active_services)


def sync_caddy(paths: ProjectPaths | None = None) -> list[str]:
    resolved_paths = paths or get_paths()
    manifest, env = _load_manifest_and_env(resolved_paths)

    with locked_active_services(resolved_paths) as active_services:
        try:
            _sync_caddy(resolved_paths, manifest, env, active_services)
        except (CaddyAPIError, ValueError) as exc:
            raise ServiceError(str(exc)) from exc
        return sorted(active_services)


def caddy_status(paths: ProjectPaths | None = None) -> dict:
    resolved_paths = paths or get_paths()
    manifest, _ = _load_manifest_and_env(resolved_paths)

    active_services = sorted(read_active_services(resolved_paths))
    status = {
        "admin_url": manifest.caddy.admin_url,
        "healthy": False,
        "active_services": active_services,
        "route_ids": [],
    }

    try:
        with CaddyAdminClient(manifest.caddy.admin_url) as client:
            client.healthcheck()
            routes = client.get_routes()
    except CaddyAPIError as exc:
        status["error"] = str(exc)
        return status

    status["healthy"] = True
    status["route_ids"] = [
        str(route.get("@id", "<unnamed>")) for route in routes if isinstance(route, dict)
    ]
    return status


def run_named_service(service_name: str, paths: ProjectPaths | None = None) -> int:
    resolved_paths = paths or get_paths()
    normalized_name = service_name.strip().lower()

    try:
        command, extra_env = _service_command(normalized_name, resolved_paths)
    except ValueError as exc:
        raise ServiceError(str(exc)) from exc
    runtime_env = os.environ.copy()
    runtime_env.update(load_env(resolved_paths.env_file))
    runtime_env.update(extra_env)

    should_register_routes = bool(SERVICE_ROUTE_KEYS.get(normalized_name))
    if should_register_routes:
        activate_service(normalized_name, resolved_paths)

    try:
        return_code = _run_process(command, runtime_env, resolved_paths.root)
    finally:
        if should_register_routes:
            deactivate_service(normalized_name, resolved_paths)

    return return_code


def run_session_up(paths: ProjectPaths | None = None, session_name: str = "caddy") -> int:
    resolved_paths = paths or get_paths()
    if not resolved_paths.layout_file.exists():
        raise ServiceError(f"Missing zellij layout file: {resolved_paths.layout_file}")

    session_info = _find_session_line(session_name)

    if session_info:
        if "EXITED" in session_info:
            _run_command(["zellij", "delete-session", session_name], cwd=resolved_paths.root)
        else:
            raise ServiceError(
                f"Session '{session_name}' is already active. Please exit it first."
            )

    return subprocess.run(
        ["zellij", "-s", session_name, "-n", str(resolved_paths.layout_file)],
        check=False,
        cwd=resolved_paths.root,
    ).returncode


def _service_command(service_name: str, paths: ProjectPaths) -> tuple[list[str], dict[str, str]]:
    env = load_env(paths.env_file)

    if service_name == "minio":
        minio_port = require_port(env, "MINIO_PORT")
        minio_console_port = require_port(env, "MINIO_CONSOLE_PORT")
        paths.data_dir.mkdir(parents=True, exist_ok=True)

        command = [
            "minio",
            "server",
            str(paths.data_dir),
            "--address",
            f":{minio_port}",
            "--console-address",
            f":{minio_console_port}",
        ]
        return command, {"MINIO_BROWSER_REDIRECT": "off"}

    if service_name == "s3browser":
        s3browser_port = require_port(env, "S3BROWSER_PORT")
        return ["s3browser", "-b", f"127.0.0.1:{s3browser_port}"], {}

    if service_name == "weed":
        weed_port = require_port(env, "WEED_S3_PORT")
        weed_dir = paths.data_dir / "weed"
        weed_dir.mkdir(parents=True, exist_ok=True)

        command = [
            "weed",
            "mini",
            f"-dir={weed_dir}",
            "-ip=127.0.0.1",
            "-ip.bind=127.0.0.1",
            f"-s3.port={weed_port}",
            "-master.port=39333",
            "-filer.port=38888",
            "-volume.port=39340",
            "-webdav=false",
            "-admin.ui=false",
        ]

        access_key = env.get("AWS_ACCESS_KEY_ID", "weedadmin")
        secret_key = env.get("AWS_SECRET_ACCESS_KEY", "weedadmin")
        return command, {
            "AWS_ACCESS_KEY_ID": access_key,
            "AWS_SECRET_ACCESS_KEY": secret_key,
        }

    raise ServiceError(f"Unknown service: {service_name}")


def _run_process(command: list[str], env: Mapping[str, str], cwd: Path) -> int:
    try:
        process = subprocess.Popen(command, env=dict(env), cwd=cwd)
    except FileNotFoundError as exc:
        raise ServiceError(f"Command not found: {command[0]}") from exc

    watched_signals = [signal.SIGINT, signal.SIGTERM]
    if hasattr(signal, "SIGHUP"):
        watched_signals.append(signal.SIGHUP)

    previous_handlers: dict[int, object] = {}

    def forward(signum: int, _frame: object) -> None:
        if process.poll() is None:
            process.send_signal(signum)

    for sig in watched_signals:
        previous_handlers[sig] = signal.getsignal(sig)
        signal.signal(sig, forward)

    try:
        return process.wait()
    finally:
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)


def _sync_caddy(
    paths: ProjectPaths,
    manifest: RoutesManifest,
    env: Mapping[str, str],
    active_services: set[str],
) -> None:
    routes = build_routes(manifest, env, active_services)

    with CaddyAdminClient(manifest.caddy.admin_url) as client:
        client.ensure_server(paths.bootstrap_config_file)
        client.set_listen_addresses(manifest.caddy.listen_addresses())
        client.set_routes(routes)


def _load_manifest_and_env(paths: ProjectPaths) -> tuple[RoutesManifest, dict[str, str]]:
    try:
        manifest = load_routes(paths.routes_file)
    except RouteConfigError as exc:
        raise ServiceError(str(exc)) from exc

    env = load_env(paths.env_file)
    return manifest, env


def _resolve_route_keys(service_name: str, manifest: RoutesManifest) -> set[str]:
    normalized_name = service_name.strip().lower()

    if normalized_name in SERVICE_ROUTE_KEYS:
        route_keys = set(SERVICE_ROUTE_KEYS[normalized_name])
    elif normalized_name in manifest.services:
        route_keys = {normalized_name}
    else:
        raise ServiceError(f"Unknown service: {service_name}")

    missing = route_keys.difference(manifest.services.keys())
    if missing:
        missing_str = ", ".join(sorted(missing))
        raise ServiceError(
            f"Route definitions missing from routes.toml for: {missing_str}"
        )

    return route_keys


def _find_session_line(session_name: str) -> str | None:
    try:
        result = subprocess.run(
            ["zellij", "list-sessions", "-n"],
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise ServiceError("Command not found: zellij") from exc

    if result.returncode != 0:
        return None

    for line in result.stdout.splitlines():
        if line.startswith(f"{session_name} "):
            return line

    return None


def _run_command(command: list[str], cwd: Path) -> None:
    try:
        subprocess.run(command, check=True, cwd=cwd)
    except FileNotFoundError as exc:
        raise ServiceError(f"Command not found: {command[0]}") from exc
    except subprocess.CalledProcessError as exc:
        joined = " ".join(command)
        raise ServiceError(f"Command failed ({exc.returncode}): {joined}") from exc
