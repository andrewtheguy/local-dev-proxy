from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

from local_dev_proxy.gui import _configure_manager_logging
from local_dev_proxy.log_rotation import (
    LOG_BACKUP_COUNT,
    LOG_MAX_BYTES,
    RotatingLogWriter,
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

    manager.start_service("app")
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        body = _read_rotated(log_path, 4)
        if b"OUT-MARKER\n" in body and b"ERR-MARKER\n" in body:
            break
        time.sleep(0.01)
    else:
        raise AssertionError("service output did not reach the rotating log")
    manager.stop_service("app")

    body = _read_rotated(log_path, 4)
    assert b"OUT-MARKER\n" in body
    assert b"ERR-MARKER\n" in body
    assert all(path.stat().st_size <= 128 for path in log_path.parent.iterdir())


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
