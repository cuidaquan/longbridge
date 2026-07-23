from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import BackgroundTasks, HTTPException
from fastapi.testclient import TestClient

from app.ai_analyzer import DeepSeekAnalyzer, calculate_technical_indicators
from app.main import app
from app.routers import stock_picker as stock_picker_router
from app.routers.stock_picker import get_pools as get_pools_route
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
    def test_pools_route_includes_inactive_stocks_by_default(self) -> None:
        service = MagicMock()
        service.get_pools.return_value = {"long_pool": [], "short_pool": []}

        with patch(
            "app.routers.stock_picker.get_stock_picker_service",
            return_value=service,
        ):
            result = asyncio.run(get_pools_route())

        self.assertEqual(result, {"long_pool": [], "short_pool": []})
        service.get_pools.assert_called_once_with(None, include_inactive=True)

    def test_get_pools_defaults_to_active_stocks(self) -> None:
        rows = [
            (1, "LONG", "ACTIVE.US", "Active", "2026-07-22", None, True, 1),
            (3, "SHORT", "SHORT.US", "Short", "2026-07-22", None, True, 1),
        ]
        connection = _FakeConnection(rows)
        service = StockPickerService()

        with patch("app.stock_picker.get_connection", return_value=connection):
            result = service.get_pools()

        statement, parameters = connection.statements[0]
        self.assertIn("is_active = TRUE", statement)
        self.assertIsNone(parameters)
        self.assertEqual([stock["symbol"] for stock in result["long_pool"]], ["ACTIVE.US"])
        self.assertEqual([stock["symbol"] for stock in result["short_pool"]], ["SHORT.US"])

    def test_get_pools_can_include_inactive_stocks_for_management(self) -> None:
        rows = [
            (1, "LONG", "ACTIVE.US", "Active", "2026-07-22", None, True, 1),
            (2, "LONG", "INACTIVE.US", "Inactive", "2026-07-22", None, False, 1),
        ]
        connection = _FakeConnection(rows)
        service = StockPickerService()

        with patch("app.stock_picker.get_connection", return_value=connection):
            result = service.get_pools(include_inactive=True)

        statement, parameters = connection.statements[0]
        self.assertNotIn("is_active = TRUE", statement)
        self.assertIsNone(parameters)
        self.assertEqual(
            [stock["symbol"] for stock in result["long_pool"]],
            ["ACTIVE.US", "INACTIVE.US"],
        )
        self.assertTrue(result["long_pool"][0]["is_active"])
        self.assertFalse(result["long_pool"][1]["is_active"])

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


class StockPickerJobIsolationTest(unittest.TestCase):
    def setUp(self) -> None:
        stock_picker_router.analysis_jobs.clear()

    def tearDown(self) -> None:
        stock_picker_router.analysis_jobs.clear()

    def test_analyze_endpoint_returns_unique_queued_jobs(self) -> None:
        first_background = BackgroundTasks()
        second_background = BackgroundTasks()

        first = asyncio.run(
            stock_picker_router.analyze_pools(
                stock_picker_router.AnalyzeRequest(pool_type="LONG"),
                first_background,
            )
        )
        second = asyncio.run(
            stock_picker_router.analyze_pools(
                stock_picker_router.AnalyzeRequest(pool_type="SHORT"),
                second_background,
            )
        )

        self.assertNotEqual(first["job_id"], second["job_id"])
        self.assertEqual(first["status"], "queued")
        self.assertEqual(second["status"], "queued")
        self.assertEqual(len(first_background.tasks), 1)
        self.assertEqual(len(second_background.tasks), 1)
        self.assertEqual(
            stock_picker_router.analysis_jobs[first["job_id"]]["pool_type"],
            "LONG",
        )
        self.assertEqual(
            stock_picker_router.analysis_jobs[second["job_id"]]["pool_type"],
            "SHORT",
        )

    def test_job_runner_only_updates_its_own_progress(self) -> None:
        first_job_id = stock_picker_router._create_analysis_job("LONG", False)
        second_job_id = stock_picker_router._create_analysis_job("SHORT", False)
        service = MagicMock()

        async def analyze_pool(pool_type, force_refresh, progress_callback):
            progress_callback({
                "status": "running",
                "total": 2,
                "completed": 1,
                "current": "AAA.US",
                "log": "first update",
            })
            await asyncio.sleep(0)
            return {"total": 2, "success": 2, "skipped": 0, "failed": 0}

        service.analyze_pool = AsyncMock(side_effect=analyze_pool)
        with patch(
            "app.routers.stock_picker.get_stock_picker_service",
            return_value=service,
        ):
            asyncio.run(
                stock_picker_router._run_analysis_job(
                    first_job_id,
                    "LONG",
                    False,
                )
            )

        first_job = stock_picker_router.analysis_jobs[first_job_id]
        second_job = stock_picker_router.analysis_jobs[second_job_id]
        self.assertEqual(first_job["status"], "completed")
        self.assertEqual(first_job["current"], "AAA.US")
        self.assertEqual(first_job["result"]["success"], 2)
        self.assertEqual(first_job["logs"][-1]["message"], "first update")
        self.assertEqual(second_job["status"], "queued")
        self.assertIsNone(second_job["current"])
        self.assertEqual(second_job["logs"], [])

    def test_job_runner_records_failure_without_touching_other_jobs(self) -> None:
        failed_job_id = stock_picker_router._create_analysis_job("LONG", False)
        other_job_id = stock_picker_router._create_analysis_job("SHORT", False)
        service = MagicMock()
        service.analyze_pool = AsyncMock(side_effect=RuntimeError("temporary outage"))

        with patch(
            "app.routers.stock_picker.get_stock_picker_service",
            return_value=service,
        ):
            asyncio.run(
                stock_picker_router._run_analysis_job(
                    failed_job_id,
                    "LONG",
                    False,
                )
            )

        failed_job = stock_picker_router.analysis_jobs[failed_job_id]
        self.assertEqual(failed_job["status"], "error")
        self.assertEqual(failed_job["error"], "temporary outage")
        self.assertIn("temporary outage", failed_job["logs"][-1]["message"])
        self.assertEqual(
            stock_picker_router.analysis_jobs[other_job_id]["status"],
            "queued",
        )

    def test_progress_endpoint_rejects_unknown_job(self) -> None:
        with self.assertRaises(HTTPException) as context:
            asyncio.run(stock_picker_router.get_analysis_progress("missing"))

        self.assertEqual(context.exception.status_code, 404)

    def test_job_registry_rejects_more_than_active_limit(self) -> None:
        for _ in range(stock_picker_router.MAX_ANALYSIS_JOBS):
            stock_picker_router._create_analysis_job("LONG", False)

        with self.assertRaises(HTTPException) as context:
            stock_picker_router._create_analysis_job("LONG", False)

        self.assertEqual(context.exception.status_code, 429)

    def test_http_job_lifecycle_returns_isolated_sse_stream(self) -> None:
        service = MagicMock()

        async def analyze_pool(pool_type, force_refresh, progress_callback):
            progress_callback({
                "status": "running",
                "total": 1,
                "completed": 0,
                "current": "AAA.US",
                "log": "working",
            })
            progress_callback({
                "completed": 1,
                "log": "done",
            })
            return {"total": 1, "success": 1, "skipped": 0, "failed": 0}

        service.analyze_pool = AsyncMock(side_effect=analyze_pool)
        with patch(
            "app.routers.stock_picker.get_stock_picker_service",
            return_value=service,
        ):
            client = TestClient(app)
            response = client.post(
                "/api/stock-picker/analyze",
                json={"pool_type": "LONG", "force_refresh": False},
            )

        self.assertEqual(response.status_code, 202)
        job_id = response.json()["job_id"]
        progress = TestClient(app).get(
            f"/api/stock-picker/analysis/progress/{job_id}",
        )
        self.assertEqual(progress.status_code, 200)
        self.assertIn(f'"job_id": "{job_id}"', progress.text)
        self.assertIn('"status": "completed"', progress.text)
        self.assertIn('"success": 1', progress.text)


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

    def test_quant_fallback_does_not_double_count_ai_alignment(self) -> None:
        score = {
            "total": 70,
            "trend_strength": 0.7,
            "momentum_direction": "bullish",
        }
        disabled = self.service._calculate_recommendation_score_v2(
            score,
            {
                "action": "BUY",
                "confidence": 0.95,
                "ai_status": "disabled",
            },
            "LONG",
        )
        fallback = self.service._calculate_recommendation_score_v2(
            score,
            {
                "action": "BUY",
                "confidence": 0.95,
                "ai_status": "fallback",
            },
            "LONG",
        )
        available = self.service._calculate_recommendation_score_v2(
            score,
            {
                "action": "BUY",
                "confidence": 0.95,
                "ai_status": "available",
            },
            "LONG",
        )

        self.assertEqual(disabled, fallback)
        self.assertGreater(available, disabled)


class StockPickerPerformanceFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = StockPickerService()
        self.klines = _trend_klines(1)

    def test_pool_syncs_once_and_only_top_n_enters_ai(self) -> None:
        stocks = [
            {
                "id": index,
                "symbol": f"TEST{index}.US",
                "pool_type": "LONG",
            }
            for index in range(1, 13)
        ]
        self.service.get_pools = MagicMock(
            return_value={"long_pool": stocks, "short_pool": []}
        )

        def prepare(pool_id, symbol, pool_type, ai_creds, force_refresh, callback):
            return {
                "pool_id": pool_id,
                "symbol": symbol,
                "pool_type": pool_type,
                "score": {"total": pool_id},
            }

        self.service._prepare_single_stock = MagicMock(side_effect=prepare)
        ai_flags = {}

        async def finalize(prepared, use_ai, progress_callback=None):
            ai_flags[prepared["pool_id"]] = use_ai
            return {"symbol": prepared["symbol"]}

        self.service._finalize_prepared_analysis = AsyncMock(side_effect=finalize)

        with (
            patch(
                "app.stock_picker.load_ai_credentials",
                return_value={"DEEPSEEK_API_KEY": "test-key"},
            ),
            patch(
                "app.services.sync_history_candlesticks",
                return_value={stock["symbol"]: 1 for stock in stocks},
            ) as sync_history,
        ):
            result = asyncio.run(self.service.analyze_pool("LONG"))

        self.assertEqual(result["success"], 12)
        sync_history.assert_called_once()
        self.assertEqual(sync_history.call_args.kwargs["count"], 250)
        self.assertTrue(sync_history.call_args.kwargs["incremental"])
        self.assertEqual(
            set(sync_history.call_args.kwargs["symbols"]),
            {stock["symbol"] for stock in stocks},
        )
        self.assertEqual(
            {pool_id for pool_id, uses_ai in ai_flags.items() if uses_ai},
            set(range(3, 13)),
        )
        self.assertEqual(sum(ai_flags.values()), self.service.AI_TOP_N_PER_POOL)

    def test_quant_only_result_cache_reuses_same_data_and_version(self) -> None:
        saved_result = {"symbol": "TEST.US", "cached": True}
        with (
            patch(
                "app.services.sync_history_candlesticks",
                return_value={"TEST.US": 1},
            ),
            patch(
                "app.stock_picker.get_cached_candlesticks",
                return_value=self.klines,
            ),
            patch("app.stock_picker.load_ai_credentials", return_value={}),
            patch.object(
                self.service,
                "_save_analysis_result",
                return_value=saved_result,
            ) as save_result,
        ):
            first = asyncio.run(
                self.service._analyze_single_stock(1, "TEST.US", "LONG")
            )
            second = asyncio.run(
                self.service._analyze_single_stock(1, "TEST.US", "LONG")
            )

        self.assertEqual(first, saved_result)
        self.assertEqual(second, saved_result)
        save_result.assert_called_once()

    def test_cache_key_changes_when_current_daily_bar_changes(self) -> None:
        first_bars = [dict(bar) for bar in self.klines]
        second_bars = [dict(bar) for bar in self.klines]
        first_bars[-1]["ts"] = "2026-07-23T00:00:00"
        second_bars[-1]["ts"] = "2026-07-23T00:00:00"
        second_bars[-1]["close"] += 1

        first_key = self.service._build_cache_key(
            1,
            "TEST.US",
            "LONG",
            first_bars,
            {},
            "quant",
        )
        second_key = self.service._build_cache_key(
            1,
            "TEST.US",
            "LONG",
            second_bars,
            {},
            "quant",
        )

        self.assertNotEqual(first_key, second_key)

    def test_ai_skipped_result_is_explicitly_marked(self) -> None:
        score = self.service._calculate_advanced_score_v2(self.klines, "LONG")
        indicators = calculate_technical_indicators(self.klines)
        prepared = {
            "pool_id": 1,
            "symbol": "TEST.US",
            "pool_type": "LONG",
            "klines": self.klines,
            "indicators": indicators,
            "score": score,
            "ai_creds": {"DEEPSEEK_API_KEY": "test-key"},
            "force_refresh": True,
        }

        with (
            patch("app.ai_analyzer.DeepSeekAnalyzer") as analyzer,
            patch.object(
                self.service,
                "_save_analysis_result",
                return_value={},
            ) as save_result,
        ):
            asyncio.run(
                self.service._finalize_prepared_analysis(prepared, use_ai=False)
            )

        analyzer.assert_not_called()
        analysis = save_result.call_args.kwargs["analysis"]
        self.assertEqual(analysis["ai_status"], "skipped")
        self.assertIn("未进入AI深度分析", analysis["reasoning"][0])


class StockPickerUnifiedAnalysisTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = StockPickerService()
        self.klines = _trend_klines(1)
        self.score = self.service._calculate_advanced_score_v2(self.klines, "LONG")
        self.indicators = calculate_technical_indicators(self.klines)

    def test_shared_indicator_snapshot_contains_price_changes(self) -> None:
        self.assertEqual(self.indicators["current_price"], self.klines[-1]["close"])
        self.assertGreater(self.indicators["price_change_1d"], 0)
        self.assertGreater(self.indicators["price_change_5d"], 0)
        self.assertIn("macd", self.indicators)

    def test_stock_picker_prompt_uses_shared_directional_score(self) -> None:
        analyzer = DeepSeekAnalyzer.__new__(DeepSeekAnalyzer)
        prompt = analyzer._build_prompt(
            symbol="TEST.US",
            klines=self.klines,
            indicators=self.indicators,
            current_positions=None,
            scenario="buy_focus",
            score=self.score,
            news_analysis=None,
        )

        self.assertIn(f"总分: {self.score['total']}/100", prompt)
        self.assertIn("【统一机会评分 V2】", prompt)
        self.assertIn(f"趋势评分: {self.score['breakdown']['trend']}/25", prompt)
        self.assertNotIn("新闻舆情权重翻倍", prompt)

    def test_analyzer_error_preserves_caller_snapshot(self) -> None:
        analyzer = DeepSeekAnalyzer.__new__(DeepSeekAnalyzer)
        analyzer.news_analyzer = None
        analyzer.client = MagicMock()
        analyzer.client.chat.completions.create.side_effect = RuntimeError("temporary outage")
        analyzer.model = "test-model"
        analyzer.temperature = 0
        analyzer._build_prompt = MagicMock(return_value="prompt")
        analyzer._get_system_prompt = MagicMock(return_value="system")

        result = analyzer.analyze_trading_opportunity(
            symbol="TEST.US",
            klines=self.klines,
            scenario="buy_focus",
            technical_indicators=self.indicators,
            quant_score=self.score,
        )

        self.assertEqual(result["ai_status"], "error")
        self.assertEqual(result["score"], self.score)
        self.assertEqual(result["indicators"], self.indicators)

    def test_ai_failure_falls_back_without_overwriting_quant_action(self) -> None:
        analysis = self.service._build_quant_analysis(
            self.score,
            self.indicators,
            "LONG",
            ai_status="fallback",
            ai_error="temporary outage",
        )

        self.assertEqual(analysis["action"], "BUY")
        self.assertGreater(analysis["confidence"], 0)
        self.assertEqual(analysis["score"], self.score)
        self.assertIn("已回退到量化结论", analysis["reasoning"][0])

    def test_single_stock_without_ai_uses_shared_snapshot(self) -> None:
        with (
            patch("app.services.sync_history_candlesticks", return_value={"TEST.US": 120}),
            patch("app.stock_picker.get_cached_candlesticks", return_value=self.klines),
            patch("app.stock_picker.load_ai_credentials", return_value={}),
            patch.object(self.service, "_save_analysis_result", return_value={}) as save_result,
        ):
            asyncio.run(self.service._analyze_single_stock(1, "TEST.US", "LONG", True))

        analysis = save_result.call_args.kwargs["analysis"]
        self.assertEqual(analysis["ai_status"], "disabled")
        self.assertGreater(analysis["indicators"]["price_change_1d"], 0)
        self.assertEqual(analysis["score"], self.score)

    def test_single_stock_ai_error_persists_quant_fallback(self) -> None:
        analyzer = MagicMock()
        analyzer.analyze_trading_opportunity.return_value = {
            "action": "HOLD",
            "confidence": 0,
            "reasoning": ["failed"],
            "error": "temporary outage",
        }
        with (
            patch("app.services.sync_history_candlesticks", return_value={"TEST.US": 120}),
            patch("app.stock_picker.get_cached_candlesticks", return_value=self.klines),
            patch(
                "app.stock_picker.load_ai_credentials",
                return_value={"DEEPSEEK_API_KEY": "test-key"},
            ),
            patch("app.ai_analyzer.DeepSeekAnalyzer", return_value=analyzer),
            patch.object(self.service, "_save_analysis_result", return_value={}) as save_result,
        ):
            asyncio.run(self.service._analyze_single_stock(1, "TEST.US", "LONG", True))

        analysis = save_result.call_args.kwargs["analysis"]
        self.assertEqual(analysis["ai_status"], "fallback")
        self.assertEqual(analysis["action"], "BUY")
        self.assertEqual(analysis["score"], self.score)


if __name__ == "__main__":
    unittest.main()
