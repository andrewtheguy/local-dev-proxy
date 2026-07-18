"""Recover the user's login-shell ``PATH`` for GUI launches.

A GUI app launched from Finder/Dock on macOS (and some Linux desktop
environments) inherits a minimal ``PATH`` (``/usr/bin:/bin:/usr/sbin:/sbin``)
rather than the login shell's ``PATH``. User-installed tools in
``~/.local/bin``, Homebrew, pyenv, etc. are therefore invisible, and every
managed service command resolves to "command not found".

Asking the login shell to print its ``PATH`` recovers the real value.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from collections.abc import MutableMapping

logger = logging.getLogger(__name__)

# Bracketing markers isolate PATH from any banner text an interactive rc file
# may print to stdout before our command runs.
_MARKER = "__LOCAL_DEV_PROXY_PATH__"
_SHELL_TIMEOUT_SECONDS = 5.0


def query_login_shell_path() -> str | None:
    """Return ``PATH`` as seen by the user's interactive login shell, or None.

    Returns None when there is no ``SHELL``, the shell cannot be run, it times
    out, or the output cannot be parsed. An interactive login shell is used so
    that both login files (``.zprofile``/``.profile``) and interactive files
    (``.zshrc``/``.bashrc``), where users commonly extend ``PATH``, are sourced.
    """
    shell = os.environ.get("SHELL")
    if not shell:
        return None

    command = f'printf %s "{_MARKER}${{PATH}}{_MARKER}"'
    try:
        completed = subprocess.run(
            [shell, "-ilc", command],
            capture_output=True,
            text=True,
            timeout=_SHELL_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        logger.warning("Could not query %s for the login PATH", shell, exc_info=True)
        return None

    output = completed.stdout
    start = output.find(_MARKER)
    if start == -1:
        return None
    start += len(_MARKER)
    end = output.find(_MARKER, start)
    if end == -1:
        return None

    path = output[start:end]
    return path or None


def merge_path(current: str, resolved: str) -> str:
    """Merge two ``PATH`` strings, resolved entries first, order-preserving.

    Entries from the login shell take precedence; any directory present only in
    the current ``PATH`` is appended so nothing already visible is lost.
    Duplicates are removed while preserving first-seen order.
    """
    seen: set[str] = set()
    merged: list[str] = []
    for entry in resolved.split(os.pathsep) + current.split(os.pathsep):
        if entry and entry not in seen:
            seen.add(entry)
            merged.append(entry)
    return os.pathsep.join(merged)


def restore_login_shell_path(environ: MutableMapping[str, str] | None = None) -> bool:
    """Repair ``PATH`` in ``environ`` from the login shell. Returns True if changed.

    A no-op on Windows, which has no equivalent GUI-launch ``PATH`` gap. Must be
    called before any subprocess environment is captured from ``os.environ``.
    """
    if sys.platform == "win32":
        return False

    target = os.environ if environ is None else environ
    resolved = query_login_shell_path()
    if not resolved:
        return False

    current = target.get("PATH", "")
    merged = merge_path(current, resolved)
    if merged == current:
        return False

    target["PATH"] = merged
    logger.info("Restored login-shell PATH for GUI launch")
    return True
