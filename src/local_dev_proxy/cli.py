from __future__ import annotations

import subprocess
import sys

import typer

from .config import ensure_config, manager_running

app = typer.Typer(
    help="Local dev proxy: reverse proxy + process manager with a PySide6 manager UI.",
    invoke_without_command=True,
    no_args_is_help=False,
)


def _spawn_detached() -> int:
    """Launch the app (proxy + services + window + system-tray icon) detached.

    Re-execs ``python -m local_dev_proxy --foreground`` in a new session with
    output handled by the manager's rotating logger and returns the spawned
    process PID. There is no startup handshake or command channel back to the
    launcher.
    """
    paths = ensure_config()
    process = subprocess.Popen(
        [sys.executable, "-m", "local_dev_proxy", "--foreground"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        cwd=str(paths.config_dir),
    )
    return process.pid


@app.callback(invoke_without_command=True)
def main(
    foreground: bool = typer.Option(
        False,
        "--foreground",
        "-f",
        help="Run the app in the foreground (blocking). Used by the detached spawn.",
    ),
) -> None:
    """Start the app detached and open its manager window."""
    ensure_config()

    if foreground:
        from .gui import run_gui

        run_gui()
        return

    if manager_running():
        typer.echo("local-dev-proxy is already running; use the tray menu to open it.")
        return

    pid = _spawn_detached()
    typer.echo(f"Started local-dev-proxy (PID {pid}).")


if __name__ == "__main__":
    app()
