from __future__ import annotations

import asyncio
from datetime import datetime
import threading
import unittest
from unittest.mock import MagicMock, patch

from app.ai_analyzer import DeepSeekAnalyzer
from app.external_service_resilience import (
    ExternalServiceBusyError,
    ExternalServiceCallError,
    ExternalServiceCircuitOpenError,
    ExternalServicePolicy,
    ExternalServiceRuntime,
    ExternalServiceTimeoutError,
    get_stock_picker_reliability_snapshot,
    record_stock_picker_ai,
    record_stock_picker_cache,
    reset_stock_picker_reliability_metrics,
)
from app.main import app
from app.news_analyzer import NewsAnalyzer
from app.stock_picker import StockPickerService


class _Clock:
    def __init__(self) -> None:
        self.value = 100.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class ExternalServiceRuntimeTests(unittest.TestCase):
    def test_transient_failure_is_retried_and_metrics_are_recorded(self) -> None:
        attempts = 0

        def callback() -> str:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("temporary outage")
            return "ok"

        runtime = ExternalServiceRuntime(
            "test",
            ExternalServicePolicy(
                timeout_seconds=1,
                max_attempts=3,
                max_concurrency=1,
                backoff_seconds=0,
            ),
        )
        self.addCleanup(runtime.shutdown)

        result = runtime.call("fetch", callback)
        metrics = runtime.snapshot()

        self.assertEqual(result, "ok")
        self.assertEqual(attempts, 2)
        self.assertEqual(metrics["requests"], 1)
        self.assertEqual(metrics["attempts"], 2)
        self.assertEqual(metrics["retries"], 1)
        self.assertEqual(metrics["successes"], 1)
        self.assertEqual(metrics["failures"], 0)
        self.assertEqual(metrics["circuit_state"], "closed")

    def test_circuit_opens_and_recovers_through_half_open_probe(self) -> None:
        clock = _Clock()
        calls = 0

        def failing() -> None:
            nonlocal calls
            calls += 1
            raise RuntimeError("down")

        runtime = ExternalServiceRuntime(
            "test",
            ExternalServicePolicy(
                timeout_seconds=1,
                max_attempts=1,
                max_concurrency=1,
                failure_threshold=2,
                recovery_seconds=5,
                backoff_seconds=0,
            ),
            clock=clock,
        )
        self.addCleanup(runtime.shutdown)

        for _ in range(2):
            with self.assertRaises(ExternalServiceCallError):
                runtime.call("fetch", failing)
        with self.assertRaises(ExternalServiceCircuitOpenError):
            runtime.call("fetch", failing)

        self.assertEqual(calls, 2)
        self.assertEqual(runtime.snapshot()["circuit_state"], "open")

        clock.advance(6)
        self.assertEqual(runtime.call("fetch", lambda: "recovered"), "recovered")
        metrics = runtime.snapshot()
        self.assertEqual(metrics["circuit_state"], "closed")
        self.assertEqual(metrics["consecutive_failures"], 0)

    def test_timeout_keeps_concurrency_slot_until_worker_finishes(self) -> None:
        release = threading.Event()
        finished = threading.Event()

        def slow_call() -> None:
            release.wait(1)
            finished.set()

        runtime = ExternalServiceRuntime(
            "test",
            ExternalServicePolicy(
                timeout_seconds=0.01,
                max_attempts=1,
                max_concurrency=1,
                failure_threshold=10,
                recovery_seconds=1,
                backoff_seconds=0,
                acquire_timeout_seconds=0.005,
            ),
        )
        self.addCleanup(runtime.shutdown)

        with self.assertRaises(ExternalServiceTimeoutError):
            runtime.call("slow", slow_call)
        self.assertEqual(runtime.snapshot()["in_flight"], 1)
        with self.assertRaises(ExternalServiceBusyError):
            runtime.call("second", lambda: "unexpected")

        release.set()
        self.assertTrue(finished.wait(0.2))
        self.assertEqual(runtime.snapshot()["in_flight"], 0)

    def test_retry_predicate_can_disable_retry(self) -> None:
        attempts = 0

        def invalid() -> None:
            nonlocal attempts
            attempts += 1
            raise ValueError("invalid request")

        runtime = ExternalServiceRuntime(
            "test",
            ExternalServicePolicy(
                timeout_seconds=1,
                max_attempts=3,
                max_concurrency=1,
                backoff_seconds=0,
            ),
        )
        self.addCleanup(runtime.shutdown)

        with self.assertRaises(ExternalServiceCallError):
            runtime.call(
                "fetch",
                invalid,
                retry_if=lambda error: not isinstance(error, ValueError),
            )

        self.assertEqual(attempts, 1)
        self.assertEqual(runtime.snapshot()["retries"], 0)


class StockPickerReliabilityMetricsTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_stock_picker_reliability_metrics()

    def tearDown(self) -> None:
        reset_stock_picker_reliability_metrics()

    def test_http_snapshot_exposes_cache_ai_and_service_metrics(self) -> None:
        record_stock_picker_cache("hit")
        record_stock_picker_cache("miss")
        record_stock_picker_cache("bypass")
        record_stock_picker_ai(degraded=False)
        record_stock_picker_ai(degraded=True)

        route = next(
            route
            for route in app.routes
            if route.path == "/api/stock-picker/reliability"
        )
        self.assertIn("GET", route.methods)
        payload = route.endpoint()
        self.assertEqual(payload["scope"], "process")
        self.assertEqual(
            set(payload["services"]),
            {"quote", "screener", "news", "ai"},
        )
        self.assertEqual(payload["stock_picker"]["cache"]["requests"], 2)
        self.assertEqual(payload["stock_picker"]["cache"]["hit_rate"], 0.5)
        self.assertEqual(payload["stock_picker"]["cache"]["bypasses"], 1)
        self.assertEqual(payload["stock_picker"]["ai"]["attempts"], 2)
        self.assertEqual(
            payload["stock_picker"]["ai"]["degradation_rate"],
            0.5,
        )
        self.assertIn("当前服务进程", payload["limitations"][0])

    def test_stock_picker_cache_path_records_hit_miss_and_bypass(self) -> None:
        service = StockPickerService()
        cache_key = ("test",)

        self.assertIsNone(service._get_cached_result(cache_key, False, 60))
        service.cache[cache_key] = {
            "time": datetime.now(),
            "data": {"id": 1},
        }
        self.assertEqual(
            service._get_cached_result(cache_key, False, 60),
            {"id": 1},
        )
        self.assertIsNone(service._get_cached_result(cache_key, True, 60))

        cache = get_stock_picker_reliability_snapshot()["stock_picker"]["cache"]
        self.assertEqual(cache["misses"], 1)
        self.assertEqual(cache["hits"], 1)
        self.assertEqual(cache["bypasses"], 1)
        self.assertEqual(cache["hit_rate"], 0.5)

    def test_ai_initialization_failure_falls_back_to_quant_result(self) -> None:
        service = StockPickerService()
        score = {
            "total": 70.0,
            "grade": "B",
            "breakdown": {
                "trend": 18,
                "momentum": 14,
                "support_resistance": 10,
                "volume": 10,
                "pattern": 10,
                "volatility": 8,
            },
            "signals": [],
            "trend_strength": 0.6,
        }
        prepared = {
            "pool_id": 1,
            "symbol": "AAA.US",
            "pool_type": "LONG",
            "klines": [{"close": 100, "ts": "2026-07-24"}],
            "indicators": {"current_price": 100},
            "score": score,
            "ai_creds": {"DEEPSEEK_API_KEY": "secret"},
            "config": {
                **StockPickerService.DEFAULT_CONFIG,
                "updated_at": "2026-07-24",
            },
            "force_refresh": False,
            "cache_checked": True,
            "job_id": "test-job",
        }

        with (
            patch(
                "app.ai_analyzer.DeepSeekAnalyzer",
                side_effect=RuntimeError("client unavailable"),
            ),
            patch.object(
                service,
                "_save_analysis_result",
                return_value={"id": 1},
            ) as save_result,
        ):
            outcome = {}

            def run_analysis() -> None:
                outcome["result"] = asyncio.run(
                    service._finalize_prepared_analysis(
                        prepared,
                        use_ai=True,
                    )
                )

            worker = threading.Thread(target=run_analysis)
            worker.start()
            worker.join(timeout=1)

        self.assertFalse(worker.is_alive())
        self.assertEqual(outcome["result"], {"id": 1})
        analysis = save_result.call_args.kwargs["analysis"]
        self.assertEqual(analysis["ai_status"], "fallback")
        self.assertEqual(analysis["action"], "BUY")
        self.assertIn("client unavailable", analysis["ai_error"])
        ai_metrics = get_stock_picker_reliability_snapshot()[
            "stock_picker"
        ]["ai"]
        self.assertEqual(ai_metrics["attempts"], 1)
        self.assertEqual(ai_metrics["degraded"], 1)
        self.assertEqual(ai_metrics["degradation_rate"], 1)

    def test_news_and_ai_clients_use_named_resilience_channels(self) -> None:
        news = NewsAnalyzer.__new__(NewsAnalyzer)
        news.client = MagicMock()
        news_response = {
            "results": [{
                "title": "Company beats earnings",
                "url": "https://example.com",
                "content": "Strong profit growth",
                "score": 0.9,
            }]
        }
        with patch(
            "app.news_analyzer.run_external_call",
            return_value=news_response,
        ) as run_news:
            result = news.search_stock_news("AAA.US")

        self.assertEqual(result["news_count"], 1)
        self.assertEqual(run_news.call_args.args[:2], (
            "news",
            "search_stock_news",
        ))

        analyzer = DeepSeekAnalyzer.__new__(DeepSeekAnalyzer)
        analyzer.news_analyzer = None
        analyzer.client = MagicMock()
        analyzer.model = "test-model"
        analyzer.temperature = 0
        analyzer._build_prompt = MagicMock(return_value="prompt")
        analyzer._get_system_prompt = MagicMock(return_value="system")
        ai_response = MagicMock()
        ai_response.choices[0].message.content = (
            '{"action":"BUY","confidence":0.8,"reasoning":["test"]}'
        )
        with patch(
            "app.ai_analyzer.run_external_call",
            return_value=ai_response,
        ) as run_ai:
            analysis = analyzer.analyze_trading_opportunity(
                "AAA.US",
                [{"close": 100}],
                technical_indicators={"current_price": 100},
                quant_score={"total": 70},
            )

        self.assertEqual(analysis["ai_status"], "available")
        self.assertEqual(run_ai.call_args.args[:2], (
            "ai",
            "chat_completion",
        ))

    def test_deepseek_sdk_retries_are_disabled_in_favor_of_runtime_policy(
        self,
    ) -> None:
        with patch("app.ai_analyzer.OpenAI") as openai:
            DeepSeekAnalyzer(api_key="test-key")

        openai.assert_called_once_with(
            api_key="test-key",
            base_url="https://api.deepseek.com",
            timeout=25.0,
            max_retries=0,
        )


if __name__ == "__main__":
    unittest.main()
