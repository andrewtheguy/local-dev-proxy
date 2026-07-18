from __future__ import annotations

import os
import subprocess

import pytest

from local_dev_proxy import shell_env


def test_merge_path_prefers_resolved_and_dedupes() -> None:
    resolved = "/opt/homebrew/bin:/usr/bin"
    current = "/usr/bin:/bin"

    merged = shell_env.merge_path(current, resolved)

    assert merged == os.pathsep.join(["/opt/homebrew/bin", "/usr/bin", "/bin"])


def test_merge_path_drops_empty_entries() -> None:
    merged = shell_env.merge_path("/bin::", ":/opt/bin:")

    assert merged == os.pathsep.join(["/opt/bin", "/bin"])


def test_query_login_shell_path_extracts_between_markers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SHELL", "/bin/zsh")

    def fake_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        marker = shell_env._MARKER
        # Simulate rc-file banner noise around the marked PATH payload.
        stdout = f"welcome!\n{marker}/home/u/.local/bin:/usr/bin{marker}"
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(shell_env.subprocess, "run", fake_run)

    assert shell_env.query_login_shell_path() == "/home/u/.local/bin:/usr/bin"


def test_query_login_shell_path_without_shell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SHELL", raising=False)

    assert shell_env.query_login_shell_path() is None


def test_query_login_shell_path_on_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SHELL", "/bin/zsh")

    def fake_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd, shell_env._SHELL_TIMEOUT_SECONDS)

    monkeypatch.setattr(shell_env.subprocess, "run", fake_run)

    assert shell_env.query_login_shell_path() is None


def test_restore_login_shell_path_updates_environ(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shell_env.sys, "platform", "darwin")
    monkeypatch.setattr(
        shell_env, "query_login_shell_path", lambda: "/opt/homebrew/bin:/usr/bin"
    )
    environ: dict[str, str] = {"PATH": "/usr/bin:/bin"}

    changed = shell_env.restore_login_shell_path(environ)

    assert changed is True
    assert environ["PATH"] == os.pathsep.join(
        ["/opt/homebrew/bin", "/usr/bin", "/bin"]
    )


def test_restore_login_shell_path_noop_when_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shell_env.sys, "platform", "darwin")
    monkeypatch.setattr(shell_env, "query_login_shell_path", lambda: "/usr/bin:/bin")
    environ: dict[str, str] = {"PATH": "/usr/bin:/bin"}

    assert shell_env.restore_login_shell_path(environ) is False
    assert environ["PATH"] == "/usr/bin:/bin"


def test_restore_login_shell_path_skipped_on_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shell_env.sys, "platform", "win32")
    environ: dict[str, str] = {"PATH": "C:\\Windows"}

    assert shell_env.restore_login_shell_path(environ) is False
    assert environ["PATH"] == "C:\\Windows"
