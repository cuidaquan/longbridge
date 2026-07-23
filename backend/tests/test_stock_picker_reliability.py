from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import unittest
from unittest.mock import MagicMock, patch

import duckdb
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import _run_migrations
from app.main import _persist_stock_picker_reliability, app
from app.stock_picker_reliability import (
    DELIVERY_RETRY_SECONDS,
    RELIABILITY_RETENTION_DAYS,
    StockPickerReliabilityService,
    _NoRedirectHandler,
    _send_webhook,
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
        self.sender = MagicMock(return_value=204)
        self.service = StockPickerReliabilityService(
            snapshot_provider=lambda: self.current,
            connection_factory=lambda: _ConnectionContext(
                self.connection
            ),
            clock=self.clock,
            process_id="test-process",
            delivery_config_provider=lambda: {
                "enabled": False,
                "configured": False,
                "signed": False,
                "url": "",
                "secret": "",
                "timeout_seconds": 5,
            },
            webhook_sender=self.sender,
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
        self.assertIn(
            "stock_picker_reliability_deliveries",
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
        self.assertEqual(
            {
                delivery["status"]
                for delivery in history["deliveries"]
            },
            {"skipped"},
        )
        self.sender.assert_not_called()

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
            delivery_config_provider=lambda: {
                "enabled": False,
                "configured": False,
                "signed": False,
            },
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

    def test_disabled_webhook_records_skipped_transition_without_secret(
        self,
    ) -> None:
        self.current = _snapshot(
            requests=1,
            attempts=1,
            failures=1,
            circuit_state="open",
        )
        capture = self.service.capture()
        self.assertEqual(len(capture["alert_transitions"]), 1)
        deliveries = self.service.get_deliveries()
        self.assertFalse(deliveries["enabled"])
        self.assertFalse(deliveries["configured"])
        self.assertEqual(
            deliveries["status_counts"],
            {"skipped": 1},
        )
        item = deliveries["items"][0]
        self.assertEqual(item["status"], "skipped")
        serialized = json.dumps(deliveries)
        self.assertNotIn("webhook_url", serialized)
        self.assertNotIn("secret", serialized)
        self.sender.assert_not_called()

    def test_enabled_webhook_delivers_trigger_and_resolution_once(
        self,
    ) -> None:
        sender = MagicMock(return_value=204)
        config = {
            "enabled": True,
            "configured": True,
            "signed": True,
            "url": "https://alerts.example.test/secret-token",
            "secret": "signing-secret",
            "timeout_seconds": 5,
        }
        service = StockPickerReliabilityService(
            snapshot_provider=lambda: self.current,
            connection_factory=lambda: _ConnectionContext(
                self.connection
            ),
            clock=self.clock,
            process_id="delivery-process",
            delivery_config_provider=lambda: config,
            webhook_sender=sender,
        )
        self.current = _snapshot(
            requests=1,
            attempts=1,
            failures=1,
            circuit_state="open",
        )
        first = service.capture()
        self.assertEqual(len(first["delivery_ids"]), 1)
        delivered = service.deliver_due()
        self.assertEqual(delivered["delivered"], 1)
        self.assertEqual(sender.call_count, 1)

        self.clock.advance(60)
        service.capture()
        self.assertEqual(sender.call_count, 1)
        self.assertEqual(
            service.get_deliveries()["status_counts"],
            {"delivered": 1},
        )

        self.clock.advance(60)
        self.current = _snapshot(
            requests=2,
            attempts=2,
            successes=1,
            failures=1,
            circuit_state="closed",
        )
        resolved = service.capture()
        self.assertEqual(
            resolved["alert_transitions"][0]["event_type"],
            "resolved",
        )
        service.deliver_due()
        self.assertEqual(sender.call_count, 2)
        deliveries = service.get_deliveries()
        self.assertEqual(
            deliveries["status_counts"],
            {"delivered": 2},
        )
        serialized = json.dumps(deliveries)
        self.assertNotIn(config["url"], serialized)
        self.assertNotIn(config["secret"], serialized)

    def test_enabled_webhook_without_url_skips_new_transition(
        self,
    ) -> None:
        sender = MagicMock(return_value=204)
        service = StockPickerReliabilityService(
            snapshot_provider=lambda: self.current,
            connection_factory=lambda: _ConnectionContext(
                self.connection
            ),
            clock=self.clock,
            process_id="unconfigured-process",
            delivery_config_provider=lambda: {
                "enabled": True,
                "configured": False,
                "signed": False,
                "url": "",
                "secret": "",
                "timeout_seconds": 5,
            },
            webhook_sender=sender,
        )
        self.current = _snapshot(
            requests=1,
            attempts=1,
            failures=1,
            circuit_state="open",
        )

        service.capture()
        delivery = service.get_deliveries()["items"][0]

        self.assertEqual(delivery["status"], "skipped")
        self.assertEqual(
            delivery["error"],
            "webhook_not_configured",
        )
        self.assertEqual(service.deliver_due()["selected"], 0)
        sender.assert_not_called()

    def test_enabling_webhook_does_not_replay_skipped_active_alert(
        self,
    ) -> None:
        self.current = _snapshot(
            requests=1,
            attempts=1,
            failures=1,
            circuit_state="open",
        )
        self.service.capture()
        sender = MagicMock(return_value=204)
        service = StockPickerReliabilityService(
            snapshot_provider=lambda: self.current,
            connection_factory=lambda: _ConnectionContext(
                self.connection
            ),
            clock=self.clock,
            process_id="enabled-later-process",
            delivery_config_provider=lambda: {
                "enabled": True,
                "configured": True,
                "signed": False,
                "url": "https://alerts.example.test/hook",
                "secret": "",
                "timeout_seconds": 5,
            },
            webhook_sender=sender,
        )
        self.clock.advance(60)
        capture = service.capture()
        self.assertEqual(capture["alert_transitions"], [])
        self.assertEqual(service.deliver_due()["selected"], 0)
        sender.assert_not_called()
        self.assertEqual(
            service.get_deliveries()["status_counts"],
            {"skipped": 1},
        )

    def test_webhook_failures_retry_then_move_to_dead_letter(
        self,
    ) -> None:
        sender = MagicMock(
            side_effect=RuntimeError(
                "https://alerts.example.test/secret-token"
            )
        )
        service = StockPickerReliabilityService(
            snapshot_provider=lambda: self.current,
            connection_factory=lambda: _ConnectionContext(
                self.connection
            ),
            clock=self.clock,
            process_id="retry-process",
            delivery_config_provider=lambda: {
                "enabled": True,
                "configured": True,
                "signed": False,
                "url": "https://alerts.example.test/secret-token",
                "secret": "",
                "timeout_seconds": 5,
            },
            webhook_sender=sender,
        )
        self.current = _snapshot(
            requests=1,
            attempts=1,
            failures=1,
            circuit_state="open",
        )
        service.capture()
        first = service.deliver_due()
        self.assertEqual(first["failed"], 1)
        self.assertEqual(service.deliver_due()["selected"], 0)

        self.clock.advance(DELIVERY_RETRY_SECONDS)
        second = service.deliver_due()
        self.assertEqual(second["failed"], 1)
        self.clock.advance(DELIVERY_RETRY_SECONDS * 2)
        third = service.deliver_due()
        self.assertEqual(third["dead_letter"], 1)

        delivery = service.get_deliveries()["items"][0]
        self.assertEqual(delivery["attempt_count"], 3)
        self.assertEqual(delivery["status"], "dead_letter")
        self.assertEqual(
            delivery["error"],
            "webhook delivery failed: RuntimeError",
        )
        self.assertNotIn("secret-token", json.dumps(delivery))

    def test_default_webhook_sender_signs_exact_request_body(
        self,
    ) -> None:
        response = MagicMock()
        response.getcode.return_value = 202
        response_context = MagicMock()
        response_context.__enter__.return_value = response
        opener = MagicMock()
        opener.open.return_value = response_context
        payload = {
            "event": "stock_picker.reliability.alert.triggered",
            "delivery_id": 7,
            "alert": {"key": "service:quote:circuit"},
        }
        config = {
            "url": "https://alerts.example.test/hook",
            "secret": "signing-secret",
            "timeout_seconds": 5,
        }
        expected_body = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        expected_signature = hmac.new(
            b"signing-secret",
            expected_body,
            hashlib.sha256,
        ).hexdigest()

        with patch(
            "app.stock_picker_reliability.build_opener",
            return_value=opener,
        ) as opener_factory:
            status = _send_webhook(config, payload)

        self.assertEqual(status, 202)
        opener_factory.assert_called_once()
        opener.open.assert_called_once()
        call = opener.open.call_args
        request = call.args[0]
        self.assertEqual(request.full_url, config["url"])
        self.assertEqual(request.data, expected_body)
        self.assertEqual(call.kwargs["timeout"], 5.0)
        self.assertEqual(
            request.headers["X-longbridge-signature"],
            f"sha256={expected_signature}",
        )
        self.assertEqual(
            request.headers["X-longbridge-event"],
            payload["event"],
        )
        self.assertEqual(
            request.headers["X-longbridge-delivery"],
            "7",
        )

    def test_webhook_sender_disables_redirects(self) -> None:
        handler = _NoRedirectHandler()
        redirected = handler.redirect_request(
            None,
            None,
            302,
            "Found",
            {},
            "https://other.example.test/hook",
        )
        self.assertIsNone(redirected)


class StockPickerReliabilityConfigTests(unittest.TestCase):
    def test_webhook_defaults_are_disabled_and_unconfigured(
        self,
    ) -> None:
        with patch.dict("os.environ", {}, clear=True):
            settings = Settings(_env_file=None)
        self.assertFalse(
            settings.stock_picker_alert_webhook_enabled
        )
        self.assertIsNone(
            settings.stock_picker_alert_webhook_url
        )
        self.assertIsNone(
            settings.stock_picker_alert_webhook_secret
        )
        self.assertEqual(
            settings.stock_picker_alert_webhook_timeout_seconds,
            5.0,
        )


class StockPickerReliabilityLoopTests(
    unittest.IsolatedAsyncioTestCase
):
    async def test_loop_captures_then_delivers_due_records(
        self,
    ) -> None:
        service = MagicMock()
        service.capture.side_effect = [
            {"alerts": []},
            asyncio.CancelledError(),
        ]
        service.deliver_due.return_value = {
            "failed": 0,
            "dead_letter": 0,
        }
        with (
            patch(
                "app.main.get_stock_picker_reliability_service",
                return_value=service,
            ),
            patch(
                "app.main.RELIABILITY_CAPTURE_INTERVAL_SECONDS",
                0,
            ),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await _persist_stock_picker_reliability()

        self.assertEqual(service.capture.call_count, 2)
        service.deliver_due.assert_called_once_with()

    async def test_loop_delivers_when_capture_fails(
        self,
    ) -> None:
        service = MagicMock()
        service.capture.side_effect = [
            RuntimeError("capture failed"),
            asyncio.CancelledError(),
        ]
        service.deliver_due.return_value = {
            "failed": 0,
            "dead_letter": 0,
        }
        with (
            patch(
                "app.main.get_stock_picker_reliability_service",
                return_value=service,
            ),
            patch(
                "app.main.RELIABILITY_CAPTURE_INTERVAL_SECONDS",
                0,
            ),
            patch("app.main.logger.warning"),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await _persist_stock_picker_reliability()

        self.assertEqual(service.capture.call_count, 2)
        service.deliver_due.assert_called_once_with()


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
            "deliveries": [],
        }
        service.get_deliveries.return_value = {
            "enabled": False,
            "configured": False,
            "items": [],
        }
        with patch(
            "app.routers.stock_picker."
            "get_stock_picker_reliability_service",
            return_value=service,
        ):
            client = TestClient(app)
            try:
                current = client.get(
                    "/api/stock-picker/reliability"
                )
                history = client.get(
                    "/api/stock-picker/reliability/history",
                    params={"hours": 12, "limit": 5},
                )
                deliveries = client.get(
                    "/api/stock-picker/reliability/deliveries",
                    params={"limit": 7},
                )
                invalid = client.get(
                    "/api/stock-picker/reliability/history",
                    params={"hours": 0},
                )
            finally:
                client.close()

        self.assertEqual(current.status_code, 200)
        self.assertTrue(current.json()["persistence"]["enabled"])
        self.assertEqual(history.status_code, 200)
        self.assertEqual(history.json()["hours"], 12)
        self.assertEqual(deliveries.status_code, 200)
        self.assertFalse(deliveries.json()["enabled"])
        self.assertEqual(invalid.status_code, 422)
        service.get_current.assert_called_once_with()
        service.get_history.assert_called_once_with(
            hours=12,
            limit=5,
        )
        service.get_deliveries.assert_called_once_with(limit=7)
