from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from typing import Any, Callable, Dict, Iterable, List, Optional
from uuid import uuid4
from zoneinfo import ZoneInfo

from .db import get_connection
from .external_service_resilience import (
    ExternalServiceTimeoutError,
    run_external_call,
)
from .security_catalog import get_security_catalog_service
from .stock_candidate_data import is_market_trading_day
from .stock_picker_ai_snapshots import sanitize_error


SNAPSHOT_VERSION = "security-universe-snapshot-v1"
SNAPSHOT_SOURCE = "longbridge-official-security-list"
SOURCE_PATH = "/v1/quote/get_security_list"
SOURCE_CATEGORY = "Overnight"
SUPPORTED_MARKETS = ("US", "HK", "CN")
MARKET_TIMEZONES = {
    "US": "America/New_York",
    "HK": "Asia/Hong_Kong",
    "CN": "Asia/Shanghai",
}
MINIMUM_SECURITY_COUNTS = {
    "US": 1000,
    "HK": 1000,
    "CN": 1000,
}
AUTO_CAPTURE_MARKETS = ("US", "HK")
AUTO_CAPTURE_LOCAL_HOUR = 17
CAPTURE_CLAIM_LEASE_MINUTES = 30


class SecurityUniverseSnapshotService:
    """Persist immutable point-in-time copies of official security lists."""

    def __init__(
        self,
        catalog_loader: Optional[Callable[[str], List[dict]]] = None,
        connection_factory: Callable = get_connection,
        clock: Callable[[], datetime] = (
            lambda: datetime.now(timezone.utc)
        ),
        minimum_security_counts: Optional[Dict[str, int]] = None,
        trading_day_loader: Callable = is_market_trading_day,
    ) -> None:
        self.catalog_loader = catalog_loader or self._refresh_catalog
        self.connection_factory = connection_factory
        self.clock = clock
        self.minimum_security_counts = (
            dict(minimum_security_counts)
            if minimum_security_counts is not None
            else dict(MINIMUM_SECURITY_COUNTS)
        )
        self.trading_day_loader = trading_day_loader
        self._trading_day_cache: Dict[tuple[str, date], bool] = {}

    def capture_market(
        self,
        market: str,
        *,
        persist: bool = True,
    ) -> Dict[str, Any]:
        normalized_market = self._normalize_market(market)
        captured_at = self._as_utc(self.clock())
        observation_date = captured_at.astimezone(
            ZoneInfo(MARKET_TIMEZONES[normalized_market])
        ).date()
        if persist:
            existing = self._find_existing_summary(
                normalized_market,
                observation_date,
            )
            if existing is not None:
                return {
                    **existing,
                    "status": "already_captured",
                    "persisted": False,
                }
        raw_items = self.catalog_loader(normalized_market)
        items = self._normalize_items(raw_items, normalized_market)
        security_count = len(items)
        minimum_count = self.minimum_security_counts.get(
            normalized_market,
            1,
        )
        if security_count < minimum_count:
            raise ValueError(
                f"{normalized_market} 证券目录只有 {security_count} 条，"
                f"低于最低完整性门槛 {minimum_count}"
            )

        payload = self._payload(
            normalized_market,
            captured_at,
            observation_date,
            items,
        )
        payload_hash = self._payload_hash(payload)
        snapshot_id = uuid4().hex
        status = "captured"
        if persist:
            persisted_id, inserted = self._save_snapshot({
                "snapshot_id": snapshot_id,
                "captured_at": captured_at,
                "observation_date": observation_date,
                "market": normalized_market,
                "security_count": security_count,
                "payload_hash": payload_hash,
                "payload": payload,
            })
            if not inserted:
                existing = self._find_existing_summary(
                    normalized_market,
                    observation_date,
                )
                if existing is None:
                    raise RuntimeError("证券目录快照写入状态不一致")
                return {
                    **existing,
                    "status": "already_captured",
                    "persisted": False,
                }
            snapshot_id = persisted_id

        return {
            "snapshot_id": snapshot_id,
            "snapshot_version": SNAPSHOT_VERSION,
            "source": SNAPSHOT_SOURCE,
            "source_query": self._source_query(normalized_market),
            "captured_at": captured_at.isoformat(),
            "observation_date": observation_date.isoformat(),
            "market": normalized_market,
            "security_count": security_count,
            "payload_hash": payload_hash,
            "status": status,
            "persisted": persist and status == "captured",
        }

    def capture_due(self, *, persist: bool = True) -> Dict[str, Any]:
        now = self._as_utc(self.clock())
        captured = []
        skipped = []
        errors = []

        for market in AUTO_CAPTURE_MARKETS:
            local_now = now.astimezone(ZoneInfo(MARKET_TIMEZONES[market]))
            observation_date = local_now.date()
            scope = {
                "market": market,
                "observation_date": observation_date.isoformat(),
            }
            if local_now.hour < AUTO_CAPTURE_LOCAL_HOUR:
                skipped.append({**scope, "reason": "session_not_closed"})
                continue
            existing = self._find_existing_summary(
                market,
                observation_date,
            )
            if existing:
                if persist:
                    self._reconcile_existing_run(existing, now)
                skipped.append({**scope, "reason": "already_captured"})
                continue
            try:
                if not self._is_market_trading_day(
                    market,
                    observation_date,
                ):
                    skipped.append({**scope, "reason": "market_closed"})
                    continue
            except Exception as exc:
                error = sanitize_error(exc)
                errors.append({
                    **scope,
                    "reason": "trading_calendar_unavailable",
                    "error": error,
                })
                continue

            claim_id = None
            if persist:
                claim_id = self._claim_capture(
                    market,
                    observation_date,
                    now,
                )
                if claim_id is None:
                    skipped.append({
                        **scope,
                        "reason": "capture_claim_unavailable",
                    })
                    continue
            try:
                result = self.capture_market(market, persist=persist)
            except Exception as exc:
                error = sanitize_error(exc)
                if claim_id is not None:
                    self._finish_capture_claim(
                        market,
                        observation_date,
                        claim_id,
                        status="failed",
                        completed_at=self._as_utc(self.clock()),
                        error=error,
                    )
                errors.append({**scope, "error": error})
                continue

            if claim_id is not None:
                self._finish_capture_claim(
                    market,
                    observation_date,
                    claim_id,
                    status="completed",
                    completed_at=self._as_utc(self.clock()),
                    snapshot_id=result["snapshot_id"],
                    security_count=result["security_count"],
                )
            captured.append(result)

        return {
            "snapshot_version": SNAPSHOT_VERSION,
            "auto_capture_markets": list(AUTO_CAPTURE_MARKETS),
            "captured": captured,
            "skipped": skipped,
            "errors": errors,
            "security_count": sum(
                item["security_count"] for item in captured
            ),
        }

    def get_history(
        self,
        market: Optional[str] = None,
        limit: int = 20,
    ) -> Dict[str, Any]:
        normalized_market = (
            self._normalize_market(market) if market else None
        )
        if not 1 <= int(limit) <= 100:
            raise ValueError("limit 必须在 1～100 之间")
        where = "WHERE market = ?" if normalized_market else ""
        parameters: List[Any] = (
            [normalized_market, int(limit)]
            if normalized_market
            else [int(limit)]
        )
        with self.connection_factory() as connection:
            rows = connection.execute(
                f"""
                SELECT snapshot_id, captured_at, observation_date,
                       snapshot_version, market, source, source_query,
                       security_count, payload_hash
                FROM security_universe_snapshots
                {where}
                ORDER BY observation_date DESC, captured_at DESC
                LIMIT ?
                """,
                parameters,
            ).fetchall()
            run_rows = connection.execute(
                """
                SELECT market, observation_date, status, claim_id,
                       started_at, completed_at, snapshot_id,
                       security_count, error
                FROM security_universe_snapshot_runs
                ORDER BY started_at DESC, market
                LIMIT ?
                """,
                [int(limit)],
            ).fetchall()
        return {
            "snapshot_version": SNAPSHOT_VERSION,
            "source": SNAPSHOT_SOURCE,
            "items": [self._summary(row) for row in rows],
            "capture_runs": [self._run_summary(row) for row in run_rows],
        }

    def get_snapshot(self, snapshot_id: str) -> Optional[Dict[str, Any]]:
        normalized_id = snapshot_id.strip()
        if not normalized_id:
            raise ValueError("snapshot_id 不能为空")
        with self.connection_factory() as connection:
            row = connection.execute(
                """
                SELECT snapshot_id, captured_at, observation_date,
                       snapshot_version, market, source, source_query,
                       security_count, payload_hash, payload
                FROM security_universe_snapshots
                WHERE snapshot_id = ?
                """,
                [normalized_id],
            ).fetchone()
        if row is None:
            return None
        summary = self._summary(row[:9])
        try:
            payload = json.loads(row[9])
        except (json.JSONDecodeError, TypeError):
            return {
                **summary,
                "computed_payload_hash": None,
                "integrity_valid": False,
                "integrity_errors": ["invalid_payload_json"],
                "payload": None,
            }
        computed_hash = self._payload_hash(payload)
        if not isinstance(payload, dict):
            return {
                **summary,
                "computed_payload_hash": computed_hash,
                "integrity_valid": False,
                "integrity_errors": ["payload_not_object"],
                "payload": payload,
            }
        items = payload.get("items")
        integrity_errors = []
        if row[3] != SNAPSHOT_VERSION:
            integrity_errors.append("unsupported_snapshot_version")
        if row[5] != SNAPSHOT_SOURCE:
            integrity_errors.append("unexpected_source")
        if computed_hash != row[8]:
            integrity_errors.append("payload_hash_mismatch")
        if payload.get("snapshot_version") != row[3]:
            integrity_errors.append("payload_version_mismatch")
        if payload.get("source") != row[5]:
            integrity_errors.append("payload_source_mismatch")
        if payload.get("market") != row[4]:
            integrity_errors.append("payload_market_mismatch")
        if payload.get("observation_date") != row[2].isoformat():
            integrity_errors.append("payload_date_mismatch")
        row_captured_at = row[1].replace(tzinfo=timezone.utc).isoformat()
        if payload.get("captured_at") != row_captured_at:
            integrity_errors.append("payload_captured_at_mismatch")
        source_request = payload.get("source_request")
        if (
            not isinstance(source_request, dict)
            or source_request.get("method") != "GET"
            or source_request.get("path") != SOURCE_PATH
            or source_request.get("query") != row[6]
        ):
            integrity_errors.append("source_request_mismatch")
        if not isinstance(items, list) or len(items) != row[7]:
            integrity_errors.append("security_count_mismatch")
        elif not self._items_are_canonical(items, row[4]):
            integrity_errors.append("non_canonical_items")
        return {
            **summary,
            "computed_payload_hash": computed_hash,
            "integrity_valid": not integrity_errors,
            "integrity_errors": integrity_errors,
            "payload": payload,
        }

    def _is_market_trading_day(
        self,
        market: str,
        observation_date: date,
    ) -> bool:
        key = (market, observation_date)
        cached = self._trading_day_cache.get(key)
        if cached is not None:
            return cached
        result = bool(run_external_call(
            "quote",
            "security_universe_trading_calendar",
            self.trading_day_loader,
            market,
            observation_date,
            retry_if=lambda error: not isinstance(
                error,
                ExternalServiceTimeoutError,
            ),
        ))
        self._trading_day_cache[key] = result
        return result

    def _claim_capture(
        self,
        market: str,
        observation_date: date,
        started_at: datetime,
    ) -> Optional[str]:
        claim_id = uuid4().hex
        stale_before = started_at - timedelta(
            minutes=CAPTURE_CLAIM_LEASE_MINUTES,
        )
        with self.connection_factory() as connection:
            row = connection.execute(
                """
                INSERT INTO security_universe_snapshot_runs (
                    market, observation_date, status, claim_id, started_at
                ) VALUES (?, ?, 'running', ?, ?)
                ON CONFLICT (market, observation_date) DO UPDATE SET
                    status = 'running',
                    claim_id = excluded.claim_id,
                    started_at = excluded.started_at,
                    completed_at = NULL,
                    snapshot_id = NULL,
                    security_count = 0,
                    error = NULL
                WHERE (
                    security_universe_snapshot_runs.status IN (
                        'failed', 'completed'
                    )
                    OR (
                        security_universe_snapshot_runs.status = 'running'
                        AND security_universe_snapshot_runs.started_at < ?
                    )
                )
                RETURNING claim_id
                """,
                [
                    market,
                    observation_date,
                    claim_id,
                    started_at.replace(tzinfo=None),
                    stale_before.replace(tzinfo=None),
                ],
            ).fetchone()
        return claim_id if row and row[0] == claim_id else None

    def _finish_capture_claim(
        self,
        market: str,
        observation_date: date,
        claim_id: str,
        *,
        status: str,
        completed_at: datetime,
        snapshot_id: Optional[str] = None,
        security_count: int = 0,
        error: Optional[str] = None,
    ) -> None:
        if status not in {"completed", "failed"}:
            raise ValueError("status 必须是 completed 或 failed")
        with self.connection_factory() as connection:
            connection.execute(
                """
                UPDATE security_universe_snapshot_runs
                SET status = ?,
                    completed_at = ?,
                    snapshot_id = ?,
                    security_count = ?,
                    error = ?
                WHERE market = ?
                  AND observation_date = ?
                  AND claim_id = ?
                """,
                [
                    status,
                    completed_at.replace(tzinfo=None),
                    snapshot_id,
                    int(security_count),
                    error,
                    market,
                    observation_date,
                    claim_id,
                ],
            )

    def _reconcile_existing_run(
        self,
        snapshot: Dict[str, Any],
        completed_at: datetime,
    ) -> None:
        with self.connection_factory() as connection:
            connection.execute(
                """
                UPDATE security_universe_snapshot_runs
                SET status = 'completed',
                    completed_at = COALESCE(completed_at, ?),
                    snapshot_id = ?,
                    security_count = ?,
                    error = NULL
                WHERE market = ?
                  AND observation_date = ?
                  AND status != 'completed'
                """,
                [
                    completed_at.replace(tzinfo=None),
                    snapshot["snapshot_id"],
                    int(snapshot["security_count"]),
                    snapshot["market"],
                    date.fromisoformat(snapshot["observation_date"]),
                ],
            )

    def _save_snapshot(
        self,
        snapshot: Dict[str, Any],
    ) -> tuple[str, bool]:
        payload_text = self._canonical_json(snapshot["payload"])
        with self.connection_factory() as connection:
            row = connection.execute(
                """
                INSERT INTO security_universe_snapshots (
                    snapshot_id, captured_at, observation_date,
                    snapshot_version, market, source, source_query,
                    security_count, payload_hash, payload
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (market, observation_date, snapshot_version)
                DO NOTHING
                RETURNING snapshot_id
                """,
                [
                    snapshot["snapshot_id"],
                    snapshot["captured_at"].replace(tzinfo=None),
                    snapshot["observation_date"],
                    SNAPSHOT_VERSION,
                    snapshot["market"],
                    SNAPSHOT_SOURCE,
                    self._source_query(snapshot["market"]),
                    snapshot["security_count"],
                    snapshot["payload_hash"],
                    payload_text,
                ],
            ).fetchone()
            if row is not None:
                return row[0], True
            existing = connection.execute(
                """
                SELECT snapshot_id
                FROM security_universe_snapshots
                WHERE market = ?
                  AND observation_date = ?
                  AND snapshot_version = ?
                """,
                [
                    snapshot["market"],
                    snapshot["observation_date"],
                    SNAPSHOT_VERSION,
                ],
            ).fetchone()
        if existing is None:
            raise RuntimeError("证券目录快照写入失败")
        return existing[0], False

    def _find_existing_summary(
        self,
        market: str,
        observation_date: date,
    ) -> Optional[Dict[str, Any]]:
        with self.connection_factory() as connection:
            row = connection.execute(
                """
                SELECT snapshot_id, captured_at, observation_date,
                       snapshot_version, market, source, source_query,
                       security_count, payload_hash
                FROM security_universe_snapshots
                WHERE market = ?
                  AND observation_date = ?
                  AND snapshot_version = ?
                """,
                [market, observation_date, SNAPSHOT_VERSION],
            ).fetchone()
        return self._summary(row) if row is not None else None

    @staticmethod
    def _normalize_items(
        items: Iterable[dict],
        market: str,
    ) -> List[Dict[str, str]]:
        normalized: List[Dict[str, str]] = []
        symbols = set()
        for item in items:
            if not isinstance(item, dict):
                raise ValueError("证券目录条目必须是对象")
            symbol = str(item.get("symbol") or "").strip().upper()
            item_market = str(item.get("market") or market).strip().upper()
            if not symbol:
                raise ValueError("证券目录存在空 symbol")
            if item_market != market:
                raise ValueError(
                    f"证券 {symbol} 的市场 {item_market} 与 {market} 不一致"
                )
            if symbol in symbols:
                raise ValueError(f"证券目录存在重复 symbol: {symbol}")
            symbols.add(symbol)
            name = str(item.get("name") or "").strip() or symbol
            normalized.append({
                "symbol": symbol,
                "name": name,
                "name_en": str(item.get("name_en") or "").strip(),
                "name_hk": str(item.get("name_hk") or "").strip(),
                "market": market,
            })
        normalized.sort(key=lambda item: item["symbol"])
        return normalized

    @classmethod
    def _items_are_canonical(
        cls,
        items: List[dict],
        market: str,
    ) -> bool:
        try:
            return cls._normalize_items(items, market) == items
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _payload(
        market: str,
        captured_at: datetime,
        observation_date: date,
        items: List[Dict[str, str]],
    ) -> Dict[str, Any]:
        return {
            "snapshot_version": SNAPSHOT_VERSION,
            "source": SNAPSHOT_SOURCE,
            "source_request": {
                "method": "GET",
                "path": SOURCE_PATH,
                "query": SecurityUniverseSnapshotService._source_query(
                    market
                ),
            },
            "market": market,
            "captured_at": captured_at.isoformat(),
            "observation_date": observation_date.isoformat(),
            "items": items,
        }

    @staticmethod
    def _payload_hash(payload: Any) -> str:
        return hashlib.sha256(
            SecurityUniverseSnapshotService._canonical_json(payload).encode(
                "utf-8"
            )
        ).hexdigest()

    @staticmethod
    def _canonical_json(value: Any) -> str:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @staticmethod
    def _source_query(market: str) -> str:
        return f"market={market}&category={SOURCE_CATEGORY}"

    @staticmethod
    def _summary(row: tuple) -> Dict[str, Any]:
        return {
            "snapshot_id": row[0],
            "captured_at": row[1].replace(
                tzinfo=timezone.utc
            ).isoformat(),
            "observation_date": row[2].isoformat(),
            "snapshot_version": row[3],
            "market": row[4],
            "source": row[5],
            "source_query": row[6],
            "security_count": row[7],
            "payload_hash": row[8],
        }

    @staticmethod
    def _run_summary(row: tuple) -> Dict[str, Any]:
        return {
            "market": row[0],
            "observation_date": row[1].isoformat(),
            "status": row[2],
            "claim_id": row[3],
            "started_at": row[4].replace(
                tzinfo=timezone.utc
            ).isoformat(),
            "completed_at": (
                row[5].replace(tzinfo=timezone.utc).isoformat()
                if row[5] is not None
                else None
            ),
            "snapshot_id": row[6],
            "security_count": row[7],
            "error": row[8],
        }

    @staticmethod
    def _normalize_market(market: str) -> str:
        normalized = market.strip().upper()
        if normalized not in SUPPORTED_MARKETS:
            raise ValueError("market 必须是 US、HK 或 CN")
        return normalized

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    @staticmethod
    def _refresh_catalog(market: str) -> List[dict]:
        return get_security_catalog_service().refresh(market)


_security_universe_snapshot_service: Optional[
    SecurityUniverseSnapshotService
] = None


def get_security_universe_snapshot_service(
) -> SecurityUniverseSnapshotService:
    global _security_universe_snapshot_service
    if _security_universe_snapshot_service is None:
        _security_universe_snapshot_service = SecurityUniverseSnapshotService()
    return _security_universe_snapshot_service


def main() -> None:
    parser = argparse.ArgumentParser(
        description="采集 Longbridge 官方可搜索证券目录点时快照",
    )
    parser.add_argument(
        "--market",
        required=True,
        choices=list(SUPPORTED_MARKETS),
    )
    parser.add_argument(
        "--no-persist",
        action="store_true",
        help="刷新并校验目录，但不写入快照表",
    )
    args = parser.parse_args()
    result = get_security_universe_snapshot_service().capture_market(
        args.market,
        persist=not args.no_persist,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
