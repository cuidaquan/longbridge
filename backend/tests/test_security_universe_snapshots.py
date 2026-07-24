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
    compare_security_universe_snapshots,
    get_security_universe_classification,
    get_security_universe_classification_coverage,
    get_security_universe_tradeability,
    get_security_universe_tradeability_coverage,
    get_security_universe_snapshot,
    get_security_universe_snapshot_coverage,
    get_security_universe_snapshots,
    router,
)
from app.security_universe_snapshots import (
    CAPTURE_CLAIM_LEASE_MINUTES,
    CLASSIFICATION_COVERAGE_VERSION,
    CLASSIFICATION_SOURCE,
    CLASSIFICATION_VERSION,
    SNAPSHOT_COMPARISON_VERSION,
    SNAPSHOT_COVERAGE_VERSION,
    SNAPSHOT_SOURCE,
    SNAPSHOT_VERSION,
    TRADEABILITY_COVERAGE_VERSION,
    TRADEABILITY_SOURCE,
    TRADEABILITY_VERSION,
    SecurityUniverseSnapshotNotFoundError,
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


def _static_items(symbols):
    board_by_market = {
        "US": "USMain",
        "HK": "HKEquity",
        "CN": "SHMainConnect",
    }
    return [
        {
            "symbol": symbol,
            "board": board_by_market[symbol.rsplit(".", 1)[-1]],
            "board_raw": board_by_market[symbol.rsplit(".", 1)[-1]],
            "exchange": symbol.rsplit(".", 1)[-1],
            "currency": {
                "US": "USD",
                "HK": "HKD",
                "CN": "CNY",
            }[symbol.rsplit(".", 1)[-1]],
            "lot_size": 1,
        }
        for symbol in symbols
    ]


def _tradeability_items(symbols, include_depth=False):
    if include_depth:
        raise AssertionError("目录交易状态快照不得请求盘口深度")
    return {
        symbol: {
            "status": "available",
            "error": None,
            "trade_status": "normal",
            "is_tradable": True,
            "last_done": 100.0,
            "volume": 1000.0,
            "turnover": 100000.0,
            "data_as_of": "2026-07-24T11:59:59+00:00",
        }
        for symbol in symbols
    }


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
            static_info_loader=kwargs.get(
                "static_info_loader",
                _static_items,
            ),
            static_info_batch_size=kwargs.get(
                "static_info_batch_size",
                500,
            ),
            tradeability_loader=kwargs.get(
                "tradeability_loader",
                _tradeability_items,
            ),
            quote_batch_size=kwargs.get("quote_batch_size", 500),
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
        classification_columns = {
            row[1]
            for row in self.connection.execute(
                "PRAGMA table_info("
                "'security_universe_classification_snapshots')"
            ).fetchall()
        }
        self.assertEqual(
            classification_columns,
            {
                "classification_snapshot_id",
                "source_snapshot_id",
                "captured_at",
                "observation_date",
                "classification_version",
                "market",
                "source_snapshot_version",
                "security_count",
                "classified_count",
                "resolved_board_count",
                "eligible_count",
                "ready_for_research_universe",
                "payload_hash",
                "payload",
            },
        )
        tradeability_columns = {
            row[1]
            for row in self.connection.execute(
                "PRAGMA table_info("
                "'security_universe_tradeability_snapshots')"
            ).fetchall()
        }
        self.assertEqual(
            tradeability_columns,
            {
                "tradeability_snapshot_id",
                "source_snapshot_id",
                "classification_snapshot_id",
                "captured_at",
                "observation_date",
                "tradeability_version",
                "market",
                "source_snapshot_version",
                "classification_version",
                "eligible_count",
                "observed_count",
                "tradable_count",
                "excluded_count",
                "ready_for_point_in_time_universe",
                "payload_hash",
                "payload",
            },
        )

    def test_capture_is_canonical_integrity_checked_and_idempotent(self) -> None:
        loader = MagicMock(side_effect=lambda market: _items(market))
        quote_loader = MagicMock(side_effect=_tradeability_items)
        service = self._service(
            catalog_loader=loader,
            tradeability_loader=quote_loader,
        )

        first = service.capture_market("us")
        second = service.capture_market("US")
        detail = service.get_snapshot(first["snapshot_id"])

        self.assertEqual(first["status"], "captured")
        self.assertTrue(first["persisted"])
        self.assertTrue(
            first["classification"]["ready_for_research_universe"]
        )
        self.assertEqual(second["status"], "already_captured")
        self.assertFalse(second["persisted"])
        self.assertEqual(first["snapshot_id"], second["snapshot_id"])
        loader.assert_called_once_with("US")
        quote_loader.assert_called_once_with(
            ["AAA.US", "BBB.US"],
            include_depth=False,
        )
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
        ), patch.object(
            service,
            "capture_classification",
            return_value={
                "classification_snapshot_id": "classification-1",
                "status": "already_classified",
                "persisted": False,
            },
        ):
            result = service.capture_market("US")

        self.assertEqual(
            result,
            {
                **existing,
                "status": "already_captured",
                "persisted": False,
                "classification": {
                    "classification_snapshot_id": "classification-1",
                    "status": "already_classified",
                    "persisted": False,
                },
                "tradeability": {
                    "status": "blocked",
                    "persisted": False,
                    "reason": "classification_not_ready",
                },
            },
        )

    def test_classification_batches_normalizes_and_persists_policy(self) -> None:
        source_items = [
            {
                "symbol": f"{symbol}.US",
                "name": symbol,
                "name_en": symbol,
                "name_hk": "",
                "market": "US",
            }
            for symbol in ("AAA", "BBB", "CCC")
        ]
        batches = []

        def load_static_info(symbols):
            batches.append(list(symbols))
            return [
                {
                    "symbol": symbol.lower(),
                    "board": "SecurityBoard.USMain",
                    "board_raw": "USMain",
                    "exchange": "NASDAQ",
                    "currency": "usd",
                    "lot_size": "100",
                }
                for symbol in reversed(symbols)
            ]

        service = self._service(
            catalog_loader=lambda market: source_items,
            static_info_loader=load_static_info,
            static_info_batch_size=2,
        )
        with patch(
            "app.security_universe_snapshots.run_external_call",
            side_effect=self._call_directly,
        ):
            capture = service.capture_market("US")

        classification = service.get_classification(capture["snapshot_id"])
        self.assertEqual(
            batches,
            [["AAA.US", "BBB.US"], ["CCC.US"]],
        )
        self.assertTrue(classification["integrity_valid"])
        self.assertTrue(classification["ready_for_research_universe"])
        self.assertEqual(classification["eligible_count"], 3)
        self.assertEqual(
            classification["payload"]["source_request"]["batch_size"],
            2,
        )
        self.assertEqual(
            classification["payload"]["policy"]["does_not_prove"],
            [
                "ordinary_stock_or_etf_type",
                "current_trade_status",
                "account_permission",
                "liquidity",
            ],
        )
        self.assertEqual(
            classification["payload"]["items"][0],
            {
                "symbol": "AAA.US",
                "static_info_status": "available",
                "board": "usmain",
                "board_raw": "USMain",
                "exchange": "NASDAQ",
                "currency": "USD",
                "lot_size": 100,
                "board_category": "listed_equity_board",
                "research_eligible": True,
                "exclusion_reason": None,
            },
        )

    def test_classification_gate_fails_closed_for_incomplete_or_unknown(self) -> None:
        source_items = [
            {
                "symbol": f"{symbol}.US",
                "name": symbol,
                "name_en": symbol,
                "name_hk": "",
                "market": "US",
            }
            for symbol in ("ELIGIBLE", "EXCLUDED", "MISSING", "UNKNOWN", "MISMATCH")
        ]

        def load_static_info(symbols):
            boards = {
                "ELIGIBLE.US": "USMain",
                "EXCLUDED.US": "USPink",
                "UNKNOWN.US": "Unknown",
                "MISMATCH.US": "HKEquity",
            }
            return [
                {"symbol": symbol, "board": board}
                for symbol, board in boards.items()
            ]

        service = self._service(
            catalog_loader=lambda market: source_items,
            static_info_loader=load_static_info,
        )
        with patch(
            "app.security_universe_snapshots.run_external_call",
            side_effect=self._call_directly,
        ):
            capture = service.capture_market("US")
        detail = service.get_classification(capture["snapshot_id"])

        self.assertFalse(detail["ready_for_research_universe"])
        self.assertEqual(detail["classified_count"], 4)
        self.assertEqual(detail["resolved_board_count"], 2)
        self.assertEqual(detail["eligible_count"], 1)
        self.assertEqual(
            detail["payload"]["readiness_reasons"],
            [
                "incomplete_static_info",
                "unknown_board",
                "board_market_mismatch",
            ],
        )
        self.assertEqual(
            detail["payload"]["exclusion_counts"],
            {
                "board_market_mismatch": 1,
                "missing_static_info": 1,
                "otc_board": 1,
                "unknown_board": 1,
            },
        )
        self.assertTrue(detail["integrity_valid"])

    def test_known_excluded_board_does_not_block_classification_readiness(
        self,
    ) -> None:
        service = self._service(
            static_info_loader=lambda symbols: [
                {"symbol": symbols[0], "board": "USMain"},
                {"symbol": symbols[1], "board": "USPink"},
            ],
        )
        with patch(
            "app.security_universe_snapshots.run_external_call",
            side_effect=self._call_directly,
        ):
            capture = service.capture_market("US")
        detail = service.get_classification(capture["snapshot_id"])

        self.assertTrue(detail["ready_for_research_universe"])
        self.assertEqual(detail["eligible_count"], 1)
        self.assertEqual(detail["payload"]["readiness_reasons"], [])
        self.assertEqual(
            detail["payload"]["exclusion_counts"],
            {"otc_board": 1},
        )

    def test_classification_is_idempotent_and_historical_batch_is_valid(
        self,
    ) -> None:
        loader = MagicMock(side_effect=_static_items)
        service = self._service(
            static_info_loader=loader,
            static_info_batch_size=1,
        )
        with patch(
            "app.security_universe_snapshots.run_external_call",
            side_effect=self._call_directly,
        ):
            capture = service.capture_market("US")
            repeated = service.capture_classification(capture["snapshot_id"])

        reader = self._service(static_info_batch_size=500)
        detail = reader.get_classification(capture["snapshot_id"])
        self.assertEqual(loader.call_count, 2)
        self.assertEqual(repeated["status"], "already_classified")
        self.assertFalse(repeated["persisted"])
        self.assertTrue(detail["integrity_valid"])
        self.assertEqual(
            detail["payload"]["source_request"]["batch_size"],
            1,
        )

    def test_classification_integrity_detects_payload_and_source_tampering(
        self,
    ) -> None:
        service = self._service()
        capture = service.capture_market("US")
        source_id = capture["snapshot_id"]
        original = service.get_classification(source_id)

        self.connection.execute(
            """
            UPDATE security_universe_classification_snapshots
            SET classified_count = 1
            WHERE source_snapshot_id = ?
            """,
            [source_id],
        )
        metadata_tampered = service.get_classification(source_id)
        self.assertFalse(metadata_tampered["integrity_valid"])
        self.assertIn(
            "classification_metadata_count_mismatch",
            metadata_tampered["integrity_errors"],
        )
        self.connection.execute(
            """
            UPDATE security_universe_classification_snapshots
            SET classified_count = 2
            WHERE source_snapshot_id = ?
            """,
            [source_id],
        )

        self.connection.execute(
            """
            UPDATE security_universe_classification_snapshots
            SET payload = ?
            WHERE source_snapshot_id = ?
            """,
            ["{broken", source_id],
        )
        broken = service.get_classification(source_id)
        self.assertEqual(broken["integrity_errors"], ["invalid_payload_json"])

        self.connection.execute(
            """
            UPDATE security_universe_classification_snapshots
            SET payload = ?
            WHERE source_snapshot_id = ?
            """,
            [
                json.dumps(original["payload"], ensure_ascii=False),
                source_id,
            ],
        )
        self.connection.execute(
            """
            UPDATE security_universe_snapshots
            SET payload = ?
            WHERE snapshot_id = ?
            """,
            ["{broken", source_id],
        )
        source_tampered = service.get_classification(source_id)
        self.assertFalse(source_tampered["integrity_valid"])
        self.assertIn(
            "source_snapshot_integrity_invalid",
            source_tampered["integrity_errors"],
        )

    def test_classification_coverage_reports_missing_and_ready_snapshots(
        self,
    ) -> None:
        first = self._service(
            clock=lambda: datetime(2026, 7, 23, 12, tzinfo=timezone.utc),
        ).capture_market("HK")
        latest = self._service().capture_market("HK")
        self.connection.execute(
            """
            DELETE FROM security_universe_classification_snapshots
            WHERE source_snapshot_id = ?
            """,
            [first["snapshot_id"]],
        )

        coverage = self._service().get_classification_coverage("HK")
        item = coverage["markets"][0]
        self.assertEqual(
            coverage["coverage_version"],
            CLASSIFICATION_COVERAGE_VERSION,
        )
        self.assertEqual(coverage["source"], CLASSIFICATION_SOURCE)
        self.assertEqual(item["source_snapshot_count"], 2)
        self.assertEqual(item["classification_snapshot_count"], 1)
        self.assertEqual(item["unclassified_snapshot_count"], 1)
        self.assertEqual(item["ready_observation_dates"], 1)
        self.assertEqual(
            item["latest"]["source_snapshot_id"],
            latest["snapshot_id"],
        )
        self.assertFalse(item["payload_integrity_checked"])

    def test_concurrent_classification_insert_returns_persisted_metadata(
        self,
    ) -> None:
        service = self._service()
        capture = service.capture_market("US")
        source_id = capture["snapshot_id"]
        self.connection.execute(
            """
            DELETE FROM security_universe_classification_snapshots
            WHERE source_snapshot_id = ?
            """,
            [source_id],
        )
        existing = {
            "classification_snapshot_id": "existing-classification",
            "source_snapshot_id": source_id,
            "captured_at": "2026-07-24T12:00:00+00:00",
            "observation_date": "2026-07-24",
            "classification_version": CLASSIFICATION_VERSION,
            "market": "US",
            "source_snapshot_version": SNAPSHOT_VERSION,
            "security_count": 2,
            "classified_count": 2,
            "resolved_board_count": 2,
            "eligible_count": 2,
            "ready_for_research_universe": True,
            "payload_hash": "existing-hash",
        }
        with patch.object(
            service,
            "_find_classification_summary",
            side_effect=[None, existing],
        ), patch.object(
            service,
            "_save_classification",
            return_value=("existing-classification", False),
        ):
            result = service.capture_classification(source_id)

        self.assertEqual(
            result,
            {
                **existing,
                "status": "already_classified",
                "persisted": False,
            },
        )

    def test_tradeability_queries_only_research_candidates_in_batches(self) -> None:
        source_items = [
            {
                "symbol": f"{index:04d}.US",
                "name": str(index),
                "name_en": "",
                "name_hk": "",
                "market": "US",
            }
            for index in range(1002)
        ]
        quote_batches = []

        def static_loader(symbols):
            return [
                {
                    "symbol": symbol,
                    "board": "USPink" if symbol == "1001.US" else "USMain",
                }
                for symbol in symbols
            ]

        def quote_loader(symbols, include_depth=False):
            self.assertFalse(include_depth)
            quote_batches.append(list(symbols))
            return _tradeability_items(symbols, include_depth=include_depth)

        service = self._service(
            catalog_loader=lambda market: source_items,
            static_info_loader=static_loader,
            tradeability_loader=quote_loader,
            quote_batch_size=500,
        )
        with patch(
            "app.security_universe_snapshots.run_external_call",
            side_effect=self._call_directly,
        ):
            capture = service.capture_market("US")

        detail = service.get_tradeability(capture["snapshot_id"])
        self.assertEqual([len(batch) for batch in quote_batches], [500, 500, 1])
        self.assertNotIn("1001.US", {item for batch in quote_batches for item in batch})
        self.assertTrue(detail["integrity_valid"])
        self.assertTrue(detail["ready_for_point_in_time_universe"])
        self.assertEqual(detail["eligible_count"], 1001)
        self.assertEqual(detail["tradable_count"], 1001)
        self.assertEqual(
            detail["payload"]["source_request"],
            {
                "method": "SDK",
                "operation": "QuoteContext.quote",
                "batch_size": 500,
                "include_depth": False,
            },
        )
        self.assertEqual(
            detail["payload"]["policy"]["data_as_of_semantics"],
            "upstream_quote_timestamp",
        )
        self.assertNotEqual(
            detail["payload"]["items"][0]["data_as_of"],
            detail["captured_at"],
        )

    def test_tradeability_known_exclusions_and_fail_closed_gaps(self) -> None:
        symbols = ("NORMAL", "HALTED", "SUSPEND", "UNKNOWN", "MISSING")
        source_items = [
            {
                "symbol": f"{symbol}.US",
                "name": symbol,
                "name_en": "",
                "name_hk": "",
                "market": "US",
            }
            for symbol in symbols
        ]

        def quote_loader(requested, include_depth=False):
            self.assertFalse(include_depth)
            statuses = {
                "NORMAL.US": "Normal",
                "HALTED.US": "Halted",
                "SUSPEND.US": "Suspend",
                "UNKNOWN.US": "UnexpectedStatus",
            }
            return {
                symbol: {
                    "status": "available",
                    "trade_status": status,
                    "data_as_of": "2020-01-01T00:00:00Z",
                }
                for symbol, status in statuses.items()
            }

        service = self._service(
            catalog_loader=lambda market: source_items,
            tradeability_loader=quote_loader,
        )
        with patch(
            "app.security_universe_snapshots.run_external_call",
            side_effect=self._call_directly,
        ):
            capture = service.capture_market("US")
        detail = service.get_tradeability(capture["snapshot_id"])

        self.assertTrue(detail["integrity_valid"])
        self.assertFalse(detail["ready_for_point_in_time_universe"])
        self.assertEqual(detail["observed_count"], 4)
        self.assertEqual(detail["tradable_count"], 1)
        self.assertEqual(detail["excluded_count"], 2)
        self.assertEqual(
            detail["payload"]["readiness_reasons"],
            ["incomplete_quote_coverage", "unknown_trade_status"],
        )
        by_symbol = {
            item["symbol"]: item for item in detail["payload"]["items"]
        }
        self.assertEqual(
            by_symbol["HALTED.US"]["exclusion_reason"],
            "trade_status_halted",
        )
        self.assertEqual(
            by_symbol["MISSING.US"]["exclusion_reason"],
            "quote_no_data",
        )
        self.assertFalse(by_symbol["UNKNOWN.US"]["point_in_time_eligible"])

    def test_tradeability_refuses_unready_or_tampered_classification(self) -> None:
        quote_loader = MagicMock()
        service = self._service(
            static_info_loader=lambda symbols: [],
            tradeability_loader=quote_loader,
        )
        capture = service.capture_market("US")
        self.assertEqual(capture["tradeability"]["status"], "blocked")
        quote_loader.assert_not_called()
        with self.assertRaisesRegex(ValueError, "研究候选门禁未通过"):
            service.capture_tradeability(capture["snapshot_id"])

        healthy = self._service().capture_market("HK")
        self.connection.execute(
            "DELETE FROM security_universe_tradeability_snapshots "
            "WHERE source_snapshot_id = ?",
            [healthy["snapshot_id"]],
        )
        self.connection.execute(
            "UPDATE security_universe_classification_snapshots "
            "SET payload_hash = 'tampered' WHERE source_snapshot_id = ?",
            [healthy["snapshot_id"]],
        )
        with self.assertRaisesRegex(ValueError, "完整性校验失败"):
            self._service().capture_tradeability(healthy["snapshot_id"])

    def test_tradeability_integrity_and_coverage(self) -> None:
        first = self._service(
            clock=lambda: datetime(2026, 7, 23, 12, tzinfo=timezone.utc),
        ).capture_market("HK")
        latest = self._service().capture_market("HK")
        self.connection.execute(
            "DELETE FROM security_universe_tradeability_snapshots "
            "WHERE source_snapshot_id = ?",
            [first["snapshot_id"]],
        )
        service = self._service()
        coverage = service.get_tradeability_coverage("HK")
        item = coverage["markets"][0]
        self.assertEqual(
            coverage["coverage_version"],
            TRADEABILITY_COVERAGE_VERSION,
        )
        self.assertEqual(coverage["source"], TRADEABILITY_SOURCE)
        self.assertEqual(item["classification_snapshot_count"], 2)
        self.assertEqual(item["tradeability_snapshot_count"], 1)
        self.assertEqual(item["missing_tradeability_snapshot_count"], 1)
        self.assertEqual(
            item["latest"]["source_snapshot_id"], latest["snapshot_id"]
        )

        original = service.get_tradeability(latest["snapshot_id"])
        self.connection.execute(
            "UPDATE security_universe_tradeability_snapshots "
            "SET tradable_count = 0 WHERE source_snapshot_id = ?",
            [latest["snapshot_id"]],
        )
        metadata_tampered = service.get_tradeability(latest["snapshot_id"])
        self.assertIn(
            "tradeability_metadata_count_mismatch",
            metadata_tampered["integrity_errors"],
        )
        self.connection.execute(
            "UPDATE security_universe_tradeability_snapshots "
            "SET tradable_count = 2, payload = ? WHERE source_snapshot_id = ?",
            [json.dumps(original["payload"]), latest["snapshot_id"]],
        )
        self.connection.execute(
            "UPDATE security_universe_classification_snapshots "
            "SET payload_hash = 'tampered' WHERE source_snapshot_id = ?",
            [latest["snapshot_id"]],
        )
        reference_tampered = service.get_tradeability(latest["snapshot_id"])
        self.assertIn(
            "classification_snapshot_integrity_invalid",
            reference_tampered["integrity_errors"],
        )
        self.assertIn(
            "classification_snapshot_hash_mismatch",
            reference_tampered["integrity_errors"],
        )

    def test_concurrent_tradeability_insert_returns_persisted_metadata(
        self,
    ) -> None:
        service = self._service()
        capture = service.capture_market("US")
        detail = service.get_tradeability(capture["snapshot_id"])
        existing = {
            key: detail[key]
            for key in (
                "tradeability_snapshot_id",
                "source_snapshot_id",
                "classification_snapshot_id",
                "captured_at",
                "observation_date",
                "tradeability_version",
                "market",
                "source_snapshot_version",
                "classification_version",
                "eligible_count",
                "observed_count",
                "tradable_count",
                "excluded_count",
                "ready_for_point_in_time_universe",
                "payload_hash",
            )
        }
        self.connection.execute(
            "DELETE FROM security_universe_tradeability_snapshots "
            "WHERE source_snapshot_id = ?",
            [capture["snapshot_id"]],
        )
        with patch.object(
            service,
            "_find_tradeability_summary",
            side_effect=[None, existing],
        ), patch.object(
            service,
            "_save_tradeability",
            return_value=(existing["tradeability_snapshot_id"], False),
        ):
            result = service.capture_tradeability(capture["snapshot_id"])

        self.assertEqual(
            result,
            {
                **existing,
                "status": "already_captured",
                "persisted": False,
            },
        )

    def test_failed_tradeability_retries_only_tradeability(self) -> None:
        catalog_loader = MagicMock(side_effect=lambda market: _items(market))
        static_loader = MagicMock(side_effect=_static_items)
        attempts = 0

        def quote_loader(symbols, include_depth=False):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("quote failed")
            return _tradeability_items(symbols, include_depth=include_depth)

        service = self._service(
            catalog_loader=catalog_loader,
            static_info_loader=static_loader,
            tradeability_loader=quote_loader,
        )
        with patch(
            "app.security_universe_snapshots.run_external_call",
            side_effect=self._call_directly,
        ):
            first = service.capture_due()
            second = service.capture_due()

        self.assertEqual(first["captured"], [])
        self.assertIn("quote failed", first["errors"][0]["error"])
        self.assertEqual(second["captured"][0]["status"], "tradeability_captured")
        self.assertEqual(catalog_loader.call_count, 1)
        self.assertEqual(static_loader.call_count, 1)
        self.assertEqual(attempts, 2)
        counts = self.connection.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM security_universe_snapshots),
                (SELECT COUNT(*) FROM security_universe_classification_snapshots),
                (SELECT COUNT(*) FROM security_universe_tradeability_snapshots)
            """
        ).fetchone()
        self.assertEqual(counts, (1, 1, 1))

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

    def test_coverage_reports_saved_dates_and_filters_run_market(self) -> None:
        empty = self._service().get_coverage()
        self.assertEqual(
            [item["market"] for item in empty["markets"]],
            ["US", "HK", "CN"],
        )
        self.assertTrue(all(
            item["snapshot_count"] == 0 for item in empty["markets"]
        ))

        first = self._service(
            clock=lambda: datetime(
                2026, 7, 22, 12, tzinfo=timezone.utc
            ),
        ).capture_market("HK")
        latest = self._service(
            clock=lambda: datetime(
                2026, 7, 24, 12, tzinfo=timezone.utc
            ),
        ).capture_market("HK")
        service = self._service()
        now = datetime(2026, 7, 24, 12, tzinfo=timezone.utc)
        service._claim_capture("US", date(2026, 7, 24), now)
        service._claim_capture("HK", date(2026, 7, 24), now)

        coverage = service.get_coverage("HK")
        item = coverage["markets"][0]
        us_history = service.get_history("US")

        self.assertEqual(
            coverage["coverage_version"],
            SNAPSHOT_COVERAGE_VERSION,
        )
        self.assertEqual(item["snapshot_count"], 2)
        self.assertEqual(item["observation_dates"], 2)
        self.assertEqual(item["first_observation_date"], "2026-07-22")
        self.assertEqual(item["latest_observation_date"], "2026-07-24")
        self.assertEqual(item["calendar_span_days"], 3)
        self.assertEqual(item["comparable_transitions"], 1)
        self.assertEqual(item["latest_interval_calendar_days"], 2)
        self.assertEqual(item["maximum_interval_calendar_days"], 2)
        self.assertEqual(item["latest_snapshot_id"], latest["snapshot_id"])
        self.assertEqual(item["latest_security_count"], 2)
        self.assertFalse(item["payload_integrity_checked"])
        self.assertNotEqual(first["snapshot_id"], latest["snapshot_id"])
        self.assertEqual(
            {run["market"] for run in us_history["capture_runs"]},
            {"US"},
        )
        with self.assertRaisesRegex(ValueError, "US、HK 或 CN"):
            service.get_coverage("SG")

    def test_comparison_reports_changes_counts_and_bounded_details(self) -> None:
        base = self._service(
            clock=lambda: datetime(
                2026, 7, 23, 12, tzinfo=timezone.utc
            ),
        ).capture_market("HK")
        target_items = [
            {
                "symbol": "AAA.HK",
                "name": "Alpha renamed",
                "name_en": "Alpha Holdings",
                "name_hk": "",
                "market": "HK",
            },
            {
                "symbol": "CCC.HK",
                "name": "Gamma",
                "name_en": "Gamma Inc.",
                "name_hk": "",
                "market": "HK",
            },
            {
                "symbol": "DDD.HK",
                "name": "Delta",
                "name_en": "Delta Inc.",
                "name_hk": "",
                "market": "HK",
            },
        ]
        service = self._service(
            catalog_loader=lambda market: target_items,
            clock=lambda: datetime(
                2026, 7, 24, 12, tzinfo=timezone.utc
            ),
        )
        target = service.capture_market("HK")

        result = service.compare_snapshots(
            base["snapshot_id"],
            target["snapshot_id"],
            detail_limit=1,
        )
        unchanged = service.compare_snapshots(
            target["snapshot_id"],
            target["snapshot_id"],
        )

        self.assertEqual(
            result["comparison_version"],
            SNAPSHOT_COMPARISON_VERSION,
        )
        self.assertTrue(result["ready"])
        self.assertEqual(result["reasons"], [])
        self.assertEqual(result["added_count"], 2)
        self.assertEqual(result["removed_count"], 1)
        self.assertEqual(result["metadata_changed_count"], 1)
        self.assertEqual(result["added"][0]["symbol"], "CCC.HK")
        self.assertEqual(result["removed"][0]["symbol"], "BBB.HK")
        self.assertEqual(
            result["metadata_changed"][0]["changes"]["name"],
            {"before": "Alpha", "after": "Alpha renamed"},
        )
        self.assertTrue(result["added_truncated"])
        self.assertFalse(result["removed_truncated"])
        self.assertFalse(result["metadata_changed_truncated"])
        self.assertTrue(result["base"]["integrity_valid"])
        self.assertTrue(result["target"]["integrity_valid"])
        self.assertTrue(unchanged["ready"])
        self.assertEqual(unchanged["added_count"], 0)
        self.assertEqual(unchanged["removed_count"], 0)
        self.assertEqual(unchanged["metadata_changed_count"], 0)

    def test_comparison_fails_closed_for_invalid_or_cross_market_data(self) -> None:
        base = self._service(
            clock=lambda: datetime(
                2026, 7, 23, 12, tzinfo=timezone.utc
            ),
        ).capture_market("HK")
        service = self._service()
        target = service.capture_market("HK")
        us = service.capture_market("US")

        cross_market = service.compare_snapshots(
            target["snapshot_id"],
            us["snapshot_id"],
        )
        self.connection.execute(
            """
            UPDATE security_universe_snapshots
            SET payload = ?
            WHERE snapshot_id = ?
            """,
            ["{broken", base["snapshot_id"]],
        )
        invalid = service.compare_snapshots(
            base["snapshot_id"],
            target["snapshot_id"],
        )

        self.assertFalse(cross_market["ready"])
        self.assertEqual(cross_market["reasons"], ["market_mismatch"])
        self.assertIsNone(cross_market["added_count"])
        self.assertFalse(invalid["ready"])
        self.assertEqual(
            invalid["reasons"],
            ["base_snapshot_integrity_invalid"],
        )
        self.assertIsNone(invalid["removed_count"])
        with self.assertRaisesRegex(ValueError, "1～1000"):
            service.compare_snapshots(
                target["snapshot_id"],
                target["snapshot_id"],
                detail_limit=0,
            )
        with self.assertRaises(SecurityUniverseSnapshotNotFoundError):
            service.compare_snapshots(
                "missing",
                target["snapshot_id"],
            )

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

    def test_failed_classification_retries_without_refreshing_catalog(
        self,
    ) -> None:
        catalog_loader = MagicMock(side_effect=lambda market: _items(market))
        attempts = 0

        def load_static_info(symbols):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("static info failed")
            return _static_items(symbols)

        static_loader = MagicMock(side_effect=load_static_info)
        service = self._service(
            catalog_loader=catalog_loader,
            static_info_loader=static_loader,
        )
        with patch(
            "app.security_universe_snapshots.run_external_call",
            side_effect=self._call_directly,
        ):
            first = service.capture_due()
            second = service.capture_due()

        raw_count = self.connection.execute(
            "SELECT COUNT(*) FROM security_universe_snapshots"
        ).fetchone()[0]
        classification_count = self.connection.execute(
            "SELECT COUNT(*) FROM security_universe_classification_snapshots"
        ).fetchone()[0]
        self.assertEqual(first["captured"], [])
        self.assertIn("static info failed", first["errors"][0]["error"])
        self.assertEqual(len(second["captured"]), 1)
        self.assertEqual(
            second["captured"][0]["status"],
            "classification_captured",
        )
        self.assertTrue(
            second["captured"][0]["classification"][
                "ready_for_research_universe"
            ]
        )
        self.assertEqual(raw_count, 1)
        self.assertEqual(classification_count, 1)
        catalog_loader.assert_called_once_with("HK")
        self.assertEqual(static_loader.call_count, 2)

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
        service.get_coverage.return_value = {
            "coverage_version": SNAPSHOT_COVERAGE_VERSION,
            "markets": [{"market": "US", "snapshot_count": 1}],
        }
        service.get_classification_coverage.return_value = {
            "coverage_version": CLASSIFICATION_COVERAGE_VERSION,
            "markets": [{"market": "US", "classification_snapshot_count": 1}],
        }
        service.get_classification.side_effect = [
            {
                "source_snapshot_id": "snapshot-1",
                "integrity_valid": True,
            },
            None,
        ]
        service.get_tradeability_coverage.return_value = {
            "coverage_version": TRADEABILITY_COVERAGE_VERSION,
            "markets": [{"market": "US", "tradeability_snapshot_count": 1}],
        }
        service.get_tradeability.side_effect = [
            {
                "source_snapshot_id": "snapshot-1",
                "integrity_valid": True,
            },
            None,
        ]
        service.compare_snapshots.return_value = {
            "comparison_version": SNAPSHOT_COMPARISON_VERSION,
            "ready": True,
            "added_count": 1,
        }
        with patch(
            "app.routers.stock_picker."
            "get_security_universe_snapshot_service",
            return_value=service,
        ):
            history = asyncio.run(
                get_security_universe_snapshots("US", 5)
            )
            coverage = asyncio.run(
                get_security_universe_snapshot_coverage("US")
            )
            classification_coverage = asyncio.run(
                get_security_universe_classification_coverage("US")
            )
            tradeability_coverage = asyncio.run(
                get_security_universe_tradeability_coverage("US")
            )
            comparison = asyncio.run(
                compare_security_universe_snapshots(
                    "snapshot-0",
                    "snapshot-1",
                    25,
                )
            )
            detail = asyncio.run(
                get_security_universe_snapshot("snapshot-1")
            )
            classification = asyncio.run(
                get_security_universe_classification("snapshot-1")
            )
            tradeability = asyncio.run(
                get_security_universe_tradeability("snapshot-1")
            )
            with self.assertRaises(HTTPException) as raised:
                asyncio.run(get_security_universe_snapshot("missing"))
            with self.assertRaises(HTTPException) as classification_raised:
                asyncio.run(
                    get_security_universe_classification("missing")
                )
            with self.assertRaises(HTTPException) as tradeability_raised:
                asyncio.run(get_security_universe_tradeability("missing"))

        paths = [route.path for route in router.routes]
        self.assertIn(
            "/api/stock-picker/security-universe-snapshots",
            paths,
        )
        self.assertIn(
            "/api/stock-picker/security-universe-snapshots/coverage",
            paths,
        )
        self.assertIn(
            "/api/stock-picker/security-universe-snapshots/compare",
            paths,
        )
        self.assertIn(
            "/api/stock-picker/security-universe-snapshots/{snapshot_id}",
            paths,
        )
        self.assertIn(
            "/api/stock-picker/security-universe-classifications/coverage",
            paths,
        )
        self.assertIn(
            "/api/stock-picker/security-universe-classifications/"
            "{source_snapshot_id}",
            paths,
        )
        self.assertIn(
            "/api/stock-picker/security-universe-tradeability/coverage",
            paths,
        )
        self.assertIn(
            "/api/stock-picker/security-universe-tradeability/"
            "{source_snapshot_id}",
            paths,
        )
        self.assertLess(
            paths.index(
                "/api/stock-picker/security-universe-classifications/coverage"
            ),
            paths.index(
                "/api/stock-picker/security-universe-classifications/"
                "{source_snapshot_id}"
            ),
        )
        self.assertLess(
            paths.index(
                "/api/stock-picker/security-universe-snapshots/coverage"
            ),
            paths.index(
                "/api/stock-picker/security-universe-snapshots/{snapshot_id}"
            ),
        )
        self.assertLess(
            paths.index(
                "/api/stock-picker/security-universe-snapshots/compare"
            ),
            paths.index(
                "/api/stock-picker/security-universe-snapshots/{snapshot_id}"
            ),
        )
        self.assertEqual(history["items"][0]["snapshot_id"], "snapshot-1")
        self.assertEqual(coverage["markets"][0]["snapshot_count"], 1)
        self.assertEqual(
            classification_coverage["markets"][0][
                "classification_snapshot_count"
            ],
            1,
        )
        self.assertEqual(
            tradeability_coverage["markets"][0][
                "tradeability_snapshot_count"
            ],
            1,
        )
        self.assertEqual(comparison["added_count"], 1)
        self.assertTrue(detail["integrity_valid"])
        self.assertTrue(classification["integrity_valid"])
        self.assertTrue(tradeability["integrity_valid"])
        self.assertEqual(raised.exception.status_code, 404)
        self.assertEqual(classification_raised.exception.status_code, 404)
        self.assertEqual(tradeability_raised.exception.status_code, 404)
        service.get_history.assert_called_once_with("US", 5)
        service.get_coverage.assert_called_once_with("US")
        service.get_classification_coverage.assert_called_once_with("US")
        service.get_tradeability_coverage.assert_called_once_with("US")
        service.compare_snapshots.assert_called_once_with(
            "snapshot-0",
            "snapshot-1",
            25,
        )

    def test_comparison_endpoint_maps_missing_snapshot_to_404(self) -> None:
        service = MagicMock()
        service.compare_snapshots.side_effect = (
            SecurityUniverseSnapshotNotFoundError("missing")
        )
        with patch(
            "app.routers.stock_picker."
            "get_security_universe_snapshot_service",
            return_value=service,
        ), self.assertRaises(HTTPException) as raised:
            asyncio.run(compare_security_universe_snapshots(
                "missing",
                "snapshot-1",
                100,
            ))

        self.assertEqual(raised.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
