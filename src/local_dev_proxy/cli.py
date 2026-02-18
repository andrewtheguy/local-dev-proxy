from __future__ import annotations

import typer

from .config import get_paths
from .routes import load_routes
from .services import ServiceError, run_service, sync_all_routes


app = typer.Typer(help="Local dev proxy orchestration CLI")


@app.command("run")
def run_command(
    name: str = typer.Argument(..., help="Service name defined in services.toml"),
) -> None:
    """Run a service in the foreground (used by zellij panes)."""
    try:
        return_code = run_service(name)
    except ServiceError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    raise typer.Exit(code=return_code)


@app.command("routes")
def routes_command() -> None:
    """Show service URLs."""
    paths = get_paths()
    manifest = load_routes(paths.services_file)
    for service in manifest.services.values():
        for route in service.routes:
            for host in route.hosts:
                typer.echo(f"{route.id:20s} http://{host}:{manifest.caddy.http_port}/")


@app.command("sync")
def sync_command() -> None:
    """Push all routes to the running Caddy instance."""
    try:
        sync_all_routes()
    except ServiceError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo("Routes synced successfully.")


@app.command("tray")
def tray_command() -> None:
    """Run as a macOS menu bar app."""
    from .tray import run_tray

    run_tray()



if __name__ == "__main__":
    app()
