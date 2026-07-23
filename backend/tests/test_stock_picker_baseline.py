from __future__ import annotations

import unittest
from unittest.mock import patch

from app.stock_picker_baseline import (
    BASELINE_DATA_AS_OF,
    BASELINE_PARAMETERS,
    BASELINE_UNIVERSES,
    BASELINE_VERSION,
    baseline_history_symbols,
    build_stock_picker_baseline_snapshot,
    run_stock_picker_baselines,
    sync_stock_picker_baseline_history,
)


class _BacktestStub:
    def __init__(self) -> None:
        self.calls = []

    def run(self, pool_type, **kwargs):
        self.calls.append((pool_type, kwargs))
        metadata = dict(kwargs["report_metadata"])
        parameters = {
            key: value
            for key, value in kwargs.items()
            if key not in {"persist", "report_metadata"}
        }
        parameters["symbols"] = list(parameters["symbols"])
        horizon_metrics = {
            str(horizon): {
                "sample_count": 6,
                "avg_net_return": 0.01,
                "hit_rate": 0.5,
                "avg_excess_return": 0.005,
                "excess_coverage": 1.0,
                "max_drawdown": 0.02,
            }
            for horizon in parameters["horizons"]
        }
        all_metrics = {
            key: {
                **value,
                "avg_net_return": 0.004,
            }
            for key, value in horizon_metrics.items()
        }
        period = {
            "signal_start": "2025-01-01",
            "signal_end": "2025-12-31",
            "signal_dates": 2,
            "all": {
                "sample_count": 20,
                "avg_score": 60,
                "horizons": all_metrics,
            },
            "top_n": {
                "sample_count": 6,
                "avg_score": 70,
                "horizons": horizon_metrics,
            },
        }
        return {
            "id": len(self.calls),
            "score_version": "stock-picker-v2.1",
            "pool_type": pool_type,
            "metadata": metadata,
            "parameters": parameters,
            "data": {
                "signal_start": "2024-01-01",
                "signal_end": "2025-12-31",
                "data_as_of": "2026-01-31",
                "symbols_requested": len(parameters["symbols"]),
                "symbols_evaluated": len(parameters["symbols"]),
                "skipped_symbols": {},
                "sample_count": 100,
                "top_n_sample_count": 30,
                "benchmark_coverage": 1.0,
            },
            "periods": {
                "train": period,
                "validation": period,
                "all": period,
            },
            "walk_forward": [{
                "fold": 1,
                "validation_start": "2025-01-01",
                "validation_end": "2025-12-31",
                "validation_metrics": period,
            }],
        }


class StockPickerBaselineTests(unittest.TestCase):
    def test_definition_uses_non_overlapping_fixed_market_universes(
        self,
    ) -> None:
        self.assertGreaterEqual(
            BASELINE_PARAMETERS["step"],
            max(BASELINE_PARAMETERS["horizons"]),
        )
        self.assertEqual(set(BASELINE_UNIVERSES), {"US", "HK"})
        self.assertEqual(
            BASELINE_PARAMETERS["data_as_of"],
            BASELINE_DATA_AS_OF,
        )
        for universe in BASELINE_UNIVERSES.values():
            self.assertEqual(len(universe["symbols"]), 10)
            self.assertEqual(
                len(universe["symbols"]),
                len(set(universe["symbols"])),
            )

        history_symbols = baseline_history_symbols()
        self.assertEqual(len(history_symbols), len(set(history_symbols)))
        self.assertIn("SPY.US", history_symbols)
        self.assertIn("2800.HK", history_symbols)

    def test_runner_tags_and_executes_all_market_direction_pairs(
        self,
    ) -> None:
        service = _BacktestStub()

        reports = run_stock_picker_baselines(
            service=service,
            persist=False,
        )

        self.assertEqual(len(reports), 4)
        self.assertEqual(
            [
                (report["metadata"]["market"], report["pool_type"])
                for report in reports
            ],
            [
                ("US", "LONG"),
                ("US", "SHORT"),
                ("HK", "LONG"),
                ("HK", "SHORT"),
            ],
        )
        for pool_type, kwargs in service.calls:
            self.assertIn(pool_type, {"LONG", "SHORT"})
            self.assertFalse(kwargs["persist"])
            self.assertEqual(
                kwargs["report_metadata"]["baseline_version"],
                BASELINE_VERSION,
            )
            self.assertTrue(kwargs["report_metadata"]["limitations"])
            for key, value in BASELINE_PARAMETERS.items():
                self.assertEqual(kwargs[key], value)

    def test_snapshot_records_top_n_lift_and_walk_forward_boundaries(
        self,
    ) -> None:
        reports = run_stock_picker_baselines(
            service=_BacktestStub(),
            persist=False,
        )

        snapshot = build_stock_picker_baseline_snapshot(reports)

        self.assertEqual(snapshot["baseline_version"], BASELINE_VERSION)
        self.assertEqual(
            snapshot["score_versions"],
            ["stock-picker-v2.1"],
        )
        first = snapshot["reports"][0]
        self.assertAlmostEqual(
            first["validation"]["5"]["top_n_lift"],
            0.006,
        )
        self.assertEqual(
            first["walk_forward"][0]["validation_start"],
            "2025-01-01",
        )

    def test_history_sync_uses_frozen_universe_and_benchmarks(self) -> None:
        with patch(
            "app.stock_picker_baseline.sync_history_candlesticks",
            return_value={"AAPL.US": 1000},
        ) as sync:
            result = sync_stock_picker_baseline_history()

        self.assertEqual(result, {"AAPL.US": 1000})
        sync.assert_called_once_with(
            symbols=baseline_history_symbols(),
            period="day",
            adjust_type="no_adjust",
            count=1000,
            continue_on_error=True,
        )


if __name__ == "__main__":
    unittest.main()
