from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

import duckdb

from app.db import _run_migrations
from app.quant_stock_selector_evaluation import (
    BOOTSTRAP_SEED,
    BOOTSTRAP_SAMPLES,
    JsonQuantOutcomeProvider,
    OUTCOME_SCHEMA_VERSION,
    QuantOutcomeError,
    QuantOutcomeSnapshot,
    QuantSelectionEvaluationService,
)
from app.quant_stock_selector_hashing import canonical_json, canonical_sha256


UTC = timezone.utc


class _ConnectionFactory:
    def __init__(self) -> None:
        self.connection = duckdb.connect(":memory:")
        _run_migrations(self.connection)

    @contextmanager
    def __call__(self):
        yield self.connection

    def close(self) -> None:
        self.connection.close()


class _OutcomeProvider:
    def __init__(self, payload):
        self.payload = payload

    def capture(self):
        return QuantOutcomeSnapshot(
            payload=self.payload,
            payload_hash=canonical_sha256(self.payload),
        )


def _dates(count=100):
    start = date(2026, 1, 2)
    return [(start + timedelta(days=index)).isoformat() for index in range(count)]


def _outcome_payload(calendar=None):
    calendar = calendar or _dates()

    def security(daily_rate):
        return {
            "return_basis": "validated_total_return",
            "corporate_actions_status": "complete",
            "terminal_status": "active_as_of_capture",
            "bars": [
                {
                    "trade_date": value,
                    "open_total_return": 100 * ((1 + daily_rate) ** index),
                    "close_total_return": 100 * ((1 + daily_rate) ** (index + 1)),
                    "official_session": True,
                }
                for index, value in enumerate(calendar)
            ],
        }

    return {
        "schema_version": OUTCOME_SCHEMA_VERSION,
        "source": "licensed-total-return-vendor",
        "source_version": "2026-07-25",
        "captured_at": "2026-07-25T22:00:00Z",
        "nyse_calendar": calendar,
        "symbols": {
            "AAA.US": security(0.004),
            "BBB.US": security(-0.003),
            "SPY.US": security(0.001),
        },
    }


def _candidate_payload(symbol):
    return {
        "symbol": symbol,
        "metadata": {
            "asset_class": "common_stock",
            "source": "licensed-metadata-vendor",
            "source_version": "2026-01-01",
        },
        "hard_filters": {
            f"H{index}": {"status": "pass"} for index in range(1, 12)
        },
    }


class QuantSelectionEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.factory = _ConnectionFactory()
        self.addCleanup(self.factory.close)
        self.payload = _outcome_payload()
        self.service = QuantSelectionEvaluationService(
            _OutcomeProvider(self.payload),
            connection_factory=self.factory,
            integrity_checker=lambda _run_id: True,
            clock=lambda: datetime(2026, 7, 25, 22, tzinfo=UTC),
        )

    def _insert_run(
        self,
        run_id,
        run_date,
        *,
        status="completed",
        completed_at=None,
        ai_completed=1,
    ):
        completed_at = completed_at or f"{run_date}T22:00:00Z"
        digest = "a" * 64
        self.factory.connection.execute(
            """
            INSERT INTO quant_selection_runs (
                run_id, runtime_id, status, force_refresh, created_at,
                completed_at, data_as_of, universe_version, filter_version,
                score_version, prompt_version, model_policy_version,
                model_alias, resolved_model_id, candidate_count,
                ai_planned_count, ai_completed_count, final_count,
                error_summary, quant_input_hash, run_input_hash
            ) VALUES (?, 'runtime', ?, FALSE, ?, ?, ?, 'universe-v1',
                      'filter-v1', 'score-v1', 'prompt-v1', 'policy-v1',
                      'deepseek-v4-flash', 'flash-immutable-v1', 2, 1, ?, 1,
                      '[]', ?, ?)
            """,
            [
                run_id,
                status,
                completed_at,
                completed_at,
                run_date,
                ai_completed,
                digest,
                digest,
            ],
        )
        if status == "completed":
            self._insert_candidate(run_id, "BBB.US", quant_rank=1)
            self._insert_candidate(
                run_id, "AAA.US", quant_rank=2, final_rank=1, final_selected=True
            )

    def _insert_candidate(
        self,
        run_id,
        symbol,
        *,
        quant_rank,
        final_rank=None,
        final_selected=False,
    ):
        payload = _candidate_payload(symbol)
        self.factory.connection.execute(
            """
            INSERT INTO quant_selection_candidates (
                run_id, symbol, selection_status,
                candidate_quant_input_hash, payload_hash, payload,
                quant_score, quant_rank, final_rank, final_selected
            ) VALUES (?, ?, 'quant_eligible', ?, ?, ?, 90, ?, ?, ?)
            """,
            [
                run_id,
                symbol,
                "b" * 64,
                canonical_sha256(payload),
                canonical_json(payload),
                quant_rank,
                final_rank,
                final_selected,
            ],
        )

    def test_fixed_slots_costs_and_authoritative_terminal_outcome(self):
        payload = _outcome_payload(_dates(8))
        payload["symbols"]["AAA.US"]["bars"][1].update({
            "open_total_return": 100.0,
            "close_total_return": 101.0,
        })
        payload["symbols"]["AAA.US"]["terminal_outcome"] = {
            "trade_date": payload["nyse_calendar"][3],
            "kind": "delisting_cash_settlement",
            "total_return_value": 80.0,
            "authoritative": True,
        }
        payload["symbols"]["AAA.US"]["terminal_status"] = "terminated"

        result = self.service._portfolio_return(
            ["AAA.US"],
            payload["nyse_calendar"][1],
            payload["nyse_calendar"][5],
            payload,
        )
        self.assertAlmostEqual(result["return"], 0.1 * (-0.2 - 0.002))
        self.assertEqual(
            self.service._portfolio_return([], "2026-01-01", "2026-01-02", payload)["return"],
            0.0,
        )

    def test_missing_filled_slot_label_never_becomes_cash(self):
        result = self.service._portfolio_return(
            ["MISSING.US"], _dates()[1], _dates()[5], self.payload
        )
        self.assertIsNone(result["return"])
        self.assertEqual(result["reason"], "symbol_outcome_missing")

    def test_run_date_deduplication_partial_slo_and_gate(self):
        first_date = _dates()[0]
        self._insert_run("qsr_old", first_date, completed_at="2026-01-02T21:00:00Z")
        self._insert_run("qsr_latest", first_date, completed_at="2026-01-02T23:00:00Z")
        self._insert_run("qsr_partial", _dates()[1], status="partial", ai_completed=0)

        report = self.service.run(persist=False)

        self.assertFalse(report["ready"])
        self.assertEqual(report["coverage"]["completed_runs_in_cohort"], 2)
        self.assertEqual(report["coverage"]["distinct_completed_run_dates"], 1)
        self.assertEqual(report["coverage"]["duplicate_completed_runs"], 1)
        self.assertAlmostEqual(report["coverage"]["ai_completion_rate"], 2 / 3)
        self.assertIsNone(report["metrics"])
        self.assertIn("insufficient_ai_completion_rate", report["gate"]["reasons"])

    def test_ready_report_uses_run_level_pairs_and_reproducible_bootstrap(self):
        for index, run_date in enumerate(_dates()[:60]):
            self._insert_run(f"qsr_{index:03d}", run_date)

        report = self.service.run(persist=True)

        self.assertTrue(report["ready"])
        self.assertEqual(report["coverage"]["paired_run_dates_by_horizon"], {
            "5": 60,
            "10": 60,
            "20": 60,
        })
        self.assertEqual(report["coverage"]["input_hash_coverage"], 1.0)
        self.assertEqual(report["coverage"]["product_scope_coverage"], 1.0)
        inference = report["metrics"]["horizons"]["20"][
            "final_minus_quant_inference"
        ]
        self.assertEqual(inference["bootstrap_samples"], BOOTSTRAP_SAMPLES)
        self.assertEqual(inference["seed"], BOOTSTRAP_SEED)
        self.assertEqual(inference["effective_block_size"], 4)
        self.assertGreaterEqual(inference["lower"], 0)
        self.assertEqual(
            report["metrics"]["horizons"]["20"]["ai_increment_conclusion"],
            "positive_increment_in_current_sample",
        )
        self.assertIsNotNone(report["metrics"]["portfolio_turnover"]["final_top_10_average"])

        history = self.service.get_history(limit=5)
        self.assertEqual(len(history), 1)
        self.assertTrue(history[0]["ready"])
        self.assertEqual(history[0]["outcome_snapshot_hash"], canonical_sha256(self.payload))

    def test_invalid_integrity_blocks_labels_and_hash_gate(self):
        self._insert_run("qsr_bad", _dates()[0])
        service = QuantSelectionEvaluationService(
            _OutcomeProvider(self.payload),
            connection_factory=self.factory,
            integrity_checker=lambda _run_id: False,
        )
        report = service.run(persist=False)
        self.assertEqual(report["coverage"]["input_hash_coverage"], 0.0)
        self.assertEqual(report["coverage"]["paired_run_dates_by_horizon"]["5"], 0)
        self.assertIn("incomplete_input_hash_coverage", report["gate"]["reasons"])


class JsonQuantOutcomeProviderTests(unittest.TestCase):
    def test_provider_normalizes_and_hashes_verified_total_returns(self):
        payload = _outcome_payload(_dates(3))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "outcomes.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            snapshot = JsonQuantOutcomeProvider(path).capture()
        self.assertEqual(snapshot.payload["nyse_calendar"], sorted(payload["nyse_calendar"]))
        self.assertEqual(snapshot.payload_hash, canonical_sha256(snapshot.payload))

    def test_provider_rejects_unofficial_or_unadjusted_prices(self):
        payload = _outcome_payload(_dates(3))
        payload["symbols"]["AAA.US"]["bars"][0]["official_session"] = False
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "outcomes.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(QuantOutcomeError, "official regular session"):
                JsonQuantOutcomeProvider(path).capture()

    def test_provider_requires_explicit_corporate_action_and_terminal_coverage(self):
        payload = _outcome_payload(_dates(3))
        payload["symbols"]["AAA.US"]["corporate_actions_status"] = "unknown"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "outcomes.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(QuantOutcomeError, "corporate_actions_status"):
                JsonQuantOutcomeProvider(path).capture()

    def test_provider_rejects_bars_after_terminal_date(self):
        payload = _outcome_payload(_dates(3))
        payload["symbols"]["AAA.US"].update({
            "terminal_status": "terminated",
            "terminal_outcome": {
                "trade_date": payload["nyse_calendar"][1],
                "kind": "delisting_cash_settlement",
                "total_return_value": 80,
                "authoritative": True,
            },
        })
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "outcomes.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(QuantOutcomeError, "bars after terminal date"):
                JsonQuantOutcomeProvider(path).capture()


if __name__ == "__main__":
    unittest.main()
