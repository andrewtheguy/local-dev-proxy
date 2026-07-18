"""Qt-local activation channel for the single running GUI instance."""

from __future__ import annotations

import hashlib
import os
import time

from PySide6.QtCore import QObject, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket

from .config import ProjectPaths


def instance_server_name(paths: ProjectPaths) -> str:
    """Return a stable, profile-specific local IPC server name."""
    root = os.path.normcase(str(paths.root))
    digest = hashlib.sha256(os.fsencode(root)).hexdigest()[:20]
    return f"local-dev-proxy-{digest}"


def activate_running_instance(paths: ProjectPaths, timeout_ms: int = 1500) -> bool:
    """Ask the primary instance to show its manager window.

    The short retry window covers the primary process holding its instance lock
    just before its Qt local server begins listening.
    """
    server_name = instance_server_name(paths)
    deadline = time.monotonic() + max(timeout_ms, 0) / 1000

    while True:
        socket = QLocalSocket()
        socket.connectToServer(server_name)
        if socket.waitForConnected(100):
            socket.write(b"\0")
            socket.waitForBytesWritten(250)
            socket.disconnectFromServer()
            return True

        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


class ActivationServer(QObject):
    """Receive second-launch requests inside the primary Qt event loop."""

    activated = Signal()

    def __init__(self, paths: ProjectPaths, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.name = instance_server_name(paths)
        self._connections: set[QLocalSocket] = set()
        self._server = QLocalServer(self)
        self._server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)

        # The instance lock is held before this object is created, so an entry
        # with this name can only be stale data left by a terminated process.
        QLocalServer.removeServer(self.name)
        if not self._server.listen(self.name):
            raise RuntimeError(
                f"Could not create application activation channel: "
                f"{self._server.errorString()}"
            )
        self._server.newConnection.connect(self._accept_connections)

    def _accept_connections(self) -> None:
        while self._server.hasPendingConnections():
            socket = self._server.nextPendingConnection()
            self._connections.add(socket)
            socket.disconnected.connect(
                lambda current=socket: self._discard(current)
            )
            self.activated.emit()
            socket.disconnectFromServer()

    def _discard(self, socket: QLocalSocket) -> None:
        self._connections.discard(socket)
        socket.deleteLater()

    def close(self) -> None:
        """Stop accepting activation requests and remove the local endpoint."""
        for socket in tuple(self._connections):
            socket.abort()
        self._connections.clear()
        self._server.close()
        QLocalServer.removeServer(self.name)
