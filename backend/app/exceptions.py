from __future__ import annotations


class LongbridgeDependencyMissing(RuntimeError):
    """Raised when the Longbridge Python SDK is not installed."""


class LongbridgeAPIError(RuntimeError):
    """Raised when calling Longbridge API fails."""
