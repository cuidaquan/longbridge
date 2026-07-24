from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import datetime, timedelta
import json
import unittest
from unittest.mock import MagicMock, patch

import duckdb
from fastapi import FastAPI
import httpx

from app.ai_analyzer import DeepSeekAnalyzer, calculate_technical_indicators
from app.db import _run_migrations
from app.news_analyzer import NewsAnalyzer
from app.routers.stock_picker import router as stock_picker_api_router
from app.stock_picker import StockPickerService
from app.stock_picker_ai_snapshots import (
    AI_INPUT_SNAPSHOT_VERSION,
    AI_OUTPUT_SNAPSHOT_VERSION,
    NEWS_SNAPSHOT_VERSION,
    canonical_json,
    klines_hash,
    sanitize_error,
    snapshot_hash,
)


def _klines(count: int = 60) -> list[dict]:
    start = datetime(2026, 1, 1)
    rows = []
    for index in range(count):
        close = 100 + index * 0.5
        rows.append({
            "ts": start + timedelta(days=index),
            "open": close - 0.2,
            "high": close + 0.8,
            "low": close - 0.8,
            "close": close,
            "volume": 1_000_000 + index * 1_000,
            "turnover": close * (1_000_000 + index * 1_000),
        })
    return rows


class SnapshotHashTests(unittest.TestCase):
    def test_canonical_hash_is_stable_and_excludes_capture_time(self) -> None:
        first = {
            "version": "v1",
            "captured_at": "2026-07-24T01:00:00+00:00",
            "nested": {"b": 2, "a": 1},
        }
        second = {
            "nested": {"a": 1, "b": 2},
            "captured_at": "2026-07-24T02:00:00+00:00",
            "version": "v1",
        }

        self.assertEqual(snapshot_hash(first), snapshot_hash(second))
        self.assertEqual(
            canonical_json({"b": 2, "a": 1}),
            '{"a":1,"b":2}',
        )

    def test_kline_change_changes_hash(self) -> None:
        first = _klines()
        second = [dict(item) for item in first]
        second[-1]["close"] += 1

        self.assertNotEqual(klines_hash(first), klines_hash(second))

    def test_error_sanitization_removes_credentials_and_urls(self) -> None:
        error = (
            "POST https://api.example.com/chat?api_key=secret-key "
            "Authorization=Bearer-token"
        )
        sanitized = sanitize_error(error, secrets=("secret-key",))

        self.assertNotIn("secret-key", sanitized)
        self.assertNotIn("https://", sanitized)
        self.assertNotIn("Bearer-token", sanitized)


class NewsAndAnalyzerSnapshotTests(unittest.TestCase):
    def test_news_snapshot_records_query_time_source_and_items(self) -> None:
        analyzer = NewsAnalyzer.__new__(NewsAnalyzer)
        analyzer.client = MagicMock()
        response = {
            "results": [{
                "title": "Company beats estimates",
                "url": "https://example.com/story",
                "published_date": "2026-07-23",
                "content": "Strong profit growth",
                "score": 0.9,
            }]
        }
        with patch(
            "app.news_analyzer.run_external_call",
            return_value=response,
        ):
            result = analyzer.search_stock_news("AAA.US", days=5)

        self.assertEqual(result["snapshot_version"], NEWS_SNAPSHOT_VERSION)
        self.assertEqual(result["status"], "available")
        self.assertEqual(result["symbol"], "AAA.US")
        self.assertEqual(result["window_days"], 5)
        self.assertEqual(result["source"], "tavily")
        self.assertEqual(
            result["news_items"][0]["published_date"],
            "2026-07-23",
        )
        self.assertTrue(result["observed_at"].endswith("+00:00"))

    def test_news_failure_snapshot_is_explicit_and_sanitized(self) -> None:
        analyzer = NewsAnalyzer.__new__(NewsAnalyzer)
        analyzer.client = MagicMock()
        with patch(
            "app.news_analyzer.run_external_call",
            side_effect=RuntimeError(
                "https://api.example.com?api_key=news-secret"
            ),
        ):
            result = analyzer.search_stock_news("AAA.US")

        serialized = canonical_json(result)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error_type"], "RuntimeError")
        self.assertNotIn("news-secret", serialized)
        self.assertNotIn("https://api.example.com", serialized)

    def test_ai_success_returns_exact_input_and_output_context(self) -> None:
        analyzer = DeepSeekAnalyzer.__new__(DeepSeekAnalyzer)
        analyzer.news_analyzer = MagicMock()
        analyzer.news_analyzer.search_stock_news.return_value = {
            "snapshot_version": NEWS_SNAPSHOT_VERSION,
            "status": "available",
            "observed_at": "2026-07-24T00:00:00+00:00",
            "query": "AAA stock news",
            "window_days": 7,
            "source": "tavily",
            "news_count": 0,
            "news_items": [],
        }
        analyzer.client = MagicMock()
        analyzer.model = "snapshot-model"
        analyzer.temperature = 0.2
        analyzer.style = "professional"
        analyzer._build_prompt = MagicMock(return_value="exact user prompt")
        analyzer._get_system_prompt = MagicMock(
            return_value="exact system prompt"
        )
        response = MagicMock()
        response.choices[0].message.content = (
            '{"action":"BUY","confidence":0.8,"reasoning":["test"]}'
        )

        with patch(
            "app.ai_analyzer.run_external_call",
            return_value=response,
        ):
            result = analyzer.analyze_trading_opportunity(
                "AAA.US",
                _klines(),
                scenario="buy_focus",
                technical_indicators={"current_price": 100},
                quant_score={"total": 70},
            )

        input_context = result["_ai_input_context"]
        output_context = result["_ai_output_context"]
        self.assertEqual(input_context["request_status"], "completed")
        self.assertEqual(input_context["system_prompt"], "exact system prompt")
        self.assertEqual(input_context["user_prompt"], "exact user prompt")
        self.assertEqual(input_context["model"], "snapshot-model")
        self.assertEqual(input_context["temperature"], 0.2)
        self.assertEqual(
            input_context["news_snapshot"]["snapshot_version"],
            NEWS_SNAPSHOT_VERSION,
        )
        self.assertEqual(output_context["status"], "completed")
        self.assertEqual(
            output_context["raw_response"],
            response.choices[0].message.content,
        )
        self.assertEqual(
            output_context["parsed_response"]["action"],
            "BUY",
        )

    def test_ai_failure_preserves_exact_request_context(self) -> None:
        analyzer = DeepSeekAnalyzer.__new__(DeepSeekAnalyzer)
        analyzer.news_analyzer = None
        analyzer.client = MagicMock()
        analyzer.model = "snapshot-model"
        analyzer.temperature = 0.2
        analyzer.style = "professional"
        analyzer._build_prompt = MagicMock(return_value="exact user prompt")
        analyzer._get_system_prompt = MagicMock(
            return_value="exact system prompt"
        )

        with patch(
            "app.ai_analyzer.run_external_call",
            side_effect=RuntimeError(
                "https://api.example.com api_key=ai-secret"
            ),
        ):
            result = analyzer.analyze_trading_opportunity(
                "AAA.US",
                _klines(),
                scenario="buy_focus",
                technical_indicators={"current_price": 100},
                quant_score={"total": 70},
            )

        input_context = result["_ai_input_context"]
        output_context = result["_ai_output_context"]
        self.assertEqual(input_context["request_status"], "failed")
        self.assertEqual(input_context["system_prompt"], "exact system prompt")
        self.assertEqual(input_context["user_prompt"], "exact user prompt")
        self.assertEqual(output_context["status"], "failed")
        self.assertEqual(output_context["error_type"], "RuntimeError")
        serialized = canonical_json(output_context)
        self.assertNotIn("ai-secret", serialized)
        self.assertNotIn("https://api.example.com", serialized)

    def test_news_initialization_failure_is_frozen_in_ai_input(self) -> None:
        with (
            patch("app.ai_analyzer.OpenAI"),
            patch(
                "app.news_analyzer.get_news_analyzer",
                side_effect=RuntimeError(
                    "https://news.example.com api_key=tavily-secret"
                ),
            ),
        ):
            analyzer = DeepSeekAnalyzer(
                api_key="deepseek-secret",
                tavily_api_key="tavily-secret",
            )
        analyzer._build_prompt = MagicMock(return_value="exact user prompt")
        analyzer._get_system_prompt = MagicMock(
            return_value="exact system prompt"
        )
        response = MagicMock()
        response.choices[0].message.content = (
            '{"action":"BUY","confidence":0.8,"reasoning":["test"]}'
        )
        with patch(
            "app.ai_analyzer.run_external_call",
            return_value=response,
        ):
            result = analyzer.analyze_trading_opportunity(
                "AAA.US",
                _klines(),
                technical_indicators={"current_price": 100},
                quant_score={"total": 70},
            )

        news_snapshot = result["_ai_input_context"]["news_snapshot"]
        self.assertEqual(news_snapshot["status"], "error")
        self.assertEqual(news_snapshot["error_type"], "RuntimeError")
        serialized = canonical_json(news_snapshot)
        self.assertNotIn("tavily-secret", serialized)
        self.assertNotIn("https://news.example.com", serialized)


class StockPickerSnapshotPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = duckdb.connect(":memory:")
        _run_migrations(self.connection)

        @contextmanager
        def connection_context():
            yield self.connection

        self.connection_patcher = patch(
            "app.stock_picker.get_connection",
            side_effect=lambda: connection_context(),
        )
        self.connection_patcher.start()
        self.service = StockPickerService()
        self.pool_id = self.service.add_stock("LONG", "AAA.US")
        self.klines = _klines()
        self.indicators = calculate_technical_indicators(self.klines)
        self.score = self.service._calculate_advanced_score_v2(
            self.klines,
            "LONG",
        )

    def tearDown(self) -> None:
        self.connection_patcher.stop()
        self.connection.close()

    def _prepared(self, credentials: dict | None = None) -> dict:
        universe = [{
            "pool_id": self.pool_id,
            "symbol": "AAA.US",
            "pool_type": "LONG",
        }]
        config = {
            **StockPickerService.DEFAULT_CONFIG,
            "updated_at": "2026-07-24T00:00:00",
            "_selection_context": "pool_ranking",
            "_universe_snapshot": universe,
            "_universe_version": snapshot_hash({
                "captured_at": "ignored",
                "universe": universe,
            }),
        }
        return {
            "pool_id": self.pool_id,
            "symbol": "AAA.US",
            "pool_type": "LONG",
            "klines": self.klines,
            "indicators": self.indicators,
            "score": self.score,
            "ai_creds": credentials or {},
            "config": config,
            "force_refresh": True,
            "cache_checked": True,
            "job_id": "snapshot-job",
        }

    def _latest_id(self) -> int:
        return self.connection.execute(
            "SELECT MAX(id) FROM stock_picker_analysis"
        ).fetchone()[0]

    def test_universe_version_participates_in_analysis_cache_key(self) -> None:
        first = self._prepared()["config"]
        second = dict(first)
        second["_universe_version"] = "different-universe"
        first_key = self.service._build_cache_key(
            self.pool_id,
            "AAA.US",
            "LONG",
            self.klines,
            {},
            first,
            "quant",
        )
        second_key = self.service._build_cache_key(
            self.pool_id,
            "AAA.US",
            "LONG",
            self.klines,
            {},
            second,
            "quant",
        )

        self.assertNotEqual(first_key, second_key)

    def test_quant_cache_does_not_depend_on_ai_ranking_version(self) -> None:
        first = self._prepared()["config"]
        second = dict(first)
        second["_selection_version"] = "different-ranking"
        first_key = self.service._build_cache_key(
            self.pool_id,
            "AAA.US",
            "LONG",
            self.klines,
            {},
            first,
            "quant",
        )
        second_key = self.service._build_cache_key(
            self.pool_id,
            "AAA.US",
            "LONG",
            self.klines,
            {},
            second,
            "quant",
        )
        self.assertEqual(first_key, second_key)
        ai_first_key = self.service._build_cache_key(
            self.pool_id,
            "AAA.US",
            "LONG",
            self.klines,
            {"DEEPSEEK_API_KEY": "key"},
            first,
            "quant",
        )
        ai_second_key = self.service._build_cache_key(
            self.pool_id,
            "AAA.US",
            "LONG",
            self.klines,
            {"DEEPSEEK_API_KEY": "key"},
            second,
            "quant",
        )
        self.assertNotEqual(ai_first_key, ai_second_key)

    def test_ai_model_version_participates_in_analysis_cache_key(self) -> None:
        config = self._prepared()["config"]
        credentials = {"DEEPSEEK_API_KEY": "key"}
        current_key = self.service._build_cache_key(
            self.pool_id,
            "AAA.US",
            "LONG",
            self.klines,
            credentials,
            config,
            "ai",
        )
        legacy_service = StockPickerService()
        legacy_service.AI_MODEL = "deepseek-chat"
        legacy_key = legacy_service._build_cache_key(
            self.pool_id,
            "AAA.US",
            "LONG",
            self.klines,
            credentials,
            config,
            "ai",
        )

        self.assertNotEqual(current_key, legacy_key)

    def test_disabled_and_skipped_requests_have_explicit_snapshots(self) -> None:
        asyncio.run(
            self.service._finalize_prepared_analysis(
                self._prepared(),
                use_ai=False,
            )
        )
        disabled = self.service.get_analysis_snapshot(self._latest_id())
        self.assertTrue(disabled["hash_valid"])
        self.assertEqual(
            disabled["ai_input_snapshot"]["request_status"],
            "not_requested",
        )
        self.assertEqual(
            disabled["ai_input_snapshot"]["request_reason"],
            "deepseek_not_configured",
        )
        self.assertIsNone(
            disabled["ai_input_snapshot"]["system_prompt"]
        )
        self.assertEqual(
            disabled["ai_output_snapshot"]["status"],
            "not_requested",
        )

        asyncio.run(
            self.service._finalize_prepared_analysis(
                self._prepared({
                    "DEEPSEEK_API_KEY": "deepseek-secret",
                }),
                use_ai=False,
            )
        )
        skipped = self.service.get_analysis_snapshot(self._latest_id())
        self.assertTrue(skipped["hash_valid"])
        self.assertEqual(
            skipped["ai_input_snapshot"]["request_reason"],
            "outside_ai_top_n",
        )
        self.assertEqual(
            skipped["ai_input_snapshot"]["ai_model"],
            StockPickerService.AI_MODEL,
        )
        self.assertNotIn(
            "deepseek-secret",
            canonical_json(skipped),
        )

    def test_success_snapshot_persists_prompt_news_raw_output_and_versions(
        self,
    ) -> None:
        analyzer = MagicMock()
        analyzer.analyze_trading_opportunity.return_value = {
            "action": "BUY",
            "confidence": 0.8,
            "reasoning": ["AI result"],
            "score": self.score,
            "indicators": self.indicators,
            "ai_status": "available",
            "_ai_input_context": {
                "request_status": "completed",
                "symbol": "AAA.US",
                "scenario": "buy_focus",
                "model": "deepseek-chat",
                "temperature": 0.3,
                "style": "professional",
                "system_prompt": "exact system prompt",
                "user_prompt": "exact user prompt",
                "news_snapshot": {
                    "snapshot_version": NEWS_SNAPSHOT_VERSION,
                    "status": "available",
                    "observed_at": "2026-07-24T00:00:00+00:00",
                    "query": "AAA stock news",
                    "window_days": 7,
                    "source": "tavily",
                    "news_count": 1,
                    "news_items": [{"title": "News"}],
                },
                "response_format": {"type": "json_object"},
            },
            "_ai_output_context": {
                "status": "completed",
                "raw_response": '{"action":"BUY"}',
                "parsed_response": {
                    "action": "BUY",
                    "confidence": 0.8,
                },
                "error_type": None,
                "error": None,
            },
        }
        credentials = {
            "DEEPSEEK_API_KEY": "deepseek-secret",
            "TAVILY_API_KEY": "tavily-secret",
            "DEEPSEEK_BASE_URL": "https://api.deepseek.com",
        }
        with patch(
            "app.ai_analyzer.DeepSeekAnalyzer",
            return_value=analyzer,
        ) as analyzer_class:
            asyncio.run(
                self.service._finalize_prepared_analysis(
                    self._prepared(credentials),
                    use_ai=True,
                )
            )

        analyzer_class.assert_called_once_with(
            api_key="deepseek-secret",
            model="deepseek-v4-flash",
            base_url="https://api.deepseek.com",
            tavily_api_key="tavily-secret",
        )

        detail = self.service.get_analysis_snapshot(self._latest_id())
        serialized = canonical_json(detail)
        self.assertTrue(detail["hash_valid"])
        self.assertEqual(
            detail["ai_input_snapshot"]["version"],
            AI_INPUT_SNAPSHOT_VERSION,
        )
        self.assertEqual(
            detail["ai_output_snapshot"]["version"],
            AI_OUTPUT_SNAPSHOT_VERSION,
        )
        self.assertEqual(
            detail["ai_input_snapshot"]["system_prompt"],
            "exact system prompt",
        )
        self.assertEqual(
            detail["ai_input_snapshot"]["news_snapshot"]["query"],
            "AAA stock news",
        )
        self.assertEqual(
            detail["ai_output_snapshot"]["raw_response"],
            '{"action":"BUY"}',
        )
        self.assertEqual(
            detail["ai_input_snapshot"]["score_version"],
            StockPickerService.SCORE_VERSION,
        )
        self.assertEqual(
            detail["ai_input_snapshot"]["ai_model"],
            "deepseek-v4-flash",
        )
        self.assertTrue(detail["ai_input_snapshot"]["config_version"])
        self.assertTrue(detail["ai_input_snapshot"]["universe_version"])
        self.assertTrue(detail["ai_input_snapshot"]["selection_version"])
        self.assertEqual(
            detail["ai_input_snapshot"]["selection"]["quant_rank"],
            1,
        )
        self.assertNotIn("deepseek-secret", serialized)
        self.assertNotIn("tavily-secret", serialized)
        self.assertNotIn("https://api.deepseek.com", serialized)

        latest = self.service.get_analysis_results("LONG")
        metadata = latest["long_analysis"][0]["metadata"]
        self.assertEqual(
            metadata["ai_snapshot_version"],
            AI_INPUT_SNAPSHOT_VERSION,
        )
        self.assertEqual(metadata["ai_request_status"], "completed")
        self.assertTrue(metadata["ai_snapshot_available"])

    def test_failed_request_and_initialization_failure_survive_fallback(
        self,
    ) -> None:
        analyzer = MagicMock()
        analyzer.analyze_trading_opportunity.return_value = {
            "error": "temporary outage",
            "_ai_input_context": {
                "request_status": "failed",
                "scenario": "buy_focus",
                "model": "deepseek-chat",
                "temperature": 0.3,
                "style": "professional",
                "system_prompt": "exact system prompt",
                "user_prompt": "exact user prompt",
                "news_snapshot": None,
                "response_format": {"type": "json_object"},
            },
            "_ai_output_context": {
                "status": "failed",
                "raw_response": '{"partial":true}',
                "parsed_response": None,
                "error_type": "RuntimeError",
                "error": "temporary outage",
            },
        }
        credentials = {"DEEPSEEK_API_KEY": "deepseek-secret"}
        with patch(
            "app.ai_analyzer.DeepSeekAnalyzer",
            return_value=analyzer,
        ):
            asyncio.run(
                self.service._finalize_prepared_analysis(
                    self._prepared(credentials),
                    use_ai=True,
                )
            )

        failed_id = self._latest_id()
        failed = self.service.get_analysis_snapshot(failed_id)
        row = self.connection.execute(
            "SELECT ai_status, ai_action FROM stock_picker_analysis WHERE id = ?",
            (failed_id,),
        ).fetchone()
        self.assertEqual(row[0], "fallback")
        self.assertEqual(row[1], "BUY")
        self.assertEqual(
            failed["ai_input_snapshot"]["user_prompt"],
            "exact user prompt",
        )
        self.assertEqual(
            failed["ai_output_snapshot"]["raw_response"],
            '{"partial":true}',
        )

        with patch(
            "app.ai_analyzer.DeepSeekAnalyzer",
            side_effect=RuntimeError(
                "https://api.example.com api_key=deepseek-secret"
            ),
        ):
            asyncio.run(
                self.service._finalize_prepared_analysis(
                    self._prepared(credentials),
                    use_ai=True,
                )
            )
        initialization_failed = self.service.get_analysis_snapshot(
            self._latest_id()
        )
        self.assertEqual(
            initialization_failed["ai_input_snapshot"]["request_status"],
            "initialization_failed",
        )
        self.assertEqual(
            initialization_failed["ai_output_snapshot"]["status"],
            "failed",
        )
        serialized = canonical_json(initialization_failed)
        self.assertNotIn("deepseek-secret", serialized)
        self.assertNotIn("https://api.example.com", serialized)

    def test_detail_detects_tampered_kline_snapshot_and_route_handles_404(
        self,
    ) -> None:
        asyncio.run(
            self.service._finalize_prepared_analysis(
                self._prepared(),
                use_ai=False,
            )
        )
        analysis_id = self._latest_id()
        tampered = [dict(item) for item in self.klines]
        tampered[-1]["close"] += 10
        self.connection.execute(
            """
            UPDATE stock_picker_analysis
            SET klines_snapshot = ?
            WHERE id = ?
            """,
            (canonical_json(tampered), analysis_id),
        )

        detail = self.service.get_analysis_snapshot(analysis_id)
        self.assertFalse(detail["hash_valid"])
        self.assertTrue(detail["integrity"]["input_hash_valid"])
        self.assertFalse(detail["integrity"]["klines_hash_valid"])

        isolated_app = FastAPI()
        isolated_app.include_router(stock_picker_api_router)

        async def request_snapshots():
            transport = httpx.ASGITransport(app=isolated_app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as client:
                response = await client.get(
                    f"/api/stock-picker/analysis-snapshots/{analysis_id}"
                )
                missing = await client.get(
                    "/api/stock-picker/analysis-snapshots/999999"
                )
            return response, missing

        with patch(
            "app.routers.stock_picker.get_stock_picker_service",
            return_value=self.service,
        ):
            response, missing = asyncio.run(request_snapshots())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["analysis_id"], analysis_id)
        self.assertEqual(missing.status_code, 404)

    def test_migration_adds_snapshot_columns_idempotently_and_keeps_rows(
        self,
    ) -> None:
        connection = duckdb.connect(":memory:")
        try:
            _run_migrations(connection)
            connection.execute(
                """
                INSERT INTO stock_picker_analysis (
                    pool_id, symbol, pool_type
                ) VALUES (1, 'LEGACY.US', 'LONG')
                """
            )
            connection.execute("DROP INDEX idx_analysis_pool")
            connection.execute("DROP INDEX idx_analysis_time")
            connection.execute("DROP INDEX idx_recommendation")
            connection.execute(
                "ALTER TABLE stock_picker_analysis DROP COLUMN ai_input_snapshot"
            )
            connection.execute(
                "ALTER TABLE stock_picker_analysis DROP COLUMN ai_input_hash"
            )
            connection.execute(
                "ALTER TABLE stock_picker_analysis DROP COLUMN ai_output_snapshot"
            )

            _run_migrations(connection)
            _run_migrations(connection)

            columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info('stock_picker_analysis')"
                ).fetchall()
            }
            row = connection.execute(
                """
                SELECT symbol, ai_input_snapshot, ai_input_hash,
                       ai_output_snapshot
                FROM stock_picker_analysis
                WHERE symbol = 'LEGACY.US'
                """
            ).fetchone()
            self.assertTrue({
                "ai_input_snapshot",
                "ai_input_hash",
                "ai_output_snapshot",
            }.issubset(columns))
            self.assertEqual(row, ("LEGACY.US", None, None, None))
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
