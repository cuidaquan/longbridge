from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import unittest
from unittest.mock import MagicMock, patch

import duckdb
from fastapi.testclient import TestClient

from app.db import _run_migrations
from app.main import app
from app.stock_screener_auto_capture import (
    AUTO_CAPTURE_VERSION,
    StockScreenerAutoCaptureService,
)
from app.stock_screener import RELATIVE_STRENGTH_VERSION
from app.stock_screener_snapshots import (
    LEGACY_RELATIVE_STRENGTH_VERSION,
    StockScreenerSnapshotService,
)


class _ConnectionContext:
    def __init__(self, connection) -> None:
        self.connection = connection

    def __enter__(self):
        return self.connection

    def __exit__(self, exc_type, exc, traceback) -> bool:
        return False


def _payload(*, heavy: bool = False) -> dict:
    return {
        "request": {
            "market": "HK",
            "target_direction": "LONG",
            "benchmark_symbol": "2800.HK",
            "strategy": {
                "id": 101,
                "name": "Growth",
                "source": "recommended",
            },
            "page": 0,
            "size": 20,
            "scan_pages": 2,
            "include_indexes": True,
            "include_short_risk": True,
            "include_tradeability": True,
            "require_normal_trade_status": True,
            "include_fundamentals": heavy,
            "include_margin_requirements": False,
            "include_short_capacity": False,
            "fundamental_event_window_days": 30,
            "include_corporate_actions": heavy,
            "filters": {"min_turnover": 150.0},
        },
        "scan": {
            "mode": "bounded",
            "first_page": 0,
            "last_page": 1,
            "pages_scanned": 2,
            "candidates_scanned": 1,
            "candidates_returned": 1,
            "duplicates_removed": 0,
        },
        "metric_basis": {
            "version": RELATIVE_STRENGTH_VERSION,
            "benchmark_symbol": "2800.HK",
            "target_direction": "LONG",
            "industry_basis": "scan_range_leave_one_out_industry_median",
            "industry_membership_source": "current_screener_scan_candidates",
            "minimum_industry_peers": 2,
            "historical_industry_membership": False,
            "benchmark_returns": {
                "ten_day_change_rate": 0.05,
                "half_year_change_rate": 0.15,
            },
            "benchmark_observations": [
                {
                    "page": 0,
                    "ten_day_change_rate": 0.05,
                    "half_year_change_rate": 0.15,
                },
                {
                    "page": 1,
                    "ten_day_change_rate": 0.05,
                    "half_year_change_rate": 0.15,
                },
            ],
        },
        "statuses": {},
        "filter_summary": {
            "before": 1,
            "after": 1,
            "excluded": 0,
            "reasons": {},
        },
        "pages": [
            {
                "page": 0,
                "total": 1,
                "has_more": True,
                "candidate_count": 1,
                "symbols": ["700.HK"],
            },
            {
                "page": 1,
                "total": 1,
                "has_more": False,
                "candidate_count": 0,
                "symbols": [],
            },
        ],
        "occurrences": [
            {
                "symbol": "700.HK",
                "source_page": 0,
                "source_rank": 1,
                "retained": True,
                "scan_order": 1,
            },
        ],
        "universe": [
            {
                "scan_order": 1,
                "source_page": 0,
                "selected": True,
                "exclusion_reason": None,
                "candidate": {"symbol": "700.HK"},
            },
        ],
        "selected_symbols": ["700.HK"],
    }


class StockScreenerAutoCaptureServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = duckdb.connect(":memory:")
        self.addCleanup(self.connection.close)
        _run_migrations(self.connection)
        self.connection_factory = lambda: _ConnectionContext(self.connection)
        self.template_time = datetime(
            2026, 7, 23, 10, tzinfo=timezone.utc
        )
        self.run_time = datetime(
            2026, 7, 24, 10, tzinfo=timezone.utc
        )
        self.snapshot_service = StockScreenerSnapshotService(
            connection_factory=self.connection_factory,
            clock=lambda: self.template_time,
        )

    def _service(self, **kwargs) -> StockScreenerAutoCaptureService:
        return StockScreenerAutoCaptureService(
            connection_factory=self.connection_factory,
            snapshot_service=self.snapshot_service,
            searcher=kwargs.pop("searcher", MagicMock()),
            trading_day_loader=kwargs.pop(
                "trading_day_loader", lambda market, day: True
            ),
            clock=kwargs.pop("clock", lambda: self.run_time),
            **kwargs,
        )

    def _mark_snapshot_legacy(self, snapshot_id: str) -> None:
        stored = self.connection.execute(
            """
            SELECT payload
            FROM stock_screener_scan_snapshots
            WHERE snapshot_id = ?
            """,
            [snapshot_id],
        ).fetchone()[0]
        payload = json.loads(stored)
        payload["capture"]["relative_strength_version"] = (
            LEGACY_RELATIVE_STRENGTH_VERSION
        )
        payload["metric_basis"].pop("version", None)
        payload["metric_basis"]["industry_basis"] = (
            "scan_range_industry_median"
        )
        payload["metric_basis"].pop("industry_membership_source", None)
        payload["metric_basis"].pop("minimum_industry_peers", None)
        payload["metric_basis"].pop(
            "historical_industry_membership",
            None,
        )
        payload["capture"].pop("payload_hash")
        payload_hash = sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
        payload["capture"]["payload_hash"] = payload_hash
        self.connection.execute(
            """
            UPDATE stock_screener_scan_snapshots
            SET payload_hash = ?, payload = ?
            WHERE snapshot_id = ?
            """,
            [
                payload_hash,
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                snapshot_id,
            ],
        )

    def test_due_capture_replays_lightweight_template_and_is_idempotent(self) -> None:
        self.snapshot_service.capture(_payload())
        searcher = MagicMock(return_value={
            "snapshot": {
                "status": "captured",
                "snapshot_id": "auto-snapshot",
                "candidates_unique": 8,
            },
        })
        service = self._service(searcher=searcher)

        first = service.capture_due()
        second = service.capture_due()
        status = service.get_status()

        self.assertEqual(first["auto_capture_version"], AUTO_CAPTURE_VERSION)
        self.assertEqual(first["captured"][0]["snapshot_id"], "auto-snapshot")
        self.assertEqual(second["skipped"][0]["reason"], "already_captured")
        searcher.assert_called_once()
        arguments = searcher.call_args.kwargs
        self.assertTrue(arguments["capture_snapshot"])
        self.assertEqual(arguments["scan_pages"], 2)
        self.assertFalse(arguments["include_fundamentals"])
        self.assertNotIn("require_normal_trade_status", arguments["filters"])
        self.assertEqual(status["status_counts"], {"succeeded": 1})
        self.assertEqual(status["runs"][0]["snapshot_id"], "auto-snapshot")

    def test_legacy_template_claims_run_under_current_policy(self) -> None:
        captured = self.snapshot_service.capture(_payload())
        self._mark_snapshot_legacy(captured["snapshot_id"])
        legacy_policy_hash = self.snapshot_service.get_coverage(
            current_time=self.run_time,
        )["groups"][0]["policy_hash"]
        template = self.snapshot_service.get_auto_capture_templates()[0]
        searcher = MagicMock(return_value={
            "snapshot": {
                "status": "captured",
                "snapshot_id": "migrated-snapshot",
            },
        })

        result = self._service(searcher=searcher).capture_due()

        self.assertNotEqual(template["policy_hash"], legacy_policy_hash)
        self.assertEqual(
            result["captured"][0]["policy_hash"],
            template["policy_hash"],
        )
        stored_run = self.connection.execute(
            """
            SELECT policy_hash, source_snapshot_id
            FROM stock_screener_auto_capture_runs
            """
        ).fetchone()
        self.assertEqual(stored_run[0], template["policy_hash"])
        self.assertEqual(stored_run[1], captured["snapshot_id"])

    def test_legacy_migration_replays_once_per_day_under_current_policy(
        self,
    ) -> None:
        legacy = self.snapshot_service.capture(_payload())
        self._mark_snapshot_legacy(legacy["snapshot_id"])
        legacy_policy_hash = self.snapshot_service.get_coverage(
            current_time=self.run_time,
        )["groups"][0]["policy_hash"]
        current_time = [self.run_time]
        self.snapshot_service.clock = lambda: current_time[0]

        def search(**kwargs) -> dict:
            return {"snapshot": self.snapshot_service.capture(_payload())}

        searcher = MagicMock(side_effect=search)
        service = self._service(
            searcher=searcher,
            clock=lambda: current_time[0],
        )

        first = service.capture_due()
        current_time[0] += timedelta(days=1)
        second = service.capture_due()

        templates = self.snapshot_service.get_auto_capture_templates()
        coverage = self.snapshot_service.get_coverage(
            current_time=current_time[0],
        )
        current_policy_hash = next(
            group["policy_hash"]
            for group in coverage["groups"]
            if group["policy_hash"] != legacy_policy_hash
        )
        runs = service.get_status()["runs"]

        self.assertEqual(searcher.call_count, 2)
        self.assertEqual(len(first["captured"]), 1)
        self.assertEqual(len(second["captured"]), 1)
        self.assertEqual(len(coverage["groups"]), 2)
        self.assertEqual(len(templates), 1)
        self.assertEqual(templates[0]["policy_hash"], current_policy_hash)
        self.assertEqual(
            {run["policy_hash"] for run in runs},
            {current_policy_hash},
        )
        current_group = next(
            group
            for group in coverage["groups"]
            if group["policy_hash"] == current_policy_hash
        )
        self.assertEqual(current_group["raw_snapshot_count"], 2)

    def test_pre_close_and_heavy_templates_do_not_call_external_services(self) -> None:
        self.snapshot_service.capture(_payload(heavy=True))
        searcher = MagicMock()
        calendar = MagicMock(return_value=True)
        service = self._service(searcher=searcher, trading_day_loader=calendar)

        heavy = service.capture_due()

        self.assertEqual(
            heavy["skipped"][0]["reason"],
            "heavy_enrichment_not_supported",
        )
        searcher.assert_not_called()
        calendar.assert_not_called()

        self.connection.execute("DELETE FROM stock_screener_scan_snapshots")
        self.snapshot_service.capture(_payload())
        pre_close = self._service(
            searcher=searcher,
            trading_day_loader=calendar,
            clock=lambda: datetime(2026, 7, 24, 7, tzinfo=timezone.utc),
        ).capture_due()
        self.assertEqual(pre_close["skipped"][0]["reason"], "session_not_closed")
        calendar.assert_not_called()

    def test_failed_capture_is_sanitized_persisted_and_retryable(self) -> None:
        self.snapshot_service.capture(_payload())
        searcher = MagicMock(side_effect=[
            RuntimeError("token=secret https://example.test/private"),
            {
                "snapshot": {
                    "status": "captured",
                    "snapshot_id": "retry-snapshot",
                },
            },
        ])
        service = self._service(searcher=searcher)

        failed = service.capture_due()
        retried = service.capture_due()
        status = service.get_status()

        self.assertIn("token=[REDACTED]", failed["errors"][0]["error"])
        self.assertIn("[REDACTED_URL]", failed["errors"][0]["error"])
        self.assertEqual(retried["captured"][0]["snapshot_id"], "retry-snapshot")
        self.assertEqual(status["status_counts"], {"failed": 1, "succeeded": 1})

    def test_market_closed_and_calendar_failure_are_explicit(self) -> None:
        self.snapshot_service.capture(_payload())
        closed = self._service(
            trading_day_loader=lambda market, day: False
        ).capture_due()
        unavailable = self._service(
            trading_day_loader=MagicMock(side_effect=RuntimeError("calendar down"))
        ).capture_due()

        self.assertEqual(closed["skipped"][0]["reason"], "market_closed")
        self.assertEqual(
            unavailable["errors"][0]["reason"],
            "trading_calendar_unavailable",
        )

    def test_daily_failure_attempts_are_bounded(self) -> None:
        self.snapshot_service.capture(_payload())
        searcher = MagicMock(side_effect=RuntimeError("upstream unavailable"))
        service = self._service(searcher=searcher)

        results = [service.capture_due() for _ in range(4)]

        self.assertEqual(searcher.call_count, 3)
        self.assertEqual(
            results[-1]["skipped"][0]["reason"],
            "attempt_limit_reached",
        )
        self.assertEqual(service.get_status()["status_counts"], {"failed": 3})


class StockScreenerAutoCaptureRouteTest(unittest.TestCase):
    def test_static_status_route_precedes_snapshot_detail(self) -> None:
        capture_service = MagicMock()
        capture_service.get_status.return_value = {
            "auto_capture_version": AUTO_CAPTURE_VERSION,
            "max_daily_attempts": 3,
            "template_count": 1,
            "eligible_template_count": 1,
            "status_counts": {},
            "templates": [],
            "runs": [],
        }
        config_service = MagicMock()
        config_service.get_config.return_value = {
            "screener_auto_capture_enabled": False,
            "screener_auto_capture_poll_interval": 900,
        }
        with (
            patch(
                "app.routers.stock_picker."
                "get_stock_screener_auto_capture_service",
                return_value=capture_service,
            ),
            patch(
                "app.routers.stock_picker.get_stock_picker_service",
                return_value=config_service,
            ),
        ):
            client = TestClient(app)
            try:
                response = client.get(
                    "/api/stock-picker/screener/snapshots/auto-capture",
                    params={"limit": 5},
                )
            finally:
                client.close()

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["enabled"])
        self.assertEqual(response.json()["template_count"], 1)
        capture_service.get_status.assert_called_once_with(5)


if __name__ == "__main__":
    unittest.main()
