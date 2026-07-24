from __future__ import annotations

from datetime import datetime, timezone
import os
import threading
from uuid import uuid4


_lock = threading.Lock()
_runtime_pid = os.getpid()
_runtime_id = uuid4().hex
_runtime_started_at = datetime.now(timezone.utc)


def get_runtime_metadata() -> dict[str, str]:
    """Return a stable identity for the current backend process."""
    global _runtime_id, _runtime_pid, _runtime_started_at
    current_pid = os.getpid()
    with _lock:
        if current_pid != _runtime_pid:
            _runtime_pid = current_pid
            _runtime_id = uuid4().hex
            _runtime_started_at = datetime.now(timezone.utc)
        return {
            "runtime_id": _runtime_id,
            "runtime_started_at": _runtime_started_at.isoformat(),
        }
