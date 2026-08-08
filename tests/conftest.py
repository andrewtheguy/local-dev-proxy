from __future__ import annotations

import os
import sys

# Default the Qt tests to the headless "offscreen" platform so the suite runs
# without a display and no windows flash on screen. Set before PySide6 is
# imported (conftest loads before test modules). Override with an explicit
# QT_QPA_PLATFORM=cocoa (or similar) to watch the GUI locally.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402

from local_dev_proxy import gui  # noqa: E402


def idle_command(prelude: str = "") -> list[str]:
    """A child process that idles, then exits as soon as it is orphaned.

    Managed services under test have to stay alive across a few manager calls,
    but a plain ``time.sleep`` leaves a stray process behind whenever the run
    dies without reaching its teardown (Ctrl-C, a hard timeout, a crashed
    worker). Polling ``getppid`` makes the child self-reaping: ``start_new_session``
    puts it outside the terminal's process group, so this is the only signal it
    reliably gets when the test run goes away.
    """
    return [
        sys.executable,
        "-c",
        f"import os, time\n{prelude}\n"
        "parent = os.getppid()\n"
        "while os.getppid() == parent:\n"
        "    time.sleep(0.05)\n",
    ]


@pytest.fixture(autouse=True)
def _no_login_shell_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stop ``run_gui`` from launching the developer's interactive login shell.

    ``run_gui`` calls ``restore_login_shell_path()``, which runs ``$SHELL -ilc``
    to recover the PATH a Finder launch is missing. Under test that sources the
    real ``.zshrc``/``.bashrc``, and anything those files background outlives the
    5 second timeout, because killing the shell does not kill its children.
    ``shell_env`` is covered directly by its own tests, with ``subprocess`` faked.
    """
    monkeypatch.setattr(gui, "restore_login_shell_path", lambda: False)
