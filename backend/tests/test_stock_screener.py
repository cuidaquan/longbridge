from __future__ import annotations

from contextlib import contextmanager
import unittest
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app.exceptions import LongbridgeAPIError
from app.external_service_resilience import (
    ExternalServiceTimeoutError,
    reset_stock_picker_reliability_metrics,
)
from app.main import app
from app.stock_screener import StockScreenerService


class _Response:
    def __init__(self, data):
        self.data = data


class _ServiceWithContext(StockScreenerService):
    def __init__(
        self,
        context,
        index_loader=None,
        short_risk_loader=None,
        tradeability_loader=None,
        fundamental_loader=None,
        margin_loader=None,
        short_capacity_loader=None,
        snapshot_service=None,
    ) -> None:
        super().__init__(
            index_loader=index_loader or (lambda _symbols: {}),
            short_risk_loader=(
                short_risk_loader
                or (lambda _symbols: {})
            ),
            tradeability_loader=(
                tradeability_loader
                or (
                    lambda symbols, _include_depth=False: {
                        symbol: {
                            "status": "available",
                            "trade_status": "normal",
                            "is_tradable": True,
                            "spread_bps": None,
                            "top_of_book_notional": None,
                            "impact_cost_status": (
                                "requires_order_size"
                            ),
                        }
                        for symbol in symbols
                    }
                )
            ),
            fundamental_loader=(
                fundamental_loader
                or (lambda *_args, **_kwargs: {})
            ),
            margin_loader=margin_loader or (lambda _symbols: {}),
            short_capacity_loader=(
                short_capacity_loader
                or (lambda _symbols: {})
            ),
            snapshot_service=snapshot_service,
        )
        self.context = context

    @contextmanager
    def _context(self):
        yield self.context


class StockScreenerServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        reset_stock_picker_reliability_metrics()

    def tearDown(self) -> None:
        reset_stock_picker_reliability_metrics()

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
        self.assertEqual(result["items"][0]["source_page"], 1)
        self.assertEqual(result["scan"]["mode"], "single_page")
        self.assertEqual(result["scan"]["pages_scanned"], 1)
        self.assertIsNone(result["scan"]["next_page"])

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

    def test_bounded_scan_preserves_order_deduplicates_and_stops_at_end(self) -> None:
        context = MagicMock()

        def search_page(_market, _strategy_id, _conditions, _show, page, _size):
            pages = {
                0: {
                    "total": 6,
                    "has_more": True,
                    "items": [
                        {"symbol": "AAA.US", "name": "AAA"},
                        {"symbol": "BBB.US", "name": "BBB"},
                    ],
                },
                1: {
                    "total": 6,
                    "has_more": True,
                    "items": [
                        {"symbol": "BBB.US", "name": "BBB duplicate"},
                        {"symbol": "CCC.US", "name": "CCC"},
                    ],
                },
                2: {
                    "total": 6,
                    "has_more": False,
                    "items": [{"symbol": "DDD.US", "name": "DDD"}],
                },
            }
            return _Response(pages[page])

        context.screener_search.side_effect = search_page
        fundamental_loader = MagicMock()
        margin_loader = MagicMock()
        short_capacity_loader = MagicMock()
        service = _ServiceWithContext(
            context,
            fundamental_loader=fundamental_loader,
            margin_loader=margin_loader,
            short_capacity_loader=short_capacity_loader,
        )

        result = service.search(
            "US",
            101,
            size=20,
            scan_pages=5,
            include_indexes=False,
            include_tradeability=False,
            require_normal_trade_status=False,
        )

        self.assertEqual(
            [item["symbol"] for item in result["items"]],
            ["AAA.US", "BBB.US", "CCC.US", "DDD.US"],
        )
        self.assertEqual(
            [item["source_page"] for item in result["items"]],
            [0, 0, 1, 2],
        )
        self.assertEqual(result["filters"]["before"], 5)
        self.assertEqual(result["filters"]["after"], 4)
        self.assertEqual(result["filters"]["excluded"], 1)
        self.assertEqual(result["filters"]["reasons"], {"duplicate_symbol": 1})
        self.assertEqual(
            result["relative_strength"]["industry_basis"],
            "scan_range_leave_one_out_industry_median",
        )
        self.assertEqual(result["scan"], {
            "mode": "bounded",
            "requested_pages": 5,
            "pages_scanned": 3,
            "first_page": 0,
            "last_page": 2,
            "next_page": None,
            "candidates_scanned": 5,
            "candidates_returned": 4,
            "duplicates_removed": 1,
            "stopped_reason": "source_exhausted",
        })
        self.assertEqual(context.screener_search.call_count, 3)
        fundamental_loader.assert_not_called()
        margin_loader.assert_not_called()
        short_capacity_loader.assert_not_called()

    def test_snapshot_capture_preserves_universe_dedup_and_filter_outcomes(
        self,
    ) -> None:
        context = MagicMock()

        def search_page(_market, _strategy_id, _conditions, _show, page, _size):
            pages = {
                0: {
                    "total": 4,
                    "has_more": True,
                    "items": [
                        {
                            "symbol": "AAA.US",
                            "name": "AAA",
                            "indicators": {"industry": "Technology"},
                        },
                        {
                            "symbol": "BBB.US",
                            "name": "BBB",
                            "indicators": {"industry": "Technology"},
                        },
                    ],
                },
                1: {
                    "total": 4,
                    "has_more": False,
                    "items": [
                        {
                            "symbol": "BBB.US",
                            "name": "BBB duplicate",
                            "indicators": {"industry": "Technology"},
                        },
                        {
                            "symbol": "CCC.US",
                            "name": "CCC",
                            "indicators": {"industry": "Technology"},
                        },
                    ],
                },
            }
            return _Response(pages[page])

        context.screener_search.side_effect = search_page

        def indexes(symbols):
            turnovers = {
                "AAA.US": 100.0,
                "BBB.US": 200.0,
                "CCC.US": 300.0,
                "SPY.US": 1_000.0,
            }
            changes = {
                "AAA.US": 0.1,
                "BBB.US": 0.2,
                "CCC.US": 0.3,
                "SPY.US": 0.05,
            }
            return {
                symbol: {
                    "turnover": turnovers[symbol],
                    "ten_day_change_rate": changes[symbol],
                    "half_year_change_rate": changes[symbol],
                }
                for symbol in symbols
            }

        snapshot_service = MagicMock()
        snapshot_service.capture.side_effect = lambda payload: {
            "status": "captured",
            "snapshot_id": "snapshot-1",
            "captured_at": "2026-07-24T00:00:00+00:00",
            "snapshot_version": "stock-screener-scan-snapshot-v2",
            "payload_hash": "hash",
            "candidates_unique": len(payload["universe"]),
        }
        service = _ServiceWithContext(
            context,
            index_loader=indexes,
            snapshot_service=snapshot_service,
        )

        result = service.search(
            "US",
            101,
            scan_pages=2,
            size=2,
            filters={"min_turnover": 150},
            capture_snapshot=True,
            strategy_name="Growth",
            strategy_source="recommended",
        )

        self.assertEqual(
            [item["symbol"] for item in result["items"]],
            ["BBB.US", "CCC.US"],
        )
        self.assertEqual(result["snapshot"]["snapshot_id"], "snapshot-1")
        payload = snapshot_service.capture.call_args.args[0]
        self.assertEqual(payload["request"]["strategy"]["name"], "Growth")
        self.assertEqual(payload["pages"][0]["symbols"], ["AAA.US", "BBB.US"])
        self.assertEqual(len(payload["occurrences"]), 4)
        duplicate = payload["occurrences"][2]
        self.assertFalse(duplicate["retained"])
        self.assertEqual(duplicate["duplicate_of_scan_order"], 2)
        self.assertEqual(
            [item["candidate"]["symbol"] for item in payload["universe"]],
            ["AAA.US", "BBB.US", "CCC.US"],
        )
        self.assertEqual(
            payload["universe"][0]["exclusion_reason"],
            "below_min_turnover",
        )
        self.assertFalse(payload["universe"][0]["selected"])
        self.assertEqual(
            payload["universe"][1]["candidate"]["relative_strength"][
                "industry_peer_count"
            ],
            2,
        )
        self.assertEqual(payload["selected_symbols"], ["BBB.US", "CCC.US"])
        self.assertEqual(
            payload["metric_basis"]["benchmark_observations"],
            [
                {
                    "page": 0,
                    "ten_day_change_rate": 0.05,
                    "half_year_change_rate": 0.05,
                },
                {
                    "page": 1,
                    "ten_day_change_rate": 0.05,
                    "half_year_change_rate": 0.05,
                },
            ],
        )

    def test_snapshot_capture_is_disabled_by_default(self) -> None:
        context = MagicMock()
        context.screener_search.return_value = _Response({
            "total": 0,
            "has_more": False,
            "items": [],
        })
        snapshot_service = MagicMock()
        service = _ServiceWithContext(
            context,
            snapshot_service=snapshot_service,
        )

        result = service.search("US", 101)

        self.assertEqual(result["snapshot"], {"status": "disabled"})
        snapshot_service.capture.assert_not_called()

    def test_bounded_scan_aggregates_filters_and_exposes_next_page(self) -> None:
        context = MagicMock()

        def search_page(_market, _strategy_id, _conditions, _show, page, _size):
            return _Response({
                "total": 8,
                "has_more": True,
                "items": [
                    {"symbol": f"FAIL{page}.US", "name": "Fail"},
                    {"symbol": f"PASS{page}.US", "name": "Pass"},
                ],
            })

        def load_indexes(symbols):
            result = {"SPY.US": {}}
            for symbol in symbols:
                if symbol != "SPY.US":
                    result[symbol] = {
                        "turnover": 100 if symbol.startswith("PASS") else 1,
                    }
            return result

        context.screener_search.side_effect = search_page
        service = _ServiceWithContext(context, index_loader=load_indexes)

        result = service.search(
            "US",
            101,
            size=2,
            scan_pages=2,
            filters={"min_turnover": 50},
            include_tradeability=False,
            require_normal_trade_status=False,
        )

        self.assertEqual(
            [item["symbol"] for item in result["items"]],
            ["PASS0.US", "PASS1.US"],
        )
        self.assertEqual(result["filters"]["before"], 4)
        self.assertEqual(result["filters"]["after"], 2)
        self.assertEqual(result["filters"]["excluded"], 2)
        self.assertEqual(result["filters"]["reasons"], {"below_min_turnover": 2})
        self.assertEqual(result["scan"]["next_page"], 2)
        self.assertEqual(result["scan"]["stopped_reason"], "page_limit")
        self.assertTrue(result["has_more"])

    def test_bounded_scan_recomputes_directional_industry_rs_before_filtering(
        self,
    ) -> None:
        context = MagicMock()

        def search_page(_market, _strategy_id, _conditions, _show, page, _size):
            symbols = (
                ("LEADER.US", "MIDDLE.US")
                if page == 0
                else ("LEADER.US", "LAGGARD.US")
            )
            return _Response({
                "total": 4,
                "has_more": page == 0,
                "items": [
                    {
                        "symbol": symbol,
                        "name": symbol,
                        "indicators": [{
                            "key": "industry",
                            "value": "Technology",
                        }],
                    }
                    for symbol in symbols
                ],
            })

        indexes = {
            "LEADER.US": {
                "ten_day_change_rate": 0.10,
                "half_year_change_rate": 0.30,
            },
            "LAGGARD.US": {
                "ten_day_change_rate": 0.00,
                "half_year_change_rate": 0.10,
            },
            "MIDDLE.US": {
                "ten_day_change_rate": 0.04,
                "half_year_change_rate": 0.20,
            },
            "SPY.US": {
                "ten_day_change_rate": 0.02,
                "half_year_change_rate": 0.15,
            },
        }

        context.screener_search.side_effect = search_page
        service = _ServiceWithContext(
            context,
            index_loader=lambda symbols: {
                symbol: indexes[symbol]
                for symbol in symbols
            },
        )

        result = service.search(
            "US",
            101,
            size=2,
            scan_pages=2,
            filters={"min_industry_rs_10d": 0.04},
            include_tradeability=False,
            require_normal_trade_status=False,
        )

        self.assertEqual(
            result["relative_strength"]["industry_basis"],
            "scan_range_leave_one_out_industry_median",
        )
        self.assertEqual(
            [item["symbol"] for item in result["items"]],
            ["LEADER.US"],
        )
        relative_strength = result["items"][0]["relative_strength"]
        self.assertEqual(relative_strength["industry_peer_count"], 2)
        self.assertEqual(relative_strength["industry_peer_counts"]["10d"], 2)
        self.assertEqual(relative_strength["industry_rs_10d"], 0.08)
        self.assertEqual(relative_strength["market_rs_10d"], 0.08)
        self.assertEqual(
            result["relative_strength"]["benchmark_observations"],
            [
                {
                    "page": 0,
                    "ten_day_change_rate": 0.02,
                    "half_year_change_rate": 0.15,
                },
                {
                    "page": 1,
                    "ten_day_change_rate": 0.02,
                    "half_year_change_rate": 0.15,
                },
            ],
        )
        self.assertEqual(result["filters"]["before"], 4)
        self.assertEqual(result["filters"]["after"], 1)
        self.assertEqual(result["filters"]["excluded"], 3)
        self.assertEqual(
            result["filters"]["reasons"],
            {
                "below_min_industry_rs_10d": 2,
                "duplicate_symbol": 1,
            },
        )

        short_result = service.search(
            "US",
            101,
            size=2,
            scan_pages=2,
            target_direction="SHORT",
            filters={"min_industry_rs_10d": 0.04},
            include_short_risk=False,
            include_tradeability=False,
            require_normal_trade_status=False,
        )

        self.assertEqual(
            [item["symbol"] for item in short_result["items"]],
            ["LAGGARD.US"],
        )
        short_relative_strength = short_result["items"][0][
            "relative_strength"
        ]
        self.assertEqual(short_relative_strength["industry_peer_count"], 2)
        self.assertEqual(short_relative_strength["industry_rs_10d"], 0.07)
        self.assertEqual(short_relative_strength["market_rs_10d"], 0.02)

    def test_bounded_scan_stopping_on_first_page_keeps_single_page_rs(self) -> None:
        context = MagicMock()
        context.screener_search.return_value = _Response({
            "total": 3,
            "has_more": False,
            "items": [
                {
                    "symbol": "LEADER.US",
                    "name": "Leader",
                    "indicators": [{
                        "key": "industry",
                        "value": "Technology",
                    }],
                },
                {
                    "symbol": "MIDDLE.US",
                    "name": "Middle",
                    "indicators": [{
                        "key": "industry",
                        "value": "Technology",
                    }],
                },
                {
                    "symbol": "LAGGARD.US",
                    "name": "Laggard",
                    "indicators": [{
                        "key": "industry",
                        "value": "Technology",
                    }],
                },
            ],
        })
        indexes = {
            "LEADER.US": {"ten_day_change_rate": 0.10},
            "MIDDLE.US": {"ten_day_change_rate": 0.04},
            "LAGGARD.US": {"ten_day_change_rate": 0.00},
            "SPY.US": {"ten_day_change_rate": 0.02},
        }
        service = _ServiceWithContext(
            context,
            index_loader=lambda symbols: {
                symbol: indexes[symbol]
                for symbol in symbols
            },
        )

        result = service.search(
            "US",
            101,
            size=20,
            scan_pages=3,
            filters={"min_industry_rs_10d": 0.04},
            include_tradeability=False,
            require_normal_trade_status=False,
        )

        self.assertEqual(result["scan"]["mode"], "bounded")
        self.assertEqual(result["scan"]["pages_scanned"], 1)
        self.assertEqual(
            result["relative_strength"]["industry_basis"],
            "current_page_leave_one_out_industry_median",
        )
        self.assertEqual(
            [item["symbol"] for item in result["items"]],
            ["LEADER.US"],
        )
        self.assertEqual(
            result["items"][0]["relative_strength"]["industry_rs_10d"],
            0.08,
        )

    def test_scan_caps_an_overfilled_upstream_page_to_requested_size(self) -> None:
        context = MagicMock()
        context.screener_search.return_value = _Response({
            "total": 3,
            "has_more": False,
            "items": [
                {"symbol": "AAA.US", "name": "AAA"},
                {"symbol": "BBB.US", "name": "BBB"},
                {"symbol": "CCC.US", "name": "CCC"},
            ],
        })
        service = _ServiceWithContext(context)

        result = service.search(
            "US",
            101,
            size=2,
            include_indexes=False,
            include_tradeability=False,
            require_normal_trade_status=False,
        )

        self.assertEqual(
            [item["symbol"] for item in result["items"]],
            ["AAA.US", "BBB.US"],
        )
        self.assertEqual(result["scan"]["candidates_scanned"], 2)

    def test_bounded_scan_fails_closed_when_a_later_page_fails(self) -> None:
        context = MagicMock()
        context.screener_search.side_effect = [
            _Response({
                "total": 4,
                "has_more": True,
                "items": [{"symbol": "AAA.US", "name": "AAA"}],
            }),
            RuntimeError("second page unavailable"),
        ]
        service = _ServiceWithContext(context)

        with self.assertRaisesRegex(LongbridgeAPIError, "主动选股失败"):
            service.search(
                "US",
                101,
                size=20,
                scan_pages=2,
                include_indexes=False,
                include_tradeability=False,
                require_normal_trade_status=False,
            )

        self.assertEqual(context.screener_search.call_count, 3)
        self.assertEqual(
            [call.args[4] for call in context.screener_search.call_args_list],
            [0, 1, 1],
        )

    def test_search_normalizes_real_sdk_counter_ids(self) -> None:
        context = MagicMock()
        context.screener_search.return_value = _Response({
            "total": 2,
            "items": [
                {
                    "counter_id": "ST/US/RGBP",
                    "name": "Regen BioPharma",
                    "indicators": [
                        {
                            "key": "industry",
                            "value": "Biotechnology",
                        },
                    ],
                },
                {
                    "counter_id": "ST/HK/00700",
                    "name": "Tencent",
                    "indicators": [],
                },
            ],
        })
        service = _ServiceWithContext(context)

        result = service.search(
            "US",
            19,
            include_indexes=False,
        )

        self.assertEqual(
            [item["symbol"] for item in result["items"]],
            ["RGBP.US", "700.HK"],
        )
        self.assertEqual(result["items"][0]["market"], "US")
        self.assertEqual(result["items"][1]["market"], "HK")
        self.assertEqual(
            result["items"][0]["indicators"]["industry"],
            "Biotechnology",
        )

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
        with self.assertRaisesRegex(ValueError, "1～5"):
            service.search("US", 1, scan_pages=6)
        with self.assertRaisesRegex(ValueError, "结束页不能大于 10000"):
            service.search("US", 1, page=9999, scan_pages=3)
        with self.assertRaisesRegex(ValueError, "乘积不能超过 100"):
            service.search("US", 1, size=100, scan_pages=2)
        with self.assertRaisesRegex(ValueError, "有限数字"):
            service.search("US", 1, filters={"min_turnover": float("nan")})
        with self.assertRaisesRegex(
            ValueError,
            "min_revenue_yoy 不能大于 max_revenue_yoy",
        ):
            service.search(
                "US",
                1,
                filters={
                    "min_revenue_yoy": 0.2,
                    "max_revenue_yoy": 0.1,
                },
            )
        with self.assertRaisesRegex(ValueError, "不能大于 1"):
            service.search(
                "US",
                1,
                filters={"min_analyst_alignment": 1.1},
            )
        service.context.screener_search.assert_not_called()

    def test_sdk_failure_is_exposed_as_longbridge_api_error(self) -> None:
        context = MagicMock()
        context.screener_search.side_effect = RuntimeError("temporary outage")
        service = _ServiceWithContext(context)

        with self.assertRaisesRegex(LongbridgeAPIError, "主动选股失败"):
            service.search("US", 1)

    def test_calc_indexes_enrich_and_filter_candidates(self) -> None:
        context = MagicMock()
        context.screener_search.return_value = _Response({
            "items": [
                {"symbol": "AAPL.US", "name": "Apple"},
                {"symbol": "PENNY.US", "name": "Penny"},
                {"symbol": "MISSING.US", "name": "Missing"},
            ],
        })
        index_loader = MagicMock(return_value={
            "AAPL.US": {
                "turnover": 50_000_000,
                "total_market_value": 3_000_000_000,
                "pe_ttm_ratio": 30,
            },
            "PENNY.US": {
                "turnover": 50_000,
                "total_market_value": 20_000_000,
                "pe_ttm_ratio": 15,
            },
        })
        service = _ServiceWithContext(context, index_loader=index_loader)

        result = service.search(
            "US",
            101,
            filters={
                "min_turnover": 1_000_000,
                "max_pe_ttm": 40,
            },
        )

        index_loader.assert_called_once_with([
            "AAPL.US",
            "PENNY.US",
            "MISSING.US",
            "SPY.US",
        ])
        self.assertEqual([item["symbol"] for item in result["items"]], ["AAPL.US"])
        self.assertEqual(result["items"][0]["indexes"]["pe_ttm_ratio"], 30)
        self.assertEqual(result["filters"]["excluded"], 2)
        self.assertEqual(
            result["filters"]["reasons"],
            {
                "below_min_turnover": 1,
                "missing_turnover": 1,
            },
        )
        self.assertEqual(result["enrichment"]["status"], "available")

    def test_relative_strength_is_directional_and_excludes_self(self) -> None:
        context = MagicMock()
        context.screener_search.return_value = _Response({
            "items": [
                {
                    "symbol": "LEADER.US",
                    "name": "Leader",
                    "indicators": [{"key": "industry", "value": "Technology"}],
                },
                {
                    "symbol": "MIDDLE.US",
                    "name": "Middle",
                    "indicators": [{"key": "industry", "value": "Technology"}],
                },
                {
                    "symbol": "LAGGARD.US",
                    "name": "Laggard",
                    "indicators": [{"key": "industry", "value": "Technology"}],
                },
            ],
        })
        indexes = {
            "LEADER.US": {
                "ten_day_change_rate": 0.10,
                "half_year_change_rate": 0.30,
            },
            "LAGGARD.US": {
                "ten_day_change_rate": 0.04,
                "half_year_change_rate": 0.10,
            },
            "MIDDLE.US": {
                "ten_day_change_rate": 0.06,
                "half_year_change_rate": 0.20,
            },
            "SPY.US": {
                "ten_day_change_rate": 0.05,
                "half_year_change_rate": 0.15,
            },
        }
        service = _ServiceWithContext(
            context,
            index_loader=lambda _symbols: indexes,
        )

        long_result = service.search(
            "US",
            101,
            target_direction="LONG",
            filters={
                "min_market_rs_10d": 0,
                "min_industry_rs_10d": 0,
            },
        )
        short_result = service.search(
            "US",
            101,
            target_direction="SHORT",
            filters={
                "min_market_rs_10d": 0,
                "min_industry_rs_10d": 0,
            },
        )

        self.assertEqual(
            [item["symbol"] for item in long_result["items"]],
            ["LEADER.US"],
        )
        self.assertEqual(
            long_result["items"][0]["relative_strength"]["market_rs_10d"],
            0.05,
        )
        self.assertEqual(
            long_result["items"][0]["relative_strength"]["industry_rs_10d"],
            0.05,
        )
        self.assertEqual(
            long_result["relative_strength"]["benchmark_returns"],
            {
                "ten_day_change_rate": 0.05,
                "half_year_change_rate": 0.15,
            },
        )
        self.assertEqual(
            [item["symbol"] for item in short_result["items"]],
            ["LAGGARD.US"],
        )
        self.assertEqual(
            short_result["items"][0]["relative_strength"]["market_rs_10d"],
            0.01,
        )
        self.assertEqual(
            short_result["items"][0]["relative_strength"]["industry_rs_10d"],
            0.04,
        )

    def test_industry_rs_requires_two_other_valid_peers(self) -> None:
        context = MagicMock()
        context.screener_search.return_value = _Response({
            "items": [
                {
                    "symbol": symbol,
                    "name": symbol,
                    "indicators": [{
                        "key": "industry",
                        "value": "Technology",
                    }],
                }
                for symbol in ("FIRST.US", "SECOND.US")
            ],
        })
        service = _ServiceWithContext(
            context,
            index_loader=lambda _symbols: {
                "FIRST.US": {"ten_day_change_rate": 0.10},
                "SECOND.US": {"ten_day_change_rate": 0.04},
                "SPY.US": {"ten_day_change_rate": 0.05},
            },
        )

        result = service.search("US", 101)

        self.assertEqual(result["relative_strength"]["minimum_industry_peers"], 2)
        self.assertFalse(
            result["relative_strength"]["historical_industry_membership"]
        )
        for candidate in result["items"]:
            relative = candidate["relative_strength"]
            self.assertEqual(relative["industry_peer_count"], 1)
            self.assertEqual(relative["industry_peer_counts"]["10d"], 1)
            self.assertIsNone(relative["industry_rs_10d"])

    def test_short_risk_filter_is_explicit_and_does_not_claim_availability(self) -> None:
        context = MagicMock()
        context.screener_search.return_value = _Response({
            "items": [
                {"symbol": "SAFE.US", "name": "Safe"},
                {"symbol": "CROWDED.US", "name": "Crowded"},
            ],
        })
        indexes = {
            "SAFE.US": {"ten_day_change_rate": -0.02},
            "CROWDED.US": {"ten_day_change_rate": -0.03},
            "SPY.US": {"ten_day_change_rate": 0.01},
        }
        short_loader = MagicMock(return_value={
            "SAFE.US": {
                "status": "available",
                "days_to_cover": 2.0,
                "short_ratio": 0.1,
            },
            "CROWDED.US": {
                "status": "available",
                "days_to_cover": 8.0,
                "short_ratio": 0.3,
            },
        })
        service = _ServiceWithContext(
            context,
            index_loader=lambda _symbols: indexes,
            short_risk_loader=short_loader,
        )

        result = service.search(
            "US",
            101,
            target_direction="SHORT",
            filters={"max_days_to_cover": 5},
        )

        self.assertEqual([item["symbol"] for item in result["items"]], ["SAFE.US"])
        self.assertEqual(result["short_risk"]["status"], "available")
        self.assertNotIn("borrow_available", result["items"][0]["short_risk"])
        short_loader.assert_called_once_with(["SAFE.US", "CROWDED.US"])

    def test_trade_status_and_depth_filters_skip_unrelated_indexes(self) -> None:
        context = MagicMock()
        context.screener_search.return_value = _Response({
            "items": [
                {"symbol": "GOOD.US", "name": "Good"},
                {"symbol": "HALT.US", "name": "Halted"},
                {"symbol": "WIDE.US", "name": "Wide"},
            ],
        })
        index_loader = MagicMock(return_value={})
        tradeability_loader = MagicMock(return_value={
            "GOOD.US": {
                "status": "available",
                "trade_status": "normal",
                "is_tradable": True,
                "spread_bps": 20,
                "top_of_book_notional": 200_000,
                "impact_cost_status": "requires_order_size",
            },
            "HALT.US": {
                "status": "available",
                "trade_status": "halted",
                "is_tradable": False,
                "spread_bps": 10,
                "top_of_book_notional": 300_000,
                "impact_cost_status": "requires_order_size",
            },
            "WIDE.US": {
                "status": "available",
                "trade_status": "normal",
                "is_tradable": True,
                "spread_bps": 80,
                "top_of_book_notional": 300_000,
                "impact_cost_status": "requires_order_size",
            },
        })
        service = _ServiceWithContext(
            context,
            index_loader=index_loader,
            tradeability_loader=tradeability_loader,
        )

        result = service.search(
            "US",
            101,
            include_indexes=False,
            filters={
                "max_spread_bps": 50,
                "min_top_of_book_notional": 100_000,
            },
        )

        self.assertEqual(
            [item["symbol"] for item in result["items"]],
            ["GOOD.US"],
        )
        self.assertEqual(
            result["filters"]["reasons"],
            {
                "trade_status_halted": 1,
                "above_max_spread_bps": 1,
            },
        )
        self.assertTrue(result["tradeability"]["depth_included"])
        self.assertEqual(
            result["items"][0]["tradeability"]["spread_bps"],
            20,
        )
        tradeability_loader.assert_called_once_with(
            ["GOOD.US", "HALT.US", "WIDE.US"],
            True,
        )
        index_loader.assert_not_called()

    def test_tradeability_without_depth_and_failure_semantics(self) -> None:
        context = MagicMock()
        context.screener_search.return_value = _Response({
            "items": [{"symbol": "AAA.US", "name": "Alpha"}],
        })
        tradeability_loader = MagicMock(return_value={
            "AAA.US": {
                "status": "available",
                "trade_status": "normal",
                "is_tradable": True,
            },
        })
        service = _ServiceWithContext(
            context,
            tradeability_loader=tradeability_loader,
        )

        result = service.search(
            "US",
            101,
            include_indexes=False,
        )

        tradeability_loader.assert_called_once_with(["AAA.US"], False)
        self.assertFalse(result["tradeability"]["depth_included"])

        def fail_tradeability(_symbols, _include_depth):
            raise RuntimeError("quote unavailable")

        failing_service = _ServiceWithContext(
            context,
            tradeability_loader=fail_tradeability,
        )
        degraded = failing_service.search(
            "US",
            101,
            include_indexes=False,
            require_normal_trade_status=False,
        )
        self.assertEqual(degraded["tradeability"]["status"], "fallback")
        self.assertEqual(
            [item["symbol"] for item in degraded["items"]],
            ["AAA.US"],
        )

        with self.assertRaisesRegex(
            LongbridgeAPIError,
            "无法应用交易可执行性过滤",
        ):
            failing_service.search(
                "US",
                101,
                include_indexes=False,
            )

        with self.assertRaisesRegex(
            ValueError,
            "include_tradeability=true",
        ):
            service.search(
                "US",
                101,
                include_tradeability=False,
                require_normal_trade_status=True,
            )

    def test_candidate_batches_do_not_retry_after_timeout(self) -> None:
        context = MagicMock()
        context.screener_search.return_value = _Response({
            "items": [{"symbol": "AAA.US", "name": "Alpha"}],
        })
        service = _ServiceWithContext(context)
        candidate_retry_predicates = {}

        def call_external(
            service_name,
            operation,
            callback,
            *args,
            retry_if=None,
            **kwargs,
        ):
            if operation in {
                "candidate_tradeability",
                "candidate_profiles",
                "margin_requirements",
            }:
                candidate_retry_predicates[operation] = retry_if
            return callback(*args, **kwargs)

        with patch(
            "app.stock_screener.run_external_call",
            side_effect=call_external,
        ):
            service.search(
                "US",
                101,
                include_indexes=False,
                target_direction="SHORT",
                include_short_risk=False,
                include_fundamentals=True,
                include_margin_requirements=True,
            )

        self.assertEqual(
            set(candidate_retry_predicates),
            {
                "candidate_tradeability",
                "candidate_profiles",
                "margin_requirements",
            },
        )
        timeout = ExternalServiceTimeoutError("batch timed out")
        for retry_if in candidate_retry_predicates.values():
            self.assertIsNotNone(retry_if)
            self.assertFalse(retry_if(timeout))
            self.assertTrue(retry_if(RuntimeError("temporary outage")))

    def test_fundamental_filters_are_directional_and_event_aware(self) -> None:
        context = MagicMock()
        context.screener_search.return_value = _Response({
            "items": [
                {"symbol": "GOOD.US", "name": "Good"},
                {"symbol": "WEAK.US", "name": "Weak"},
                {"symbol": "MISSING.US", "name": "Missing"},
            ],
        })
        index_loader = MagicMock(return_value={})
        fundamental_loader = MagicMock(return_value={
            "GOOD.US": {
                "status": "available",
                "revenue_yoy": 0.2,
                "net_profit_yoy": 0.3,
                "operating_cash_flow_yoy": 0.15,
                "analyst_alignment": 0.6,
                "eps_revision_alignment": 0.4,
                "days_to_financial_event": 60,
                "days_to_corporate_action": 20,
            },
            "WEAK.US": {
                "status": "available",
                "revenue_yoy": 0.05,
                "net_profit_yoy": 0.2,
                "operating_cash_flow_yoy": 0.1,
                "analyst_alignment": 0.3,
                "eps_revision_alignment": 0.2,
                "days_to_financial_event": 60,
                "days_to_corporate_action": 20,
            },
            "MISSING.US": {
                "status": "partial",
                "revenue_yoy": None,
                "net_profit_yoy": 0.3,
                "analyst_alignment": 0.5,
                "eps_revision_alignment": 0.5,
                "days_to_financial_event": 60,
                "days_to_corporate_action": 20,
            },
        })
        service = _ServiceWithContext(
            context,
            index_loader=index_loader,
            fundamental_loader=fundamental_loader,
        )

        result = service.search(
            "US",
            101,
            include_indexes=False,
            target_direction="LONG",
            filters={
                "min_revenue_yoy": 0.1,
                "min_analyst_alignment": 0.5,
                "min_eps_revision_alignment": 0.3,
                "min_days_to_financial_event": 45,
                "min_days_to_corporate_action": 10,
            },
        )

        self.assertEqual(
            [item["symbol"] for item in result["items"]],
            ["GOOD.US"],
        )
        self.assertEqual(
            result["filters"]["reasons"],
            {
                "below_min_revenue_yoy": 1,
                "missing_revenue_yoy": 1,
            },
        )
        fundamental_loader.assert_called_once_with(
            ["GOOD.US", "WEAK.US", "MISSING.US"],
            "US",
            "LONG",
            45,
            True,
        )
        self.assertEqual(
            result["fundamentals"]["event_window_days"],
            45,
        )
        self.assertTrue(
            result["fundamentals"]["corporate_actions_included"]
        )
        index_loader.assert_not_called()

    def test_margin_filter_preserves_borrow_unknown_boundary(self) -> None:
        context = MagicMock()
        context.screener_search.return_value = _Response({
            "items": [
                {"symbol": "LOW.US", "name": "Low"},
                {"symbol": "HIGH.US", "name": "High"},
            ],
        })
        margin_loader = MagicMock(return_value={
            "LOW.US": {
                "status": "available",
                "initial_margin_ratio": 0.5,
                "borrow_availability": "unknown",
                "borrow_fee_rate": None,
                "note": "保证金比例不代表实时券源可借或融券费率",
            },
            "HIGH.US": {
                "status": "available",
                "initial_margin_ratio": 0.8,
                "borrow_availability": "unknown",
                "borrow_fee_rate": None,
            },
        })
        service = _ServiceWithContext(
            context,
            margin_loader=margin_loader,
        )

        result = service.search(
            "US",
            101,
            include_indexes=False,
            include_short_risk=False,
            target_direction="SHORT",
            filters={"max_initial_margin_ratio": 0.6},
        )

        self.assertEqual(
            [item["symbol"] for item in result["items"]],
            ["LOW.US"],
        )
        margin = result["items"][0]["margin_requirements"]
        self.assertEqual(margin["borrow_availability"], "unknown")
        self.assertIsNone(margin["borrow_fee_rate"])
        self.assertEqual(
            result["margin_requirements"]["borrow_availability"],
            "unknown",
        )
        margin_loader.assert_called_once_with(["LOW.US", "HIGH.US"])

    def test_short_capacity_filter_uses_account_estimate_for_us_short(
        self,
    ) -> None:
        context = MagicMock()
        context.screener_search.return_value = _Response({
            "items": [
                {"symbol": "ENOUGH.US", "name": "Enough"},
                {"symbol": "SMALL.US", "name": "Small"},
                {"symbol": "UNKNOWN.US", "name": "Unknown"},
            ],
        })
        capacity_loader = MagicMock(return_value={
            "ENOUGH.US": {
                "status": "available",
                "cash_max_qty": 10,
                "margin_max_qty": 500,
                "short_selling_max_qty": 500,
                "availability": "available",
                "borrow_fee_rate": None,
                "recall_risk": "unknown",
            },
            "SMALL.US": {
                "status": "available",
                "cash_max_qty": 0,
                "margin_max_qty": 50,
                "short_selling_max_qty": 50,
                "availability": "available",
                "borrow_fee_rate": None,
                "recall_risk": "unknown",
            },
            "UNKNOWN.US": {
                "status": "error",
                "error": "permission denied",
                "short_selling_max_qty": None,
                "availability": "unknown",
                "borrow_fee_rate": None,
                "recall_risk": "unknown",
            },
        })
        service = _ServiceWithContext(
            context,
            short_capacity_loader=capacity_loader,
        )

        result = service.search(
            "US",
            101,
            include_indexes=False,
            include_short_risk=False,
            target_direction="SHORT",
            filters={"min_short_selling_quantity": 100},
        )

        self.assertEqual(
            [item["symbol"] for item in result["items"]],
            ["ENOUGH.US"],
        )
        self.assertEqual(
            result["items"][0]["short_capacity"][
                "short_selling_max_qty"
            ],
            500,
        )
        self.assertEqual(result["short_capacity"]["status"], "available")
        self.assertEqual(
            result["short_capacity"]["failure_categories"],
            {"unknown_error": 1},
        )
        self.assertTrue(result["short_capacity"]["account_specific"])
        self.assertIsNone(result["short_capacity"]["borrow_fee_rate"])
        self.assertEqual(result["short_capacity"]["recall_risk"], "unknown")
        self.assertEqual(
            result["filters"]["reasons"],
            {
                "below_min_short_selling_quantity": 1,
                "missing_short_selling_max_qty": 1,
            },
        )
        capacity_loader.assert_called_once_with([
            "ENOUGH.US",
            "SMALL.US",
            "UNKNOWN.US",
        ])

    def test_short_capacity_scope_and_failure_semantics(self) -> None:
        context = MagicMock()
        context.screener_search.return_value = _Response({
            "items": [{"symbol": "AAA.US", "name": "Alpha"}],
        })

        def fail_capacity(_symbols):
            raise RuntimeError("trade estimate unavailable")

        service = _ServiceWithContext(
            context,
            short_capacity_loader=fail_capacity,
        )
        degraded = service.search(
            "US",
            101,
            include_indexes=False,
            include_short_risk=False,
            target_direction="SHORT",
            include_short_capacity=True,
        )
        self.assertEqual(degraded["short_capacity"]["status"], "fallback")
        self.assertEqual(
            degraded["short_capacity"]["failure_category"],
            "unknown_error",
        )
        self.assertEqual(
            degraded["short_capacity"]["failure_categories"],
            {"unknown_error": 1},
        )
        self.assertEqual(
            degraded["items"][0]["short_capacity"]["status"],
            "fallback",
        )
        self.assertEqual(
            degraded["items"][0]["short_capacity"][
                "failure_category"
            ],
            "unknown_error",
        )

        reset_stock_picker_reliability_metrics()
        with self.assertRaisesRegex(
            LongbridgeAPIError,
            "无法应用账户卖空数量过滤",
        ):
            service.search(
                "US",
                101,
                include_indexes=False,
                include_short_risk=False,
                target_direction="SHORT",
                filters={"min_short_selling_quantity": 1},
            )

        with self.assertRaisesRegex(
            ValueError,
            "仅适用于 SHORT",
        ):
            service.search(
                "US",
                101,
                include_indexes=False,
                target_direction="LONG",
                include_short_capacity=True,
            )

        with self.assertRaisesRegex(
            ValueError,
            "仅支持美股",
        ):
            service.search(
                "HK",
                101,
                include_indexes=False,
                include_short_risk=False,
                target_direction="SHORT",
                filters={"min_short_selling_quantity": 1},
            )

        unsupported = service.search(
            "HK",
            101,
            include_indexes=False,
            include_short_risk=False,
            target_direction="SHORT",
            include_short_capacity=True,
        )
        self.assertEqual(
            unsupported["short_capacity"]["status"],
            "unsupported",
        )
        self.assertEqual(
            unsupported["items"][0]["short_capacity"]["status"],
            "unsupported",
        )

    def test_short_capacity_failure_counts_are_combined_across_pages(
        self,
    ) -> None:
        combined = StockScreenerService._combine_status([
            {
                "status": "available",
                "error": None,
                "failure_category": None,
                "failure_categories": {
                    "response_no_data": 2,
                    "timeout": 1,
                },
            },
            {
                "status": "fallback",
                "error": "trade timeout",
                "failure_category": "timeout",
                "failure_categories": {"timeout": 3},
            },
        ])

        self.assertEqual(combined["status"], "fallback")
        self.assertEqual(combined["failure_category"], "timeout")
        self.assertEqual(
            combined["failure_categories"],
            {"response_no_data": 2, "timeout": 4},
        )

    def test_fundamental_and_margin_failures_only_degrade_without_filters(
        self,
    ) -> None:
        context = MagicMock()
        context.screener_search.return_value = _Response({
            "items": [{"symbol": "AAA.US", "name": "Alpha"}],
        })

        def fail_fundamentals(*_args):
            raise RuntimeError("fundamental unavailable")

        service = _ServiceWithContext(
            context,
            fundamental_loader=fail_fundamentals,
        )
        degraded = service.search(
            "US",
            101,
            include_indexes=False,
            include_fundamentals=True,
        )
        self.assertEqual(degraded["fundamentals"]["status"], "fallback")
        self.assertEqual(
            [item["symbol"] for item in degraded["items"]],
            ["AAA.US"],
        )

        reset_stock_picker_reliability_metrics()
        with self.assertRaisesRegex(
            LongbridgeAPIError,
            "无法应用财务或事件过滤",
        ):
            service.search(
                "US",
                101,
                include_indexes=False,
                filters={"min_revenue_yoy": 0.1},
            )

        def fail_margin(_symbols):
            raise RuntimeError("trade unavailable")

        reset_stock_picker_reliability_metrics()
        margin_service = _ServiceWithContext(
            context,
            margin_loader=fail_margin,
        )
        degraded_margin = margin_service.search(
            "US",
            101,
            include_indexes=False,
            include_margin_requirements=True,
        )
        self.assertEqual(
            degraded_margin["margin_requirements"]["status"],
            "fallback",
        )

        reset_stock_picker_reliability_metrics()
        with self.assertRaisesRegex(
            LongbridgeAPIError,
            "无法应用保证金比例过滤",
        ):
            margin_service.search(
                "US",
                101,
                include_indexes=False,
                filters={"max_initial_margin_ratio": 0.6},
            )

    def test_index_failure_only_degrades_when_no_hard_filter_is_requested(self) -> None:
        context = MagicMock()
        context.screener_search.return_value = _Response({
            "items": [{"symbol": "AAPL.US", "name": "Apple"}],
        })

        def fail_indexes(_symbols):
            raise RuntimeError("quote unavailable")

        service = _ServiceWithContext(context, index_loader=fail_indexes)

        degraded = service.search("US", 101)
        self.assertEqual(degraded["enrichment"]["status"], "fallback")
        self.assertEqual(degraded["items"][0]["symbol"], "AAPL.US")
        self.assertEqual(
            degraded["relative_strength"]["benchmark_returns"],
            {
                "ten_day_change_rate": None,
                "half_year_change_rate": None,
            },
        )
        self.assertEqual(
            degraded["relative_strength"]["benchmark_observations"],
            [{
                "page": 0,
                "ten_day_change_rate": None,
                "half_year_change_rate": None,
            }],
        )

        with self.assertRaisesRegex(LongbridgeAPIError, "无法应用候选过滤条件"):
            service.search(
                "US",
                101,
                filters={"min_turnover": 1_000_000},
            )


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
            "scan": {
                "mode": "bounded",
                "requested_pages": 3,
                "pages_scanned": 1,
                "first_page": 0,
                "last_page": 0,
                "next_page": None,
                "candidates_scanned": 1,
                "candidates_returned": 1,
                "duplicates_removed": 0,
                "stopped_reason": "source_exhausted",
            },
            "enrichment": {"status": "available", "error": None},
            "relative_strength": {
                "benchmark_symbol": "SPY.US",
                "target_direction": "LONG",
                "industry_basis": "current_page_industry_median",
            },
            "short_risk": {"status": "not_applicable", "error": None},
            "filters": {
                "applied": {"min_turnover": 1000000},
                "before": 1,
                "after": 1,
                "excluded": 0,
                "reasons": {},
            },
            "items": [{
                "rank": 1,
                "source_page": 0,
                "symbol": "AAPL.US",
                "name": "Apple",
                "market": "US",
                "indicators": {},
                "indexes": {"turnover": 2000000},
                "relative_strength": {
                    "benchmark_symbol": "SPY.US",
                    "target_direction": "LONG",
                    "industry": None,
                    "industry_peer_count": 0,
                    "market_rs_10d": None,
                    "market_rs_half_year": None,
                    "industry_rs_10d": None,
                    "industry_rs_half_year": None,
                },
                "short_risk": {
                    "status": "not_applicable",
                    "error": None,
                },
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
                        "scan_pages": 3,
                        "filters": {
                            "min_turnover": 1000000,
                            "max_spread_bps": 50,
                            "min_revenue_yoy": 0.1,
                            "max_initial_margin_ratio": 0.6,
                            "min_short_selling_quantity": 100,
                        },
                        "target_direction": "SHORT",
                        "include_fundamentals": True,
                        "include_margin_requirements": True,
                        "include_short_capacity": True,
                        "fundamental_event_window_days": 60,
                        "include_corporate_actions": True,
                        "capture_snapshot": True,
                        "strategy_name": "盈利增长",
                        "strategy_source": "recommended",
                    },
                )
                invalid = client.post(
                    "/api/stock-picker/screener/search",
                    json={"market": "US", "strategy_id": 0},
                )
                invalid_scan = client.post(
                    "/api/stock-picker/screener/search",
                    json={
                        "market": "US",
                        "strategy_id": 101,
                        "scan_pages": 6,
                    },
                )
            finally:
                client.close()

        self.assertEqual(strategies.status_code, 200)
        self.assertEqual(strategies.json()["items"][0]["id"], 101)
        self.assertEqual(search.status_code, 200)
        self.assertEqual(search.json()["items"][0]["symbol"], "AAPL.US")
        self.assertEqual(search.json()["scan"]["requested_pages"], 3)
        self.assertEqual(invalid.status_code, 422)
        self.assertEqual(invalid_scan.status_code, 422)
        screener.list_strategies.assert_called_once_with("US", False)
        screener.search.assert_called_once_with(
            market="US",
            strategy_id=101,
            page=0,
            size=20,
            scan_pages=3,
            filters={
                "min_turnover": 1000000.0,
                "max_spread_bps": 50.0,
                "min_revenue_yoy": 0.1,
                "max_initial_margin_ratio": 0.6,
                "min_short_selling_quantity": 100.0,
            },
            include_indexes=True,
            target_direction="SHORT",
            benchmark_symbol=None,
            include_short_risk=True,
            include_tradeability=True,
            require_normal_trade_status=True,
            include_fundamentals=True,
            include_margin_requirements=True,
            include_short_capacity=True,
            fundamental_event_window_days=60,
            include_corporate_actions=True,
            capture_snapshot=True,
            strategy_name="盈利增长",
            strategy_source="recommended",
        )

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
