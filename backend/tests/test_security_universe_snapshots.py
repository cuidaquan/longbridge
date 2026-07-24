from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
import json
import unittest
from unittest.mock import MagicMock, patch

import duckdb
from fastapi import HTTPException

from app.db import _run_migrations
from app.routers.stock_picker import (
    get_security_universe_snapshot,
    get_security_universe_snapshots,
    router,
)
from app.security_universe_snapshots import (
    CAPTURE_CLAIM_LEASE_MINUTES,
    SNAPSHOT_SOURCE,
    SNAPSHOT_VERSION,
    SecurityUniverseSnapshotService,
)


class _ConnectionContext:
    def __init__(self, connection) -> None:
        self.connection = connection

    def __enter__(self):
        return self.connection

    def __exit__(self, exc_type, exc, traceback) -> bool:
        return False


def _items(market: str = "US"):
    suffix = market
    return [
        {
            "symbol": f"BBB.{suffix}",
            "name": "Beta",
            "name_en": "Beta Inc.",
            "name_hk": "",
            "market": market,
        },
        {
            "symbol": f"AAA.{suffix}",
            "name": "Alpha",
            "name_en": "Alpha Inc.",
            "name_hk": "",
            "market": market,
        },
    ]


class SecurityUniverseSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = duckdb.connect(":memory:")
        self.addCleanup(self.connection.close)
        _run_migrations(self.connection)
        self.factory = lambda: _ConnectionContext(self.connection)

    def _service(self, **kwargs) -> SecurityUniverseSnapshotService:
        return SecurityUniverseSnapshotService(
            catalog_loader=kwargs.get(
                "catalog_loader",
                lambda market: _items(market),
            ),
            connection_factory=self.factory,
            clock=kwargs.get(
                "clock",
                lambda: datetime(
                    2026,
                    7,
                    24,
                    12,
                    tzinfo=timezone.utc,
                ),
            ),
            minimum_security_counts=kwargs.get(
                "minimum_security_counts",
                {"US": 1, "HK": 1, "CN": 1},
            ),
            trading_day_loader=kwargs.get(
                "trading_day_loader",
                lambda market, observation_date: True,
            ),
        )

    @staticmethod
    def _call_directly(service, operation, callback, *args, **kwargs):
        kwargs.pop("retry_if", None)
        return callback(*args, **kwargs)

    def test_migration_is_idempotent_and_has_daily_unique_key(self) -> None:
        _run_migrations(self.connection)
        columns = {
            row[1]
            for row in self.connection.execute(
                "PRAGMA table_info('security_universe_snapshots')"
            ).fetchall()
        }

        self.assertEqual(
            columns,
            {
                "snapshot_id",
                "captured_at",
                "observation_date",
                "snapshot_version",
                "market",
                "source",
                "source_query",
                "security_count",
                "payload_hash",
                "payload",
            },
        )

    def test_capture_is_canonical_integrity_checked_and_idempotent(self) -> None:
        loader = MagicMock(side_effect=lambda market: _items(market))
        service = self._service(catalog_loader=loader)

        first = service.capture_market("us")
        second = service.capture_market("US")
        detail = service.get_snapshot(first["snapshot_id"])

        self.assertEqual(first["status"], "captured")
        self.assertTrue(first["persisted"])
        self.assertEqual(second["status"], "already_captured")
        self.assertFalse(second["persisted"])
        self.assertEqual(first["snapshot_id"], second["snapshot_id"])
        loader.assert_called_once_with("US")
        self.assertEqual(detail["snapshot_version"], SNAPSHOT_VERSION)
        self.assertEqual(detail["source"], SNAPSHOT_SOURCE)
        self.assertTrue(detail["integrity_valid"])
        self.assertEqual(
            [item["symbol"] for item in detail["payload"]["items"]],
            ["AAA.US", "BBB.US"],
        )
        self.assertEqual(
            detail["payload"]["source_request"]["query"],
            "market=US&category=Overnight",
        )
        self.assertEqual(
            detail["payload"]["captured_at"],
            first["captured_at"],
        )

    def test_non_persistent_capture_is_order_independent(self) -> None:
        first = self._service(
            catalog_loader=lambda market: _items(market),
        ).capture_market("US", persist=False)
        second = self._service(
            catalog_loader=lambda market: list(reversed(_items(market))),
        ).capture_market("US", persist=False)
        count = self.connection.execute(
            "SELECT COUNT(*) FROM security_universe_snapshots"
        ).fetchone()[0]

        self.assertEqual(first["payload_hash"], second["payload_hash"])
        self.assertEqual(first["security_count"], 2)
        self.assertFalse(first["persisted"])
        self.assertEqual(count, 0)

    def test_concurrent_daily_insert_returns_persisted_metadata(self) -> None:
        service = self._service()
        existing = {
            "snapshot_id": "existing-snapshot",
            "captured_at": "2026-07-24T11:00:00+00:00",
            "observation_date": "2026-07-24",
            "snapshot_version": SNAPSHOT_VERSION,
            "market": "US",
            "source": SNAPSHOT_SOURCE,
            "source_query": "market=US&category=Overnight",
            "security_count": 12345,
            "payload_hash": "existing-hash",
        }
        with patch.object(
            service,
            "_find_existing_summary",
            side_effect=[None, existing],
        ), patch.object(
            service,
            "_save_snapshot",
            return_value=("existing-snapshot", False),
        ):
            result = service.capture_market("US")

        self.assertEqual(
            result,
            {
                **existing,
                "status": "already_captured",
                "persisted": False,
            },
        )

    def test_rejects_incomplete_duplicate_and_cross_market_payloads(self) -> None:
        with self.assertRaisesRegex(ValueError, "最低完整性门槛"):
            self._service(
                minimum_security_counts={"US": 3},
            ).capture_market("US", persist=False)

        duplicate = [_items()[0], _items()[0]]
        with self.assertRaisesRegex(ValueError, "重复 symbol"):
            self._service(
                catalog_loader=lambda market: duplicate,
            ).capture_market("US", persist=False)

        wrong_market = [{**_items()[0], "market": "HK"}]
        with self.assertRaisesRegex(ValueError, "市场 HK 与 US 不一致"):
            self._service(
                catalog_loader=lambda market: wrong_market,
            ).capture_market("US", persist=False)

    def test_market_local_date_history_filter_and_integrity_failure(self) -> None:
        us_service = self._service(
            clock=lambda: datetime(
                2026,
                7,
                25,
                1,
                tzinfo=timezone.utc,
            ),
        )
        us = us_service.capture_market("US")
        hk = self._service().capture_market("HK")

        history = us_service.get_history(limit=10)
        us_history = us_service.get_history(market="US", limit=1)
        self.assertEqual(len(history["items"]), 2)
        self.assertEqual(len(us_history["items"]), 1)
        self.assertEqual(us["observation_date"], "2026-07-24")
        self.assertEqual(hk["observation_date"], "2026-07-24")

        self.connection.execute(
            """
            UPDATE security_universe_snapshots
            SET captured_at = ?
            WHERE snapshot_id = ?
            """,
            [datetime(2026, 7, 24, 13), us["snapshot_id"]],
        )
        metadata_tampered = us_service.get_snapshot(us["snapshot_id"])
        self.assertFalse(metadata_tampered["integrity_valid"])
        self.assertIn(
            "payload_captured_at_mismatch",
            metadata_tampered["integrity_errors"],
        )

        self.connection.execute(
            """
            UPDATE security_universe_snapshots
            SET payload = ?
            WHERE snapshot_id = ?
            """,
            [json.dumps({"items": []}), us["snapshot_id"]],
        )
        detail = us_service.get_snapshot(us["snapshot_id"])
        self.assertFalse(detail["integrity_valid"])
        self.assertIn("payload_hash_mismatch", detail["integrity_errors"])
        self.assertIn("security_count_mismatch", detail["integrity_errors"])
        self.assertNotEqual(
            detail["computed_payload_hash"],
            detail["payload_hash"],
        )

        self.connection.execute(
            """
            UPDATE security_universe_snapshots
            SET payload = ?
            WHERE snapshot_id = ?
            """,
            ["{broken", us["snapshot_id"]],
        )
        invalid_json = us_service.get_snapshot(us["snapshot_id"])
        self.assertFalse(invalid_json["integrity_valid"])
        self.assertEqual(
            invalid_json["integrity_errors"],
            ["invalid_payload_json"],
        )
        self.assertIsNone(invalid_json["payload"])

    def test_history_validation_and_missing_detail(self) -> None:
        service = self._service()
        with self.assertRaisesRegex(ValueError, "1～100"):
            service.get_history(limit=0)
        with self.assertRaisesRegex(ValueError, "US、HK 或 CN"):
            service.get_history(market="SG")
        with self.assertRaisesRegex(ValueError, "不能为空"):
            service.get_snapshot(" ")
        self.assertIsNone(service.get_snapshot("missing"))

    def test_due_capture_applies_local_close_and_daily_idempotency(self) -> None:
        loader = MagicMock(side_effect=lambda market: _items(market))
        calendar = MagicMock(return_value=True)
        service = self._service(
            catalog_loader=loader,
            trading_day_loader=calendar,
        )
        with patch(
            "app.security_universe_snapshots.run_external_call",
            side_effect=self._call_directly,
        ):
            first = service.capture_due()
            second = service.capture_due()

        self.assertEqual(
            [item["market"] for item in first["captured"]],
            ["HK"],
        )
        self.assertEqual(first["security_count"], 2)
        self.assertEqual(
            first["skipped"],
            [{
                "market": "US",
                "observation_date": "2026-07-24",
                "reason": "session_not_closed",
            }],
        )
        self.assertEqual(second["captured"], [])
        self.assertIn(
            "already_captured",
            {item["reason"] for item in second["skipped"]},
        )
        loader.assert_called_once_with("HK")
        calendar.assert_called_once_with("HK", date(2026, 7, 24))
        history = service.get_history("HK")
        self.assertEqual(history["capture_runs"][0]["status"], "completed")
        self.assertEqual(history["capture_runs"][0]["security_count"], 2)

    def test_due_capture_fails_closed_and_caches_market_closed(self) -> None:
        loader = MagicMock()
        calendar = MagicMock(return_value=False)
        service = self._service(
            catalog_loader=loader,
            trading_day_loader=calendar,
        )
        with patch(
            "app.security_universe_snapshots.run_external_call",
            side_effect=self._call_directly,
        ):
            first = service.capture_due()
            second = service.capture_due()

        self.assertEqual(first["captured"], [])
        self.assertEqual(first["errors"], [])
        self.assertIn(
            "market_closed",
            {item["reason"] for item in first["skipped"]},
        )
        self.assertEqual(second["captured"], [])
        calendar.assert_called_once_with("HK", date(2026, 7, 24))
        loader.assert_not_called()

    def test_calendar_error_does_not_claim_or_fetch(self) -> None:
        loader = MagicMock()
        service = self._service(
            catalog_loader=loader,
            trading_day_loader=MagicMock(
                side_effect=RuntimeError("calendar token=secret"),
            ),
        )
        with patch(
            "app.security_universe_snapshots.run_external_call",
            side_effect=self._call_directly,
        ):
            result = service.capture_due()

        run_count = self.connection.execute(
            "SELECT COUNT(*) FROM security_universe_snapshot_runs"
        ).fetchone()[0]
        self.assertEqual(run_count, 0)
        self.assertEqual(result["captured"], [])
        self.assertEqual(
            result["errors"][0]["reason"],
            "trading_calendar_unavailable",
        )
        self.assertNotIn("secret", result["errors"][0]["error"])
        loader.assert_not_called()

    def test_failed_capture_retries_and_replaces_failed_run(self) -> None:
        loader = MagicMock(
            side_effect=[RuntimeError("refresh failed"), _items("HK")],
        )
        service = self._service(catalog_loader=loader)
        with patch(
            "app.security_universe_snapshots.run_external_call",
            side_effect=self._call_directly,
        ):
            first = service.capture_due()
            second = service.capture_due()

        self.assertEqual(first["captured"], [])
        self.assertIn("refresh failed", first["errors"][0]["error"])
        self.assertEqual(len(second["captured"]), 1)
        run = service.get_history("HK")["capture_runs"][0]
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["snapshot_id"], second["captured"][0]["snapshot_id"])

    def test_claim_blocks_concurrency_and_stale_owner_cannot_finish(self) -> None:
        service = self._service()
        now = datetime(2026, 7, 24, 12, tzinfo=timezone.utc)
        observation_date = date(2026, 7, 24)
        first = service._claim_capture("HK", observation_date, now)
        blocked = service._claim_capture(
            "HK",
            observation_date,
            now + timedelta(minutes=1),
        )
        replacement = service._claim_capture(
            "HK",
            observation_date,
            now + timedelta(minutes=CAPTURE_CLAIM_LEASE_MINUTES + 1),
        )
        service._finish_capture_claim(
            "HK",
            observation_date,
            first,
            status="completed",
            completed_at=now + timedelta(minutes=32),
            snapshot_id="stale-snapshot",
            security_count=999,
        )

        row = self.connection.execute(
            """
            SELECT status, claim_id, snapshot_id, security_count
            FROM security_universe_snapshot_runs
            WHERE market = 'HK' AND observation_date = ?
            """,
            [observation_date],
        ).fetchone()
        self.assertIsNotNone(first)
        self.assertIsNone(blocked)
        self.assertIsNotNone(replacement)
        self.assertNotEqual(first, replacement)
        self.assertEqual(row, ("running", replacement, None, 0))

    def test_existing_snapshot_reconciles_interrupted_running_claim(self) -> None:
        service = self._service()
        now = datetime(2026, 7, 24, 12, tzinfo=timezone.utc)
        observation_date = date(2026, 7, 24)
        claim_id = service._claim_capture("HK", observation_date, now)
        snapshot = service.capture_market("HK")

        result = service.capture_due()
        row = self.connection.execute(
            """
            SELECT status, snapshot_id, security_count, error
            FROM security_universe_snapshot_runs
            WHERE market = 'HK' AND observation_date = ?
            """,
            [observation_date],
        ).fetchone()

        self.assertIsNotNone(claim_id)
        self.assertEqual(result["captured"], [])
        self.assertIn(
            "already_captured",
            {item["reason"] for item in result["skipped"]},
        )
        self.assertEqual(
            row,
            ("completed", snapshot["snapshot_id"], 2, None),
        )


class SecurityUniverseSnapshotHttpTests(unittest.TestCase):
    def test_history_and_detail_endpoints(self) -> None:
        service = MagicMock()
        service.get_history.return_value = {
            "snapshot_version": SNAPSHOT_VERSION,
            "source": SNAPSHOT_SOURCE,
            "items": [{"snapshot_id": "snapshot-1"}],
        }
        service.get_snapshot.side_effect = [
            {"snapshot_id": "snapshot-1", "integrity_valid": True},
            None,
        ]
        with patch(
            "app.routers.stock_picker."
            "get_security_universe_snapshot_service",
            return_value=service,
        ):
            history = asyncio.run(
                get_security_universe_snapshots("US", 5)
            )
            detail = asyncio.run(
                get_security_universe_snapshot("snapshot-1")
            )
            with self.assertRaises(HTTPException) as raised:
                asyncio.run(get_security_universe_snapshot("missing"))

        paths = {route.path for route in router.routes}
        self.assertIn(
            "/api/stock-picker/security-universe-snapshots",
            paths,
        )
        self.assertIn(
            "/api/stock-picker/security-universe-snapshots/{snapshot_id}",
            paths,
        )
        self.assertEqual(history["items"][0]["snapshot_id"], "snapshot-1")
        self.assertTrue(detail["integrity_valid"])
        self.assertEqual(raised.exception.status_code, 404)
        service.get_history.assert_called_once_with("US", 5)


if __name__ == "__main__":
    unittest.main()
