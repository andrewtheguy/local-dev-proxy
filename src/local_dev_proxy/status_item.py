"""A minimal macOS menu-bar icon backed by AppKit's NSStatusItem.

This replaces the old rumps dependency. Tkinter's ``mainloop()`` on macOS is
itself the Cocoa run loop (Tk's Aqua port owns the ``NSApplication``), so a bare
``NSStatusItem`` is serviced by it with no second run loop and no menu. Clicking
the icon fires ``on_click``; there is no dropdown.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)


def install_status_item(icon_path: Path | None, on_click: Callable[[], None]) -> object | None:
    """Create a menu-bar status item, or return ``None`` if AppKit is unavailable.

    The returned handle must be kept alive for the life of the process (both the
    item and its click target are retained by it). ``on_click`` runs on the
    AppKit main thread, so it must be cheap — set a flag and let Tk pick it up.
    """
    try:
        import AppKit  # noqa: PLC0415 — optional, macOS-only
        import objc  # noqa: PLC0415
    except ImportError as exc:  # not on macOS / pyobjc missing
        logger.warning("Menu-bar icon unavailable (%s); running without it.", exc)
        return None

    class _Target(AppKit.NSObject):
        def initWithCallback_(self, callback):  # noqa: N802 — ObjC selector
            self = objc.super(_Target, self).init()
            if self is None:
                return None
            self._callback = callback
            return self

        def onClick_(self, _sender):  # noqa: N802 — ObjC selector
            if self._callback is not None:
                self._callback()

    status_bar = AppKit.NSStatusBar.systemStatusBar()
    item = status_bar.statusItemWithLength_(AppKit.NSVariableStatusItemLength)
    target = _Target.alloc().initWithCallback_(on_click)

    button = item.button()
    if button is not None:
        image = None
        if icon_path is not None:
            image = AppKit.NSImage.alloc().initWithContentsOfFile_(str(icon_path))
        if image is not None:
            # Scale to the menu-bar height; the raw PNG is far larger than a
            # status item, so without this it renders enormous. Leave a couple
            # points of padding, matching typical menu-bar icons (~18pt on a
            # 22pt bar).
            height = max(status_bar.thickness() - 4, 1)
            size = image.size()
            if size.height:
                width = size.width * (height / size.height)
            else:
                width = height
            image.setSize_(AppKit.NSMakeSize(width, height))
            image.setTemplate_(True)
            button.setImage_(image)
        else:
            button.setTitle_("LDP")
        button.setTarget_(target)
        button.setAction_("onClick:")

    return (item, target)


def remove_status_item(handle: object | None) -> None:
    """Remove a status item created by :func:`install_status_item`."""
    if handle is None:
        return
    try:
        import AppKit  # noqa: PLC0415

        item, _target = handle  # type: ignore[misc]
        AppKit.NSStatusBar.systemStatusBar().removeStatusItem_(item)
    except (ImportError, TypeError, ValueError):
        pass
