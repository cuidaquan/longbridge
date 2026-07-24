from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
from typing import Any, Callable, Dict, Optional
from uuid import uuid4

from .db import get_connection


SNAPSHOT_VERSION = "stock-screener-scan-snapshot-v1"
FILTER_VERSION = "stock-screener-candidate-filter-v1"
RELATIVE_STRENGTH_VERSION = "directional-return-difference-v1"


def _utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


class StockScreenerSnapshotService:
    def __init__(
        self,
        connection_factory: Callable = get_connection,
        clock: Callable[[], datetime] = (
            lambda: datetime.now(timezone.utc)
        ),
    ) -> None:
        self.connection_factory = connection_factory
        self.clock = clock

    def capture(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        request = payload["request"]
        scan = payload["scan"]
        snapshot_id = str(uuid4())
        captured_at = self.clock()
        capture = {
            "snapshot_id": snapshot_id,
            "captured_at": _utc_iso(captured_at),
            "snapshot_version": SNAPSHOT_VERSION,
            "filter_version": FILTER_VERSION,
            "relative_strength_version": RELATIVE_STRENGTH_VERSION,
        }
        stored_payload = {"capture": capture, **payload}
        payload_hash = sha256(
            _canonical_json(stored_payload).encode("utf-8")
        ).hexdigest()
        stored_payload["capture"]["payload_hash"] = payload_hash
        serialized = _canonical_json(stored_payload)

        with self.connection_factory() as connection:
            connection.execute(
                """
                INSERT INTO stock_screener_scan_snapshots (
                    snapshot_id,
                    captured_at,
                    snapshot_version,
                    market,
                    target_direction,
                    strategy_id,
                    strategy_name,
                    strategy_source,
                    scan_mode,
                    first_page,
                    last_page,
                    pages_scanned,
                    candidates_scanned,
                    candidates_unique,
                    candidates_returned,
                    duplicates_removed,
                    payload_hash,
                    payload
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    snapshot_id,
                    captured_at,
                    SNAPSHOT_VERSION,
                    request["market"],
                    request["target_direction"],
                    request["strategy"]["id"],
                    request["strategy"].get("name"),
                    request["strategy"].get("source"),
                    scan["mode"],
                    scan["first_page"],
                    scan["last_page"],
                    scan["pages_scanned"],
                    scan["candidates_scanned"],
                    len(payload["universe"]),
                    scan["candidates_returned"],
                    scan["duplicates_removed"],
                    payload_hash,
                    serialized,
                ],
            )

        return {
            "status": "captured",
            **capture,
            "payload_hash": payload_hash,
            "candidates_unique": len(payload["universe"]),
        }

    def get_history(
        self,
        *,
        market: Optional[str] = None,
        target_direction: Optional[str] = None,
        strategy_id: Optional[int] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> Dict[str, Any]:
        if not 1 <= limit <= 100:
            raise ValueError("limit 必须在 1～100 之间")
        if offset < 0:
            raise ValueError("offset 不能小于 0")
        normalized_market = market.strip().upper() if market else None
        if normalized_market and normalized_market not in {"US", "HK", "CN", "SG"}:
            raise ValueError("market 必须是 US、HK、CN 或 SG")
        direction = (
            target_direction.strip().upper()
            if target_direction
            else None
        )
        if direction and direction not in {"LONG", "SHORT"}:
            raise ValueError("target_direction 必须是 LONG 或 SHORT")
        if strategy_id is not None and strategy_id <= 0:
            raise ValueError("strategy_id 必须是正整数")

        clauses = []
        parameters = []
        if normalized_market:
            clauses.append("market = ?")
            parameters.append(normalized_market)
        if direction:
            clauses.append("target_direction = ?")
            parameters.append(direction)
        if strategy_id is not None:
            clauses.append("strategy_id = ?")
            parameters.append(strategy_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        with self.connection_factory() as connection:
            total = connection.execute(
                f"SELECT COUNT(*) FROM stock_screener_scan_snapshots {where}",
                parameters,
            ).fetchone()[0]
            rows = connection.execute(
                f"""
                SELECT
                    snapshot_id,
                    captured_at,
                    snapshot_version,
                    market,
                    target_direction,
                    strategy_id,
                    strategy_name,
                    strategy_source,
                    scan_mode,
                    first_page,
                    last_page,
                    pages_scanned,
                    candidates_scanned,
                    candidates_unique,
                    candidates_returned,
                    duplicates_removed,
                    payload_hash
                FROM stock_screener_scan_snapshots
                {where}
                ORDER BY captured_at DESC, snapshot_id DESC
                LIMIT ? OFFSET ?
                """,
                [*parameters, limit, offset],
            ).fetchall()

        return {
            "snapshot_version": SNAPSHOT_VERSION,
            "items": [self._summary(row) for row in rows],
            "pagination": {
                "total": int(total),
                "limit": limit,
                "offset": offset,
            },
        }

    def get_snapshot(self, snapshot_id: str) -> Dict[str, Any]:
        normalized_id = snapshot_id.strip()
        if not normalized_id or len(normalized_id) > 64:
            raise ValueError("snapshot_id 无效")
        with self.connection_factory() as connection:
            row = connection.execute(
                """
                SELECT
                    snapshot_id,
                    captured_at,
                    snapshot_version,
                    market,
                    target_direction,
                    strategy_id,
                    strategy_name,
                    strategy_source,
                    scan_mode,
                    first_page,
                    last_page,
                    pages_scanned,
                    candidates_scanned,
                    candidates_unique,
                    candidates_returned,
                    duplicates_removed,
                    payload_hash,
                    payload
                FROM stock_screener_scan_snapshots
                WHERE snapshot_id = ?
                """,
                [normalized_id],
            ).fetchone()
        if row is None:
            raise KeyError(normalized_id)
        return {**self._summary(row[:17]), "payload": json.loads(row[17])}

    @staticmethod
    def _summary(row) -> Dict[str, Any]:
        return {
            "snapshot_id": row[0],
            "captured_at": _utc_iso(row[1]),
            "snapshot_version": row[2],
            "market": row[3],
            "target_direction": row[4],
            "strategy_id": int(row[5]),
            "strategy_name": row[6],
            "strategy_source": row[7],
            "scan_mode": row[8],
            "first_page": int(row[9]),
            "last_page": int(row[10]),
            "pages_scanned": int(row[11]),
            "candidates_scanned": int(row[12]),
            "candidates_unique": int(row[13]),
            "candidates_returned": int(row[14]),
            "duplicates_removed": int(row[15]),
            "payload_hash": row[16],
        }


_stock_screener_snapshot_service: Optional[StockScreenerSnapshotService] = None


def get_stock_screener_snapshot_service() -> StockScreenerSnapshotService:
    global _stock_screener_snapshot_service
    if _stock_screener_snapshot_service is None:
        _stock_screener_snapshot_service = StockScreenerSnapshotService()
    return _stock_screener_snapshot_service
