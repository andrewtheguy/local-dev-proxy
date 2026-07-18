"""Frozen-app entry point for the cx_Freeze Windows build.

cx_Freeze freezes a real script file rather than a console/gui-script entry
point, so this module mirrors ``local_dev_proxy.__main__`` for the packaged MSI.
"""

from local_dev_proxy.gui import run_gui

if __name__ == "__main__":
    raise SystemExit(run_gui())
