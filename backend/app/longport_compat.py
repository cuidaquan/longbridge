from __future__ import annotations

from typing import Any


def close_longport_context(context: Any) -> None:
    """Close a Longport context when the installed SDK exposes close()."""
    close = getattr(context, "close", None)
    if callable(close):
        close()
