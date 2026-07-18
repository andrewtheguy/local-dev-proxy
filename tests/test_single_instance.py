from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from local_dev_proxy import cli, config


def test_instance_lock_prevents_a_second_manager(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config, "LOCK_PATH", str(tmp_path / "instance.lock"))

    first = config.acquire_instance_lock()
    try:
        assert config.manager_running()
        with pytest.raises(config.AlreadyRunningError, match="already running"):
            config.acquire_instance_lock()
    finally:
        config.release_instance_lock(first)

    assert not config.manager_running()


def test_repeated_cli_launch_has_no_command_ipc_or_second_spawn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawn_called = False

    def unexpected_spawn() -> int:
        nonlocal spawn_called
        spawn_called = True
        return 12345

    monkeypatch.setattr(cli, "manager_running", lambda: True)
    monkeypatch.setattr(cli, "_spawn_detached", unexpected_spawn)

    result = CliRunner().invoke(cli.app, [])

    assert result.exit_code == 0
    assert "already running" in result.stdout
    assert "tray menu" in result.stdout
    assert not spawn_called


def test_raise_request_ipc_api_is_absent() -> None:
    assert not hasattr(config, "request_raise")
    assert not hasattr(config, "consume_raise_request")
    assert not hasattr(config, "RAISE_REQUEST_PATH")
    assert not hasattr(config, "PID_PATH")
