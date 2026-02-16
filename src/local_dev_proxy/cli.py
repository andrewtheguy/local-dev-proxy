from __future__ import annotations

import json

import typer

from .services import (
    ServiceError,
    activate_service,
    caddy_status,
    deactivate_service,
    run_named_service,
    run_session_up,
    sync_caddy,
)


app = typer.Typer(help="Local dev proxy orchestration CLI")
caddy_app = typer.Typer(help="Caddy Admin API controls")
session_app = typer.Typer(help="Zellij session controls")

app.add_typer(caddy_app, name="caddy")
app.add_typer(session_app, name="session")


@app.command("service")
def service_command(
    service_name: str = typer.Argument(..., help="Service name: minio, s3browser, weed")
) -> None:
    """Run a managed local service in the foreground."""
    try:
        return_code = run_named_service(service_name)
    except ServiceError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    raise typer.Exit(code=return_code)


@caddy_app.command("activate")
def caddy_activate(service_name: str) -> None:
    """Activate routing for a service and sync Caddy."""
    try:
        active = activate_service(service_name)
    except ServiceError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(
        json.dumps(
            {"active_services": active},
            indent=2,
        )
    )


@caddy_app.command("deactivate")
def caddy_deactivate(service_name: str) -> None:
    """Deactivate routing for a service and sync Caddy."""
    try:
        active = deactivate_service(service_name)
    except ServiceError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(
        json.dumps(
            {"active_services": active},
            indent=2,
        )
    )


@caddy_app.command("sync")
def caddy_sync() -> None:
    """Resync Caddy from current active service state."""
    try:
        active = sync_caddy()
    except ServiceError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(
        json.dumps(
            {"active_services": active},
            indent=2,
        )
    )


@caddy_app.command("status")
def caddy_status_command() -> None:
    """Show Caddy admin health and current route IDs."""
    status = caddy_status()
    typer.echo(json.dumps(status, indent=2))

    if not status.get("healthy", False):
        raise typer.Exit(code=1)


@session_app.command("up")
def session_up() -> None:
    """Start zellij caddy session layout."""
    try:
        return_code = run_session_up()
    except ServiceError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    raise typer.Exit(code=return_code)


if __name__ == "__main__":
    app()
