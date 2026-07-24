from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
import json
import unittest
from unittest.mock import MagicMock, patch

import duckdb
from fastapi.testclient import TestClient

from app.db import _run_migrations
from app.main import app
from app.stock_picker_factor_evaluation import (
    FACTOR_INCREMENT_EVALUATION_VERSION,
    FACTOR_VARIANTS,
    StockPickerFactorIncrementEvaluationService,
)
from app.stock_picker_factor_snapshots import SNAPSHOT_VERSION


class _ConnectionContext:
    def __init__(self, connection) -> None:
        self.connection = connection

    def __enter__(self):
        return self.connection

    def __exit__(self, exc_type, exc, traceback) -> bool:
        return False


def _payload(
    *,
    fundamental: bool = True,
    execution: bool = True,
    phase: str = "post_close",
) -> dict:
    return {
        "relative_strength": {
            "status": "available",
            "market_rs_10d": 0.05,
            "market_rs_half_year": 0.1,
        },
        "fundamentals": {
            "status": "available",
            "errors": [],
            "revenue_yoy": 0.1 if fundamental else -0.1,
            "net_profit_yoy": 0.1 if fundamental else -0.1,
            "operating_cash_flow_yoy": 0.1 if fundamental else -0.1,
            "analyst_alignment": 0.5 if fundamental else -0.5,
            "eps_revision_alignment": 0.5 if fundamental else -0.5,
            "days_to_financial_event": 30,
            "days_to_corporate_action": 30,
        },
        "tradeability": {
            "status": "available",
            "trade_status": "normal" if execution else "halted",
            "is_tradable": execution,
            "spread_bps": 5.0 if execution else 100.0,
            "top_of_book_notional": 200_000.0 if execution else 10_000.0,
        },
        "margin_requirements": {
            "status": "available",
            "initial_margin_ratio": 0.5 if execution else 0.8,
        },
        "short_risk": {
            "status": "available",
            "short_ratio": 0.1 if execution else 0.3,
            "days_to_cover": 2.0 if execution else 8.0,
        },
        "short_capacity": {
            "status": "available",
            "short_selling_max_qty": 100 if execution else 0,
        },
        "capture": {
            "session_phase": phase,
        },
    }


class StockPickerFactorIncrementEvaluationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = duckdb.connect(":memory:")
        self.addCleanup(self.connection.close)
        _run_migrations(self.connection)
        self.factory = lambda: _ConnectionContext(self.connection)
        self.start = date(2026, 7, 1)
        self.now = datetime(2026, 7, 7, 12, tzinfo=timezone.utc)

    def _insert(
        self,
        symbol: str,
        observation_date: date,
        payload: dict,
        *,
        direction: str = "LONG",
        market: str = "US",
        observed_offset_minutes: int = 0,
        snapshot_version: str = SNAPSHOT_VERSION,
    ) -> None:
        observed_at = datetime.combine(
            observation_date,
            datetime.min.time(),
        ) + timedelta(hours=22, minutes=observed_offset_minutes)
        self.connection.execute(
            """
            INSERT INTO stock_picker_factor_snapshots (
                request_id, observed_at, observation_date,
                snapshot_version, market, target_direction, symbol,
                benchmark_symbol, source, source_versions, payload
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                f"{market}-{direction}-{symbol}-{observation_date}-{observed_offset_minutes}",
                observed_at,
                observation_date,
                snapshot_version,
                market,
                direction,
                symbol,
                "SPY.US" if market == "US" else "2800.HK",
                "test",
                "{}",
                json.dumps(payload),
            ],
        )

    @staticmethod
    def _bars(symbol: str, **kwargs):
        daily_multiplier = 1.1 if symbol == "AAA.US" else 0.9
        close = 100.0
        bars = []
        for offset in range(10):
            bars.append({
                "ts": date(2026, 7, 1) + timedelta(days=offset),
                "close": close,
            })
            close *= daily_multiplier
        return bars

    def _seed_ready_rows(self, direction: str = "LONG") -> None:
        for offset in range(4):
            observation_date = self.start + timedelta(days=offset)
            self._insert(
                "AAA.US",
                observation_date,
                _payload(fundamental=True, execution=True),
                direction=direction,
            )
            self._insert(
                "BBB.US",
                observation_date,
                _payload(fundamental=False, execution=True),
                direction=direction,
            )
            self._insert(
                "CCC.US",
                observation_date,
                _payload(fundamental=True, execution=False),
                direction=direction,
            )

    def _service(self, bar_loader=None):
        return StockPickerFactorIncrementEvaluationService(
            bar_loader=bar_loader or self._bars,
            connection_factory=self.factory,
            now_provider=lambda: self.now,
        )

    @staticmethod
    def _ready_parameters() -> dict:
        return {
            "horizons": [1],
            "lookback_days": 30,
            "max_bars": 100,
            "minimum_observation_dates": 4,
            "minimum_distinct_symbols": 3,
            "minimum_factor_coverage": 1.0,
            "maximum_snapshot_age_hours": 200,
            "minimum_label_coverage": 1.0,
            "minimum_paired_dates": 4,
            "minimum_selected_per_date": 1,
            "bootstrap_samples": 200,
            "bootstrap_seed": 42,
        }

    def test_migration_is_idempotent_and_creates_report_table(self) -> None:
        _run_migrations(self.connection)

        columns = self.connection.execute(
            "DESCRIBE stock_picker_factor_evaluations"
        ).fetchall()

        self.assertEqual(
            [row[0] for row in columns],
            [
                "id",
                "created_at",
                "market",
                "pool_type",
                "evaluation_version",
                "parameters",
                "result",
                "ready",
                "data_as_of",
            ],
        )

    def test_empty_coverage_fails_closed_and_persists_report(self) -> None:
        report = self._service().run(
            "US",
            "LONG",
            horizons=[1],
            minimum_observation_dates=1,
            minimum_distinct_symbols=1,
            minimum_paired_dates=1,
            minimum_selected_per_date=1,
            bootstrap_samples=200,
        )

        self.assertFalse(report["ready"])
        self.assertIsNone(report["metrics"])
        self.assertIn(
            "insufficient_observation_dates",
            report["coverage"]["gate_reasons"],
        )
        self.assertIn("missing_latest_snapshot", report["coverage"]["gate_reasons"])
        history = self._service().get_history()
        self.assertEqual(len(history), 1)
        self.assertFalse(history[0]["ready"])
        self.assertEqual(
            history[0]["evaluation_version"],
            FACTOR_INCREMENT_EVALUATION_VERSION,
        )

    def test_ready_report_compares_frozen_variants_by_equal_weight_date(self) -> None:
        self._seed_ready_rows()

        report = self._service().run(
            "US",
            "LONG",
            **self._ready_parameters(),
        )

        self.assertTrue(report["ready"])
        self.assertEqual(report["coverage"]["observation_dates"], 4)
        self.assertEqual(report["coverage"]["distinct_symbols"], 3)
        self.assertEqual(
            set(report["metrics"]),
            set(FACTOR_VARIANTS),
        )
        fundamental = report["metrics"]["fundamental_quality"]["1"]
        combined = report["metrics"]["fundamental_and_execution"]["1"]
        self.assertEqual(fundamental["paired_dates"], 4)
        self.assertGreater(
            fundamental["paired_delta"]["average"],
            0,
        )
        self.assertGreater(combined["paired_delta"]["average"], 0)
        self.assertTrue(combined["paired_delta_inference"]["ready"])
        self.assertEqual(report["coverage"]["latest_label_date"], "2026-07-05")
        self.assertIsInstance(report["id"], int)
        history = self._service().get_history()
        self.assertEqual(history[0]["data_as_of"], "2026-07-05")
        self.assertTrue(history[0]["result"]["ready"])
        self.assertNotIn("parameters", history[0]["result"])

        repeated = self._service().run(
            "US",
            "LONG",
            **self._ready_parameters(),
            persist=False,
        )
        self.assertEqual(
            repeated["metrics"]["fundamental_quality"]["1"][
                "paired_delta_inference"
            ],
            fundamental["paired_delta_inference"],
        )

    def test_incomplete_factor_coverage_suppresses_all_return_metrics(self) -> None:
        self._seed_ready_rows()
        row = self.connection.execute(
            """
            SELECT id, payload
            FROM stock_picker_factor_snapshots
            WHERE symbol = 'AAA.US'
            ORDER BY observation_date
            LIMIT 1
            """
        ).fetchone()
        payload = json.loads(row[1])
        payload["fundamentals"]["revenue_yoy"] = None
        self.connection.execute(
            "UPDATE stock_picker_factor_snapshots SET payload = ? WHERE id = ?",
            [json.dumps(payload), row[0]],
        )

        report = self._service().run(
            "US",
            "LONG",
            **self._ready_parameters(),
            persist=False,
        )

        self.assertFalse(report["ready"])
        self.assertIsNone(report["metrics"])
        self.assertEqual(
            report["coverage"]["factor_coverage"][
                "fundamental_quality"
            ]["coverage"],
            11 / 12,
        )
        self.assertIn(
            "insufficient_factor_coverage:fundamental_quality",
            report["coverage"]["gate_reasons"],
        )

    def test_signal_price_requires_exact_observation_date(self) -> None:
        self._insert("AAA.US", self.start, _payload())

        def bars_after_signal(symbol: str, **kwargs):
            return [
                {"ts": self.start + timedelta(days=1), "close": 100},
                {"ts": self.start + timedelta(days=2), "close": 120},
            ]

        report = self._service(bars_after_signal).run(
            "US",
            "LONG",
            horizons=[1],
            minimum_observation_dates=1,
            minimum_distinct_symbols=1,
            minimum_factor_coverage=1,
            maximum_snapshot_age_hours=1000,
            minimum_label_coverage=1,
            minimum_paired_dates=1,
            minimum_selected_per_date=1,
            bootstrap_samples=200,
            persist=False,
        )

        self.assertFalse(report["ready"])
        self.assertIsNone(report["metrics"])
        self.assertEqual(
            report["coverage"]["excluded_rows"]["missing_signal_bar"],
            1,
        )
        self.assertIn(
            "insufficient_label_coverage:1",
            report["coverage"]["gate_reasons"],
        )

    def test_latest_post_close_v2_snapshot_wins_same_day(self) -> None:
        for offset in range(4):
            observation_date = self.start + timedelta(days=offset)
            self._insert(
                "AAA.US",
                observation_date,
                _payload(fundamental=False),
            )
            self._insert(
                "AAA.US",
                observation_date,
                _payload(fundamental=True),
                observed_offset_minutes=5,
            )
            self._insert(
                "BBB.US",
                observation_date,
                _payload(),
                snapshot_version="stock-picker-factor-snapshot-v1",
            )
            self._insert(
                "CCC.US",
                observation_date,
                _payload(phase="intraday"),
            )

        report = self._service().run(
            "US",
            "LONG",
            **{
                **self._ready_parameters(),
                "minimum_distinct_symbols": 1,
            },
        )

        self.assertTrue(report["ready"])
        self.assertEqual(report["coverage"]["eligible_snapshot_rows"], 4)
        self.assertEqual(
            report["coverage"]["selection"]["fundamental_quality"][
                "selected_records"
            ],
            4,
        )
        self.assertEqual(
            report["coverage"]["excluded_rows"][
                "duplicate_same_day_replaced"
            ],
            4,
        )
        self.assertEqual(
            report["coverage"]["excluded_rows"][
                "unsupported_snapshot_version"
            ],
            4,
        )
        self.assertEqual(
            report["coverage"]["excluded_rows"]["not_post_close"],
            4,
        )

    def test_future_and_market_date_mismatched_snapshots_are_rejected(self) -> None:
        self._insert("AAA.US", self.start, _payload())
        self.connection.execute(
            """
            UPDATE stock_picker_factor_snapshots
            SET observed_at = ?
            WHERE symbol = 'AAA.US'
            """,
            [datetime(2026, 7, 2, 22)],
        )
        future_date = date(2026, 7, 8)
        self._insert("BBB.US", future_date, _payload())

        report = self._service().run(
            "US",
            "LONG",
            horizons=[1],
            minimum_observation_dates=1,
            minimum_distinct_symbols=1,
            minimum_factor_coverage=1,
            maximum_snapshot_age_hours=1000,
            minimum_label_coverage=1,
            minimum_paired_dates=1,
            minimum_selected_per_date=1,
            bootstrap_samples=200,
            persist=False,
        )

        self.assertFalse(report["ready"])
        self.assertEqual(report["coverage"]["eligible_snapshot_rows"], 0)
        self.assertEqual(
            report["coverage"]["excluded_rows"][
                "observation_date_mismatch"
            ],
            1,
        )
        self.assertEqual(
            report["coverage"]["excluded_rows"]["future_observed_at"],
            1,
        )

    def test_short_direction_reverses_future_returns(self) -> None:
        self._seed_ready_rows(direction="SHORT")

        def falling_bars(symbol: str, **kwargs):
            close = 100.0
            result = []
            for offset in range(10):
                result.append({
                    "ts": self.start + timedelta(days=offset),
                    "close": close,
                })
                close *= 0.9
            return result

        report = self._service(falling_bars).run(
            "US",
            "SHORT",
            **self._ready_parameters(),
        )

        self.assertTrue(report["ready"])
        execution = report["metrics"]["execution_risk"]["1"]
        self.assertGreater(execution["variant"]["average"], 0)
        self.assertIn(
            "short_capacity",
            report["coverage"]["applicable_factors"],
        )

    def test_parameter_validation_rejects_lookahead_and_sampling_errors(self) -> None:
        service = self._service()

        with self.assertRaisesRegex(ValueError, "horizons"):
            service.run("US", "LONG", horizons=[0], persist=False)
        with self.assertRaisesRegex(ValueError, "bootstrap_block_size"):
            service.run(
                "US",
                "LONG",
                bootstrap_block_size=1,
                persist=False,
            )
        with self.assertRaisesRegex(ValueError, "market"):
            service.run("CN", "LONG", persist=False)

    def test_frozen_filter_boundaries_and_direction_scope_are_explicit(self) -> None:
        passing = _payload()
        boundary = _payload()
        boundary["fundamentals"].update({
            "revenue_yoy": 0,
            "net_profit_yoy": 0,
            "operating_cash_flow_yoy": 0,
            "analyst_alignment": 0,
            "eps_revision_alignment": 0,
            "days_to_financial_event": 5,
            "days_to_corporate_action": 5,
        })
        boundary["tradeability"].update({
            "spread_bps": 50,
            "top_of_book_notional": 100_000,
        })
        boundary["margin_requirements"]["initial_margin_ratio"] = 0.6
        boundary["short_risk"].update({
            "days_to_cover": 5,
            "short_ratio": 0.2,
        })
        boundary["short_capacity"]["short_selling_max_qty"] = 1

        for variant in FACTOR_VARIANTS:
            self.assertTrue(
                StockPickerFactorIncrementEvaluationService._variant_matches(
                    passing,
                    variant,
                    "US",
                    "LONG",
                )
            )
            self.assertTrue(
                StockPickerFactorIncrementEvaluationService._variant_matches(
                    boundary,
                    variant,
                    "US",
                    "SHORT",
                )
            )
        failing_capacity = _payload()
        failing_capacity["short_capacity"]["short_selling_max_qty"] = 0
        self.assertTrue(
            StockPickerFactorIncrementEvaluationService._variant_matches(
                failing_capacity,
                "execution_risk",
                "US",
                "LONG",
            )
        )
        self.assertFalse(
            StockPickerFactorIncrementEvaluationService._variant_matches(
                failing_capacity,
                "execution_risk",
                "US",
                "SHORT",
            )
        )


class StockPickerFactorIncrementEvaluationRouteTests(unittest.TestCase):
    def test_run_and_history_routes_delegate_validated_parameters(self) -> None:
        service = MagicMock()
        service.run.return_value = {
            "evaluation_version": FACTOR_INCREMENT_EVALUATION_VERSION,
            "ready": False,
            "metrics": None,
        }
        service.get_history.return_value = [{"id": 1, "ready": False}]

        with patch(
            "app.routers.stock_picker."
            "get_stock_picker_factor_evaluation_service",
            return_value=service,
        ):
            client = TestClient(app)
            response = client.post(
                "/api/stock-picker/factor-evaluation",
                json={
                    "market": "US",
                    "pool_type": "LONG",
                    "horizons": [1, 5],
                    "bootstrap_samples": 200,
                },
            )
            history = client.get(
                "/api/stock-picker/factor-evaluations?limit=5"
            )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["ready"])
        self.assertEqual(history.status_code, 200)
        self.assertEqual(history.json()["items"][0]["id"], 1)
        kwargs = service.run.call_args.kwargs
        self.assertEqual(kwargs["market"], "US")
        self.assertEqual(kwargs["pool_type"], "LONG")
        self.assertEqual(kwargs["horizons"], [1, 5])
        self.assertTrue(kwargs["persist"])
        service.get_history.assert_called_once_with(5)

    def test_route_rejects_unknown_fields_and_maps_service_value_errors(self) -> None:
        service = MagicMock()
        service.run.side_effect = ValueError("invalid evaluation")
        with patch(
            "app.routers.stock_picker."
            "get_stock_picker_factor_evaluation_service",
            return_value=service,
        ):
            client = TestClient(app)
            invalid = client.post(
                "/api/stock-picker/factor-evaluation",
                json={
                    "market": "US",
                    "pool_type": "LONG",
                    "unknown": True,
                },
            )
            failed = client.post(
                "/api/stock-picker/factor-evaluation",
                json={"market": "US", "pool_type": "LONG"},
            )

        self.assertEqual(invalid.status_code, 422)
        self.assertEqual(failed.status_code, 400)
        self.assertEqual(failed.json()["detail"], "invalid evaluation")


if __name__ == "__main__":
    unittest.main()
