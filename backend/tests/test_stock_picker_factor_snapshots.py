from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import unittest
from unittest.mock import MagicMock, patch

import duckdb
from fastapi.testclient import TestClient

from app.db import _run_migrations
from app.main import app
from app.stock_picker_factor_snapshots import (
    AUTO_CAPTURE_LOCAL_HOUR,
    MAX_SNAPSHOT_AGE_HOURS,
    MIN_OBSERVATION_DATES,
    SNAPSHOT_VERSION,
    StockPickerFactorSnapshotService,
)


def _indexes(symbols):
    result = {}
    for symbol in symbols:
        result[symbol] = {
            "ten_day_change_rate": (
                0.04 if symbol == "SPY.US" else 0.1
            ),
            "half_year_change_rate": (
                0.1 if symbol == "SPY.US" else 0.2
            ),
        }
    return result


def _tradeability(symbols, include_depth):
    return {
        symbol: {
            "status": "available",
            "error": None,
            "trade_status": "normal",
            "is_tradable": True,
            "spread_bps": 5.0 if include_depth else None,
            "top_of_book_notional": (
                100_000.0 if include_depth else None
            ),
        }
        for symbol in symbols
    }


def _fundamentals(
    symbols,
    market,
    target_direction,
    event_window_days,
    include_corporate_actions,
    today,
):
    return {
        symbol: {
            "status": "available",
            "errors": [],
            "revenue_yoy": 0.1,
            "net_profit_yoy": 0.12,
            "operating_cash_flow_yoy": 0.08,
            "analyst_alignment": (
                0.8 if target_direction == "LONG" else 0.2
            ),
            "eps_revision_alignment": 0.75,
            "days_to_financial_event": event_window_days,
            "days_to_corporate_action": (
                event_window_days
                if include_corporate_actions
                else None
            ),
        }
        for symbol in symbols
    }


def _margin(symbols):
    return {
        symbol: {
            "status": "available",
            "error": None,
            "initial_margin_ratio": 0.5,
            "borrow_availability": "unknown",
            "borrow_fee_rate": None,
        }
        for symbol in symbols
    }


def _short_risk(symbols):
    return {
        symbol: {
            "status": "available",
            "error": None,
            "short_ratio": 0.1,
            "days_to_cover": 2.0,
        }
        for symbol in symbols
    }


def _service(**kwargs):
    return StockPickerFactorSnapshotService(
        index_loader=kwargs.get("index_loader", _indexes),
        short_risk_loader=kwargs.get(
            "short_risk_loader",
            _short_risk,
        ),
        tradeability_loader=kwargs.get(
            "tradeability_loader",
            _tradeability,
        ),
        fundamental_loader=kwargs.get(
            "fundamental_loader",
            _fundamentals,
        ),
        margin_loader=kwargs.get("margin_loader", _margin),
        connection_factory=kwargs.get("connection_factory"),
        clock=kwargs.get(
            "clock",
            lambda: datetime(
                2026,
                7,
                23,
                23,
                tzinfo=timezone.utc,
            ),
        ),
    )


class _ConnectionContext:
    def __init__(self, connection) -> None:
        self.connection = connection

    def __enter__(self):
        return self.connection

    def __exit__(self, exc_type, exc, traceback) -> bool:
        return False


class StockPickerFactorSnapshotTests(unittest.TestCase):
    def test_capture_records_directional_rs_and_all_point_in_time_sections(
        self,
    ) -> None:
        service = _service()

        long_result = service.capture_group(
            "US",
            "LONG",
            ["AAA.US"],
            persist=False,
            include_payloads=True,
        )
        short_result = service.capture_group(
            "US",
            "SHORT",
            ["AAA.US"],
            persist=False,
            include_payloads=True,
        )

        long_row = long_result["snapshots"][0]
        short_row = short_result["snapshots"][0]
        self.assertEqual(
            long_row["snapshot_version"],
            SNAPSHOT_VERSION,
        )
        self.assertEqual(long_row["benchmark_symbol"], "SPY.US")
        self.assertEqual(
            long_row["observation_date"],
            long_result["observation_date"],
        )
        self.assertEqual(long_result["session_phase"], "post_close")
        self.assertAlmostEqual(
            long_row["payload"]["relative_strength"][
                "market_rs_10d"
            ],
            0.06,
        )
        self.assertAlmostEqual(
            short_row["payload"]["relative_strength"][
                "market_rs_10d"
            ],
            -0.06,
        )
        for section in (
            "fundamentals",
            "tradeability",
            "margin_requirements",
        ):
            self.assertEqual(
                long_row["payload"][section]["status"],
                "available",
            )
        self.assertEqual(
            short_row["payload"]["short_risk"]["status"],
            "available",
        )
        self.assertEqual(
            long_row["payload"]["short_risk"]["status"],
            "not_applicable",
        )

    def test_external_failure_is_persistable_as_explicit_missing_data(
        self,
    ) -> None:
        def unavailable(*args, **kwargs):
            raise RuntimeError("fundamental unavailable")

        service = _service(fundamental_loader=unavailable)

        result = service.capture_group(
            "US",
            "LONG",
            ["AAA.US"],
            persist=False,
            include_payloads=True,
        )

        self.assertEqual(
            result["channel_status"]["fundamental"]["status"],
            "error",
        )
        snapshot = result["snapshots"][0]["payload"]
        self.assertEqual(
            snapshot["fundamentals"]["status"],
            "error",
        )
        self.assertIn(
            "fundamental unavailable",
            snapshot["fundamentals"]["errors"][0],
        )

    def test_capture_persists_grouped_rows_and_coverage_is_not_premature(
        self,
    ) -> None:
        connection = duckdb.connect(":memory:")
        self.addCleanup(connection.close)
        _run_migrations(connection)
        factory = lambda: _ConnectionContext(connection)
        service = _service(connection_factory=factory)

        result = service.capture_group(
            "US",
            "SHORT",
            ["AAA.US", "BBB.US"],
            persist=True,
        )
        coverage = service.get_coverage()
        group = next(
            item
            for item in coverage["groups"]
            if item["market"] == "US"
            and item["target_direction"] == "SHORT"
        )
        row = connection.execute(
            """
            SELECT request_id, observed_at, source_versions, payload
            FROM stock_picker_factor_snapshots
            ORDER BY symbol
            LIMIT 1
            """
        ).fetchone()

        self.assertEqual(result["row_count"], 2)
        self.assertEqual(row[0], result["request_id"])
        self.assertIsInstance(row[1], datetime)
        self.assertEqual(
            json.loads(row[2])["snapshot_schema"],
            SNAPSHOT_VERSION,
        )
        self.assertEqual(
            json.loads(row[3])["short_risk"]["status"],
            "available",
        )
        self.assertEqual(group["snapshot_count"], 2)
        self.assertEqual(
            group["captured_daily_snapshot_count"],
            2,
        )
        self.assertEqual(coverage["raw_snapshot_count"], 2)
        self.assertEqual(coverage["daily_snapshot_count"], 2)
        self.assertEqual(coverage["evaluation_snapshot_count"], 2)
        self.assertFalse(group["ready_for_return_evaluation"])
        self.assertFalse(coverage["ready_for_return_evaluation"])

    def test_coverage_uses_latest_snapshot_per_symbol_market_date(
        self,
    ) -> None:
        connection = duckdb.connect(":memory:")
        self.addCleanup(connection.close)
        _run_migrations(connection)
        factory = lambda: _ConnectionContext(connection)
        intraday_service = _service(
            connection_factory=factory,
            clock=lambda: datetime(
                2026,
                7,
                23,
                18,
                tzinfo=timezone.utc,
            ),
        )
        intraday_service.capture_group(
            "US",
            "LONG",
            ["AAA.US"],
            persist=True,
        )
        service = _service(connection_factory=factory)
        service.capture_group(
            "US",
            "LONG",
            ["AAA.US"],
            persist=True,
        )
        service.capture_group(
            "US",
            "LONG",
            ["AAA.US"],
            persist=True,
        )

        coverage = service.get_coverage()
        group = next(
            item
            for item in coverage["groups"]
            if item["market"] == "US"
            and item["target_direction"] == "LONG"
        )

        self.assertEqual(coverage["raw_snapshot_count"], 3)
        self.assertEqual(coverage["daily_snapshot_count"], 1)
        self.assertEqual(coverage["evaluation_snapshot_count"], 1)
        self.assertEqual(group["snapshot_count"], 1)
        self.assertEqual(group["observation_dates"], 1)

    def test_coverage_gate_requires_history_freshness_symbols_and_fields(
        self,
    ) -> None:
        now = datetime(2026, 7, 24, tzinfo=timezone.utc)
        service = _service()
        payload = {
            "relative_strength": {
                "market_rs_10d": 0.01,
                "market_rs_half_year": 0.02,
            },
            "fundamentals": {
                "status": "available",
                "revenue_yoy": 0.1,
                "net_profit_yoy": 0.1,
                "operating_cash_flow_yoy": 0.1,
                "analyst_alignment": 0.7,
                "eps_revision_alignment": 0.7,
                "days_to_financial_event": 10,
                "days_to_corporate_action": 20,
            },
            "tradeability": {
                "status": "available",
                "is_tradable": True,
                "spread_bps": 5,
                "top_of_book_notional": 100_000,
            },
            "margin_requirements": {
                "status": "available",
                "initial_margin_ratio": 0.5,
            },
            "short_risk": {
                "status": "available",
                "short_ratio": 0.1,
                "days_to_cover": 2,
            },
            "capture": {
                "session_phase": "post_close",
            },
        }
        rows = [
            {
                "observed_at": (
                    now
                    - timedelta(
                        days=MIN_OBSERVATION_DATES - day - 1,
                    )
                ),
                "observation_date": (
                    now
                    - timedelta(
                        days=MIN_OBSERVATION_DATES - day - 1,
                    )
                ).date().isoformat(),
                "market": "US",
                "target_direction": "SHORT",
                "symbol": f"S{symbol}.US",
                "payload": payload,
            }
            for day in range(MIN_OBSERVATION_DATES)
            for symbol in range(10)
        ]

        group = service._coverage_group(
            "US",
            "SHORT",
            rows,
            now,
        )

        self.assertLessEqual(
            group["snapshot_age_hours"],
            MAX_SNAPSHOT_AGE_HOURS,
        )
        self.assertTrue(group["ready_for_return_evaluation"])
        self.assertTrue(all(
            factor["coverage_ready"]
            for factor in group["factors"].values()
        ))

        missing_depth = [
            {
                **row,
                "payload": {
                    **payload,
                    "tradeability": {
                        "status": "available",
                        "is_tradable": True,
                        "spread_bps": None,
                        "top_of_book_notional": None,
                    },
                },
            }
            for row in rows
        ]
        incomplete = service._coverage_group(
            "US",
            "SHORT",
            missing_depth,
            now,
        )
        self.assertFalse(
            incomplete["factors"]["depth"]["coverage_ready"]
        )
        self.assertFalse(incomplete["ready_for_return_evaluation"])

    def test_session_phase_uses_market_local_time_and_weekends(
        self,
    ) -> None:
        service = _service()
        us_before_cutoff = datetime(
            2026,
            7,
            23,
            20,
            59,
            tzinfo=timezone.utc,
        )
        us_at_cutoff = datetime(
            2026,
            7,
            23,
            21,
            tzinfo=timezone.utc,
        )
        hk_before_cutoff = datetime(
            2026,
            7,
            23,
            8,
            59,
            tzinfo=timezone.utc,
        )
        hk_at_cutoff = datetime(
            2026,
            7,
            23,
            9,
            tzinfo=timezone.utc,
        )
        us_weekend = datetime(
            2026,
            7,
            25,
            23,
            tzinfo=timezone.utc,
        )

        self.assertEqual(
            service._session_phase("US", us_before_cutoff),
            "intraday",
        )
        self.assertEqual(
            service._session_phase("US", us_at_cutoff),
            "post_close",
        )
        self.assertEqual(
            service._session_phase("HK", hk_before_cutoff),
            "intraday",
        )
        self.assertEqual(
            service._session_phase("HK", hk_at_cutoff),
            "post_close",
        )
        self.assertEqual(
            service._session_phase("US", us_weekend),
            "non_trading_day",
        )
        self.assertGreaterEqual(AUTO_CAPTURE_LOCAL_HOUR, 17)

    def test_only_post_close_snapshots_pass_coverage_gate(
        self,
    ) -> None:
        now = datetime(
            2026,
            7,
            24,
            tzinfo=timezone.utc,
        )
        phases = (
            "post_close",
            "intraday",
            "non_trading_day",
            None,
        )
        rows = []
        for index, phase in enumerate(phases):
            payload = {}
            if phase is not None:
                payload["capture"] = {
                    "session_phase": phase,
                }
            rows.append({
                "observed_at": now - timedelta(hours=index),
                "observation_date": (
                    now - timedelta(days=index)
                ).date().isoformat(),
                "market": "US",
                "target_direction": "LONG",
                "symbol": f"S{index}.US",
                "payload": payload,
            })

        group = _service()._coverage_group(
            "US",
            "LONG",
            rows,
            now,
        )

        self.assertEqual(
            group["captured_daily_snapshot_count"],
            4,
        )
        self.assertEqual(group["snapshot_count"], 1)
        self.assertEqual(group["observation_dates"], 1)
        self.assertEqual(group["distinct_symbols"], 1)
        self.assertEqual(
            group["session_phase_counts"],
            {
                "post_close": 1,
                "intraday": 1,
                "non_trading_day": 1,
                "unknown": 1,
            },
        )
        self.assertFalse(group["ready_for_return_evaluation"])

    def test_due_capture_skips_existing_and_non_post_close_groups(
        self,
    ) -> None:
        connection = duckdb.connect(":memory:")
        self.addCleanup(connection.close)
        _run_migrations(connection)
        factory = lambda: _ConnectionContext(connection)
        intraday_service = _service(
            connection_factory=factory,
            clock=lambda: datetime(
                2026,
                7,
                23,
                18,
                tzinfo=timezone.utc,
            ),
        )
        intraday_service.capture_group(
            "US",
            "LONG",
            ["AAA.US"],
            persist=True,
        )
        service = _service(connection_factory=factory)

        first = service.capture_due_baseline(persist=True)
        second = service.capture_due_baseline(persist=True)

        self.assertEqual(
            [
                (group["market"], group["target_direction"])
                for group in first["captured"]
            ],
            [("US", "LONG"), ("US", "SHORT")],
        )
        self.assertEqual(first["row_count"], 20)
        self.assertEqual(
            {
                item["reason"]
                for item in first["skipped"]
            },
            {"session_phase:intraday"},
        )
        self.assertEqual(second["row_count"], 0)
        self.assertIn(
            "already_captured",
            {
                item["reason"]
                for item in second["skipped"]
            },
        )

    def test_invalid_capture_scope_is_rejected(self) -> None:
        service = _service()
        with self.assertRaisesRegex(ValueError, "market"):
            service.capture_baseline(market="CN", persist=False)
        with self.assertRaisesRegex(ValueError, "target_direction"):
            service.capture_baseline(
                market="US",
                target_direction="HOLD",
                persist=False,
            )


class StockPickerFactorSnapshotHttpTests(unittest.TestCase):
    def test_capture_and_coverage_endpoints(self) -> None:
        service = MagicMock()
        service.capture_baseline.return_value = {"row_count": 10}
        service.get_coverage.return_value = {
            "ready_for_return_evaluation": False,
        }
        with patch(
            "app.routers.stock_picker."
            "get_stock_picker_factor_snapshot_service",
            return_value=service,
        ):
            with TestClient(app) as client:
                capture = client.post(
                    "/api/stock-picker/factor-snapshots/capture",
                    json={
                        "market": "US",
                        "target_direction": "LONG",
                    },
                )
                coverage = client.get(
                    "/api/stock-picker/factor-snapshots/coverage",
                    params={"days": 180},
                )
                invalid_capture = client.post(
                    "/api/stock-picker/factor-snapshots/capture",
                    json={},
                )

        self.assertEqual(capture.status_code, 200)
        self.assertEqual(capture.json(), {"row_count": 10})
        service.capture_baseline.assert_called_once_with(
            market="US",
            target_direction="LONG",
            persist=True,
        )
        self.assertEqual(coverage.status_code, 200)
        self.assertEqual(invalid_capture.status_code, 422)
        self.assertFalse(
            coverage.json()["ready_for_return_evaluation"]
        )
        service.get_coverage.assert_called_once_with(180)


if __name__ == "__main__":
    unittest.main()
