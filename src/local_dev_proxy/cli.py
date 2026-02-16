from __future__ import annotations

import typer

from .services import ServiceError, run_service, run_session_up


app = typer.Typer(help="Local dev proxy orchestration CLI")
session_app = typer.Typer(help="Zellij session controls")

app.add_typer(session_app, name="session")


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


@session_app.command("up")
def session_up() -> None:
    """Start or reattach to the zellij session."""
    try:
        return_code = run_session_up()
    except ServiceError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    raise typer.Exit(code=return_code)


if __name__ == "__main__":
    app()
