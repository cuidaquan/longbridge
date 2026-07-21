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


if __name__ == "__main__":
    unittest.main()
