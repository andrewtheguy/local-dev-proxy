from __future__ import annotations

import os
import threading
from pathlib import Path
from types import TracebackType
from typing import BinaryIO, Self

LOG_MAX_BYTES = 10 * 1024 * 1024
LOG_BACKUP_COUNT = 5
_READ_SIZE = 64 * 1024


class RotatingLogWriter:
    """Write bytes to size-bounded, rename-rotated log files."""

    def __init__(
        self,
        path: Path,
        *,
        max_bytes: int = LOG_MAX_BYTES,
        backup_count: int = LOG_BACKUP_COUNT,
    ) -> None:
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        if backup_count < 1:
            raise ValueError("backup_count must be positive")
        self.path = path
        self.max_bytes = max_bytes
        self.backup_count = backup_count
        self._lock = threading.Lock()
        self._stream: BinaryIO | None = None
        self._size = 0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._open()

    def _open(self) -> None:
        self._stream = self.path.open("ab")
        self._size = self.path.stat().st_size

    def _backup_path(self, index: int) -> Path:
        return self.path.with_name(f"{self.path.name}.{index}")

    def _rotate(self) -> None:
        assert self._stream is not None
        self._stream.close()
        self._backup_path(self.backup_count).unlink(missing_ok=True)
        for index in range(self.backup_count - 1, 0, -1):
            source = self._backup_path(index)
            if source.exists():
                source.replace(self._backup_path(index + 1))
        if self.path.exists():
            self.path.replace(self._backup_path(1))
        self._open()

    def write(self, data: bytes) -> None:
        if not data:
            return
        with self._lock:
            if self._stream is None:
                raise ValueError("log writer is closed")
            remaining = memoryview(data)
            while remaining:
                if self._size >= self.max_bytes:
                    self._rotate()
                write_size = min(len(remaining), self.max_bytes - self._size)
                written = self._stream.write(remaining[:write_size])
                self._size += written
                remaining = remaining[written:]
            self._stream.flush()

    def close(self) -> None:
        with self._lock:
            if self._stream is not None:
                self._stream.close()
                self._stream = None

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc_value: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        self.close()


def pump_log_stream(
    source: BinaryIO,
    writer: RotatingLogWriter,
    stop_event: threading.Event | None = None,
) -> None:
    """Drain a pipe into a rotating writer until every available byte is read."""
    write_enabled = True
    if stop_event is not None:
        os.set_blocking(source.fileno(), False)
    try:
        while True:
            if stop_event is not None and stop_event.is_set():
                break
            try:
                chunk = os.read(source.fileno(), _READ_SIZE)
            except BlockingIOError:
                assert stop_event is not None
                stop_event.wait(timeout=0.05)
                continue
            except OSError:
                break
            if not chunk:
                break
            if write_enabled:
                try:
                    writer.write(chunk)
                except OSError:
                    # Keep draining so a logging failure cannot block the child.
                    write_enabled = False
    finally:
        source.close()
        writer.close()
