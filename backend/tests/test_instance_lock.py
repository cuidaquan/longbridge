from __future__ import annotations

import asyncio
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from pydantic import ValidationError

from app.config import Settings
from app.instance_lock import InstanceLockError, SingleInstanceLock
from app import main


_CHILD_LOCK_SCRIPT = """
import os
from pathlib import Path
import sys

from app.instance_lock import InstanceLockError, SingleInstanceLock

lock = SingleInstanceLock(
    Path(sys.argv[1]),
    lock_dir=Path(sys.argv[2]),
)
try:
    lock.acquire()
except InstanceLockError as exc:
    print(str(exc))
    raise SystemExit(23)

if sys.argv[3] == "crash":
    os._exit(0)
lock.release()
"""


class SingleInstanceLockTest(unittest.TestCase):
    def _run_child(
        self,
        database_path: Path,
        lock_dir: Path,
        action: str = "release",
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                "-c",
                _CHILD_LOCK_SCRIPT,
                str(database_path),
                str(lock_dir),
                action,
            ],
            cwd=Path(__file__).resolve().parents[1],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )

    def test_second_process_is_rejected_until_owner_releases(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            database_path = root / "quant.db"
            lock = SingleInstanceLock(database_path, lock_dir=root)
            lock.acquire()
            try:
                blocked = self._run_child(database_path, root)
            finally:
                lock.release()

            accepted = self._run_child(database_path, root)

        self.assertEqual(blocked.returncode, 23, blocked.stderr)
        self.assertIn("already using DuckDB database", blocked.stdout)
        self.assertEqual(accepted.returncode, 0, accepted.stderr)

    def test_abnormal_process_exit_releases_os_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            database_path = root / "quant.db"

            crashed = self._run_child(database_path, root, "crash")
            lock = SingleInstanceLock(database_path, lock_dir=root)
            lock.acquire()
            lock.release()

        self.assertEqual(crashed.returncode, 0, crashed.stderr)

    def test_different_database_paths_can_be_locked_together(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            first = SingleInstanceLock(root / "first.db", lock_dir=root)
            second = SingleInstanceLock(root / "second.db", lock_dir=root)
            first.acquire()
            second.acquire()
            self.assertTrue(first.acquired)
            self.assertTrue(second.acquired)
            self.assertNotEqual(first.database_id, second.database_id)
            second.release()
            first.release()

    def test_acquire_and_release_are_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            lock = SingleInstanceLock(root / "quant.db", lock_dir=root)
            lock.acquire()
            lock.acquire()
            self.assertTrue(lock.acquired)
            lock.release()
            lock.release()
            self.assertFalse(lock.acquired)


class SingleInstanceLifecycleTest(unittest.TestCase):
    @staticmethod
    def _run_async(coroutine):
        result = []
        error = []

        def run() -> None:
            try:
                result.append(asyncio.run(coroutine))
            except BaseException as exc:
                error.append(exc)

        thread = threading.Thread(target=run)
        thread.start()
        thread.join(timeout=10)
        if thread.is_alive():
            raise TimeoutError("async lifecycle test did not finish")
        if error:
            raise error[0]
        return result[0] if result else None

    @staticmethod
    def _close_background_coroutine(coroutine):
        coroutine.close()
        return MagicMock()

    def test_only_single_instance_deployment_mode_is_valid(self) -> None:
        self.assertEqual(Settings().deployment_mode, "single_instance")
        with self.assertRaises(ValidationError):
            Settings(deployment_mode="multi_instance")

    def test_fastapi_lifecycle_and_health_report_instance_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            instance_lock = SingleInstanceLock(
                root / "quant.db",
                lock_dir=root,
            )
            monitor = MagicMock()
            monitor.start_monitoring = AsyncMock()
            monitor.stop_monitoring = AsyncMock()
            quote_stream = MagicMock()
            quote_stream.stop = AsyncMock()
            ai_engine = MagicMock()
            ai_engine.stop = AsyncMock()

            with (
                patch.object(main, "instance_lock", instance_lock),
                patch.object(main, "get_position_monitor", return_value=monitor),
                patch.object(main, "quote_stream_manager", quote_stream),
                patch.object(
                    main,
                    "get_ai_trading_engine",
                    return_value=ai_engine,
                ),
                patch.object(
                    main,
                    "_start_background_task",
                    side_effect=self._close_background_coroutine,
                ),
                patch.object(main, "close_connection") as close_connection,
            ):
                self._run_async(main.on_startup())
                response = main.health()
                self.assertTrue(instance_lock.acquired)
                self._run_async(main.on_shutdown())
                self.assertFalse(instance_lock.acquired)

        self.assertEqual(
            response,
            {
                "status": "ok",
                "deployment_mode": "single_instance",
                "instance_lock_acquired": True,
                "database_id": instance_lock.database_id,
            },
        )
        close_connection.assert_called_once_with()
        monitor.stop_monitoring.assert_awaited_once_with()
        quote_stream.stop.assert_awaited_once_with()
        ai_engine.stop.assert_awaited_once_with()

    def test_fastapi_startup_fails_before_workers_when_lock_is_owned(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            database_path = root / "quant.db"
            owner = SingleInstanceLock(database_path, lock_dir=root)
            contender = SingleInstanceLock(database_path, lock_dir=root)
            owner.acquire()
            try:
                with (
                    patch.object(main, "instance_lock", contender),
                    patch.object(
                        main,
                        "_start_background_task",
                    ) as start_background_task,
                ):
                    with self.assertRaises(InstanceLockError):
                        self._run_async(main.on_startup())
            finally:
                owner.release()

        start_background_task.assert_not_called()

    def test_startup_initialization_failure_releases_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            database_path = root / "quant.db"
            failed_lock = SingleInstanceLock(database_path, lock_dir=root)
            next_lock = SingleInstanceLock(database_path, lock_dir=root)
            quote_stream = MagicMock()
            quote_stream.attach_loop.side_effect = RuntimeError(
                "initialization failed"
            )

            with (
                patch.object(main, "instance_lock", failed_lock),
                patch.object(main, "quote_stream_manager", quote_stream),
                patch.object(main, "close_connection") as close_connection,
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "initialization failed",
                ):
                    self._run_async(main.on_startup())

            self.assertFalse(failed_lock.acquired)
            next_lock.acquire()
            next_lock.release()

        close_connection.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
