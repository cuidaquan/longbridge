from __future__ import annotations

import unittest
from unittest.mock import patch

from app.stock_picker import StockPickerService


class _FakeConnection:
    def __init__(self, rows=None) -> None:
        self.rows = rows or []
        self.statements: list[tuple[str, object]] = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None

    def execute(self, statement: str, parameters=None):
        self.statements.append((statement, parameters))
        return self

    def fetchall(self):
        return self.rows


class StockPickerPersistenceTest(unittest.TestCase):
    def test_analysis_save_persists_support_resistance_score(self) -> None:
        connection = _FakeConnection()
        service = StockPickerService()
        analysis = {
            "score": {
                "total": 75,
                "grade": "B",
                "breakdown": {
                    "trend": 20,
                    "momentum": 15,
                    "support_resistance": 12,
                    "volume": 10,
                    "pattern": 10,
                    "volatility": 8,
                },
                "signals": [],
            },
            "indicators": {"current_price": 12.5},
            "action": "BUY",
            "confidence": 0.8,
            "reasoning": ["test"],
        }

        with patch("app.stock_picker.get_connection", return_value=connection):
            service._save_analysis_result(
                pool_id=1,
                symbol="TEST.US",
                pool_type="LONG",
                analysis=analysis,
                recommendation_score=80,
                recommendation_reason="test",
            )

        statement, parameters = connection.statements[0]
        self.assertIn("score_support_resistance", statement)
        self.assertEqual(parameters[-1], 12)

    def test_analysis_response_returns_support_resistance_score(self) -> None:
        row = (
            1, 1, "TEST.US", "LONG", "2026-07-22", 12.5, 1.0, 3.0,
            75.0, "B", 20.0, 15.0, 10.0, 8.0, 10.0,
            "BUY", 0.8, '["test"]', None, '[]', 80.0, "test", None,
            12.0, "Test", "reason",
        )
        connection = _FakeConnection([row])
        service = StockPickerService()

        with patch("app.stock_picker.get_connection", return_value=connection):
            result = service.get_analysis_results()

        statement, _ = connection.statements[0]
        self.assertNotIn("a.*", statement)
        self.assertEqual(
            result["long_analysis"][0]["score"]["breakdown"]["support_resistance"],
            12.0,
        )
        self.assertEqual(result["long_analysis"][0]["name"], "Test")


def _trend_klines(direction: int, count: int = 120) -> list[dict]:
    """Build deterministic daily candles with a clear up or down trend."""
    klines = []
    for index in range(count):
        close = 100 + direction * index * 0.4
        open_price = close - direction * 0.25
        klines.append({
            "open": open_price,
            "high": max(open_price, close) + 0.5,
            "low": min(open_price, close) - 0.5,
            "close": close,
            "volume": 1_000_000 + index * 1_000,
        })
    return klines


class StockPickerDirectionalScoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = StockPickerService()
        self.uptrend = _trend_klines(1)
        self.downtrend = _trend_klines(-1)

    def test_long_opportunity_prefers_uptrend(self) -> None:
        up_score = self.service._calculate_advanced_score_v2(self.uptrend, "LONG")
        down_score = self.service._calculate_advanced_score_v2(self.downtrend, "LONG")

        self.assertGreater(up_score["total"], down_score["total"])
        self.assertEqual(up_score["opportunity_direction"], "LONG")

    def test_short_opportunity_prefers_downtrend(self) -> None:
        down_score = self.service._calculate_advanced_score_v2(self.downtrend, "SHORT")
        up_score = self.service._calculate_advanced_score_v2(self.uptrend, "SHORT")

        self.assertGreater(down_score["total"], up_score["total"])
        self.assertEqual(down_score["opportunity_direction"], "SHORT")
        self.assertGreater(down_score["breakdown"]["pattern"], up_score["breakdown"]["pattern"])

    def test_breakdown_is_directional_and_bounded(self) -> None:
        limits = {
            "trend": 25,
            "momentum": 20,
            "support_resistance": 15,
            "volume": 15,
            "pattern": 15,
            "volatility": 10,
        }

        for pool_type, klines in (("LONG", self.uptrend), ("SHORT", self.downtrend)):
            with self.subTest(pool_type=pool_type):
                score = self.service._calculate_advanced_score_v2(klines, pool_type)
                self.assertAlmostEqual(score["total"], sum(score["breakdown"].values()), places=1)
                for name, maximum in limits.items():
                    self.assertGreaterEqual(score["breakdown"][name], 0)
                    self.assertLessEqual(score["breakdown"][name], maximum)

    def test_short_action_uses_same_high_score_semantics_as_long(self) -> None:
        high_short_score = {
            "total": 80,
            "trend_strength": 0.8,
            "momentum_direction": "bearish",
        }
        low_short_score = {
            "total": 45,
            "trend_strength": 0.2,
            "momentum_direction": "bearish",
        }

        self.assertEqual(self.service._determine_action_v2(high_short_score, "SHORT"), "SELL")
        self.assertEqual(self.service._determine_action_v2(low_short_score, "SHORT"), "HOLD")

    def test_insufficient_data_returns_complete_neutral_breakdown(self) -> None:
        score = self.service._calculate_advanced_score_v2(self.uptrend[:20], "SHORT")

        self.assertEqual(score["total"], 50)
        self.assertEqual(sum(score["breakdown"].values()), 50)
        self.assertEqual(score["opportunity_direction"], "SHORT")

    def test_recommendation_score_covers_zero_to_one_hundred(self) -> None:
        maximum = self.service._calculate_recommendation_score_v2(
            {
                "total": 100,
                "trend_strength": 1,
                "momentum_direction": "bullish",
            },
            {"action": "BUY", "confidence": 1},
            "LONG",
        )
        minimum = self.service._calculate_recommendation_score_v2(
            {
                "total": 0,
                "trend_strength": 0,
                "momentum_direction": "bearish",
            },
            {"action": "BUY", "confidence": 1},
            "SHORT",
        )

        self.assertEqual(maximum, 100)
        self.assertEqual(minimum, 0)

    def test_ai_alignment_improves_recommendation(self) -> None:
        score = {
            "total": 70,
            "trend_strength": 0.7,
            "momentum_direction": "bearish",
        }
        aligned = self.service._calculate_recommendation_score_v2(
            score, {"action": "SELL", "confidence": 0.8}, "SHORT"
        )
        hold = self.service._calculate_recommendation_score_v2(
            score, {"action": "HOLD", "confidence": 0.8}, "SHORT"
        )
        opposed = self.service._calculate_recommendation_score_v2(
            score, {"action": "BUY", "confidence": 0.8}, "SHORT"
        )

        self.assertGreater(aligned, hold)
        self.assertGreater(hold, opposed)


if __name__ == "__main__":
    unittest.main()
