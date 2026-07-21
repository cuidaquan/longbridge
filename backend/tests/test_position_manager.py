from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient

from app.main import app
from app.auto_position_manager import AutoPositionManager


class _FakeConnection:
    def __init__(self) -> None:
        self.statements: list[tuple[str, object]] = []
        self._fetchone = (1, False)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None

    def execute(self, statement: str, parameters=None):
        self.statements.append((statement, parameters))
        if "COALESCE(MAX(id)" in statement:
            self._fetchone = (7,)
        return self

    def fetchone(self):
        return self._fetchone


class PositionManagerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)
        self.account_balance = {"USD": {"available_cash": 10_000}}
        self.positions = [
            {
                "symbol": "RLX.US",
                "qty": 500,
                "available_quantity": 500,
                "avg_price": 2.34,
            }
        ]

    def test_calculate_extracts_price_from_repository_snapshot(self) -> None:
        with (
            patch(
                "app.routers.position_manager.get_account_balance",
                return_value=self.account_balance,
            ),
            patch(
                "app.routers.position_manager.get_positions",
                return_value=self.positions,
            ),
            patch(
                "app.routers.position_manager.fetch_latest_prices",
                return_value={"RLX.US": {"price": 2.02, "source": "tick"}},
            ),
        ):
            response = self.client.post(
                "/position-manager/calculate",
                json={"symbol": "rlx.us", "action": "sell", "method": "equal_weight"},
            )

        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(2.02, response.json()["estimated_price"])
        self.assertEqual("sell", response.json()["action"])

    def test_batch_strategy_extracts_price_from_repository_snapshot(self) -> None:
        with (
            patch(
                "app.routers.position_manager.get_account_balance",
                return_value=self.account_balance,
            ),
            patch(
                "app.routers.position_manager.get_positions",
                return_value=self.positions,
            ),
            patch(
                "app.routers.position_manager.fetch_latest_prices",
                return_value={"RLX.US": {"price": 2.02, "source": "tick"}},
            ),
        ):
            response = self.client.post(
                "/position-manager/auto-strategy",
                json={"symbols": ["RLX.US"], "auto_execute": False},
            )

        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(2.02, response.json()[0]["recommendation"]["estimated_price"])

    def test_disabling_running_manager_does_not_restart_it(self) -> None:
        manager = Mock()
        manager.is_running.return_value = True
        manager.stop = AsyncMock()
        manager.start = AsyncMock()

        with (
            patch("app.db.get_connection", return_value=_FakeConnection()),
            patch(
                "app.routers.position_manager.get_auto_position_manager",
                return_value=manager,
            ),
        ):
            response = self.client.put(
                "/position-manager/auto/config",
                json={"enabled": False},
            )

        self.assertEqual(200, response.status_code, response.text)
        manager.stop.assert_awaited_once()
        manager.start.assert_not_awaited()

    def test_record_trade_assigns_integer_primary_key(self) -> None:
        connection = _FakeConnection()
        manager = object.__new__(AutoPositionManager)

        with patch("app.db.get_connection", return_value=connection):
            manager._record_trade(
                "SELL",
                "RLX.US",
                500,
                2.02,
                "simulation test",
            )

        insert = next(
            item for item in connection.statements if "INSERT INTO auto_position_trades" in item[0]
        )
        self.assertIn("(id, action", insert[0])
        self.assertEqual(7, insert[1][0])


if __name__ == "__main__":
    unittest.main()
