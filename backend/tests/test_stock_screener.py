from __future__ import annotations

from contextlib import contextmanager
import unittest
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app.exceptions import LongbridgeAPIError
from app.main import app
from app.stock_screener import StockScreenerService


class _Response:
    def __init__(self, data):
        self.data = data


class _ServiceWithContext(StockScreenerService):
    def __init__(self, context) -> None:
        self.context = context

    @contextmanager
    def _context(self):
        yield self.context


class StockScreenerServiceTest(unittest.TestCase):
    def test_strategy_lists_are_normalized_and_deduplicated(self) -> None:
        context = MagicMock()
        context.screener_recommend_strategies.return_value = _Response({
            "groups": [{
                "items": [
                    {
                        "id": "101",
                        "name": "盈利增长",
                        "description": "筛选持续增长公司",
                        "market": "US",
                    },
                    {
                        "id": 102,
                        "title": {"zh_cn": "低估值"},
                        "market": "-",
                    },
                ],
            }],
        })
        context.screener_user_strategies.return_value = _Response({
            "data": {
                "strategies": [
                    {"strategyId": 102, "strategyName": "重复策略"},
                    {"strategyId": 201, "strategyName": "我的策略"},
                ],
            },
        })
        service = _ServiceWithContext(context)

        result = service.list_strategies("us")

        self.assertEqual(result["market"], "US")
        self.assertEqual([item["id"] for item in result["items"]], [101, 102, 201])
        self.assertEqual(result["items"][0]["source"], "recommended")
        self.assertEqual(result["items"][1]["name"], "低估值")
        self.assertEqual(result["items"][1]["market"], "US")
        self.assertEqual(result["items"][2]["source"], "user")
        context.screener_recommend_strategies.assert_called_once_with("US")
        context.screener_user_strategies.assert_called_once_with("US")

    def test_user_strategy_failure_keeps_recommended_strategies(self) -> None:
        context = MagicMock()
        context.screener_recommend_strategies.return_value = _Response([
            {"id": 101, "name": "盈利增长"},
        ])
        context.screener_user_strategies.side_effect = RuntimeError(
            "permission denied"
        )
        service = _ServiceWithContext(context)

        result = service.list_strategies("US")

        self.assertEqual([item["id"] for item in result["items"]], [101])

    def test_search_uses_sdk_signature_and_normalizes_candidates(self) -> None:
        context = MagicMock()
        context.screener_search.return_value = _Response({
            "total_count": 23,
            "items": [
                {
                    "symbol": "AAPL.US",
                    "name": "Apple",
                    "market": "US",
                    "indicators": [
                        {"key": "prevchg", "value": "1.25%"},
                        {"key": "filter_pettm", "value": "31.4"},
                        {"key": "industry", "value": "Technology"},
                    ],
                },
                {
                    "stockSymbol": "MSFT.US",
                    "stockName": "Microsoft",
                    "indicators": {
                        "filter_pbmrq": 10.2,
                        "marketcap": "3.7T",
                    },
                },
            ],
        })
        service = _ServiceWithContext(context)

        result = service.search("US", 101, page=1, size=20)

        context.screener_search.assert_called_once_with(
            "US",
            101,
            [],
            [],
            1,
            20,
        )
        self.assertEqual(result["total"], 23)
        self.assertFalse(result["has_more"])
        self.assertEqual(
            [item["rank"] for item in result["items"]],
            [21, 22],
        )
        self.assertEqual(result["items"][0]["indicators"]["pettm"], "31.4")
        self.assertEqual(result["items"][1]["indicators"]["pbmrq"], 10.2)

    def test_search_supports_nested_payload_and_explicit_has_more(self) -> None:
        context = MagicMock()
        context.screener_search.return_value = _Response({
            "data": {
                "results": [{
                    "stock": {
                        "stock_code": "700.HK",
                        "name_cn": "腾讯控股",
                    },
                    "indicators": [
                        {"key": "marketcap", "value": "3.8T"},
                    ],
                }],
                "has_more": True,
            },
        })
        service = _ServiceWithContext(context)

        result = service.search("HK", 9, page=0, size=20)

        self.assertEqual(result["items"][0]["symbol"], "700.HK")
        self.assertEqual(result["items"][0]["name"], "腾讯控股")
        self.assertEqual(result["items"][0]["indicators"]["marketcap"], "3.8T")
        self.assertTrue(result["has_more"])

    def test_installed_sdk_exposes_required_screener_methods(self) -> None:
        from longbridge.openapi import ScreenerCondition, ScreenerContext

        self.assertTrue(hasattr(ScreenerContext, "screener_search"))
        self.assertTrue(hasattr(ScreenerContext, "screener_recommend_strategies"))
        condition = ScreenerCondition("pettm", "0", "30", "{}")
        self.assertEqual(condition.key, "pettm")

    def test_market_and_pagination_validation_happen_before_sdk_call(self) -> None:
        service = _ServiceWithContext(MagicMock())

        with self.assertRaisesRegex(ValueError, "US、HK、CN 或 SG"):
            service.search("JP", 1)
        with self.assertRaisesRegex(ValueError, "正整数"):
            service.search("US", 0)
        with self.assertRaisesRegex(ValueError, "1～100"):
            service.search("US", 1, size=101)

    def test_sdk_failure_is_exposed_as_longbridge_api_error(self) -> None:
        context = MagicMock()
        context.screener_search.side_effect = RuntimeError("temporary outage")
        service = _ServiceWithContext(context)

        with self.assertRaisesRegex(LongbridgeAPIError, "主动选股失败"):
            service.search("US", 1)


class StockScreenerRouteTest(unittest.TestCase):
    def test_strategy_and_search_http_contracts(self) -> None:
        screener = MagicMock()
        screener.list_strategies.return_value = {
            "market": "US",
            "source": "longbridge-screener",
            "items": [{
                "id": 101,
                "name": "盈利增长",
                "description": None,
                "market": "US",
                "source": "recommended",
            }],
        }
        screener.search.return_value = {
            "market": "US",
            "strategy_id": 101,
            "source": "longbridge-screener",
            "page": 0,
            "size": 20,
            "total": 1,
            "has_more": False,
            "items": [{
                "rank": 1,
                "symbol": "AAPL.US",
                "name": "Apple",
                "market": "US",
                "indicators": {},
            }],
        }

        with patch(
            "app.routers.stock_picker.get_stock_screener_service",
            return_value=screener,
        ):
            client = TestClient(app)
            try:
                strategies = client.get(
                    "/api/stock-picker/screener/strategies",
                    params={"market": "US", "include_user": "false"},
                )
                search = client.post(
                    "/api/stock-picker/screener/search",
                    json={
                        "market": "US",
                        "strategy_id": 101,
                        "page": 0,
                        "size": 20,
                    },
                )
                invalid = client.post(
                    "/api/stock-picker/screener/search",
                    json={"market": "US", "strategy_id": 0},
                )
            finally:
                client.close()

        self.assertEqual(strategies.status_code, 200)
        self.assertEqual(strategies.json()["items"][0]["id"], 101)
        self.assertEqual(search.status_code, 200)
        self.assertEqual(search.json()["items"][0]["symbol"], "AAPL.US")
        self.assertEqual(invalid.status_code, 422)
        screener.list_strategies.assert_called_once_with("US", False)
        screener.search.assert_called_once_with("US", 101, 0, 20)

    def test_import_keeps_manual_pool_and_records_strategy_source(self) -> None:
        stock_picker = MagicMock()
        stock_picker.add_stock.side_effect = [1, ValueError("股票池已满")]

        with patch(
            "app.routers.stock_picker.get_stock_picker_service",
            return_value=stock_picker,
        ):
            client = TestClient(app)
            try:
                response = client.post(
                    "/api/stock-picker/screener/import",
                    json={
                        "pool_type": "LONG",
                        "strategy_id": 101,
                        "strategy_name": "盈利增长",
                        "items": [
                            {"symbol": "AAPL.US", "name": "Apple"},
                            {"symbol": "MSFT.US", "name": "Microsoft"},
                        ],
                    },
                )
            finally:
                client.close()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["success"], ["AAPL.US"])
        self.assertEqual(response.json()["failed"][0]["symbol"], "MSFT.US")
        first_call = stock_picker.add_stock.call_args_list[0]
        self.assertEqual(first_call.args, ("LONG", "AAPL.US"))
        self.assertEqual(first_call.kwargs["name"], "Apple")
        self.assertIn("strategy_id=101", first_call.kwargs["added_reason"])

    def test_longbridge_failure_maps_to_bad_gateway(self) -> None:
        screener = MagicMock()
        screener.list_strategies.side_effect = LongbridgeAPIError("unavailable")

        with patch(
            "app.routers.stock_picker.get_stock_screener_service",
            return_value=screener,
        ):
            client = TestClient(app)
            try:
                response = client.get(
                    "/api/stock-picker/screener/strategies",
                    params={"market": "US"},
                )
            finally:
                client.close()

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["detail"], "unavailable")


if __name__ == "__main__":
    unittest.main()
