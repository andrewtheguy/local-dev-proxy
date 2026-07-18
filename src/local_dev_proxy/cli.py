from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

import typer

from .config import ensure_config, manager_pid

app = typer.Typer(
    help="Local dev proxy: reverse proxy + process manager with a Tkinter manager UI.",
    invoke_without_command=True,
    no_args_is_help=False,
)


def _spawn_detached() -> int | None:
    """Launch the manager (proxy + services + tray) as a detached process.

    Re-execs ``python -m local_dev_proxy start-manager --foreground`` in a new
    session with output redirected to the log dir, then waits briefly for the
    single-instance lock to be taken and returns the manager PID.
    """
    paths = ensure_config()
    log_file = paths.logs_dir / "manager.log"
    out = open(log_file, "ab")
    try:
        subprocess.Popen(
            [sys.executable, "-m", "local_dev_proxy", "start-manager", "--foreground"],
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=out,
            start_new_session=True,
            cwd=str(paths.config_dir),
        )
    finally:
        out.close()

    for _ in range(50):  # wait up to ~5s for the lock to be acquired
        pid = manager_pid()
        if pid is not None:
            return pid
        time.sleep(0.1)
    return None


def _spawn_gui() -> None:
    """Open the Tkinter manager window in its own process."""
    subprocess.Popen(
        [sys.executable, "-m", "local_dev_proxy", "gui"],
        start_new_session=True,
    )


@app.callback(invoke_without_command=True)
def main(ctx: typer.Context) -> None:
    """Start the manager (detached) and open the manager window."""
    if ctx.invoked_subcommand is not None:
        return

    ensure_config()
    pid = manager_pid()
    if pid is None:
        pid = _spawn_detached()
        if pid is None:
            typer.echo("Error: manager did not start in time (see manager.log).", err=True)
            raise typer.Exit(code=1)
        typer.echo(f"Started local-dev-proxy (PID {pid}).")
    else:
        typer.echo(f"local-dev-proxy is already running (PID {pid}).")

    _spawn_gui()


@app.command("start-manager")
def start_manager_command(
    foreground: bool = typer.Option(
        False,
        "--foreground",
        "-f",
        help="Run the manager in the foreground (blocking) instead of detaching.",
    ),
) -> None:
    """Start the manager. Detaches by default; --foreground runs the worker inline."""
    if foreground:
        from .tray import run_tray

        run_tray()
        return

    if manager_pid() is not None:
        typer.echo("local-dev-proxy is already running.")
        return
    pid = _spawn_detached()
    if pid is None:
        typer.echo("Error: manager did not start in time (see manager.log).", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"Started local-dev-proxy (PID {pid}).")


@app.command("stop-manager")
def stop_manager_command() -> None:
    """Stop the running manager process."""
    pid = manager_pid()
    if pid is None:
        typer.echo("local-dev-proxy is not running.")
        raise typer.Exit(code=1)

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        typer.echo(f"Manager process (PID {pid}) already exited.")
        raise typer.Exit(code=1)
    typer.echo(f"Sent SIGTERM to manager (PID {pid}).")


@app.command("restart-manager")
def restart_manager_command() -> None:
    """Stop the running manager (if any) and start it again, detached."""
    pid = manager_pid()
    if pid is not None:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        else:
            typer.echo(f"Sent SIGTERM to manager (PID {pid}). Waiting for shutdown...")
            for _ in range(50):  # wait up to 5 seconds
                if manager_pid() is None:
                    break
                time.sleep(0.1)
            else:
                typer.echo("Manager did not stop in time.", err=True)
                raise typer.Exit(code=1)

    new_pid = _spawn_detached()
    if new_pid is None:
        typer.echo("Error: manager did not start in time (see manager.log).", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"Started local-dev-proxy (PID {new_pid}).")


@app.command("gui")
def gui_command() -> None:
    """Open the Tkinter manager window (services, logs, routes, config)."""
    ensure_config()
    from .gui import run_gui

    run_gui()


if __name__ == "__main__":
    app()
