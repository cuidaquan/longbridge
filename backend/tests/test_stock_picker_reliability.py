from __future__ import annotations

from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import MagicMock, patch

import duckdb
from fastapi.testclient import TestClient

from app.db import _run_migrations
from app.main import app
from app.stock_picker_reliability import (
    RELIABILITY_RETENTION_DAYS,
    StockPickerReliabilityService,
)


class _ConnectionContext:
    def __init__(self, connection) -> None:
        self.connection = connection

    def __enter__(self):
        return self.connection

    def __exit__(self, exc_type, exc, traceback) -> bool:
        return False


class _Clock:
    def __init__(self) -> None:
        self.value = datetime(
            2026,
            7,
            24,
            12,
            tzinfo=timezone.utc,
        )

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


def _snapshot(
    *,
    requests: int = 0,
    attempts: int = 0,
    successes: int = 0,
    failures: int = 0,
    timeouts: int = 0,
    retries: int = 0,
    rejected: int = 0,
    total_latency_ms: float = 0,
    in_flight: int = 0,
    circuit_state: str = "closed",
    ai_attempts: int = 0,
    ai_available: int = 0,
    ai_degraded: int = 0,
) -> dict:
    return {
        "scope": "process",
        "services": {
            "quote": {
                "requests": requests,
                "attempts": attempts,
                "successes": successes,
                "failures": failures,
                "timeouts": timeouts,
                "retries": retries,
                "rejected": rejected,
                "total_latency_ms": total_latency_ms,
                "in_flight": in_flight,
                "max_in_flight": in_flight,
                "last_error": (
                    "quote unavailable"
                    if failures or circuit_state != "closed"
                    else None
                ),
                "avg_latency_ms": 0,
                "failure_rate": 0,
                "circuit_state": circuit_state,
                "consecutive_failures": failures,
                "open_for_seconds": (
                    30 if circuit_state == "open" else 0
                ),
                "policy": {
                    "timeout_seconds": 30,
                    "max_attempts": 2,
                    "max_concurrency": 3,
                    "failure_threshold": 3,
                    "recovery_seconds": 30,
                },
            }
        },
        "stock_picker": {
            "cache": {
                "requests": 0,
                "hits": 0,
                "misses": 0,
                "bypasses": 0,
                "hit_rate": 0,
            },
            "ai": {
                "attempts": ai_attempts,
                "available": ai_available,
                "degraded": ai_degraded,
                "degradation_rate": 0,
            },
        },
        "limitations": [],
    }


class StockPickerReliabilityPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = duckdb.connect(":memory:")
        _run_migrations(self.connection)
        self.clock = _Clock()
        self.current = _snapshot()
        self.service = StockPickerReliabilityService(
            snapshot_provider=lambda: self.current,
            connection_factory=lambda: _ConnectionContext(
                self.connection
            ),
            clock=self.clock,
            process_id="test-process",
        )

    def tearDown(self) -> None:
        self.connection.close()

    def test_migration_creates_snapshot_and_alert_tables(self) -> None:
        tables = {
            row[0]
            for row in self.connection.execute("SHOW TABLES").fetchall()
        }
        self.assertIn(
            "stock_picker_reliability_snapshots",
            tables,
        )
        self.assertIn(
            "stock_picker_reliability_alerts",
            tables,
        )

    def test_capture_uses_interval_deltas_and_resolves_alerts(self) -> None:
        self.current = _snapshot(
            requests=10,
            attempts=12,
            successes=4,
            failures=6,
            timeouts=3,
            retries=2,
            rejected=2,
            total_latency_ms=1000,
            ai_attempts=10,
            ai_available=4,
            ai_degraded=6,
        )
        first = self.service.capture()
        first_keys = {alert["key"] for alert in first["alerts"]}
        self.assertEqual(
            first["window_metrics"]["services"]["quote"][
                "failure_rate"
            ],
            0.6,
        )
        self.assertEqual(
            first["window_metrics"]["services"]["quote"][
                "timeout_rate"
            ],
            0.3,
        )
        self.assertEqual(
            first["window_metrics"]["services"]["quote"][
                "rejected_rate"
            ],
            0.2,
        )
        self.assertIn(
            "service:quote:failure_rate",
            first_keys,
        )
        self.assertIn(
            "service:quote:timeout_rate",
            first_keys,
        )
        self.assertIn(
            "service:quote:rejected_rate",
            first_keys,
        )
        self.assertIn(
            "stock_picker:ai:degradation_rate",
            first_keys,
        )

        self.clock.advance(60)
        self.current = _snapshot(
            requests=14,
            attempts=16,
            successes=8,
            failures=6,
            timeouts=3,
            retries=2,
            rejected=2,
            total_latency_ms=1200,
            ai_attempts=14,
            ai_available=8,
            ai_degraded=6,
        )
        second = self.service.capture()
        self.assertEqual(
            second["window_metrics"]["services"]["quote"]["requests"],
            4,
        )
        self.assertEqual(
            second["window_metrics"]["services"]["quote"][
                "failures"
            ],
            0,
        )
        self.assertEqual(second["alerts"], [])

        current = self.service.get_current()
        self.assertEqual(current["alerts"]["active_count"], 0)
        self.assertFalse(current["persistence"]["stale"])
        self.assertEqual(
            current["persistence"]["latest_snapshot_age_seconds"],
            0,
        )
        history = self.service.get_history(hours=1, limit=10)
        self.assertEqual(len(history["items"]), 2)
        self.assertTrue(history["alerts"])
        self.assertTrue(
            all(
                alert["status"] == "resolved"
                for alert in history["alerts"]
            )
        )

    def test_circuit_and_capacity_alerts_do_not_need_sample_minimum(
        self,
    ) -> None:
        self.current = _snapshot(
            requests=1,
            attempts=1,
            failures=1,
            in_flight=3,
            circuit_state="open",
        )
        capture = self.service.capture()
        alerts = {alert["key"]: alert for alert in capture["alerts"]}
        self.assertEqual(
            alerts["service:quote:circuit"]["severity"],
            "critical",
        )
        self.assertIn("service:quote:capacity", alerts)
        self.assertNotIn(
            "service:quote:failure_rate",
            alerts,
        )

    def test_capture_prunes_snapshots_outside_retention(self) -> None:
        old_time = self.clock.value - timedelta(
            days=RELIABILITY_RETENTION_DAYS + 1
        )
        self.connection.execute(
            """
            INSERT INTO stock_picker_reliability_snapshots (
                observed_at,
                process_id,
                payload,
                window_metrics,
                alerts
            )
            VALUES (?, 'old-process', '{}', '{}', '[]')
            """,
            [old_time.replace(tzinfo=None)],
        )
        self.service.capture()
        rows = self.connection.execute(
            """
            SELECT process_id
            FROM stock_picker_reliability_snapshots
            ORDER BY observed_at
            """
        ).fetchall()
        self.assertEqual(rows, [("test-process",)])

    def test_current_metrics_survive_persistence_read_failure(
        self,
    ) -> None:
        def unavailable_connection():
            raise RuntimeError("database unavailable")

        service = StockPickerReliabilityService(
            snapshot_provider=lambda: _snapshot(requests=2),
            connection_factory=unavailable_connection,
            clock=self.clock,
            process_id="unavailable-process",
        )
        current = service.get_current()
        self.assertEqual(
            current["services"]["quote"]["requests"],
            2,
        )
        self.assertFalse(current["persistence"]["available"])
        self.assertTrue(current["persistence"]["stale"])
        self.assertEqual(
            current["persistence"]["error"],
            "database unavailable",
        )
        self.assertEqual(current["alerts"]["active_count"], 0)

    def test_current_marks_snapshot_stale_after_two_intervals(
        self,
    ) -> None:
        self.service.capture()
        self.clock.advance(121)
        current = self.service.get_current()
        self.assertTrue(current["persistence"]["available"])
        self.assertTrue(current["persistence"]["stale"])
        self.assertEqual(
            current["persistence"]["latest_snapshot_age_seconds"],
            121,
        )


class StockPickerReliabilityHttpTests(unittest.TestCase):
    def test_current_and_history_routes_use_persistence_service(
        self,
    ) -> None:
        service = MagicMock()
        service.get_current.return_value = {
            "scope": "process",
            "persistence": {"enabled": True},
            "alerts": {"active_count": 0, "active": []},
        }
        service.get_history.return_value = {
            "hours": 12,
            "limit": 5,
            "items": [],
            "alerts": [],
        }
        with (
            patch(
                "app.routers.stock_picker."
                "get_stock_picker_reliability_service",
                return_value=service,
            ),
            TestClient(app) as client,
        ):
            current = client.get(
                "/api/stock-picker/reliability"
            )
            history = client.get(
                "/api/stock-picker/reliability/history",
                params={"hours": 12, "limit": 5},
            )
            invalid = client.get(
                "/api/stock-picker/reliability/history",
                params={"hours": 0},
            )

        self.assertEqual(current.status_code, 200)
        self.assertTrue(current.json()["persistence"]["enabled"])
        self.assertEqual(history.status_code, 200)
        self.assertEqual(history.json()["hours"], 12)
        self.assertEqual(invalid.status_code, 422)
        service.get_current.assert_called_once_with()
        service.get_history.assert_called_once_with(
            hours=12,
            limit=5,
        )
