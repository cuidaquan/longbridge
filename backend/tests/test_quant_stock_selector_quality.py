from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import unittest

import duckdb

from app.db import _run_migrations
from app.quant_stock_selector_hashing import canonical_json, canonical_sha256
from app.quant_stock_selector_quality import QuantSelectionQualityService


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


class QuantSelectionQualityTests(unittest.TestCase):
    def test_catalog_evidence_requires_usmain_source_and_capture_fields(self):
        complete = {
            "catalog_evidence": {
                "market": "US",
                "board": "USMAIN",
                "exchange": "NASDAQ",
                "source": "longbridge",
                "source_version": "2026-07-24",
                "captured_at": "2026-07-24T20:05:00.000Z",
            }
        }
        self.assertTrue(
            QuantSelectionQualityService._has_catalog_evidence(complete)
        )
        for field in ("board", "captured_at", "source_version"):
            with self.subTest(field=field):
                incomplete = json.loads(json.dumps(complete))
                incomplete["catalog_evidence"][field] = ""
                self.assertFalse(
                    QuantSelectionQualityService._has_catalog_evidence(
                        incomplete
                    )
                )

    def setUp(self) -> None:
        self.factory = _ConnectionFactory()
        self.addCleanup(self.factory.close)
        self.service = QuantSelectionQualityService(
            connection_factory=self.factory,
            clock=lambda: NOW,
        )

    def _insert_run(
        self,
        run_id: str,
        *,
        status: str,
        marker: int,
        stable_manifest: bool,
        ai_planned: int,
        ai_completed: int,
        duration_seconds: int,
        missing_hard_filter_reason: bool = False,
    ) -> None:
        snapshot_payload = {"revision": 1, "symbols": ["AAA.US", "BAD.US"]}
        snapshot_hash = canonical_sha256(snapshot_payload)
        references = [{
            "source": "licensed-source",
            "schema_version": "source-v1",
            "payload_hash": snapshot_hash,
        }]
        quant_manifest = {
            "candidates": [{"symbol": "AAA.US", "quant_score": marker}],
            "input_snapshots": references,
            "selection_manifest": {
                "candidate_set_method": (
                    "deterministic-full-score-v1.3"
                    if stable_manifest else "legacy-boundary"
                ),
                "ranking": [{
                    "symbol": "AAA.US",
                    "q": marker,
                    "median_turnover_20d": 100_000_000,
                    "rank": 1,
                }],
                "top_n": 30,
                "top_symbols": ["AAA.US"],
            },
        }
        quant_hash = canonical_sha256(quant_manifest)
        ai_input = {"symbol": "AAA.US", "facts": {"revision": 1}}
        ai_hash = canonical_sha256(ai_input)
        run_manifest = {
            "quant_input_hash": quant_hash,
            "ai_inputs": [{"symbol": "AAA.US", "ai_input_hash": ai_hash}],
        }
        run_hash = canonical_sha256(run_manifest)
        started_at = NOW.replace(hour=20)
        completed_at = started_at.timestamp() + duration_seconds
        completed_datetime = datetime.fromtimestamp(
            completed_at,
            tz=timezone.utc,
        )
        self.factory.connection.execute(
            """
            INSERT INTO quant_selection_runs (
                run_id, runtime_id, status, force_refresh, created_at,
                started_at, completed_at, data_as_of, universe_version,
                filter_version, score_version, prompt_version,
                model_policy_version, model_alias, resolved_model_id,
                candidate_count, ai_planned_count, ai_completed_count,
                final_count, error_summary, quant_manifest, run_manifest,
                quant_input_hash, run_input_hash
            ) VALUES (?, 'runtime', ?, FALSE, ?, ?, ?, '2026-07-24',
                      'universe-v1', 'filter-v1', 'score-v1', 'prompt-v1',
                      'policy-v1', 'deepseek-v4-flash', 'flash-v1', 2, ?, ?,
                      0, '[]', ?, ?, ?, ?)
            """,
            [
                run_id,
                status,
                started_at,
                started_at,
                completed_datetime,
                ai_planned,
                ai_completed,
                canonical_json(quant_manifest),
                canonical_json(run_manifest),
                quant_hash,
                run_hash,
            ],
        )
        self.factory.connection.execute(
            """
            INSERT INTO quant_selection_input_snapshots (
                run_id, snapshot_id, snapshot_kind, symbol, source,
                schema_version, captured_at, data_as_of, payload_hash, payload
            ) VALUES (?, 'snapshot', 'security_catalog', NULL,
                      'licensed-source', 'source-v1', ?, ?, ?, ?)
            """,
            [run_id, NOW, NOW, snapshot_hash, canonical_json(snapshot_payload)],
        )
        selected = {
            "symbol": "AAA.US",
            "catalog_evidence": {
                "market": "US",
                "board": "USMAIN",
                "exchange": "NASDAQ",
                "source": "longbridge",
                "source_version": "2026-07-24",
                "captured_at": "2026-07-24T20:05:00.000Z",
            },
            "hard_filters": {
                f"H{index}": {"status": "pass"} for index in range(1, 9)
            },
            "exclusion_reasons": [],
        }
        excluded = {
            "symbol": "BAD.US",
            "catalog_evidence": dict(selected["catalog_evidence"]),
            "hard_filters": {
                "H1": {"status": "fail", "reason": "market_not_us"}
            },
            "exclusion_reasons": (
                [] if missing_hard_filter_reason else ["market_not_us"]
            ),
        }
        for payload, selected_for_ai in ((selected, True), (excluded, False)):
            self.factory.connection.execute(
                """
                INSERT INTO quant_selection_candidates (
                    run_id, symbol, selection_status,
                    candidate_quant_input_hash, payload_hash, payload,
                    selected_for_ai
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    run_id,
                    payload["symbol"],
                    "quant_eligible" if selected_for_ai else "hard_filter_failed",
                    "c" * 64,
                    canonical_sha256(payload),
                    canonical_json(payload),
                    selected_for_ai,
                ],
            )
        if status == "completed":
            self.factory.connection.execute(
                """
                INSERT INTO quant_selection_ai_snapshots (
                    run_id, symbol, request_status, attempts, ai_input_hash,
                    input_snapshot, raw_attempt_outputs, model_alias,
                    resolved_model_id, prompt_version, model_policy_version
                ) VALUES (?, 'AAA.US', 'completed', 1, ?, ?, '[]',
                          'deepseek-v4-flash', 'flash-v1', 'prompt-v1',
                          'policy-v1')
                """,
                [run_id, ai_hash, canonical_json(ai_input)],
            )

    def test_report_exposes_all_operational_quality_metrics(self) -> None:
        self._insert_run(
            "qsr_complete",
            status="completed",
            marker=90,
            stable_manifest=True,
            ai_planned=1,
            ai_completed=1,
            duration_seconds=100,
        )
        self._insert_run(
            "qsr_partial",
            status="partial",
            marker=80,
            stable_manifest=False,
            ai_planned=2,
            ai_completed=1,
            duration_seconds=400,
            missing_hard_filter_reason=True,
        )
        self._insert_run(
            "qsr_replay",
            status="completed",
            marker=80,
            stable_manifest=True,
            ai_planned=1,
            ai_completed=1,
            duration_seconds=120,
        )

        report = self.service.report()
        metrics = report["metrics"]

        self.assertFalse(report["ready"])
        self.assertEqual(report["sample"]["terminal_runs"], 3)
        self.assertEqual(
            metrics["catalog_evidence_coverage"]["value"],
            1.0,
        )
        self.assertAlmostEqual(
            metrics["hard_filter_reason_coverage"]["value"],
            2 / 3,
        )
        self.assertEqual(metrics["ai_completion_rate"]["value"], 0.75)
        self.assertEqual(metrics["stable_candidate_ranking_rate"]["value"], 1.0)
        self.assertEqual(metrics["input_hash_validity_rate"]["value"], 1.0)
        self.assertEqual(metrics["quant_replay_consistency_rate"]["value"], 0.0)
        self.assertEqual(metrics["run_duration_p95_seconds"]["value"], 400)
        self.assertIn("ai_completion_rate", report["gate_reasons"])
        self.assertIn("run_duration_p95_seconds", report["gate_reasons"])

    def test_tampered_snapshot_is_counted_as_invalid_hash_input(self) -> None:
        self._insert_run(
            "qsr_tampered",
            status="completed",
            marker=90,
            stable_manifest=True,
            ai_planned=1,
            ai_completed=1,
            duration_seconds=100,
        )
        self.factory.connection.execute(
            """
            UPDATE quant_selection_input_snapshots SET payload = '{"tampered":true}'
            WHERE run_id = 'qsr_tampered'
            """
        )

        report = self.service.report()

        self.assertEqual(
            report["metrics"]["input_hash_validity_rate"]["value"],
            0.0,
        )

    def test_empty_history_reports_insufficient_samples_without_crashing(self) -> None:
        report = self.service.report()

        self.assertFalse(report["ready"])
        self.assertEqual(report["sample"]["terminal_runs"], 0)
        self.assertIsNone(
            report["metrics"]["run_duration_p95_seconds"]["value"]
        )
        self.assertIsNone(
            report["metrics"]["stable_candidate_ranking_rate"]["value"]
        )
        self.assertIsNone(
            report["metrics"]["input_hash_validity_rate"]["value"]
        )
        self.assertIn("insufficient_auditable_runs", report["gate_reasons"])
        self.assertIn("insufficient_duration_samples", report["gate_reasons"])


if __name__ == "__main__":
    unittest.main()
