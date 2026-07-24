from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
import unittest
from unittest.mock import MagicMock, patch

import duckdb
from fastapi.testclient import TestClient

from app.db import _run_migrations
from app.external_service_resilience import ExternalServiceTimeoutError
from app.main import app
from app.stock_picker_factor_snapshots import (
    AUTO_CAPTURE_LOCAL_HOUR,
    CAPTURE_CLAIM_LEASE_MINUTES,
    FUNDAMENTAL_CAPTURE_CHUNK_SIZE,
    MARGIN_CAPTURE_CHUNK_SIZE,
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


def _short_capacity(symbols):
    return {
        symbol: {
            "status": "available",
            "error": None,
            "cash_max_qty": 100,
            "margin_max_qty": 250,
            "short_selling_max_qty": 250,
            "availability": "available",
            "borrow_fee_rate": None,
            "recall_risk": "unknown",
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
        short_capacity_loader=kwargs.get(
            "short_capacity_loader",
            _short_capacity,
        ),
        trading_day_loader=kwargs.get(
            "trading_day_loader",
            lambda market, trading_date: True,
        ),
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
    @staticmethod
    def _call_loader_directly(
        channel,
        operation,
        loader,
        *args,
        **kwargs,
    ):
        kwargs.pop("retry_if", None)
        return loader(*args, **kwargs)

    def test_capture_records_directional_rs_and_all_point_in_time_sections(
        self,
    ) -> None:
        capacity_loader = MagicMock(side_effect=_short_capacity)
        service = _service(short_capacity_loader=capacity_loader)

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
        self.assertEqual(
            short_row["payload"]["short_capacity"][
                "short_selling_max_qty"
            ],
            250,
        )
        self.assertEqual(
            long_row["payload"]["short_capacity"]["status"],
            "not_applicable",
        )
        capacity_loader.assert_called_once_with(["AAA.US"])

    def test_short_capacity_scope_and_failure_are_explicit(self) -> None:
        capacity_loader = MagicMock(
            side_effect=RuntimeError("account estimate unavailable"),
        )
        service = _service(short_capacity_loader=capacity_loader)

        us_short = service.capture_group(
            "US",
            "SHORT",
            ["AAA.US"],
            persist=False,
            include_payloads=True,
        )
        hk_short = service.capture_group(
            "HK",
            "SHORT",
            ["700.HK"],
            persist=False,
            include_payloads=True,
        )

        self.assertEqual(
            us_short["channel_status"]["short_capacity"]["status"],
            "error",
        )
        us_payload = us_short["snapshots"][0]["payload"][
            "short_capacity"
        ]
        self.assertEqual(us_payload["status"], "error")
        self.assertIn("account estimate unavailable", us_payload["error"])
        self.assertEqual(
            us_payload["failure_category"],
            "unknown_error",
        )
        self.assertEqual(
            us_short["channel_status"]["short_capacity"][
                "failure_category"
            ],
            "unknown_error",
        )
        self.assertEqual(us_payload["availability"], "unknown")
        self.assertIsNone(us_payload["borrow_fee_rate"])
        self.assertEqual(us_payload["recall_risk"], "unknown")

        self.assertEqual(
            hk_short["channel_status"]["short_capacity"]["status"],
            "unsupported",
        )
        self.assertEqual(
            hk_short["snapshots"][0]["payload"]["short_capacity"][
                "status"
            ],
            "unsupported",
        )
        self.assertEqual(capacity_loader.call_count, 2)
        self.assertTrue(all(
            item.args == (["AAA.US"],)
            for item in capacity_loader.call_args_list
        ))

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

    def test_fundamental_chunks_keep_success_and_mark_unstarted_symbols(
        self,
    ) -> None:
        calls = []

        def partially_available(symbols, *args):
            calls.append(symbols)
            if symbols == ["CCC.US", "DDD.US"]:
                raise ExternalServiceTimeoutError("fundamental timeout")
            return _fundamentals(symbols, *args)

        service = _service(fundamental_loader=partially_available)
        with patch(
            "app.stock_picker_factor_snapshots.run_external_call",
            side_effect=self._call_loader_directly,
        ):
            result = service.capture_group(
                "US",
                "LONG",
                ["AAA.US", "BBB.US", "CCC.US", "DDD.US", "EEE.US"],
                persist=False,
                include_payloads=True,
            )

        self.assertEqual(FUNDAMENTAL_CAPTURE_CHUNK_SIZE, 2)
        self.assertEqual(
            calls,
            [["AAA.US", "BBB.US"], ["CCC.US", "DDD.US"]],
        )
        channel = result["channel_status"]["fundamental"]
        self.assertEqual(channel["status"], "partial")
        self.assertEqual(channel["completed_symbol_count"], 2)
        self.assertEqual(channel["failed_symbol_count"], 3)
        self.assertTrue(channel["timed_out"])
        payloads = {
            row["symbol"]: row["payload"]["fundamentals"]
            for row in result["snapshots"]
        }
        self.assertEqual(payloads["AAA.US"]["status"], "available")
        self.assertEqual(payloads["CCC.US"]["status"], "error")
        self.assertIn("fundamental timeout", payloads["CCC.US"]["errors"][0])
        self.assertEqual(payloads["EEE.US"]["status"], "skipped")
        self.assertIn("未执行", payloads["EEE.US"]["errors"][0])

    def test_margin_timeout_keeps_success_and_stops_trade_channel(self) -> None:
        margin_calls = []

        def partially_available(symbols):
            margin_calls.append(symbols)
            if symbols == ["BBB.US"]:
                raise ExternalServiceTimeoutError("margin timeout")
            return _margin(symbols)

        capacity_loader = MagicMock(side_effect=_short_capacity)
        service = _service(
            margin_loader=partially_available,
            short_capacity_loader=capacity_loader,
        )
        with patch(
            "app.stock_picker_factor_snapshots.run_external_call",
            side_effect=self._call_loader_directly,
        ):
            result = service.capture_group(
                "US",
                "SHORT",
                ["AAA.US", "BBB.US", "CCC.US"],
                persist=False,
                include_payloads=True,
            )

        self.assertEqual(MARGIN_CAPTURE_CHUNK_SIZE, 1)
        self.assertEqual(margin_calls, [["AAA.US"], ["BBB.US"]])
        channel = result["channel_status"]["margin"]
        self.assertEqual(channel["status"], "partial")
        self.assertEqual(channel["completed_symbol_count"], 1)
        self.assertEqual(channel["failed_symbol_count"], 2)
        self.assertTrue(channel["timed_out"])
        capacity_loader.assert_not_called()
        self.assertEqual(
            result["channel_status"]["short_capacity"],
            {
                "status": "skipped",
                "error": "保证金分片超时，未继续调用交易服务",
                "failure_category": "timeout",
            },
        )
        payloads = {
            row["symbol"]: row["payload"]
            for row in result["snapshots"]
        }
        self.assertEqual(
            payloads["AAA.US"]["margin_requirements"]["status"],
            "available",
        )
        self.assertEqual(
            payloads["BBB.US"]["margin_requirements"]["status"],
            "error",
        )
        self.assertEqual(
            payloads["CCC.US"]["margin_requirements"]["status"],
            "skipped",
        )
        self.assertTrue(all(
            payload["short_capacity"]["status"] == "skipped"
            for payload in payloads.values()
        ))

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
        self.assertEqual(
            json.loads(row[3])["short_capacity"][
                "short_selling_max_qty"
            ],
            250,
        )
        self.assertTrue(
            json.loads(row[3])["short_capacity"]["account_specific"]
        )
        self.assertEqual(
            json.loads(row[3])["short_capacity"]["supported_market"],
            "US",
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

    def test_legacy_snapshot_remains_missing_for_v2_short_capacity(
        self,
    ) -> None:
        connection = duckdb.connect(":memory:")
        self.addCleanup(connection.close)
        _run_migrations(connection)
        factory = lambda: _ConnectionContext(connection)
        observed_at = datetime(
            2026,
            7,
            23,
            23,
            tzinfo=timezone.utc,
        )
        connection.execute(
            """
            INSERT INTO stock_picker_factor_snapshots (
                request_id, observed_at, observation_date,
                snapshot_version, market, target_direction,
                symbol, benchmark_symbol, source,
                source_versions, payload
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                "legacy-request",
                observed_at.replace(tzinfo=None),
                observed_at.date(),
                "stock-picker-factor-snapshot-v1",
                "US",
                "SHORT",
                "AAA.US",
                "SPY.US",
                "longbridge-live",
                json.dumps({
                    "snapshot_schema": (
                        "stock-picker-factor-snapshot-v1"
                    ),
                }),
                json.dumps({
                    "capture": {"session_phase": "post_close"},
                }),
            ],
        )
        service = _service(connection_factory=factory)

        coverage = service.get_coverage(
            current_time=datetime(
                2026,
                7,
                24,
                tzinfo=timezone.utc,
            ),
        )
        group = next(
            item
            for item in coverage["groups"]
            if item["market"] == "US"
            and item["target_direction"] == "SHORT"
        )

        self.assertEqual(coverage["snapshot_version"], SNAPSHOT_VERSION)
        self.assertEqual(
            group["factors"]["short_capacity"],
            {
                "available_count": 0,
                "total_count": 1,
                "coverage": 0,
                "missing_reasons": {
                    "missing_required_values": 1,
                },
                "coverage_ready": False,
            },
        )

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
            "short_capacity": {
                "status": "available",
                "short_selling_max_qty": 0,
                "availability": "unavailable",
                "borrow_fee_rate": None,
                "recall_risk": "unknown",
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
        self.assertEqual(
            group["factors"]["short_capacity"]["available_count"],
            len(rows),
        )

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

        missing_capacity = [
            {
                **row,
                "payload": {
                    **payload,
                    "short_capacity": {
                        "status": "error",
                        "error": "estimate unavailable",
                        "failure_category": "timeout",
                        "short_selling_max_qty": None,
                        "availability": "unknown",
                    },
                },
            }
            for row in rows
        ]
        capacity_incomplete = service._coverage_group(
            "US",
            "SHORT",
            missing_capacity,
            now,
        )
        self.assertFalse(
            capacity_incomplete["factors"]["short_capacity"][
                "coverage_ready"
            ]
        )
        self.assertEqual(
            capacity_incomplete["factors"]["short_capacity"][
                "missing_reasons"
            ],
            {"failure:timeout": len(rows)},
        )
        self.assertFalse(
            capacity_incomplete["ready_for_return_evaluation"]
        )

    def test_short_capacity_coverage_applies_only_to_us_short(self) -> None:
        now = datetime(2026, 7, 24, tzinfo=timezone.utc)
        base_row = {
            "observed_at": now,
            "observation_date": now.date().isoformat(),
            "symbol": "AAA.US",
            "payload": {
                "capture": {"session_phase": "post_close"},
            },
        }
        service = _service()

        us_long = service._coverage_group(
            "US",
            "LONG",
            [{**base_row, "market": "US", "target_direction": "LONG"}],
            now,
        )
        hk_short = service._coverage_group(
            "HK",
            "SHORT",
            [{
                **base_row,
                "market": "HK",
                "target_direction": "SHORT",
                "symbol": "700.HK",
            }],
            now,
        )
        us_short = service._coverage_group(
            "US",
            "SHORT",
            [{**base_row, "market": "US", "target_direction": "SHORT"}],
            now,
        )

        self.assertNotIn("short_capacity", us_long["factors"])
        self.assertNotIn("short_capacity", hk_short["factors"])
        self.assertIn("short_capacity", us_short["factors"])
        self.assertEqual(
            us_short["factors"]["short_capacity"]["missing_reasons"],
            {"missing_required_values": 1},
        )

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
        coverage = service.get_coverage()

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
        runs = connection.execute(
            """
            SELECT target_direction, status, row_count
            FROM stock_picker_factor_snapshot_runs
            ORDER BY target_direction
            """
        ).fetchall()
        self.assertEqual(
            runs,
            [
                ("LONG", "completed", 10),
                ("SHORT", "completed", 10),
            ],
        )
        self.assertEqual(
            coverage["capture_runs"]["status_counts"],
            {"completed": 2},
        )
        self.assertEqual(
            coverage["capture_runs"]["active"],
            [],
        )

    def test_market_calendar_fails_closed_and_caches_closed_days(
        self,
    ) -> None:
        connection = duckdb.connect(":memory:")
        self.addCleanup(connection.close)
        _run_migrations(connection)
        factory = lambda: _ConnectionContext(connection)
        calendar = MagicMock(return_value=False)
        index_loader = MagicMock()
        service = _service(
            connection_factory=factory,
            trading_day_loader=calendar,
            index_loader=index_loader,
        )

        first = service.capture_due_baseline(persist=True)
        second = service.capture_due_baseline(persist=True)

        self.assertEqual(first["row_count"], 0)
        self.assertEqual(first["errors"], [])
        self.assertEqual(
            {
                item["reason"]
                for item in first["skipped"]
            },
            {"market_closed", "session_phase:intraday"},
        )
        self.assertEqual(second["row_count"], 0)
        calendar.assert_called_once()
        index_loader.assert_not_called()

    def test_market_calendar_error_does_not_capture_or_claim(
        self,
    ) -> None:
        connection = duckdb.connect(":memory:")
        self.addCleanup(connection.close)
        _run_migrations(connection)
        factory = lambda: _ConnectionContext(connection)
        index_loader = MagicMock()
        service = _service(
            connection_factory=factory,
            trading_day_loader=MagicMock(
                side_effect=RuntimeError("calendar unavailable"),
            ),
            index_loader=index_loader,
        )

        result = service.capture_due_baseline(persist=True)
        claim_count = connection.execute(
            """
            SELECT COUNT(*)
            FROM stock_picker_factor_snapshot_runs
            """
        ).fetchone()[0]

        self.assertEqual(result["row_count"], 0)
        self.assertEqual(
            [
                item["reason"]
                for item in result["skipped"]
                if item["market"] == "US"
            ],
            [
                "trading_calendar_unavailable",
                "trading_calendar_unavailable",
            ],
        )
        self.assertIn(
            "calendar unavailable",
            result["errors"][0]["error"],
        )
        self.assertEqual(claim_count, 0)
        index_loader.assert_not_called()

    def test_capture_claim_blocks_concurrency_and_allows_failed_retry(
        self,
    ) -> None:
        connection = duckdb.connect(":memory:")
        self.addCleanup(connection.close)
        _run_migrations(connection)
        factory = lambda: _ConnectionContext(connection)
        now = datetime(
            2026,
            7,
            23,
            23,
            tzinfo=timezone.utc,
        )
        local_date = date(2026, 7, 23)
        first_service = _service(connection_factory=factory)
        second_service = _service(connection_factory=factory)

        first_claim = first_service._claim_capture(
            "US",
            "LONG",
            local_date,
            now,
        )
        concurrent_claim = second_service._claim_capture(
            "US",
            "LONG",
            local_date,
            now + timedelta(minutes=1),
        )
        first_service._finish_capture_claim(
            "US",
            "LONG",
            local_date,
            first_claim,
            status="failed",
            completed_at=now + timedelta(minutes=2),
            error="temporary failure",
        )
        retry_claim = second_service._claim_capture(
            "US",
            "LONG",
            local_date,
            now + timedelta(minutes=3),
        )
        stale_reclaim = first_service._claim_capture(
            "US",
            "LONG",
            local_date,
            now + timedelta(
                minutes=CAPTURE_CLAIM_LEASE_MINUTES + 4,
            ),
        )
        second_service._finish_capture_claim(
            "US",
            "LONG",
            local_date,
            retry_claim,
            status="completed",
            completed_at=now + timedelta(
                minutes=CAPTURE_CLAIM_LEASE_MINUTES + 5,
            ),
            request_id="stale-worker",
            row_count=10,
        )
        stored = connection.execute(
            """
            SELECT claim_id, status, request_id
            FROM stock_picker_factor_snapshot_runs
            WHERE market = 'US'
              AND target_direction = 'LONG'
              AND observation_date = ?
            """,
            [local_date],
        ).fetchone()

        self.assertIsNotNone(first_claim)
        self.assertIsNone(concurrent_claim)
        self.assertIsNotNone(retry_claim)
        self.assertNotEqual(first_claim, retry_claim)
        self.assertIsNotNone(stale_reclaim)
        self.assertNotEqual(retry_claim, stale_reclaim)
        self.assertEqual(
            stored,
            (stale_reclaim, "running", None),
        )
        self.assertGreaterEqual(CAPTURE_CLAIM_LEASE_MINUTES, 30)

    def test_due_capture_persists_failure_and_continues_other_groups(
        self,
    ) -> None:
        connection = duckdb.connect(":memory:")
        self.addCleanup(connection.close)
        _run_migrations(connection)
        factory = lambda: _ConnectionContext(connection)
        service = _service(connection_factory=factory)
        with patch.object(
            service,
            "capture_group",
            side_effect=[
                RuntimeError("long capture failed"),
                {
                    "request_id": "short-request",
                    "row_count": 10,
                },
            ],
        ):
            result = service.capture_due_baseline(persist=True)
        runs = connection.execute(
            """
            SELECT
                target_direction,
                status,
                request_id,
                row_count,
                error
            FROM stock_picker_factor_snapshot_runs
            ORDER BY target_direction
            """
        ).fetchall()
        coverage = service.get_coverage()

        self.assertEqual(result["row_count"], 10)
        self.assertEqual(len(result["errors"]), 1)
        self.assertIn(
            "long capture failed",
            result["errors"][0]["error"],
        )
        self.assertEqual(
            runs,
            [
                (
                    "LONG",
                    "failed",
                    None,
                    0,
                    "long capture failed",
                ),
                (
                    "SHORT",
                    "completed",
                    "short-request",
                    10,
                    None,
                ),
            ],
        )
        self.assertEqual(
            coverage["capture_runs"]["status_counts"],
            {
                "failed": 1,
                "completed": 1,
            },
        )
        self.assertIn(
            "long capture failed",
            coverage["capture_runs"][
                "recent_failures"
            ][0]["error"],
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
