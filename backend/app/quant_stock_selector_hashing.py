"""Canonical input hashing for the quantitative stock selector."""

from __future__ import annotations

from datetime import date, datetime, timezone
from hashlib import sha256
import math
from typing import Any, Mapping

import rfc8785


class CanonicalInputError(ValueError):
    """Raised when an input cannot be represented by the hashing contract."""


def _utc_timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise CanonicalInputError("datetime must be timezone-aware")
    normalized = value.astimezone(timezone.utc)
    return normalized.isoformat(timespec="milliseconds").replace(
        "+00:00",
        "Z",
    )


def normalize_canonical_input(value: Any) -> Any:
    """Convert supported runtime values into the selector's JCS domain."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CanonicalInputError("non-finite numbers are not allowed")
        return value
    if isinstance(value, datetime):
        return _utc_timestamp(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        normalized = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise CanonicalInputError("object keys must be strings")
            normalized[key] = normalize_canonical_input(item)
        return normalized
    if isinstance(value, (list, tuple)):
        return [normalize_canonical_input(item) for item in value]
    raise CanonicalInputError(
        f"unsupported canonical input type: {type(value).__name__}"
    )


def canonical_json_bytes(value: Any) -> bytes:
    """Serialize an input using RFC 8785 JSON Canonicalization Scheme."""
    try:
        return rfc8785.dumps(normalize_canonical_input(value))
    except (rfc8785.CanonicalizationError, UnicodeEncodeError) as exc:
        raise CanonicalInputError(str(exc)) from exc


def canonical_json(value: Any) -> str:
    return canonical_json_bytes(value).decode("utf-8")


def canonical_sha256(value: Any) -> str:
    return sha256(canonical_json_bytes(value)).hexdigest()


def immutable_snapshot_reference(
    *,
    source: str,
    schema_version: str,
    payload_hash: str,
) -> dict[str, str]:
    """Build a location-independent reference suitable for root manifests."""
    normalized_hash = str(payload_hash).strip().lower()
    if len(normalized_hash) != 64 or any(
        char not in "0123456789abcdef" for char in normalized_hash
    ):
        raise CanonicalInputError("payload_hash must be a SHA-256 hex digest")
    if not str(source).strip() or not str(schema_version).strip():
        raise CanonicalInputError("source and schema_version are required")
    return {
        "payload_hash": normalized_hash,
        "schema_version": str(schema_version).strip(),
        "source": str(source).strip(),
    }
