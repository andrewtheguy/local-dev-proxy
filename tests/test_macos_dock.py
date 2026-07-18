from __future__ import annotations

import pytest

from local_dev_proxy import macos_dock


def test_set_dock_icon_visible_is_noop_off_macos(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(macos_dock.sys, "platform", "linux")

    assert macos_dock.set_dock_icon_visible(True) is False
    assert macos_dock.set_dock_icon_visible(False) is False


def test_set_dock_icon_visible_reports_failure_without_objc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(macos_dock.sys, "platform", "darwin")
    monkeypatch.setattr(macos_dock.ctypes.util, "find_library", lambda _name: None)

    assert macos_dock.set_dock_icon_visible(True) is False


def test_set_dock_icon_visible_swallows_runtime_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(macos_dock.sys, "platform", "darwin")

    def boom(_name: str) -> str:
        raise OSError("objc runtime unavailable")

    monkeypatch.setattr(macos_dock.ctypes.util, "find_library", boom)

    assert macos_dock.set_dock_icon_visible(True) is False
