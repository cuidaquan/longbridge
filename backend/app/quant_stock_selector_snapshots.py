"""Immutable content-addressed market-bar snapshots for quant selection."""

from __future__ import annotations

from datetime import date, datetime, time, timezone
import math
from typing import Any, Callable, Mapping, Sequence

from .db import get_connection
from .quant_stock_selector_hashing import (
    canonical_sha256,
    immutable_snapshot_reference,
)


MARKET_BAR_SNAPSHOT_VERSION = "quant-selector-market-bars-v1"
SUPPORTED_ADJUST_TYPES = frozenset({"forward_adjust", "no_adjust"})


class MarketBarSnapshotError(ValueError):
    pass


class MarketBarSnapshotIntegrityError(RuntimeError):
    pass


def _utc_datetime(value: Any, *, field: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, time.min, tzinfo=timezone.utc)
    else:
        text = str(value or "").strip()
        if not text:
            raise MarketBarSnapshotError(f"{field} is required")
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            try:
                parsed = datetime.combine(
                    date.fromisoformat(text),
                    time.min,
                    tzinfo=timezone.utc,
                )
            except ValueError as exc:
                raise MarketBarSnapshotError(
                    f"{field} must be an ISO date or timestamp"
                ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _number(
    value: Any,
    *,
    field: str,
    positive: bool = False,
    nonnegative: bool = False,
    optional: bool = False,
) -> float | None:
    if value is None and optional:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise MarketBarSnapshotError(f"{field} must be numeric") from exc
    if not math.isfinite(number):
        raise MarketBarSnapshotError(f"{field} must be finite")
    if positive and number <= 0:
        raise MarketBarSnapshotError(f"{field} must be positive")
    if nonnegative and number < 0:
        raise MarketBarSnapshotError(f"{field} must be nonnegative")
    return number


def _timestamp_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")


def _normalize_rows(
    rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    if not rows:
        raise MarketBarSnapshotError("at least one market bar is required")
    normalized = []
    seen = set()
    for raw in rows:
        timestamp = _utc_datetime(raw.get("ts"), field="ts")
        if timestamp in seen:
            raise MarketBarSnapshotError(
                f"duplicate market bar timestamp: {_timestamp_text(timestamp)}"
            )
        seen.add(timestamp)
        row = {
            "ts": _timestamp_text(timestamp),
            "open": _number(raw.get("open"), field="open", positive=True),
            "high": _number(raw.get("high"), field="high", positive=True),
            "low": _number(raw.get("low"), field="low", positive=True),
            "close": _number(raw.get("close"), field="close", positive=True),
            "volume": _number(
                raw.get("volume"),
                field="volume",
                nonnegative=True,
                optional=True,
            ),
            "turnover": _number(
                raw.get("turnover"),
                field="turnover",
                nonnegative=True,
                optional=True,
            ),
        }
        if row["high"] < max(row["open"], row["close"], row["low"]):
            raise MarketBarSnapshotError("high is inconsistent with OHLC")
        if row["low"] > min(row["open"], row["close"], row["high"]):
            raise MarketBarSnapshotError("low is inconsistent with OHLC")
        normalized.append(row)
    normalized.sort(key=lambda item: item["ts"])
    return normalized


class MarketBarSnapshotStore:
    def __init__(
        self,
        *,
        connection_factory: Callable[..., Any] = get_connection,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.connection_factory = connection_factory
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    @staticmethod
    def _hash_payload(
        *,
        symbol: str,
        period: str,
        adjust_type: str,
        source: str,
        data_as_of: datetime,
        rows: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        return {
            "adjust_type": adjust_type,
            "data_as_of": data_as_of,
            "period": period,
            "rows": list(rows),
            "snapshot_version": MARKET_BAR_SNAPSHOT_VERSION,
            "source": source,
            "symbol": symbol,
        }

    def capture(
        self,
        *,
        symbol: str,
        period: str,
        adjust_type: str,
        source: str,
        data_as_of: Any,
        rows: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        normalized_symbol = str(symbol).strip().upper()
        normalized_period = str(period).strip().lower()
        normalized_adjust = str(adjust_type).strip().lower()
        normalized_source = str(source).strip()
        if not normalized_symbol or not normalized_period or not normalized_source:
            raise MarketBarSnapshotError("symbol, period and source are required")
        if normalized_adjust not in SUPPORTED_ADJUST_TYPES:
            raise MarketBarSnapshotError(
                f"unsupported adjust_type: {normalized_adjust}"
            )
        normalized_data_as_of = _utc_datetime(
            data_as_of,
            field="data_as_of",
        )
        normalized_rows = _normalize_rows(rows)
        last_timestamp = _utc_datetime(
            normalized_rows[-1]["ts"],
            field="rows[-1].ts",
        )
        if last_timestamp.date() != normalized_data_as_of.date():
            raise MarketBarSnapshotError(
                "last bar date must equal data_as_of date"
            )
        payload = self._hash_payload(
            symbol=normalized_symbol,
            period=normalized_period,
            adjust_type=normalized_adjust,
            source=normalized_source,
            data_as_of=normalized_data_as_of,
            rows=normalized_rows,
        )
        payload_hash = canonical_sha256(payload)
        snapshot_id = f"mbs_{payload_hash}"
        captured_at = _utc_datetime(self.clock(), field="captured_at")

        with self.connection_factory() as connection:
            existing = connection.execute(
                "SELECT snapshot_id FROM market_bar_snapshots "
                "WHERE snapshot_id = ?",
                [snapshot_id],
            ).fetchone()
            if existing is None:
                connection.execute("BEGIN TRANSACTION")
                try:
                    connection.execute(
                        """
                        INSERT INTO market_bar_snapshots (
                            snapshot_id, symbol, period, adjust_type, source,
                            captured_at, data_as_of, snapshot_version,
                            payload_hash, row_count
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        [
                            snapshot_id,
                            normalized_symbol,
                            normalized_period,
                            normalized_adjust,
                            normalized_source,
                            captured_at.replace(tzinfo=None),
                            normalized_data_as_of.replace(tzinfo=None),
                            MARKET_BAR_SNAPSHOT_VERSION,
                            payload_hash,
                            len(normalized_rows),
                        ],
                    )
                    connection.executemany(
                        """
                        INSERT INTO market_bar_snapshot_rows (
                            snapshot_id, ts, open, high, low, close,
                            volume, turnover
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        [
                            [
                                snapshot_id,
                                _utc_datetime(row["ts"], field="ts").replace(
                                    tzinfo=None
                                ),
                                row["open"],
                                row["high"],
                                row["low"],
                                row["close"],
                                row["volume"],
                                row["turnover"],
                            ]
                            for row in normalized_rows
                        ],
                    )
                    connection.execute("COMMIT")
                except Exception:
                    connection.execute("ROLLBACK")
                    raise

        detail = self.get(snapshot_id)
        detail["persisted"] = existing is None
        return detail

    def get(self, snapshot_id: str) -> dict[str, Any]:
        with self.connection_factory() as connection:
            header = connection.execute(
                """
                SELECT symbol, period, adjust_type, source, captured_at,
                       data_as_of, snapshot_version, payload_hash, row_count
                FROM market_bar_snapshots
                WHERE snapshot_id = ?
                """,
                [snapshot_id],
            ).fetchone()
            if header is None:
                raise MarketBarSnapshotError(
                    f"market bar snapshot not found: {snapshot_id}"
                )
            stored_rows = connection.execute(
                """
                SELECT ts, open, high, low, close, volume, turnover
                FROM market_bar_snapshot_rows
                WHERE snapshot_id = ?
                ORDER BY ts ASC
                """,
                [snapshot_id],
            ).fetchall()

        rows = [
            {
                "ts": _timestamp_text(row[0]),
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "volume": None if row[5] is None else float(row[5]),
                "turnover": None if row[6] is None else float(row[6]),
            }
            for row in stored_rows
        ]
        data_as_of = header[5]
        if data_as_of.tzinfo is None or data_as_of.utcoffset() is None:
            data_as_of = data_as_of.replace(tzinfo=timezone.utc)
        payload = self._hash_payload(
            symbol=header[0],
            period=header[1],
            adjust_type=header[2],
            source=header[3],
            data_as_of=data_as_of,
            rows=rows,
        )
        calculated_hash = canonical_sha256(payload)
        if (
            header[6] != MARKET_BAR_SNAPSHOT_VERSION
            or len(rows) != header[8]
            or calculated_hash != header[7]
            or snapshot_id != f"mbs_{calculated_hash}"
        ):
            raise MarketBarSnapshotIntegrityError(
                f"market bar snapshot integrity check failed: {snapshot_id}"
            )
        return {
            "snapshot_id": snapshot_id,
            "symbol": header[0],
            "period": header[1],
            "adjust_type": header[2],
            "source": header[3],
            "captured_at": _timestamp_text(header[4]),
            "data_as_of": _timestamp_text(data_as_of),
            "snapshot_version": header[6],
            "payload_hash": header[7],
            "row_count": header[8],
            "rows": rows,
            "integrity_valid": True,
            "reference": immutable_snapshot_reference(
                source=header[3],
                schema_version=header[6],
                payload_hash=header[7],
            ),
        }
