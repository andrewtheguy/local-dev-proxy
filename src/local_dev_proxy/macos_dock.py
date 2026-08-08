"""Toggle the macOS Dock icon by switching the app's activation policy.

A tray-first app should not keep a Dock icon while it lives in the menu bar.
macOS controls the Dock icon via the ``NSApplication`` activation policy:

* ``NSApplicationActivationPolicyRegular`` (0) — normal app, shows a Dock icon.
* ``NSApplicationActivationPolicyAccessory`` (1) — agent app, no Dock icon, but
  windows and the menu bar still work.

Qt does not expose this, so the Objective-C messages are sent directly through
the runtime with ctypes (no third-party dependency). Every entry point is a
no-op returning False off macOS, under a headless Qt platform, or when the
runtime cannot be reached.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
import os
import sys

logger = logging.getLogger(__name__)

_POLICY_REGULAR = 0
_POLICY_ACCESSORY = 1


def _qt_platform_is_cocoa() -> bool:
    """True when Qt is (or will be) driving a real cocoa GUI.

    Headless Qt platforms such as ``offscreen`` never pump AppKit events, yet
    ``setActivationPolicy:`` registers the process with the window server all
    the same. That leaves a blank "Python" Dock tile that ignores Force Quit,
    because no event loop ever answers the Dock. Skip the runtime entirely
    unless the platform is cocoa (Qt's macOS default when the variable is
    unset).
    """
    platform = os.environ.get("QT_QPA_PLATFORM")
    if not platform:
        return True
    # The variable may carry fallbacks ("offscreen;cocoa") or plugin options
    # ("cocoa:option"); only the first plugin name decides.
    return platform.split(";", 1)[0].split(":", 1)[0] == "cocoa"


def set_dock_icon_visible(visible: bool) -> bool:
    """Show (Regular) or hide (Accessory) the Dock icon; return True if applied."""
    if sys.platform != "darwin":
        return False
    if not _qt_platform_is_cocoa():
        return False
    try:
        return _apply_activation_policy(
            _POLICY_REGULAR if visible else _POLICY_ACCESSORY
        )
    except OSError:
        logger.warning("Could not adjust the macOS Dock icon", exc_info=True)
        return False


def _apply_activation_policy(policy: int) -> bool:
    objc_path = ctypes.util.find_library("objc")
    if objc_path is None:
        return False
    objc = ctypes.CDLL(objc_path)

    objc.objc_getClass.restype = ctypes.c_void_p
    objc.objc_getClass.argtypes = [ctypes.c_char_p]
    objc.sel_registerName.restype = ctypes.c_void_p
    objc.sel_registerName.argtypes = [ctypes.c_char_p]

    ns_application = objc.objc_getClass(b"NSApplication")
    if not ns_application:
        return False

    # app = [NSApplication sharedApplication] — the instance Qt already created.
    msg_send = objc.objc_msgSend
    msg_send.restype = ctypes.c_void_p
    msg_send.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    app = msg_send(ns_application, objc.sel_registerName(b"sharedApplication"))
    if not app:
        return False

    # return [app setActivationPolicy:policy]
    msg_send.restype = ctypes.c_bool
    msg_send.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long]
    return bool(
        msg_send(app, objc.sel_registerName(b"setActivationPolicy:"), policy)
    )
