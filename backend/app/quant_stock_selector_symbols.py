"""Shared symbol normalization for quantitative selection."""

from __future__ import annotations

from typing import Any


class QuantSymbolError(ValueError):
    pass


def normalize_symbol(value: Any) -> str:
    normalized = str(value or "").strip().upper()
    if not normalized:
        raise QuantSymbolError("symbol is required")
    return normalized
