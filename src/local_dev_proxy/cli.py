from __future__ import annotations

import json
import subprocess
import sys
import urllib.error
import urllib.request

import typer

from .config import get_paths
from .proxy import ADMIN_PORT
from .routes import load_routes
from .services import ServiceError, sync_all_routes


app = typer.Typer(help="Local dev proxy orchestration CLI")


def _admin_request(method: str, path: str) -> str:
    url = f"http://127.0.0.1:{ADMIN_PORT}{path}"
    req = urllib.request.Request(url, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.read().decode()
    except urllib.error.URLError as exc:
        typer.echo(f"Error: could not reach admin API: {exc}", err=True)
        raise typer.Exit(code=1)


@app.command("start-manager")
def start_manager_command() -> None:
    """Run as a macOS menu bar app with built-in process manager."""
    from .tray import run_tray

    run_tray()


@app.command("routes")
def routes_command() -> None:
    """Show service URLs."""
    paths = get_paths()
    manifest = load_routes(paths.services_file)
    for service in manifest.services.values():
        for route in service.routes:
            for host in route.hosts:
                typer.echo(f"{route.id:20s} http://{host}:{manifest.http_port}/")


@app.command("sync")
def sync_command() -> None:
    """Push all routes to the running proxy instance."""
    try:
        sync_all_routes()
    except ServiceError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo("Routes synced successfully.")


@app.command("status")
def status_command() -> None:
    """Show status of all managed services."""
    body = _admin_request("GET", "/services")
    services = json.loads(body)

    typer.echo(f"{'NAME':15s} {'STATUS':10s} {'PID':>8s} {'RESTARTS':>10s} {'EXIT CODE':>10s}")
    typer.echo("-" * 58)
    for svc in services:
        pid_str = str(svc["pid"]) if svc["pid"] is not None else "-"
        exit_str = str(svc["exit_code"]) if svc["exit_code"] is not None else "-"
        typer.echo(
            f"{svc['name']:15s} {svc['status']:10s} {pid_str:>8s} "
            f"{svc['restart_count']:>10d} {exit_str:>10s}"
        )


@app.command("restart")
def restart_command(
    name: str = typer.Argument(..., help="Service name to restart"),
) -> None:
    """Restart a managed service."""
    _admin_request("POST", f"/services/{name}/restart")
    typer.echo(f"Restarted {name}")


@app.command("stop")
def stop_command(
    name: str = typer.Argument(..., help="Service name to stop"),
) -> None:
    """Stop a managed service."""
    _admin_request("POST", f"/services/{name}/stop")
    typer.echo(f"Stopped {name}")


@app.command("start")
def start_command(
    name: str = typer.Argument(..., help="Service name to start"),
) -> None:
    """Start a stopped managed service."""
    _admin_request("POST", f"/services/{name}/start")
    typer.echo(f"Started {name}")


@app.command("logs")
def logs_command(
    name: str = typer.Argument(..., help="Service name"),
    lines: int = typer.Option(100, "-n", "--lines", help="Number of lines to show"),
    follow: bool = typer.Option(False, "-f", "--follow", help="Follow log output"),
) -> None:
    """Show logs for a managed service."""
    if follow:
        paths = get_paths()
        log_path = paths.root / "logs" / f"{name}.log"
        if not log_path.exists():
            typer.echo(f"Log file not found: {log_path}", err=True)
            raise typer.Exit(code=1)
        proc = subprocess.Popen(
            ["tail", "-n", str(lines), "-f", str(log_path)],
            stdout=sys.stdout,
            stderr=sys.stderr,
        )
        try:
            proc.wait()
        except KeyboardInterrupt:
            proc.terminate()
        return

    body = _admin_request("GET", f"/services/{name}/logs?lines={lines}")
    typer.echo(body, nl=False)


if __name__ == "__main__":
    app()
