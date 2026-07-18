from __future__ import annotations

import subprocess
import sys
import time

import typer

from .config import ensure_config, manager_pid, request_raise

app = typer.Typer(
    help="Local dev proxy: reverse proxy + process manager with a Slint manager UI.",
    invoke_without_command=True,
    no_args_is_help=False,
)


def _spawn_detached() -> int | None:
    """Launch the app (proxy + services + window + system-tray icon) detached.

    Re-execs ``python -m local_dev_proxy --foreground`` in a new session with
    output redirected to the log dir, then waits briefly for the single-instance
    lock to be taken and returns the process PID.
    """
    paths = ensure_config()
    log_file = paths.logs_dir / "manager.log"
    out = open(log_file, "ab")
    try:
        subprocess.Popen(
            [sys.executable, "-m", "local_dev_proxy", "--foreground"],
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


@app.callback(invoke_without_command=True)
def main(
    foreground: bool = typer.Option(
        False,
        "--foreground",
        "-f",
        help="Run the app in the foreground (blocking). Used by the detached spawn.",
    ),
) -> None:
    """Start the app detached and open the manager window (re-run to reopen it)."""
    ensure_config()

    if foreground:
        from .gui import run_gui

        run_gui()
        return

    pid = manager_pid()
    if pid is not None:
        # Ask the running app to raise its window (cross-platform, via a marker
        # file the manager polls — replaces the Unix-only SIGUSR1 signal).
        request_raise()
        typer.echo(f"local-dev-proxy is already running (PID {pid}).")
        return

    pid = _spawn_detached()
    if pid is None:
        typer.echo("Error: app did not start in time (see manager.log).", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"Started local-dev-proxy (PID {pid}).")


if __name__ == "__main__":
    app()
