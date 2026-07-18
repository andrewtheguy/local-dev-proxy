"""Toggle the macOS Dock icon by switching the app's activation policy.

A tray-first app should not keep a Dock icon while it lives in the menu bar.
macOS controls the Dock icon via the ``NSApplication`` activation policy:

* ``NSApplicationActivationPolicyRegular`` (0) — normal app, shows a Dock icon.
* ``NSApplicationActivationPolicyAccessory`` (1) — agent app, no Dock icon, but
  windows and the menu bar still work.

Qt does not expose this, so the Objective-C messages are sent directly through
the runtime with ctypes (no third-party dependency). Every entry point is a
no-op returning False off macOS or when the runtime cannot be reached.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
import sys

logger = logging.getLogger(__name__)

_POLICY_REGULAR = 0
_POLICY_ACCESSORY = 1


def set_dock_icon_visible(visible: bool) -> bool:
    """Show (Regular) or hide (Accessory) the Dock icon; return True if applied."""
    if sys.platform != "darwin":
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
