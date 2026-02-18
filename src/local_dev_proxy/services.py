from __future__ import annotations

import os
import pty
from pathlib import Path
import signal
import subprocess
import threading
import time
from typing import Mapping

from .caddy_api import CaddyAPIError, CaddyAdminClient
from .config import ProjectPaths, get_paths
from .routes import RouteConfigError, RoutesManifest, build_routes, load_routes, resolve_command


class ServiceError(RuntimeError):
    pass


def run_service(name: str, paths: ProjectPaths | None = None) -> int:
    resolved_paths = paths or get_paths()
    manifest = _load_manifest(resolved_paths)

    service = manifest.services.get(name)
    if service is None:
        raise ServiceError(f"Unknown service: {name}")
    if service.command is None:
        raise ServiceError(f"Service '{name}' has no command (unmanaged service)")

    effective_env = {**service.env, **os.environ}
    command = resolve_command(service.command, effective_env)

    runtime_env = os.environ.copy()
    runtime_env.update(service.env)

    if service.routes:
        _wait_for_caddy_health(manifest.caddy.admin_url, timeout_seconds=10.0)
        _sync_all_routes(resolved_paths, manifest)

    return _run_process(command, runtime_env, resolved_paths.root)


def sync_all_routes(paths: ProjectPaths | None = None) -> None:
    resolved_paths = paths or get_paths()
    manifest = _load_manifest(resolved_paths)
    _sync_all_routes(resolved_paths, manifest)


def _sync_all_routes(paths: ProjectPaths, manifest: RoutesManifest) -> None:
    routes = build_routes(manifest, env_override=os.environ)

    with CaddyAdminClient(manifest.caddy.admin_url) as client:
        client.ensure_server(paths.root / "config" / "caddy-bootstrap.json")
        client.set_listen_addresses(manifest.caddy.listen_addresses())
        client.set_routes(routes)


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


def _wait_for_caddy_health(admin_url: str, timeout_seconds: float) -> None:
    deadline = time.monotonic() + timeout_seconds

    while time.monotonic() < deadline:
        try:
            with CaddyAdminClient(admin_url) as client:
                client.healthcheck()
                return
        except CaddyAPIError:
            time.sleep(0.2)

    raise ServiceError(f"Caddy did not become healthy at {admin_url} within {timeout_seconds}s")


def _load_manifest(paths: ProjectPaths) -> RoutesManifest:
    try:
        return load_routes(paths.services_file)
    except RouteConfigError as exc:
        raise ServiceError(str(exc)) from exc


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


def start_caddy_background(paths: ProjectPaths | None = None) -> subprocess.Popen:
    """Start Caddy as a non-blocking subprocess. Returns the Popen handle."""
    resolved_paths = paths or get_paths()
    manifest = _load_manifest(resolved_paths)

    service = manifest.services.get("caddy")
    if service is None:
        raise ServiceError("No 'caddy' service defined in services.toml")
    if service.command is None:
        raise ServiceError("Service 'caddy' has no command")

    effective_env = {**service.env, **os.environ}
    command = resolve_command(service.command, effective_env)

    runtime_env = os.environ.copy()
    runtime_env.update(service.env)

    try:
        return subprocess.Popen(command, env=runtime_env, cwd=resolved_paths.root)
    except FileNotFoundError as exc:
        raise ServiceError(f"Command not found: {command[0]}") from exc


def start_zellij_headless(
    paths: ProjectPaths | None = None, session_name: str = "local-dev-proxy",
) -> tuple[int, int]:
    """Start a headless zellij session using pty.fork().

    Returns (child_pid, master_fd). A daemon thread drains the master fd
    so zellij doesn't block on output.
    """
    resolved_paths = paths or get_paths()
    if not resolved_paths.layout_file.exists():
        raise ServiceError(f"Missing zellij layout file: {resolved_paths.layout_file}")

    # Clean up any exited session with the same name.
    session_info = _find_session_line(session_name)
    if session_info and "EXITED" in session_info:
        _run_command(["zellij", "delete-session", session_name], cwd=resolved_paths.root)

    pid, master_fd = pty.fork()
    if pid == 0:
        # Child process — pty.fork() already set up the slave pty as stdin/stdout/stderr.
        os.chdir(resolved_paths.root)
        os.execvp(
            "zellij",
            ["zellij", "-s", session_name, "-n", str(resolved_paths.layout_file)],
        )
    else:
        # Parent — drain the master fd so zellij doesn't block on output.
        def _drain_pty(fd: int) -> None:
            while True:
                try:
                    data = os.read(fd, 4096)
                    if not data:
                        break
                except OSError:
                    break

        drain_thread = threading.Thread(target=_drain_pty, args=(master_fd,), daemon=True)
        drain_thread.start()
        return pid, master_fd


def kill_zellij_session(session_name: str = "local-dev-proxy") -> None:
    """Force-delete a zellij session."""
    subprocess.run(
        ["zellij", "delete-session", session_name, "--force"],
        check=False,
        capture_output=True,
    )
