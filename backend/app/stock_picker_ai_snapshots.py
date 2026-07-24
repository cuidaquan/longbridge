"""Immutable AI/news input and output snapshot helpers for stock picker analysis."""

from __future__ import annotations

from datetime import date, datetime, timezone
import hashlib
import json
import math
import re
from typing import Any, Iterable, Mapping


AI_INPUT_SNAPSHOT_VERSION = "stock-picker-ai-input-v1"
AI_OUTPUT_SNAPSHOT_VERSION = "stock-picker-ai-output-v1"
NEWS_SNAPSHOT_VERSION = "stock-picker-news-v1"
DATA_DEFINITION_VERSION = "stock-picker-data-v1"


def utc_now_iso() -> str:
    """Return a stable, timezone-aware capture timestamp for display only."""
    return datetime.now(timezone.utc).isoformat()


def _jsonable(value: Any) -> Any:
    """Convert runtime values into deterministic JSON-compatible values."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {
            str(key): _jsonable(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted(_jsonable(item) for item in value)
    return str(value)


def canonical_json(value: Any) -> str:
    """Serialize a value with a deterministic compact JSON representation."""
    return json.dumps(
        _jsonable(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_text(canonical_json(value))


def klines_hash(klines: Any) -> str:
    """Hash the complete K-line snapshot used by an analysis."""
    return sha256_json(klines or [])


def snapshot_hash(snapshot: Mapping[str, Any]) -> str:
    """Hash an input snapshot without its non-semantic capture timestamp."""
    payload = dict(snapshot)
    payload.pop("captured_at", None)
    payload.pop("ai_input_hash", None)
    return sha256_json(payload)


def sanitize_error(error: Any, secrets: Iterable[str] = ()) -> str:
    """Keep persisted error context useful without retaining credentials/URLs."""
    text = str(error or "unknown error")
    for secret in secrets:
        if secret:
            text = text.replace(str(secret), "[REDACTED]")
    text = re.sub(
        r"(?i)(api[_-]?key|token|secret|password|authorization)"
        r"\s*[:=]\s*[^\s,;]+",
        r"\1=[REDACTED]",
        text,
    )
    text = re.sub(r"(?i)bearer\s+[^\s,;]+", "Bearer [REDACTED]", text)
    text = re.sub(r"https?://[^\s\"'<>]+", "[REDACTED_URL]", text)
    return text[:1000]


def parse_snapshot(raw: Any) -> tuple[Any, str | None]:
    """Decode a persisted JSON snapshot, returning a non-throwing error."""
    if raw is None:
        return None, None
    try:
        return json.loads(raw), None
    except (TypeError, json.JSONDecodeError) as exc:
        return None, type(exc).__name__
