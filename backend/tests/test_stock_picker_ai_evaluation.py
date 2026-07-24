from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import asyncio
import unittest
from unittest.mock import patch

import duckdb
from fastapi import FastAPI
import httpx

from app.db import _run_migrations
from app.routers.stock_picker import router as stock_picker_api_router
from app.stock_picker import StockPickerService
from app.stock_picker_ai_evaluation import (
    AI_INCREMENT_EVALUATION_VERSION,
    StockPickerAIIncrementEvaluationService,
)
from app.stock_picker_ai_snapshots import (
    AI_INPUT_SNAPSHOT_VERSION,
    AI_OUTPUT_SNAPSHOT_VERSION,
    canonical_json,
    sha256_json,
    snapshot_hash,
)


class StockPickerAIIncrementEvaluationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = duckdb.connect(":memory:")
        _run_migrations(self.connection)

        @contextmanager
        def connection_context():
            yield self.connection

        self.connection_factory = connection_context
        self.bars_by_symbol: dict[str, list[dict]] = {}

        def bar_loader(symbol, period="day", limit=5000):
            self.assertEqual(period, "day")
            return list(self.bars_by_symbol.get(symbol, []))[-limit:]

        self.service = StockPickerAIIncrementEvaluationService(
            stock_picker=StockPickerService(),
            bar_loader=bar_loader,
            connection_factory=self.connection_factory,
            now_provider=lambda: datetime(
                2026,
                7,
                24,
                tzinfo=timezone.utc,
            ),
        )

    def tearDown(self) -> None:
        self.connection.close()

    def _insert_batch(
        self,
        *,
        job_id: str = "job-1",
        pool_type: str = "LONG",
        included_pool_ids: tuple[int, ...] = (1, 2, 3, 4),
        failed_pool_id: int | None = None,
        tampered_pool_id: int | None = None,
        invalid_selection_version: bool = False,
        score_version: str = StockPickerService.SCORE_VERSION,
        news_enabled: bool = True,
        analysis_time: str = "2026-07-10T12:00:00",
    ) -> None:
        symbols = {
            1: "AAA.US",
            2: "BBB.US",
            3: "CCC.US",
            4: "DDD.US",
        }
        ai_results = {
            1: {"action": "BUY", "confidence": 0.5},
            2: {"action": "HOLD", "confidence": 0.9},
            3: {"action": "BUY", "confidence": 0.9},
            4: None,
        }
        if pool_type == "SHORT":
            ai_results[1]["action"] = "SELL"
            ai_results[3]["action"] = "SELL"
        ranking = [
            {
                "rank": pool_id,
                "pool_id": pool_id,
                "symbol": symbols[pool_id],
                "score_total": 100.0 - pool_id,
            }
            for pool_id in symbols
        ]
        selection_version = sha256_json({
            "pool_type": pool_type,
            "ai_top_n_per_pool": 3,
            "ranking": ranking,
        })
        if invalid_selection_version:
            selection_version = "invalid-selection-version"
        for pool_id in included_pool_ids:
            selected = pool_id <= 3
            failed = pool_id == failed_pool_id
            request_status = (
                "failed"
                if failed
                else "completed"
                if selected
                else "not_requested"
            )
            snapshot = {
                "version": AI_INPUT_SNAPSHOT_VERSION,
                "captured_at": "2026-07-10T12:00:00+00:00",
                "request_status": request_status,
                "request_reason": (
                    "outside_ai_top_n"
                    if not selected
                    else None
                ),
                "symbol": symbols[pool_id],
                "pool_type": pool_type,
                "score_version": score_version,
                "prompt_version": StockPickerService.PROMPT_VERSION,
                "ai_model": StockPickerService.AI_MODEL,
                "news_enabled": news_enabled,
                "technical_indicators": {
                    "current_price": 100.0,
                },
                "quant_score": {
                    "total": 100.0 - pool_id,
                    "trend_strength": 0.5,
                },
                "selection_context": "pool_ranking",
                "selection_version": selection_version,
                "selection": {
                    "quant_rank": pool_id,
                    "ai_top_n_per_pool": 3,
                    "ai_selected": selected,
                    "ranking": ranking,
                },
            }
            input_hash = snapshot_hash(snapshot)
            if pool_id == tampered_pool_id:
                input_hash = "0" * 64
            parsed_response = (
                ai_results[pool_id]
                if selected and not failed
                else None
            )
            output_snapshot = {
                "version": AI_OUTPUT_SNAPSHOT_VERSION,
                "captured_at": "2026-07-10T12:00:00+00:00",
                "status": (
                    "failed"
                    if failed
                    else "completed"
                    if selected
                    else "not_requested"
                ),
                "raw_response": None,
                "parsed_response": parsed_response,
                "error_type": "RuntimeError" if failed else None,
                "error": "temporary outage" if failed else None,
            }
            recommendation_score = (
                StockPickerService()._calculate_recommendation_score_v2(
                    snapshot["quant_score"],
                    {
                        **parsed_response,
                        "ai_status": "available",
                    },
                    pool_type,
                )
                if parsed_response
                else StockPickerService()._calculate_recommendation_score_v2(
                    snapshot["quant_score"],
                    {"ai_status": "fallback" if failed else "skipped"},
                    pool_type,
                )
            )
            self.connection.execute(
                """
                INSERT INTO stock_picker_analysis (
                    pool_id, symbol, pool_type, analysis_time,
                    current_price, score_total, recommendation_score,
                    ai_action, ai_confidence, ai_status, data_as_of,
                    job_id, ai_input_snapshot, ai_input_hash,
                    ai_output_snapshot
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pool_id,
                    symbols[pool_id],
                    pool_type,
                    analysis_time,
                    100.0,
                    100.0 - pool_id,
                    recommendation_score,
                    (
                        parsed_response["action"]
                        if parsed_response
                        else "HOLD"
                    ),
                    (
                        parsed_response["confidence"]
                        if parsed_response
                        else 0.0
                    ),
                    (
                        "fallback"
                        if failed
                        else "available"
                        if selected
                        else "skipped"
                    ),
                    "2026-07-01T00:00:00",
                    job_id,
                    canonical_json(snapshot),
                    input_hash,
                    canonical_json(output_snapshot),
                ),
            )

    def _set_future_prices(
        self,
        prices: dict[str, list[float]] | None = None,
    ) -> None:
        prices = prices or {
            "AAA.US": [110.0, 120.0],
            "BBB.US": [90.0, 80.0],
            "CCC.US": [130.0, 140.0],
            "DDD.US": [100.0, 100.0],
        }
        for symbol, closes in prices.items():
            self.bars_by_symbol[symbol] = [
                {
                    "ts": "2026-07-01T00:00:00",
                    "close": 100.0,
                },
                *[
                    {
                        "ts": f"2026-07-{index + 1:02d}T00:00:00",
                        "close": close,
                    }
                    for index, close in enumerate(closes, start=1)
                ],
            ]

    def _run_ready(self, **overrides):
        parameters = {
            "pool_type": "LONG",
            "horizons": [1],
            "top_k": 2,
            "minimum_complete_batches": 1,
            "minimum_labeled_records": 3,
            "minimum_ai_completion_rate": 1.0,
            "persist": False,
        }
        parameters.update(overrides)
        return self.service.run(**parameters)

    def test_ready_report_compares_ai_and_quant_rankings(self) -> None:
        self._insert_batch()
        self._set_future_prices()

        report = self._run_ready()
        metrics = report["metrics"]["1"]

        self.assertTrue(report["ready"])
        self.assertEqual(
            report["evaluation_version"],
            AI_INCREMENT_EVALUATION_VERSION,
        )
        self.assertAlmostEqual(
            metrics["quant_top_k"]["average"],
            0.0,
        )
        self.assertAlmostEqual(
            metrics["ai_top_k"]["average"],
            0.2,
        )
        self.assertAlmostEqual(
            metrics["paired_delta"]["average"],
            0.2,
        )
        self.assertEqual(metrics["selection_changed_batches"], 1)
        self.assertEqual(
            report["coverage"]["labeled_records_by_horizon"]["1"],
            3,
        )

    def test_short_direction_reverses_future_return_labels(self) -> None:
        self._insert_batch(pool_type="SHORT")
        self._set_future_prices()

        report = self._run_ready(pool_type="SHORT")
        metrics = report["metrics"]["1"]

        self.assertAlmostEqual(
            metrics["quant_top_k"]["average"],
            0.0,
        )
        self.assertAlmostEqual(
            metrics["ai_top_k"]["average"],
            -0.2,
        )
        self.assertAlmostEqual(
            metrics["paired_delta"]["average"],
            -0.2,
        )

    def test_gate_suppresses_metrics_when_sample_is_too_small(self) -> None:
        self._insert_batch()
        self._set_future_prices()

        report = self.service.run(
            pool_type="LONG",
            horizons=[1],
            top_k=2,
            minimum_complete_batches=2,
            minimum_labeled_records=6,
            persist=False,
        )

        self.assertFalse(report["ready"])
        self.assertIsNone(report["metrics"])
        self.assertIn(
            "insufficient_ai_complete_batches",
            report["gate"]["reasons"],
        )
        self.assertIn(
            "insufficient_paired_batches_1d",
            report["gate"]["reasons"],
        )
        self.assertIn(
            "insufficient_labeled_records_1d",
            report["gate"]["reasons"],
        )

    def test_missing_future_bars_never_becomes_zero_return(self) -> None:
        self._insert_batch()

        report = self._run_ready()

        self.assertFalse(report["ready"])
        self.assertIsNone(report["metrics"])
        self.assertEqual(
            report["coverage"]["labeled_records_by_horizon"]["1"],
            0,
        )
        self.assertEqual(
            report["coverage"]["paired_batches_by_horizon"]["1"],
            0,
        )

    def test_incomplete_batch_is_excluded(self) -> None:
        self._insert_batch(included_pool_ids=(1, 2, 3))
        self._set_future_prices()

        report = self._run_ready()

        self.assertFalse(report["ready"])
        self.assertEqual(
            report["coverage"]["structurally_complete_batches"],
            0,
        )
        self.assertEqual(
            report["coverage"]["excluded_batches"]["incomplete_batch"],
            1,
        )

    def test_tampered_input_hash_excludes_batch(self) -> None:
        self._insert_batch(tampered_pool_id=2)
        self._set_future_prices()

        report = self._run_ready()

        self.assertFalse(report["ready"])
        self.assertEqual(
            report["coverage"]["excluded_batches"]["input_hash_invalid"],
            1,
        )

    def test_selection_version_is_recomputed_from_frozen_ranking(self) -> None:
        self._insert_batch(invalid_selection_version=True)
        self._set_future_prices()

        report = self._run_ready()

        self.assertFalse(report["ready"])
        self.assertEqual(
            report["coverage"]["excluded_batches"][
                "selection_version_invalid"
            ],
            1,
        )

    def test_version_cohort_mismatch_is_not_mixed(self) -> None:
        self._insert_batch(score_version="legacy-score")
        self._set_future_prices()

        report = self._run_ready()

        self.assertFalse(report["ready"])
        self.assertEqual(
            report["coverage"]["excluded_batches"][
                "version_cohort_mismatch"
            ],
            1,
        )

    def test_news_mode_filters_explicit_cohorts(self) -> None:
        self._insert_batch(news_enabled=False)
        self._set_future_prices()

        excluded = self._run_ready(news_mode="enabled")
        included = self._run_ready(news_mode="disabled")

        self.assertFalse(excluded["ready"])
        self.assertEqual(
            excluded["coverage"]["excluded_batches"][
                "news_mode_mismatch"
            ],
            1,
        )
        self.assertTrue(included["ready"])
        self.assertEqual(
            included["parameters"]["news_mode"],
            "disabled",
        )

    def test_denormalized_price_must_match_frozen_input(self) -> None:
        self._insert_batch()
        self._set_future_prices()
        self.connection.execute(
            """
            UPDATE stock_picker_analysis
            SET current_price = 999
            WHERE pool_id = 1
            """
        )

        report = self._run_ready()

        self.assertFalse(report["ready"])
        self.assertEqual(
            report["coverage"]["excluded_batches"][
                "snapshot_record_mismatch"
            ],
            1,
        )

    def test_final_score_is_recomputed_from_frozen_ai_output(self) -> None:
        self._insert_batch()
        self._set_future_prices()
        self.connection.execute(
            """
            UPDATE stock_picker_analysis
            SET recommendation_score = 0
            WHERE pool_id = 1
            """
        )

        report = self._run_ready()

        self.assertFalse(report["ready"])
        self.assertEqual(
            report["coverage"]["excluded_batches"][
                "derived_score_mismatch"
            ],
            1,
        )

    def test_ai_failure_is_reported_and_excluded_from_primary_metrics(
        self,
    ) -> None:
        self._insert_batch(failed_pool_id=2)
        self._set_future_prices()

        report = self._run_ready(minimum_ai_completion_rate=0.5)

        self.assertFalse(report["ready"])
        self.assertAlmostEqual(
            report["coverage"]["ai_completion_rate"],
            2 / 3,
        )
        self.assertEqual(
            report["coverage"]["excluded_batches"]["ai_incomplete_batch"],
            1,
        )
        self.assertIsNone(report["metrics"])

    def test_multiple_horizons_use_the_nth_future_trading_bar(self) -> None:
        self._insert_batch()
        self._set_future_prices()

        report = self._run_ready(
            horizons=[1, 2],
            minimum_labeled_records=3,
        )

        self.assertTrue(report["ready"])
        self.assertAlmostEqual(
            report["metrics"]["1"]["paired_delta"]["average"],
            0.2,
        )
        self.assertAlmostEqual(
            report["metrics"]["2"]["paired_delta"]["average"],
            0.3,
        )
        self.assertEqual(
            report["coverage"]["latest_label_date"],
            "2026-07-03",
        )

    def test_report_persistence_and_history_round_trip(self) -> None:
        self._insert_batch()
        self._set_future_prices()

        report = self._run_ready(persist=True)
        history = self.service.get_history(limit=5)

        self.assertGreater(report["id"], 0)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["id"], report["id"])
        self.assertTrue(history[0]["ready"])
        self.assertEqual(
            history[0]["result"]["metrics"]["1"]["paired_batches"],
            1,
        )

    def test_migration_is_idempotent_and_preserves_reports(self) -> None:
        self.connection.execute(
            """
            INSERT INTO stock_picker_ai_evaluations (
                pool_type, evaluation_version, parameters,
                result, ready
            ) VALUES ('LONG', 'v-test', '{}', '{}', FALSE)
            """
        )

        _run_migrations(self.connection)
        columns = {
            row[1]
            for row in self.connection.execute(
                "PRAGMA table_info('stock_picker_ai_evaluations')"
            ).fetchall()
        }
        count = self.connection.execute(
            "SELECT COUNT(*) FROM stock_picker_ai_evaluations"
        ).fetchone()[0]

        self.assertTrue({
            "evaluation_version",
            "parameters",
            "result",
            "ready",
            "data_as_of",
        }.issubset(columns))
        self.assertEqual(count, 1)

    def test_http_run_history_and_validation_contract(self) -> None:
        self._insert_batch()
        self._set_future_prices()
        app = FastAPI()
        app.include_router(stock_picker_api_router)

        async def exercise():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as client:
                response = await client.post(
                    "/api/stock-picker/ai-evaluation",
                    json={
                        "pool_type": "LONG",
                        "horizons": [1],
                        "top_k": 2,
                        "minimum_complete_batches": 1,
                        "minimum_labeled_records": 3,
                        "minimum_ai_completion_rate": 1,
                    },
                )
                invalid = await client.post(
                    "/api/stock-picker/ai-evaluation",
                    json={
                        "pool_type": "LONG",
                        "top_k": 0,
                    },
                )
                history = await client.get(
                    "/api/stock-picker/ai-evaluations?limit=5"
                )
            return response, invalid, history

        with patch(
            "app.routers.stock_picker."
            "get_stock_picker_ai_evaluation_service",
            return_value=self.service,
        ):
            response, invalid, history = asyncio.run(exercise())

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ready"])
        self.assertEqual(invalid.status_code, 422)
        self.assertEqual(history.status_code, 200)
        self.assertEqual(len(history.json()["items"]), 1)

    def test_invalid_service_parameters_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "horizons"):
            self.service.run(
                pool_type="LONG",
                horizons=[0],
                persist=False,
            )
        with self.assertRaisesRegex(ValueError, "整数"):
            self.service.run(
                pool_type="LONG",
                horizons=[1.5],
                persist=False,
            )
        with self.assertRaisesRegex(ValueError, "completion_rate"):
            self.service.run(
                pool_type="LONG",
                minimum_ai_completion_rate=float("nan"),
                persist=False,
            )


if __name__ == "__main__":
    unittest.main()
