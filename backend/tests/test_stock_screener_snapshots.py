from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import unittest
from unittest.mock import MagicMock, patch

import duckdb
from fastapi.testclient import TestClient

from app.db import _run_migrations
from app.main import app
from app.stock_screener_snapshots import (
    COVERAGE_VERSION,
    FILTER_VERSION,
    LEGACY_SNAPSHOT_VERSION,
    LEGACY_RELATIVE_STRENGTH_VERSION,
    MIN_CALENDAR_SPAN_DAYS,
    MIN_CAPTURE_DATES,
    MIN_DISTINCT_SELECTED_SYMBOLS,
    MIN_DISTINCT_UNIVERSE_SYMBOLS,
    MIN_SELECTED_OBSERVATIONS,
    MIN_UNIVERSE_OBSERVATIONS,
    RELATIVE_STRENGTH_VERSION,
    SNAPSHOT_VERSION,
    StockScreenerSnapshotService,
)


class _ConnectionContext:
    def __init__(self, connection) -> None:
        self.connection = connection

    def __enter__(self):
        return self.connection

    def __exit__(self, exc_type, exc, traceback) -> bool:
        return False


def _payload() -> dict:
    return {
        "request": {
            "market": "US",
            "target_direction": "LONG",
            "benchmark_symbol": "SPY.US",
            "strategy": {
                "id": 101,
                "name": "Growth",
                "source": "recommended",
            },
            "filters": {
                "min_turnover": 150.0,
                "require_normal_trade_status": True,
            },
        },
        "scan": {
            "mode": "bounded",
            "first_page": 0,
            "last_page": 1,
            "pages_scanned": 2,
            "candidates_scanned": 3,
            "candidates_returned": 1,
            "duplicates_removed": 1,
        },
        "metric_basis": {
            "version": RELATIVE_STRENGTH_VERSION,
            "benchmark_symbol": "SPY.US",
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
            "before": 3,
            "after": 1,
            "excluded": 2,
            "reasons": {
                "below_min_turnover": 1,
                "duplicate_symbol": 1,
            },
        },
        "pages": [
            {
                "page": 0,
                "total": 3,
                "has_more": True,
                "candidate_count": 2,
                "symbols": ["AAA.US", "BBB.US"],
            },
            {
                "page": 1,
                "total": 3,
                "has_more": False,
                "candidate_count": 1,
                "symbols": ["BBB.US"],
            },
        ],
        "occurrences": [
            {
                "symbol": "AAA.US",
                "source_page": 0,
                "source_rank": 1,
                "retained": True,
                "scan_order": 1,
            },
            {
                "symbol": "BBB.US",
                "source_page": 0,
                "source_rank": 2,
                "retained": True,
                "scan_order": 2,
            },
            {
                "symbol": "BBB.US",
                "source_page": 1,
                "source_rank": 3,
                "retained": False,
                "duplicate_of_scan_order": 2,
            },
        ],
        "universe": [
            {
                "scan_order": 1,
                "source_page": 0,
                "selected": False,
                "exclusion_reason": "below_min_turnover",
                "candidate": {
                    "symbol": "AAA.US",
                    "indicators": {"industry": "Technology"},
                    "relative_strength": {"industry_peer_count": 2},
                },
            },
            {
                "scan_order": 2,
                "source_page": 0,
                "selected": True,
                "exclusion_reason": None,
                "candidate": {
                    "symbol": "BBB.US",
                    "indicators": {"industry": "Technology"},
                    "relative_strength": {"industry_peer_count": 2},
                },
            },
        ],
        "selected_symbols": ["BBB.US"],
    }


class StockScreenerSnapshotServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = duckdb.connect(":memory:")
        self.addCleanup(self.connection.close)
        _run_migrations(self.connection)
        _run_migrations(self.connection)
        self.service = StockScreenerSnapshotService(
            connection_factory=lambda: _ConnectionContext(self.connection),
            clock=lambda: datetime(
                2026,
                7,
                24,
                3,
                4,
                5,
                tzinfo=timezone.utc,
            ),
        )

    def _rewrite_payload(self, snapshot_id: str, mutate) -> None:
        stored = self.connection.execute(
            """
            SELECT payload
            FROM stock_screener_scan_snapshots
            WHERE snapshot_id = ?
            """,
            [snapshot_id],
        ).fetchone()[0]
        payload = json.loads(stored)
        mutate(payload)
        payload["capture"].pop("payload_hash", None)
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        payload_hash = sha256(canonical.encode("utf-8")).hexdigest()
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

    def test_capture_history_and_detail_preserve_versioned_payload(self) -> None:
        captured = self.service.capture(_payload())
        history = self.service.get_history(
            market="us",
            target_direction="long",
            strategy_id=101,
        )
        detail = self.service.get_snapshot(captured["snapshot_id"])

        self.assertEqual(captured["status"], "captured")
        self.assertEqual(captured["snapshot_version"], SNAPSHOT_VERSION)
        self.assertEqual(captured["candidates_unique"], 2)
        self.assertEqual(history["pagination"]["total"], 1)
        self.assertEqual(history["items"][0]["strategy_name"], "Growth")
        self.assertEqual(history["items"][0]["duplicates_removed"], 1)
        self.assertEqual(detail["payload"]["selected_symbols"], ["BBB.US"])
        self.assertEqual(
            detail["payload"]["capture"]["filter_version"],
            FILTER_VERSION,
        )
        self.assertEqual(
            detail["payload"]["capture"]["relative_strength_version"],
            RELATIVE_STRENGTH_VERSION,
        )
        hash_payload = json.loads(json.dumps(detail["payload"]))
        hash_payload["capture"].pop("payload_hash")
        canonical = json.dumps(
            hash_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        self.assertEqual(
            sha256(canonical.encode("utf-8")).hexdigest(),
            detail["payload_hash"],
        )

    def test_history_filters_paginates_and_missing_detail_is_explicit(self) -> None:
        self.service.capture(_payload())
        no_match = self.service.get_history(market="HK", limit=1, offset=0)

        self.assertEqual(no_match["items"], [])
        self.assertEqual(no_match["pagination"]["total"], 0)
        with self.assertRaises(KeyError):
            self.service.get_snapshot("missing")
        with self.assertRaises(ValueError):
            self.service.get_history(limit=101)

    def test_empty_coverage_is_not_ready(self) -> None:
        coverage = self.service.get_coverage(current_time=datetime(
            2026,
            7,
            24,
            tzinfo=timezone.utc,
        ))

        self.assertEqual(coverage["coverage_version"], COVERAGE_VERSION)
        self.assertEqual(coverage["raw_snapshot_count"], 0)
        self.assertEqual(coverage["groups"], [])
        self.assertFalse(coverage["ready_for_scope_evaluation"])
        self.assertFalse(coverage["market_environment"]["ready"])

    def test_coverage_deduplicates_market_dates_and_applies_readiness_gate(self) -> None:
        payload = _payload()
        payload["universe"] = [
            {
                "scan_order": index + 1,
                "source_page": 0,
                "selected": index < MIN_DISTINCT_SELECTED_SYMBOLS,
                "exclusion_reason": (
                    None if index < MIN_DISTINCT_SELECTED_SYMBOLS
                    else "below_min_turnover"
                ),
                "candidate": {
                    "symbol": f"S{index:02d}.US",
                    "indicators": {"industry": "Technology"},
                    "relative_strength": {"industry_peer_count": 30},
                },
            }
            for index in range(MIN_DISTINCT_UNIVERSE_SYMBOLS)
        ]
        payload["selected_symbols"] = [
            f"S{index:02d}.US"
            for index in range(MIN_DISTINCT_SELECTED_SYMBOLS)
        ]
        base = datetime(2026, 6, 1, 3, tzinfo=timezone.utc)
        offsets = list(range(MIN_CAPTURE_DATES - 1)) + [
            MIN_CALENDAR_SPAN_DAYS - 1
        ]
        for offset in offsets:
            StockScreenerSnapshotService(
                connection_factory=lambda: _ConnectionContext(self.connection),
                clock=lambda offset=offset: base + timedelta(days=offset),
            ).capture(payload)
        StockScreenerSnapshotService(
            connection_factory=lambda: _ConnectionContext(self.connection),
            clock=lambda: base + timedelta(days=offsets[-1], minutes=30),
        ).capture(payload)

        coverage = self.service.get_coverage(
            market="us",
            target_direction="long",
            strategy_id=101,
            current_time=base + timedelta(days=40),
        )
        group = coverage["groups"][0]

        self.assertEqual(coverage["raw_snapshot_count"], MIN_CAPTURE_DATES + 1)
        self.assertEqual(coverage["daily_snapshot_count"], MIN_CAPTURE_DATES)
        self.assertEqual(group["duplicate_same_day_count"], 1)
        self.assertEqual(group["capture_dates"], MIN_CAPTURE_DATES)
        self.assertEqual(group["calendar_span_days"], MIN_CALENDAR_SPAN_DAYS)
        self.assertEqual(
            group["distinct_universe_symbols"],
            MIN_DISTINCT_UNIVERSE_SYMBOLS,
        )
        self.assertEqual(
            group["distinct_selected_symbols"],
            MIN_DISTINCT_SELECTED_SYMBOLS,
        )
        self.assertGreaterEqual(
            group["universe_observations"],
            MIN_UNIVERSE_OBSERVATIONS,
        )
        self.assertGreaterEqual(
            group["selected_observations"],
            MIN_SELECTED_OBSERVATIONS,
        )
        self.assertEqual(group["first_capture_date"], "2026-05-31")
        self.assertTrue(group["ready"])
        self.assertTrue(coverage["ready_for_scope_evaluation"])
        self.assertTrue(group["market_environment"]["ready"])
        self.assertTrue(coverage["market_environment"]["ready"])

    def test_coverage_keeps_legacy_snapshot_valid_but_environment_missing(self) -> None:
        captured = self.service.capture(_payload())
        stored = self.connection.execute(
            "SELECT payload FROM stock_screener_scan_snapshots WHERE snapshot_id = ?",
            [captured["snapshot_id"]],
        ).fetchone()[0]
        legacy = json.loads(stored)
        legacy["capture"]["snapshot_version"] = LEGACY_SNAPSHOT_VERSION
        legacy["capture"].pop("payload_hash")
        legacy["metric_basis"].pop("benchmark_returns")
        legacy["metric_basis"].pop("benchmark_observations")
        canonical = json.dumps(
            legacy,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        payload_hash = sha256(canonical.encode("utf-8")).hexdigest()
        legacy["capture"]["payload_hash"] = payload_hash
        self.connection.execute(
            """
            UPDATE stock_screener_scan_snapshots
            SET snapshot_version = ?, payload_hash = ?, payload = ?
            WHERE snapshot_id = ?
            """,
            [
                LEGACY_SNAPSHOT_VERSION,
                payload_hash,
                json.dumps(
                    legacy,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                captured["snapshot_id"],
            ],
        )

        coverage = self.service.get_coverage(
            current_time=datetime(2026, 7, 25, tzinfo=timezone.utc),
        )
        group = coverage["groups"][0]

        self.assertEqual(group["snapshot_version"], LEGACY_SNAPSHOT_VERSION)
        self.assertEqual(group["integrity_rate"], 1.0)
        self.assertEqual(
            group["market_environment"]["missing_reasons"],
            {"legacy_snapshot_version": 1},
        )
        self.assertFalse(coverage["market_environment"]["ready"])

    def test_incomplete_benchmark_returns_are_valid_but_not_environment_ready(self) -> None:
        payload = _payload()
        payload["metric_basis"]["benchmark_returns"][
            "half_year_change_rate"
        ] = None
        for observation in payload["metric_basis"]["benchmark_observations"]:
            observation["half_year_change_rate"] = None
        self.service.capture(payload)

        coverage = self.service.get_coverage(
            current_time=datetime(2026, 7, 25, tzinfo=timezone.utc),
        )
        group = coverage["groups"][0]

        self.assertEqual(group["integrity_rate"], 1.0)
        self.assertEqual(
            group["market_environment"]["missing_reasons"],
            {"incomplete_benchmark_returns": 1},
        )
        self.assertFalse(group["market_environment"]["ready"])

    def test_coverage_rejects_benchmark_summary_mismatch(self) -> None:
        captured = self.service.capture(_payload())
        self._rewrite_payload(
            captured["snapshot_id"],
            lambda payload: payload["metric_basis"][
                "benchmark_returns"
            ].update({"ten_day_change_rate": 0.06}),
        )

        coverage = self.service.get_coverage(
            current_time=datetime(2026, 7, 25, tzinfo=timezone.utc),
        )
        group = coverage["groups"][0]

        self.assertEqual(group["integrity_rate"], 0.0)
        self.assertEqual(
            group["integrity_reasons"],
            {"benchmark_summary_mismatch": 1},
        )

    def test_coverage_rejects_benchmark_observation_page_mismatch(self) -> None:
        captured = self.service.capture(_payload())
        self._rewrite_payload(
            captured["snapshot_id"],
            lambda payload: payload["metric_basis"][
                "benchmark_observations"
            ][1].update({"page": 2}),
        )

        coverage = self.service.get_coverage(
            current_time=datetime(2026, 7, 25, tzinfo=timezone.utc),
        )
        group = coverage["groups"][0]

        self.assertEqual(group["integrity_rate"], 0.0)
        self.assertEqual(
            group["integrity_reasons"],
            {"benchmark_page_mismatch": 1},
        )

    def test_coverage_separates_policies_and_reports_tampered_payload(self) -> None:
        first = self.service.capture(_payload())
        second_payload = _payload()
        second_payload["request"]["filters"]["min_turnover"] = 999.0
        second = self.service.capture(second_payload)
        stored = self.connection.execute(
            "SELECT payload FROM stock_screener_scan_snapshots WHERE snapshot_id = ?",
            [second["snapshot_id"]],
        ).fetchone()[0]
        tampered = json.loads(stored)
        tampered["selected_symbols"] = []
        self.connection.execute(
            "UPDATE stock_screener_scan_snapshots SET payload = ? WHERE snapshot_id = ?",
            [json.dumps(tampered), second["snapshot_id"]],
        )

        coverage = self.service.get_coverage(
            current_time=datetime(2026, 7, 25, tzinfo=timezone.utc),
        )

        self.assertNotEqual(first["snapshot_id"], second["snapshot_id"])
        self.assertEqual(coverage["cohort_count"], 2)
        invalid_group = next(
            group for group in coverage["groups"]
            if group["integrity_rate"] == 0
        )
        self.assertEqual(
            invalid_group["integrity_reasons"],
            {
                "selection_mismatch": 1,
                "payload_hash_invalid": 1,
            },
        )
        self.assertIn(
            "snapshot_integrity_incomplete",
            invalid_group["not_ready_reasons"],
        )

    def test_coverage_separates_relative_strength_versions(self) -> None:
        current = self.service.capture(_payload())
        legacy = self.service.capture(_payload())

        def mark_legacy(payload: dict) -> None:
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

        self._rewrite_payload(legacy["snapshot_id"], mark_legacy)
        coverage = self.service.get_coverage(
            current_time=datetime(2026, 7, 25, tzinfo=timezone.utc),
        )

        self.assertNotEqual(current["snapshot_id"], legacy["snapshot_id"])
        self.assertEqual(coverage["cohort_count"], 2)
        self.assertEqual(
            {group["integrity_rate"] for group in coverage["groups"]},
            {1.0},
        )
        self.assertEqual(
            len({group["policy_hash"] for group in coverage["groups"]}),
            2,
        )

    def test_coverage_rejects_mismatched_relative_strength_basis(self) -> None:
        captured = self.service.capture(_payload())
        self._rewrite_payload(
            captured["snapshot_id"],
            lambda payload: payload["metric_basis"].update({
                "industry_basis": "scan_range_industry_median",
            }),
        )

        coverage = self.service.get_coverage(
            current_time=datetime(2026, 7, 25, tzinfo=timezone.utc),
        )

        self.assertEqual(
            coverage["groups"][0]["integrity_reasons"],
            {"relative_strength_basis_mismatch": 1},
        )

    def test_coverage_rejects_invalid_parameters(self) -> None:
        with self.assertRaises(ValueError):
            self.service.get_coverage(days=0)
        with self.assertRaises(ValueError):
            self.service.get_coverage(market="JP")
        with self.assertRaises(ValueError):
            self.service.get_coverage(target_direction="SIDEWAYS")
        with self.assertRaises(ValueError):
            self.service.get_coverage(strategy_id=0)


class StockScreenerSnapshotRouteTest(unittest.TestCase):
    def test_snapshot_history_and_detail_endpoints(self) -> None:
        service = MagicMock()
        service.get_history.return_value = {
            "snapshot_version": SNAPSHOT_VERSION,
            "items": [{"snapshot_id": "snapshot-1"}],
            "pagination": {"total": 1, "limit": 10, "offset": 0},
        }
        service.get_snapshot.side_effect = [
            {"snapshot_id": "snapshot-1", "payload": {}},
            KeyError("missing"),
        ]
        service.get_coverage.return_value = {
            "coverage_version": COVERAGE_VERSION,
            "groups": [],
            "ready_for_scope_evaluation": False,
        }
        with patch(
            "app.routers.stock_picker.get_stock_screener_snapshot_service",
            return_value=service,
        ):
            client = TestClient(app)
            try:
                history = client.get(
                    "/api/stock-picker/screener/snapshots",
                    params={
                        "market": "US",
                        "target_direction": "LONG",
                        "strategy_id": 101,
                        "limit": 10,
                    },
                )
                coverage = client.get(
                    "/api/stock-picker/screener/snapshots/coverage",
                    params={
                        "days": 180,
                        "market": "US",
                        "target_direction": "LONG",
                        "strategy_id": 101,
                    },
                )
                detail = client.get(
                    "/api/stock-picker/screener/snapshots/snapshot-1"
                )
                missing = client.get(
                    "/api/stock-picker/screener/snapshots/missing"
                )
            finally:
                client.close()

        self.assertEqual(history.status_code, 200)
        self.assertEqual(coverage.status_code, 200)
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(missing.status_code, 404)
        service.get_history.assert_called_once_with(
            market="US",
            target_direction="LONG",
            strategy_id=101,
            limit=10,
            offset=0,
        )
        service.get_coverage.assert_called_once_with(
            days=180,
            market="US",
            target_direction="LONG",
            strategy_id=101,
        )
