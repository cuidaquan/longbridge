from __future__ import annotations

from copy import deepcopy
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app
from app.quant_stock_selector_service import QuantSelectionRunNotFound
from app.routers.quant_stock_selector import (
    configure_quant_selection_evaluation_service,
    configure_quant_selection_quality_service,
    configure_quant_selection_service,
    configure_quant_shadow_evaluation_service,
)


def _run(run_id="qsr_test", status="queued", **overrides):
    value = {
        "run_id": run_id,
        "runtime_id": "runtime-test",
        "status": status,
        "force_refresh": False,
        "created_at": "2026-07-24T21:30:00.000Z",
        "started_at": None,
        "completed_at": None,
        "data_as_of": None,
        "universe_version": "quant-selector-universe-v1.4",
        "filter_version": "quant-selector-filter-v1.4",
        "score_version": "quant-selector-score-v1.2",
        "prompt_version": "quant-selector-ai-prompt-v2",
        "model_policy_version": "quant-selector-deepseek-flash-v1",
        "model_alias": "deepseek-v4-flash",
        "resolved_model_id": None,
        "candidate_count": 0,
        "ai_planned_count": 0,
        "ai_completed_count": 0,
        "final_count": 0,
        "error_summary": [],
        "quant_manifest": {"private": "large"},
        "run_manifest": {"private": "large"},
        "quant_input_hash": None,
        "run_input_hash": None,
        "cache_key": None,
        "reused_from_run_id": None,
    }
    value.update(overrides)
    return value


class _Repository:
    def __init__(self) -> None:
        self.runs = {}

    def list_runs(self, limit=20):
        return list(reversed(list(self.runs.values())))[:limit]

    def latest_completed(self):
        return next(
            (run for run in reversed(list(self.runs.values())) if run["status"] == "completed"),
            None,
        )

    def get_results(self, run_id):
        if run_id not in self.runs:
            raise QuantSelectionRunNotFound(run_id)
        return {
            "run": deepcopy(self.runs[run_id]),
            "results": [{"symbol": "AAA.US", "final_score": 88.0}],
            "candidates": [{"symbol": "AAA.US"}, {"symbol": "SPY.US"}],
        }


class _Service:
    def __init__(self) -> None:
        self.repository = _Repository()
        self.force_refresh_values = []
        self.executed = []

    def create_run(self, *, force_refresh=False):
        run_id = f"qsr_{len(self.repository.runs) + 1}"
        run = _run(run_id, force_refresh=force_refresh)
        self.repository.runs[run_id] = run
        self.force_refresh_values.append(force_refresh)
        return deepcopy(run)

    def execute_run(self, run_id):
        self.executed.append(run_id)
        self.repository.runs[run_id].update({
            "status": "completed",
            "started_at": "2026-07-24T21:30:01.000Z",
            "completed_at": "2026-07-24T21:30:02.000Z",
            "data_as_of": "2026-07-24",
            "candidate_count": 2,
            "ai_planned_count": 1,
            "ai_completed_count": 1,
            "final_count": 1,
        })
        return deepcopy(self.repository.runs[run_id])

    def terminate_stale_run(self, run_id):
        try:
            return deepcopy(self.repository.runs[run_id])
        except KeyError as exc:
            raise QuantSelectionRunNotFound(run_id) from exc


class _EvaluationService:
    def __init__(self) -> None:
        self.run_calls = []

    def run(self, *, persist=True):
        self.run_calls.append(persist)
        return {
            "id": 1,
            "evaluation_version": "quant-selector-effect-v1",
            "ready": False,
            "gate": {"ready": False, "reasons": ["insufficient_completed_run_dates"]},
            "coverage": {"distinct_completed_run_dates": 1},
            "metrics": None,
        }

    def get_history(self, limit=20):
        return [{"id": 1, "ready": False, "limit": limit}]


class _QualityService:
    def __init__(self) -> None:
        self.limits = []

    def report(self, *, limit=100):
        self.limits.append(limit)
        return {
            "quality_report_version": "quant-selector-quality-v1",
            "ready": False,
            "sample": {"terminal_runs": 1},
            "metrics": {"ai_completion_rate": {"value": 1.0}},
        }


class _ShadowRepository:
    def __init__(self, owner) -> None:
        self.owner = owner

    def list(self, *, limit=20):
        self.owner.limits.append(limit)
        return list(self.owner.evaluations.values())

    def get(self, shadow_id):
        from app.quant_stock_selector_shadow import QuantShadowEvaluationError

        try:
            return self.owner.evaluations[shadow_id]
        except KeyError as exc:
            raise QuantShadowEvaluationError("shadow evaluation not found") from exc


class _ShadowService:
    def __init__(self) -> None:
        self.evaluations = {}
        self.executed = []
        self.limits = []
        self.repository = _ShadowRepository(self)

    def start(self, source_run_id):
        item = {
            "shadow_id": "qse_1",
            "source_run_id": source_run_id,
            "status": "running",
        }
        self.evaluations[item["shadow_id"]] = item
        return item

    def execute(self, shadow_id):
        self.executed.append(shadow_id)
        self.evaluations[shadow_id] = {
            **self.evaluations[shadow_id],
            "status": "completed",
        }
        return self.evaluations[shadow_id]


class QuantStockSelectorApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = _Service()
        self.evaluation_service = _EvaluationService()
        self.quality_service = _QualityService()
        self.shadow_service = _ShadowService()
        configure_quant_selection_service(self.service)
        configure_quant_selection_evaluation_service(self.evaluation_service)
        configure_quant_selection_quality_service(self.quality_service)
        configure_quant_shadow_evaluation_service(self.shadow_service)
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.client.close()
        configure_quant_selection_service(None)
        configure_quant_selection_evaluation_service(None)
        configure_quant_selection_quality_service(None)
        configure_quant_shadow_evaluation_service(None)

    def test_create_accepts_only_force_refresh_and_runs_in_background(self) -> None:
        response = self.client.post(
            "/api/quant-stock-selector/runs",
            json={"force_refresh": True},
        )
        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertEqual(payload["status"], "queued")
        self.assertTrue(payload["force_refresh"])
        self.assertNotIn("quant_manifest", payload)
        self.assertEqual(self.service.force_refresh_values, [True])
        self.assertEqual(self.service.executed, [payload["run_id"]])

        invalid = self.client.post(
            "/api/quant-stock-selector/runs",
            json={"market": "US"},
        )
        self.assertEqual(invalid.status_code, 422)

    def test_run_history_results_and_latest_contracts(self) -> None:
        run_id = self.client.post(
            "/api/quant-stock-selector/runs",
            json={},
        ).json()["run_id"]

        detail = self.client.get(f"/api/quant-stock-selector/runs/{run_id}")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["status"], "completed")
        self.assertNotIn("run_manifest", detail.json())

        history = self.client.get("/api/quant-stock-selector/runs?limit=20")
        self.assertEqual(history.status_code, 200)
        self.assertEqual(history.json()["items"][0]["run_id"], run_id)

        results = self.client.get(
            f"/api/quant-stock-selector/runs/{run_id}/results"
        )
        self.assertEqual(results.status_code, 200)
        self.assertEqual(results.json()["results"][0]["symbol"], "AAA.US")
        self.assertEqual(len(results.json()["candidates"]), 2)

        latest = self.client.get("/api/quant-stock-selector/latest")
        self.assertEqual(latest.status_code, 200)
        self.assertEqual(latest.json()["run"]["run_id"], run_id)

    def test_sse_immediately_recovers_persisted_terminal_snapshot(self) -> None:
        run_id = self.client.post(
            "/api/quant-stock-selector/runs",
            json={},
        ).json()["run_id"]
        response = self.client.get(
            f"/api/quant-stock-selector/runs/{run_id}/events"
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "text/event-stream; charset=utf-8")
        self.assertIn("event: completed", response.text)
        self.assertIn(f'"run_id": "{run_id}"', response.text)
        self.assertNotIn("quant_manifest", response.text)

    def test_missing_and_empty_latest_are_404(self) -> None:
        missing = self.client.get(
            "/api/quant-stock-selector/runs/qsr_missing"
        )
        self.assertEqual(missing.status_code, 404)
        latest = self.client.get("/api/quant-stock-selector/latest")
        self.assertEqual(latest.status_code, 404)

    def test_unconfigured_service_fails_closed(self) -> None:
        configure_quant_selection_service(None)
        with patch(
            "app.quant_stock_selector_data.build_configured_quant_selection_service",
            side_effect=RuntimeError("DEEPSEEK_API_KEY is not configured"),
        ):
            response = self.client.post(
                "/api/quant-stock-selector/runs",
                json={},
            )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json()["detail"],
            "量化优选服务尚未就绪：DeepSeek API Key 未配置",
        )

    def test_service_initialization_error_reports_specific_cause(self) -> None:
        configure_quant_selection_service(None)
        with patch(
            "app.quant_stock_selector_data.build_configured_quant_selection_service",
            side_effect=RuntimeError("snapshot repository unavailable"),
        ):
            response = self.client.get(
                "/api/quant-stock-selector/runs?limit=1"
            )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json()["detail"],
            "量化优选服务初始化失败：snapshot repository unavailable",
        )

    def test_fixed_evaluation_and_history_contracts(self) -> None:
        evaluation = self.client.post(
            "/api/quant-stock-selector/evaluation",
            json={},
        )
        self.assertEqual(evaluation.status_code, 200)
        self.assertFalse(evaluation.json()["ready"])
        self.assertEqual(self.evaluation_service.run_calls, [True])

        invalid = self.client.post(
            "/api/quant-stock-selector/evaluation",
            json={"bootstrap_samples": 10},
        )
        self.assertEqual(invalid.status_code, 422)

        history = self.client.get("/api/quant-stock-selector/evaluations?limit=7")
        self.assertEqual(history.status_code, 200)
        self.assertEqual(history.json()["items"][0]["limit"], 7)

    def test_quality_report_contract(self) -> None:
        response = self.client.get("/api/quant-stock-selector/quality?limit=25")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["quality_report_version"],
            "quant-selector-quality-v1",
        )
        self.assertEqual(self.quality_service.limits, [25])

    def test_shadow_evaluation_contracts(self) -> None:
        created = self.client.post(
            "/api/quant-stock-selector/shadow-evaluations",
            json={"source_run_id": "qsr_source"},
        )
        self.assertEqual(created.status_code, 202)
        self.assertEqual(created.json()["status"], "running")
        self.assertEqual(self.shadow_service.executed, ["qse_1"])

        detail = self.client.get(
            "/api/quant-stock-selector/shadow-evaluations/qse_1"
        )
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["status"], "completed")

        history = self.client.get(
            "/api/quant-stock-selector/shadow-evaluations?limit=7"
        )
        self.assertEqual(history.status_code, 200)
        self.assertEqual(history.json()["items"][0]["shadow_id"], "qse_1")
        self.assertEqual(self.shadow_service.limits, [7])

        invalid = self.client.post(
            "/api/quant-stock-selector/shadow-evaluations",
            json={"source_run_id": "qsr_source", "model": "pro"},
        )
        self.assertEqual(invalid.status_code, 422)

    def test_unconfigured_evaluation_fails_closed(self) -> None:
        configure_quant_selection_evaluation_service(None)
        response = self.client.post(
            "/api/quant-stock-selector/evaluation",
            json={},
        )
        self.assertEqual(response.status_code, 503)
        self.assertIn("总回报价格", response.json()["detail"])


if __name__ == "__main__":
    unittest.main()
