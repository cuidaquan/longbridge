from __future__ import annotations

from datetime import date, timedelta
import unittest
from unittest.mock import MagicMock, patch

import duckdb
from fastapi.testclient import TestClient

from app.db import _run_migrations
from app.main import app
from app.stock_picker_backtest import StockPickerBacktestService


def _bars(
    count: int,
    start_price: float = 100.0,
    daily_return: float = 0.01,
) -> list[dict]:
    rows = []
    price = start_price
    start = date(2024, 1, 1)
    for index in range(count):
        rows.append({
            "date": (start + timedelta(days=index)).isoformat(),
            "open": price * 0.995,
            "high": price * 1.01,
            "low": price * 0.99,
            "close": price,
            "volume": 1_000_000 + index * 1_000,
        })
        price *= 1 + daily_return
    return rows


class _StubStockPicker:
    SCORE_VERSION = "test-v2"

    def __init__(self, symbols: list[str] | None = None) -> None:
        self.symbols = symbols or ["AAA.US"]

    @staticmethod
    def _validate_pool_type(pool_type: str) -> str:
        normalized = pool_type.strip().upper()
        if normalized not in {"LONG", "SHORT"}:
            raise ValueError("pool_type 必须是 LONG 或 SHORT")
        return normalized

    @staticmethod
    def _normalize_symbol(symbol: str) -> str:
        return symbol.strip().upper()

    def get_pools(self, pool_type: str) -> dict:
        items = [
            {"symbol": symbol, "is_active": True}
            for symbol in self.symbols
        ]
        return {
            "long_pool": items if pool_type == "LONG" else [],
            "short_pool": items if pool_type == "SHORT" else [],
        }

    @staticmethod
    def _calculate_advanced_score_v2(
        history: list[dict],
        pool_type: str,
    ) -> dict:
        return {
            "total": float(history[-1]["close"]),
            "grade": f"{pool_type}-TEST",
        }


class _ConnectionContext:
    def __init__(self, connection) -> None:
        self.connection = connection

    def __enter__(self):
        return self.connection

    def __exit__(self, exc_type, exc, traceback) -> bool:
        return False


class StockPickerBacktestServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.stock_picker = _StubStockPicker()
        self.service = StockPickerBacktestService(
            stock_picker=self.stock_picker,
            bar_loader=lambda *args, **kwargs: [],
        )

    def test_signal_score_does_not_read_future_bars(self) -> None:
        original = _bars(90)
        modified = [dict(bar) for bar in original]
        signal_index = 40
        modified[signal_index + 5]["close"] *= 2

        first = self.service._evaluate_symbol(
            "AAA.US",
            "LONG",
            original,
            "SPY.US",
            {},
            [5],
            lookback=30,
            min_history=30,
            step=1,
        )
        second = self.service._evaluate_symbol(
            "AAA.US",
            "LONG",
            modified,
            "SPY.US",
            {},
            [5],
            lookback=30,
            min_history=30,
            step=1,
        )
        signal_date = original[signal_index]["date"]
        first_signal = next(
            record for record in first
            if record["signal_date"] == signal_date
        )
        second_signal = next(
            record for record in second
            if record["signal_date"] == signal_date
        )

        self.assertEqual(first_signal["score"], second_signal["score"])
        self.assertNotEqual(
            first_signal["returns"]["5"]["gross_return"],
            second_signal["returns"]["5"]["gross_return"],
        )

    def test_long_and_short_returns_have_opposite_direction(self) -> None:
        bars = _bars(70, daily_return=0.02)
        long_record = self.service._evaluate_symbol(
            "AAA.US", "LONG", bars, "SPY.US", {}, [5], 40, 30, 5
        )[0]
        short_record = self.service._evaluate_symbol(
            "AAA.US", "SHORT", bars, "SPY.US", {}, [5], 40, 30, 5
        )[0]

        self.assertGreater(long_record["returns"]["5"]["gross_return"], 0)
        self.assertLess(short_record["returns"]["5"]["gross_return"], 0)
        self.assertAlmostEqual(
            long_record["returns"]["5"]["gross_return"],
            -short_record["returns"]["5"]["gross_return"],
        )

    def test_time_split_and_walk_forward_are_strictly_ordered(self) -> None:
        symbols = ["AAA.US", "BBB.US"]
        stock_picker = _StubStockPicker(symbols)
        source = {
            "AAA.US": _bars(140, 100, 0.008),
            "BBB.US": _bars(140, 80, 0.004),
            "SPY.US": _bars(140, 200, 0.003),
        }

        service = StockPickerBacktestService(
            stock_picker=stock_picker,
            bar_loader=lambda symbol, period="day", limit=1000: source[
                symbol
            ][-limit:],
        )
        report = service.run(
            "LONG",
            horizons=[5, 10],
            lookback=60,
            max_bars=140,
            min_history=30,
            step=5,
            top_n=1,
            train_ratio=0.6,
            walk_forward_folds=3,
            persist=False,
        )

        train = report["periods"]["train"]
        validation = report["periods"]["validation"]
        self.assertLess(train["signal_end"], validation["signal_start"])
        self.assertGreater(len(report["walk_forward"]), 1)
        for fold in report["walk_forward"]:
            self.assertLess(fold["train_end"], fold["validation_start"])
            self.assertEqual(
                fold["validation_start"],
                fold["validation_metrics"]["signal_start"],
            )

    def test_top_n_is_limited_and_uses_score_then_symbol(self) -> None:
        records = [
            {"signal_date": "2024-01-01", "symbol": "CCC.US", "score": 70},
            {"signal_date": "2024-01-01", "symbol": "BBB.US", "score": 90},
            {"signal_date": "2024-01-01", "symbol": "AAA.US", "score": 90},
            {"signal_date": "2024-01-02", "symbol": "DDD.US", "score": 80},
            {"signal_date": "2024-01-02", "symbol": "EEE.US", "score": 60},
        ]

        selected = self.service._select_top_n(records, 2)
        by_date = {}
        for record in selected:
            by_date.setdefault(record["signal_date"], []).append(record)

        self.assertTrue(all(len(items) <= 2 for items in by_date.values()))
        self.assertEqual(
            [item["symbol"] for item in by_date["2024-01-01"]],
            ["AAA.US", "BBB.US"],
        )

    def test_transaction_cost_is_deducted_once_per_signal(self) -> None:
        records = [{
            "symbol": "AAA.US",
            "signal_date": "2024-01-01",
            "score": 80.0,
            "returns": {
                "5": {
                    "gross_return": 0.03,
                    "excess_return": 0.01,
                }
            },
        }]

        no_cost = self.service._summarize_records(records, [5], 0)
        with_cost = self.service._summarize_records(records, [5], 0.001)

        self.assertAlmostEqual(
            with_cost["horizons"]["5"]["avg_net_return"],
            0.029,
        )
        self.assertAlmostEqual(
            no_cost["horizons"]["5"]["avg_net_return"]
            - with_cost["horizons"]["5"]["avg_net_return"],
            0.001,
        )
        self.assertAlmostEqual(
            with_cost["horizons"]["5"]["estimated_cost_sum"],
            0.001,
        )

    def test_excess_return_and_missing_benchmark_coverage(self) -> None:
        bars = _bars(70, 100, 0.01)
        benchmark = _bars(70, 100, 0.005)
        benchmark_closes = {
            bar["date"]: bar["close"]
            for bar in benchmark
        }
        covered = self.service._evaluate_symbol(
            "AAA.US",
            "LONG",
            bars,
            "SPY.US",
            benchmark_closes,
            [5],
            40,
            30,
            5,
        )
        missing = self.service._evaluate_symbol(
            "AAA.US",
            "LONG",
            bars,
            "SPY.US",
            {},
            [5],
            40,
            30,
            5,
        )
        covered_metrics = self.service._summarize_records(covered, [5], 0)
        missing_metrics = self.service._summarize_records(missing, [5], 0)

        self.assertGreater(
            covered_metrics["horizons"]["5"]["avg_excess_return"],
            0,
        )
        self.assertEqual(
            covered_metrics["horizons"]["5"]["excess_coverage"],
            1,
        )
        self.assertIsNone(
            missing_metrics["horizons"]["5"]["avg_excess_return"],
        )
        self.assertEqual(
            missing_metrics["horizons"]["5"]["excess_coverage"],
            0,
        )

    def test_report_is_persisted_and_read_back(self) -> None:
        connection = duckdb.connect(":memory:")
        self.addCleanup(connection.close)
        _run_migrations(connection)
        source = {
            "AAA.US": _bars(100, 100, 0.01),
            "SPY.US": _bars(100, 200, 0.004),
        }
        service = StockPickerBacktestService(
            stock_picker=self.stock_picker,
            bar_loader=lambda symbol, period="day", limit=1000: source[
                symbol
            ][-limit:],
            connection_factory=lambda: _ConnectionContext(connection),
        )

        report = service.run(
            "LONG",
            horizons=[5],
            lookback=60,
            max_bars=100,
            min_history=30,
            step=5,
            persist=True,
        )
        history = service.get_history()

        self.assertGreater(report["id"], 0)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["id"], report["id"])
        self.assertEqual(history[0]["pool_type"], "LONG")
        self.assertEqual(history[0]["parameters"]["horizons"], [5])
        self.assertEqual(
            history[0]["result"]["score_version"],
            self.stock_picker.SCORE_VERSION,
        )

    def test_parameter_relationships_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "min_history"):
            self.service.run(
                "LONG",
                symbols=["AAA.US"],
                horizons=[5],
                min_history=80,
                lookback=60,
                persist=False,
            )


class StockPickerBacktestHttpTests(unittest.TestCase):
    def test_run_and_history_contracts(self) -> None:
        service = MagicMock()
        service.run.return_value = {
            "id": 7,
            "pool_type": "LONG",
            "score_version": "v2",
        }
        service.get_history.return_value = [{"id": 7}]

        with patch(
            "app.routers.stock_picker.get_stock_picker_backtest_service",
            return_value=service,
        ):
            with TestClient(app) as client:
                response = client.post(
                    "/api/stock-picker/backtest",
                    json={
                        "pool_type": "LONG",
                        "symbols": ["AAA.US"],
                        "horizons": [5, 10],
                        "lookback": 60,
                        "max_bars": 100,
                        "min_history": 30,
                        "step": 5,
                        "top_n": 2,
                        "train_ratio": 0.7,
                        "walk_forward_folds": 2,
                        "transaction_cost_bps": 15,
                    },
                )
                history = client.get(
                    "/api/stock-picker/backtests",
                    params={"limit": 3},
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["id"], 7)
        service.run.assert_called_once_with(
            pool_type="LONG",
            symbols=["AAA.US"],
            horizons=[5, 10],
            lookback=60,
            max_bars=100,
            min_history=30,
            step=5,
            top_n=2,
            train_ratio=0.7,
            walk_forward_folds=2,
            transaction_cost_bps=15.0,
        )
        self.assertEqual(history.status_code, 200)
        self.assertEqual(history.json(), {"items": [{"id": 7}]})
        service.get_history.assert_called_once_with(3)

    def test_invalid_http_parameters_are_rejected(self) -> None:
        service = MagicMock()
        with patch(
            "app.routers.stock_picker.get_stock_picker_backtest_service",
            return_value=service,
        ):
            with TestClient(app) as client:
                invalid_schema = client.post(
                    "/api/stock-picker/backtest",
                    json={"pool_type": "LONG", "top_n": 0},
                )
                service.run.side_effect = ValueError(
                    "必须满足 30 <= min_history <= lookback <= max_bars <= 5000"
                )
                invalid_relationship = client.post(
                    "/api/stock-picker/backtest",
                    json={
                        "pool_type": "LONG",
                        "min_history": 100,
                        "lookback": 60,
                    },
                )

        self.assertEqual(invalid_schema.status_code, 422)
        self.assertEqual(invalid_relationship.status_code, 400)
        self.assertIn("min_history", invalid_relationship.json()["detail"])


if __name__ == "__main__":
    unittest.main()
