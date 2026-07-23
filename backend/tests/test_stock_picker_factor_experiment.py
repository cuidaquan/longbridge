from __future__ import annotations

import unittest

import duckdb

from app.db import _run_migrations
from app.stock_picker_baseline import BASELINE_PARAMETERS
from app.stock_picker_factor_experiment import (
    EXPERIMENT_VERSION,
    MARKET_RS_VARIANTS,
    StockPickerFactorExperimentService,
)


class _BacktestStub:
    def __init__(self) -> None:
        self.calls = []

    def run(self, pool_type, **kwargs):
        self.calls.append((pool_type, kwargs))
        metadata = kwargs["report_metadata"]
        variant = metadata["variant"]
        variant_index = [
            item["name"]
            for item in MARKET_RS_VARIANTS
        ].index(variant)
        top_net = 0.01 + variant_index * 0.002
        all_net = 0.005
        horizons = {}
        all_horizons = {}
        for horizon in BASELINE_PARAMETERS["horizons"]:
            key = str(horizon)
            horizons[key] = {
                "sample_count": 36,
                "avg_net_return": top_net,
                "avg_excess_return": top_net - 0.002,
                "hit_rate": 0.6,
                "max_drawdown": 0.03,
            }
            all_horizons[key] = {
                "sample_count": 120,
                "avg_net_return": all_net,
                "avg_excess_return": all_net - 0.002,
                "hit_rate": 0.5,
                "max_drawdown": 0.04,
            }
        validation = {
            "signal_start": "2025-01-01",
            "signal_end": "2025-12-31",
            "signal_dates": 12,
            "all": {
                "sample_count": 120,
                "avg_score": 60,
                "horizons": all_horizons,
            },
            "top_n": {
                "sample_count": 36,
                "avg_score": 70,
                "horizons": horizons,
            },
        }
        return {
            "score_version": "stock-picker-v2.1",
            "pool_type": pool_type,
            "metadata": metadata,
            "parameters": {
                **{
                    key: value
                    for key, value in kwargs.items()
                    if key not in {
                        "persist",
                        "report_metadata",
                        "top_n_filter",
                    }
                },
                "market_benchmarks": {},
            },
            "data": {
                "signal_start": "2023-01-01",
                "signal_end": "2026-06-01",
                "data_as_of": BASELINE_PARAMETERS["data_as_of"],
                "symbols_requested": 10,
                "symbols_evaluated": 10,
                "skipped_symbols": {},
                "sample_count": 370,
                "top_n_sample_count": 111,
                "benchmark_coverage": 1.0,
            },
            "selection": {
                "all": {
                    "sample_count": 370,
                    "eligible_sample_count": 300,
                    "eligible_coverage": 300 / 370,
                    "signal_dates": 37,
                    "eligible_signal_dates": 37,
                    "underfilled_signal_dates": 0,
                },
                "validation": {
                    "sample_count": 120,
                    "eligible_sample_count": 100,
                    "eligible_coverage": 100 / 120,
                    "signal_dates": 12,
                    "eligible_signal_dates": 12,
                    "underfilled_signal_dates": 0,
                },
            },
            "periods": {
                "train": validation,
                "validation": validation,
                "all": validation,
            },
            "walk_forward": [
                {
                    "fold": fold,
                    "validation_metrics": validation,
                }
                for fold in range(1, 4)
            ],
        }


class _ConnectionContext:
    def __init__(self, connection) -> None:
        self.connection = connection

    def __enter__(self):
        return self.connection

    def __exit__(self, exc_type, exc, traceback) -> bool:
        return False


class StockPickerFactorExperimentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.backtest = _BacktestStub()

    def test_runner_executes_all_variants_for_market_direction_pairs(
        self,
    ) -> None:
        service = StockPickerFactorExperimentService(
            backtest_service=self.backtest,
        )

        snapshot = service.run(persist=False)

        self.assertEqual(
            snapshot["experiment_version"],
            EXPERIMENT_VERSION,
        )
        self.assertEqual(len(snapshot["groups"]), 4)
        self.assertEqual(
            len(self.backtest.calls),
            4 * len(MARKET_RS_VARIANTS),
        )
        self.assertEqual(
            [
                (group["market"], group["pool_type"])
                for group in snapshot["groups"]
            ],
            [
                ("US", "LONG"),
                ("US", "SHORT"),
                ("HK", "LONG"),
                ("HK", "SHORT"),
            ],
        )
        for _, kwargs in self.backtest.calls:
            self.assertFalse(kwargs["persist"])
            self.assertEqual(
                kwargs["data_as_of"],
                BASELINE_PARAMETERS["data_as_of"],
            )

    def test_variant_filter_requires_available_nonnegative_features(
        self,
    ) -> None:
        predicate = StockPickerFactorExperimentService._build_filter([
            "market_rs_10d",
            "market_rs_half_year",
        ])

        self.assertTrue(predicate({
            "features": {
                "market_rs_10d": 0,
                "market_rs_half_year": 0.01,
            }
        }))
        self.assertFalse(predicate({
            "features": {
                "market_rs_10d": -0.001,
                "market_rs_half_year": 0.01,
            }
        }))
        self.assertFalse(predicate({
            "features": {
                "market_rs_10d": 0.01,
                "market_rs_half_year": None,
            }
        }))

    def test_snapshot_reports_deltas_and_frozen_gate(self) -> None:
        service = StockPickerFactorExperimentService(
            backtest_service=self.backtest,
        )

        snapshot = service.run(persist=False)
        variants = {
            variant["name"]: variant
            for variant in snapshot["groups"][0]["variants"]
        }

        self.assertAlmostEqual(
            variants["market_rs_10d"]["delta_vs_quant"]["5"][
                "avg_net_return"
            ],
            0.002,
        )
        self.assertTrue(
            variants["market_rs_both"][
                "passes_frozen_return_gate"
            ]
        )
        self.assertTrue(all(
            snapshot["groups"][0]["quality_gate"].values()
        ))

    def test_snapshot_is_persisted_and_read_back(self) -> None:
        connection = duckdb.connect(":memory:")
        self.addCleanup(connection.close)
        _run_migrations(connection)
        service = StockPickerFactorExperimentService(
            backtest_service=self.backtest,
            connection_factory=lambda: _ConnectionContext(connection),
        )

        snapshot = service.run(persist=True)
        history = service.get_history()

        self.assertGreater(snapshot["id"], 0)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["id"], snapshot["id"])
        self.assertEqual(
            history[0]["experiment_version"],
            EXPERIMENT_VERSION,
        )
        self.assertEqual(
            history[0]["parameters"]["data_as_of"],
            BASELINE_PARAMETERS["data_as_of"],
        )
        self.assertEqual(
            len(history[0]["result"]["groups"]),
            4,
        )


if __name__ == "__main__":
    unittest.main()
