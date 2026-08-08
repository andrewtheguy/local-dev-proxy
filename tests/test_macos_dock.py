from __future__ import annotations

import pytest

from local_dev_proxy import macos_dock


def test_set_dock_icon_visible_is_noop_off_macos(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(macos_dock.sys, "platform", "linux")

    assert macos_dock.set_dock_icon_visible(True) is False
    assert macos_dock.set_dock_icon_visible(False) is False


def _forbid_objc(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(_name: str) -> str:
        raise AssertionError("the Objective-C runtime must not be touched")

    monkeypatch.setattr(macos_dock.ctypes.util, "find_library", fail)


class _NoLiveQt:
    """QGuiApplication stand-in for a process where Qt has not started yet."""

    @staticmethod
    def instance() -> None:
        return None


def _without_live_qt(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the guard to its pre-startup path, even under pytest-qt's session app."""
    monkeypatch.setattr(macos_dock, "QGuiApplication", _NoLiveQt)


@pytest.mark.parametrize("platform", ["offscreen", "minimal", "offscreen;cocoa"])
def test_set_dock_icon_visible_is_noop_under_headless_qt(
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
) -> None:
    # Registering a headless process with the window server leaves a ghost
    # "Python" Dock tile that not even Force Quit can remove.
    monkeypatch.setattr(macos_dock.sys, "platform", "darwin")
    monkeypatch.setenv("QT_QPA_PLATFORM", platform)
    _without_live_qt(monkeypatch)
    _forbid_objc(monkeypatch)

    assert macos_dock.set_dock_icon_visible(True) is False
    assert macos_dock.set_dock_icon_visible(False) is False


@pytest.mark.parametrize("platform", [None, "cocoa", "cocoa:option"])
def test_qt_platform_is_cocoa_for_gui_launches(
    monkeypatch: pytest.MonkeyPatch,
    platform: str | None,
) -> None:
    if platform is None:
        monkeypatch.delenv("QT_QPA_PLATFORM", raising=False)
    else:
        monkeypatch.setenv("QT_QPA_PLATFORM", platform)
    _without_live_qt(monkeypatch)

    assert macos_dock._qt_platform_is_cocoa() is True


def test_dock_guard_follows_live_qt_platform_over_environment(
    monkeypatch: pytest.MonkeyPatch,
    qapp: object,
) -> None:
    # The environment may promise cocoa while Qt actually loaded another
    # plugin (a ``-platform`` override, or fallback selection); the running
    # application's plugin decides, so the runtime stays untouched.
    assert macos_dock.QGuiApplication.platformName() == "offscreen"
    monkeypatch.setattr(macos_dock.sys, "platform", "darwin")
    monkeypatch.setenv("QT_QPA_PLATFORM", "cocoa")
    _forbid_objc(monkeypatch)

    assert macos_dock._qt_platform_is_cocoa() is False
    assert macos_dock.set_dock_icon_visible(True) is False
    assert macos_dock.set_dock_icon_visible(False) is False


def test_set_dock_icon_visible_reports_failure_without_objc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(macos_dock.sys, "platform", "darwin")
    monkeypatch.setenv("QT_QPA_PLATFORM", "cocoa")
    _without_live_qt(monkeypatch)
    monkeypatch.setattr(macos_dock.ctypes.util, "find_library", lambda _name: None)

    assert macos_dock.set_dock_icon_visible(True) is False


def test_set_dock_icon_visible_swallows_runtime_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(macos_dock.sys, "platform", "darwin")
    monkeypatch.setenv("QT_QPA_PLATFORM", "cocoa")
    _without_live_qt(monkeypatch)

    def boom(_name: str) -> str:
        raise OSError("objc runtime unavailable")

    monkeypatch.setattr(macos_dock.ctypes.util, "find_library", boom)

    assert macos_dock.set_dock_icon_visible(True) is False


class _FakeObjcFunc:
    """Stand-in for a ctypes foreign function: accepts restype/argtypes."""

    def __init__(self, impl) -> None:
        self._impl = impl
        self.restype = None
        self.argtypes = None

    def __call__(self, *args: object) -> object:
        return self._impl(*args)


class _FakeObjc:
    """Minimal Objective-C runtime stub recording the policy that was sent."""

    def __init__(self, sent_policies: list[int]) -> None:
        self.objc_getClass = _FakeObjcFunc(lambda _name: 1)  # non-null class ptr
        self.sel_registerName = _FakeObjcFunc(lambda name: name)
        self.objc_msgSend = _FakeObjcFunc(self._msg_send)
        self._sent = sent_policies

    def _msg_send(self, *args: object) -> object:
        # [app setActivationPolicy:policy] is the only 3-arg send.
        if len(args) == 3:
            self._sent.append(int(args[2]))  # type: ignore[arg-type]
            return True
        # [NSApplication sharedApplication] -> a non-null app pointer.
        return 2


@pytest.mark.parametrize(
    ("visible", "expected_policy"),
    [(True, 0), (False, 1)],
)
def test_set_dock_icon_visible_sends_expected_policy(
    monkeypatch: pytest.MonkeyPatch,
    visible: bool,
    expected_policy: int,
) -> None:
    sent: list[int] = []
    monkeypatch.setattr(macos_dock.sys, "platform", "darwin")
    monkeypatch.setenv("QT_QPA_PLATFORM", "cocoa")
    _without_live_qt(monkeypatch)
    monkeypatch.setattr(macos_dock.ctypes.util, "find_library", lambda _name: "libobjc")
    monkeypatch.setattr(macos_dock.ctypes, "CDLL", lambda _path: _FakeObjc(sent))

    assert macos_dock.set_dock_icon_visible(visible) is True
    assert sent == [expected_policy]
