from __future__ import annotations

import hashlib
import os
from pathlib import Path
import tempfile
import threading
from typing import BinaryIO, Optional


class InstanceLockError(RuntimeError):
    """Raised when another backend owns the database instance lock."""


class SingleInstanceLock:
    """Hold an OS-managed exclusive lock for one DuckDB database path."""

    def __init__(
        self,
        database_path: Path,
        *,
        lock_dir: Optional[Path] = None,
    ) -> None:
        self.database_path = database_path.expanduser().resolve()
        self.database_id = hashlib.sha256(
            os.fsencode(self.database_path)
        ).hexdigest()
        directory = (
            lock_dir.expanduser().resolve()
            if lock_dir is not None
            else Path(tempfile.gettempdir())
        )
        self.lock_path = directory / (
            f"longbridge-quant-{self.database_id}.lock"
        )
        self._handle: Optional[BinaryIO] = None
        self._mutex = threading.RLock()

    @property
    def acquired(self) -> bool:
        with self._mutex:
            return self._handle is not None

    def acquire(self) -> None:
        with self._mutex:
            if self._handle is not None:
                return

            self.lock_path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(
                self.lock_path,
                os.O_CREAT | os.O_RDWR,
                0o600,
            )
            handle = os.fdopen(descriptor, "r+b", buffering=0)
            try:
                self._lock(handle)
            except InstanceLockError:
                handle.close()
                raise
            except BaseException:
                handle.close()
                raise
            self._handle = handle

    def release(self) -> None:
        with self._mutex:
            handle = self._handle
            if handle is None:
                return
            self._handle = None
            try:
                self._unlock(handle)
            finally:
                handle.close()

    def __enter__(self) -> "SingleInstanceLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.release()

    def _lock(self, handle: BinaryIO) -> None:
        if os.name == "nt":
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise self._already_running_error() from exc
            return

        import fcntl

        try:
            fcntl.flock(
                handle.fileno(),
                fcntl.LOCK_EX | fcntl.LOCK_NB,
            )
        except BlockingIOError as exc:
            raise self._already_running_error() from exc

    @staticmethod
    def _unlock(handle: BinaryIO) -> None:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            return

        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _already_running_error(self) -> InstanceLockError:
        return InstanceLockError(
            "another Longbridge backend is already using DuckDB database "
            f"{self.database_path}; stop that backend before starting a "
            "second process"
        )
