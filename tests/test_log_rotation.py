from __future__ import annotations

import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import cast

import pytest

from local_dev_proxy.gui import _configure_manager_logging
from local_dev_proxy.log_rotation import (
    LOG_BACKUP_COUNT,
    LOG_MAX_BYTES,
    RotatingLogWriter,
    pump_log_stream,
)
from local_dev_proxy.process_manager import ServiceManager
from local_dev_proxy.routes import RoutesManifest, ServiceDef


def _read_rotated(path: Path, backup_count: int) -> bytes:
    chunks: list[bytes] = []
    for index in range(backup_count, 0, -1):
        try:
            chunks.append(path.with_name(f"{path.name}.{index}").read_bytes())
        except FileNotFoundError:
            pass
    try:
        chunks.append(path.read_bytes())
    except FileNotFoundError:
        pass
    return b"".join(chunks)


def test_rotating_writer_preserves_newest_bytes_within_cap(tmp_path: Path) -> None:
    log_path = tmp_path / "service.log"
    payload = bytes(range(80))

    with RotatingLogWriter(log_path, max_bytes=16, backup_count=2) as writer:
        writer.write(payload[:13])
        writer.write(payload[13:57])
        writer.write(payload[57:])

    assert _read_rotated(log_path, 2) == payload[-48:]
    assert all(
        path.stat().st_size <= 16
        for path in (log_path, tmp_path / "service.log.1", tmp_path / "service.log.2")
    )


def test_service_stdout_and_stderr_are_drained_through_rotation(
    tmp_path: Path,
) -> None:
    script = (
        "import os, time; "
        "os.write(1, b'OUT-MARKER\\n'); "
        "os.write(2, b'ERR-MARKER\\n'); "
        "time.sleep(30)"
    )
    manifest = RoutesManifest(
        http_port=2800,
        bind=("127.0.0.1",),
        services={
            "app": ServiceDef(name="app", command=[sys.executable, "-c", script])
        },
    )
    manager = ServiceManager(
        manifest,
        tmp_path / "logs",
        tmp_path,
        log_max_bytes=128,
        log_backup_count=4,
    )
    log_path = manager.get_log_path("app")

    try:
        manager.start_service("app")
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            body = _read_rotated(log_path, 4)
            if b"OUT-MARKER\n" in body and b"ERR-MARKER\n" in body:
                break
            time.sleep(0.01)
        else:
            raise AssertionError("service output did not reach the rotating log")

        body = _read_rotated(log_path, 4)
        assert b"OUT-MARKER\n" in body
        assert b"ERR-MARKER\n" in body
        assert all(path.stat().st_size <= 128 for path in log_path.parent.iterdir())
    finally:
        manager.stop_service("app")


def test_log_pump_can_be_cancelled_while_pipe_is_idle(tmp_path: Path) -> None:
    read_fd, write_fd = os.pipe()
    source = os.fdopen(read_fd, "rb", buffering=0)
    writer = RotatingLogWriter(tmp_path / "service.log")
    stop_event = threading.Event()
    thread = threading.Thread(
        target=pump_log_stream,
        args=(source, writer, stop_event),
    )
    thread.start()
    try:
        stop_event.set()
        thread.join(timeout=1)
        assert not thread.is_alive()
    finally:
        os.close(write_fd)
        source.close()
        writer.close()
        thread.join(timeout=1)


def test_restart_rejected_while_previous_log_capture_is_alive(
    tmp_path: Path,
) -> None:
    class StuckLogThread:
        def __init__(self) -> None:
            self.join_timeouts: list[float | None] = []

        def join(self, timeout: float | None = None) -> None:
            self.join_timeouts.append(timeout)

        @staticmethod
        def is_alive() -> bool:
            return True

    manifest = RoutesManifest(
        http_port=2800,
        bind=("127.0.0.1",),
        services={
            "app": ServiceDef(name="app", command=[sys.executable, "-c", "pass"])
        },
    )
    manager = ServiceManager(manifest, tmp_path / "logs", tmp_path)
    info = manager._services["app"]
    stuck_thread = StuckLogThread()
    info.log_thread = cast(threading.Thread, stuck_thread)
    info.log_stop_event = threading.Event()

    with pytest.raises(RuntimeError, match="previous log capture is still active"):
        manager.restart_service("app")

    assert info.log_thread is stuck_thread
    assert info.log_stop_event.is_set()
    assert info.restart_count == 0
    assert stuck_thread.join_timeouts == [5, 1]


def test_manager_logger_uses_standard_rotation(tmp_path: Path) -> None:
    log_path = tmp_path / "manager.log"
    package_logger = logging.getLogger("local_dev_proxy")
    previous_level = package_logger.level
    handler = _configure_manager_logging(log_path)
    try:
        logging.getLogger("local_dev_proxy.rotation_test").info("manager marker")
        handler.flush()
        assert handler.maxBytes == LOG_MAX_BYTES
        assert handler.backupCount == LOG_BACKUP_COUNT
        assert "manager marker" in log_path.read_text()
    finally:
        package_logger.removeHandler(handler)
        package_logger.setLevel(previous_level)
        handler.close()
