from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import unittest

import duckdb

from app.db import _run_migrations
from app.quant_stock_selector_ai import AICompletion
from app.quant_stock_selector_hashing import canonical_json, canonical_sha256
from app.quant_stock_selector_shadow import (
    FLASH_MODEL_ALIAS,
    PRO_MODEL_ALIAS,
    QuantShadowEvaluationError,
    QuantShadowEvaluationRepository,
    QuantShadowEvaluationService,
)


NOW = datetime(2026, 7, 25, 22, tzinfo=timezone.utc)


class _ConnectionFactory:
    def __init__(self) -> None:
        self.connection = duckdb.connect(":memory:")
        _run_migrations(self.connection)

    @contextmanager
    def __call__(self):
        yield self.connection

    def close(self) -> None:
        self.connection.close()


def _decision(*, suitability=80, confidence=0.8, decision="SELECT"):
    return json.dumps({
        "decision": decision,
        "confidence": confidence,
        "suitability_score": suitability,
        "risk_level": "MEDIUM",
        "time_horizon_days": 10,
        "reasons": ["trend", "liquidity"],
        "risks": ["volatility"],
        "entry_condition": "close above MA20",
        "invalidation_condition": "close below MA20",
        "data_conflicts": [],
    })


class _Provider:
    temperature = 0.1

    def __init__(self, alias, model_id, *, raw_text, fail=False):
        self.model_alias = alias
        self.model_id = model_id
        self.raw_text = raw_text
        self.fail = fail
        self.calls = []

    def resolve_model_id(self):
        return self.model_id

    def complete(self, *, system_prompt, user_prompt, response_schema):
        self.calls.append({
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
            "response_schema": response_schema,
        })
        if self.fail:
            raise RuntimeError("provider unavailable")
        return AICompletion(
            raw_text=self.raw_text,
            resolved_model_id=self.model_id,
            input_tokens=100,
            output_tokens=20,
        )


class QuantShadowEvaluationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.factory = _ConnectionFactory()
        self.addCleanup(self.factory.close)
        self.repository = QuantShadowEvaluationRepository(
            connection_factory=self.factory,
            clock=lambda: NOW,
        )
        self.input_snapshot = {
            "input_schema_version": "quant-selector-ai-input-v1",
            "model_alias": FLASH_MODEL_ALIAS,
            "model_policy_version": "quant-selector-deepseek-flash-v1",
            "prompt_version": "quant-selector-ai-prompt-v1",
            "response_schema": {"type": "object"},
            "system_prompt": "frozen system prompt",
            "temperature": 0.1,
            "user_prompt": "frozen user prompt",
            "facts": {"symbol": "AAA.US", "revision": 1},
        }
        self.ai_input_hash = canonical_sha256(self.input_snapshot)
        self._insert_source_run()

    def _insert_source_run(self, *, status="completed", tampered=False):
        self.factory.connection.execute(
            """
            INSERT INTO quant_selection_runs (
                run_id, runtime_id, status, force_refresh, created_at,
                data_as_of, universe_version, filter_version, score_version,
                prompt_version, model_policy_version, model_alias,
                ai_planned_count, ai_completed_count, error_summary
            ) VALUES ('qsr_source', 'runtime', ?, FALSE, ?, '2026-07-24',
                      'universe-v1', 'filter-v1', 'score-v1',
                      'quant-selector-ai-prompt-v1', 'policy-v1',
                      'deepseek-v4-flash', 1, 1, '[]')
            """,
            [status, NOW],
        )
        persisted = {**self.input_snapshot, "ai_input_hash": self.ai_input_hash}
        if tampered:
            persisted["facts"] = {"symbol": "TAMPERED.US"}
        self.factory.connection.execute(
            """
            INSERT INTO quant_selection_ai_snapshots (
                run_id, symbol, request_status, attempts, ai_input_hash,
                input_snapshot, raw_attempt_outputs, model_alias,
                resolved_model_id, prompt_version, model_policy_version
            ) VALUES ('qsr_source', 'AAA.US', 'completed', 1, ?, ?, '[]',
                      'deepseek-v4-flash', 'flash-production-v1',
                      'quant-selector-ai-prompt-v1', 'policy-v1')
            """,
            [self.ai_input_hash, canonical_json(persisted)],
        )

    def _service(self, *, pro_fail=False):
        flash = _Provider(
            FLASH_MODEL_ALIAS,
            "flash-v1",
            raw_text=_decision(suitability=80),
        )
        pro = _Provider(
            PRO_MODEL_ALIAS,
            "pro-v1",
            raw_text=_decision(suitability=85),
            fail=pro_fail,
        )
        service = QuantShadowEvaluationService(
            flash,
            pro,
            repository=self.repository,
            max_workers=2,
            pro_latency_budget_ms=60_000,
            pro_cost_budget_usd=1,
            pro_input_cost_per_million_usd=1,
            pro_output_cost_per_million_usd=2,
        )
        return service, flash, pro

    def test_flash_and_pro_receive_identical_frozen_requests(self):
        service, flash, pro = self._service()

        result = service.run("qsr_source")

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["paired_input_count"], 1)
        self.assertEqual(flash.calls, pro.calls)
        self.assertEqual(result["result"]["flash"]["completion_rate"], 1.0)
        self.assertEqual(result["result"]["pro"]["completion_rate"], 1.0)
        self.assertEqual(
            result["result"]["paired"]["average_pro_minus_flash_suitability"],
            5.0,
        )
        self.assertAlmostEqual(
            result["result"]["pro"]["estimated_cost_usd"],
            0.00014,
        )
        self.assertEqual(
            result["result"]["promotion_gate"]["reasons"],
            ["paired_outcome_or_blind_quality_not_attached"],
        )
        observations = self.repository.get_observations(result["shadow_id"])
        self.assertEqual(len(observations), 2)
        self.assertEqual(
            {item["paired_input_hash"] for item in observations},
            {observations[0]["paired_input_hash"]},
        )
        self.assertEqual(
            {item["production_ai_input_hash"] for item in observations},
            {self.ai_input_hash},
        )

    def test_one_model_failure_is_partial_and_never_changes_source_run(self):
        service, flash, pro = self._service(pro_fail=True)

        result = service.run("qsr_source")

        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["flash_completed_count"], 1)
        self.assertEqual(result["pro_completed_count"], 0)
        self.assertEqual(len(flash.calls), 1)
        self.assertEqual(len(pro.calls), 2)
        source = self.factory.connection.execute(
            "SELECT status, ai_completed_count FROM quant_selection_runs"
        ).fetchone()
        self.assertEqual(source, ("completed", 1))

    def test_noncompleted_or_tampered_source_fails_before_creation(self):
        self.factory.connection.execute(
            "DELETE FROM quant_selection_ai_snapshots"
        )
        self.factory.connection.execute("DELETE FROM quant_selection_runs")
        self._insert_source_run(status="partial")
        service, _flash, _pro = self._service()
        with self.assertRaisesRegex(QuantShadowEvaluationError, "must be completed"):
            service.start("qsr_source")
        count = self.factory.connection.execute(
            "SELECT COUNT(*) FROM quant_selection_shadow_evaluations"
        ).fetchone()[0]
        self.assertEqual(count, 0)

        self.factory.connection.execute(
            "DELETE FROM quant_selection_ai_snapshots"
        )
        self.factory.connection.execute("DELETE FROM quant_selection_runs")
        self._insert_source_run(tampered=True)
        with self.assertRaisesRegex(QuantShadowEvaluationError, "hash mismatch"):
            service.start("qsr_source")


if __name__ == "__main__":
    unittest.main()
