from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
import json
import unittest

import duckdb

from app.db import _run_migrations
from app.quant_stock_selector_ai import (
    AICompletion,
    AICandidateContext,
    QuantAISelectionService,
)
from app.quant_stock_selector_hashing import canonical_sha256
from app.quant_stock_selector_service import (
    CapturedInputSnapshot,
    CapturedQuantRun,
    QuantSelectionRunConflict,
    QuantSelectionRunRepository,
    QuantSelectionService,
)


NOW = datetime(2026, 7, 24, 21, 30, tzinfo=timezone.utc)
DATA_AS_OF = date(2026, 7, 24)


class _ConnectionFactory:
    def __init__(self) -> None:
        self.connection = duckdb.connect(":memory:")
        _run_migrations(self.connection)

    @contextmanager
    def __call__(self):
        yield self.connection

    def close(self) -> None:
        self.connection.close()


class _AIProvider:
    model_alias = "deepseek-v4-flash"
    temperature = 0.1

    def __init__(self, *, fail_symbols=()) -> None:
        self.fail_symbols = set(fail_symbols)
        self.resolve_calls = 0
        self.complete_calls = 0

    def resolve_model_id(self) -> str:
        self.resolve_calls += 1
        return "deepseek-v4-flash-20260701"

    def complete(self, *, system_prompt, user_prompt, response_schema):
        self.complete_calls += 1
        symbol = next(
            value
            for value in ("AAA.US", "BBB.US")
            if value in user_prompt
        )
        if symbol in self.fail_symbols:
            raise RuntimeError("provider unavailable")
        return AICompletion(
            raw_text=json.dumps({
                "decision": "SELECT",
                "confidence": 0.9,
                "suitability_score": 90,
                "risk_level": "MEDIUM",
                "time_horizon_days": 10,
                "reasons": ["trend", "liquidity"],
                "risks": ["volatility"],
                "entry_condition": "close above MA20",
                "invalidation_condition": "close below MA20",
                "data_conflicts": [],
            }),
            resolved_model_id="deepseek-v4-flash-20260701",
        )


class _InputProvider:
    def __init__(self, captured=None, error=None) -> None:
        self.captured = captured
        self.error = error
        self.calls = 0

    def capture(self):
        self.calls += 1
        if self.error:
            raise self.error
        return self.captured


def _bars() -> list[dict]:
    start = datetime(2026, 3, 20, tzinfo=timezone.utc)
    return [
        {
            "ts": (start + timedelta(days=index)).isoformat(),
            "open": 100 + index,
            "high": 102 + index,
            "low": 99 + index,
            "close": 101 + index,
            "volume": 1_000_000 + index,
            "turnover": 100_000_000 + index,
        }
        for index in range(90)
    ]


def _candidate(symbol: str, *, selected=True) -> dict:
    catalog_evidence = {
        "board": "USMAIN",
        "exchange": "NASDAQ",
        "market": "US",
        "source": "longbridge",
        "source_version": "2026-07-24",
        "captured_at": "2026-07-25T00:00:00Z",
    }
    indicators = {
        "data_as_of": DATA_AS_OF.isoformat(),
        "close": 120.0,
        "median_turnover_20d": 100_000_000.0,
        "ma20": 110.0,
        "ma60": 100.0,
    }
    score = {
        "score_version": "quant-selector-score-v1.2",
        "total": 90.0,
        "liquidity": 24.0,
        "trend": 24.0,
        "relative_strength": 19.0,
        "momentum": 14.0,
        "risk": 9.0,
    }
    candidate_input = {
        "symbol": symbol,
        "name": symbol,
        "catalog_evidence": catalog_evidence,
        "indicators": indicators,
    }
    return {
        **candidate_input,
        "candidate_quant_input_hash": canonical_sha256(candidate_input),
        "hard_filters": {f"H{index}": {"status": "pass"} for index in range(1, 9)},
        "quant_score": score,
        "selection_status": "quant_eligible" if selected else "hard_filter_failed",
        "exclusion_reasons": [] if selected else ["benchmark_excluded"],
        "selected_for_ai": selected,
    }


def _context(candidate: dict) -> AICandidateContext:
    return AICandidateContext(
        symbol=candidate["symbol"],
        name=candidate["name"],
        data_as_of=DATA_AS_OF.isoformat(),
        security_context=candidate["catalog_evidence"],
        quant_score=candidate["quant_score"],
        indicators=candidate["indicators"],
        daily_bars=_bars(),
        spy_state={"return_20d": 0.02, "return_60d": 0.05},
        news_snapshot={"status": "available", "news_items": []},
        event_snapshot={"status": "available", "events": []},
        missing_fields=[],
    )


def _captured(
    *,
    symbols=("AAA.US",),
    snapshot_revision=1,
    quant_status="completed",
    required_inputs_complete=True,
) -> CapturedQuantRun:
    selected = [_candidate(symbol) for symbol in symbols]
    excluded = _candidate("SPY.US", selected=False)
    candidates = [*selected, excluded]
    ranking = [
        {
            "symbol": item["symbol"],
            "rank": index + 1,
            "q": item["quant_score"]["total"],
            "median_turnover_20d": item["indicators"]["median_turnover_20d"],
        }
        for index, item in enumerate(selected)
    ]
    manifest = {
        "boundary_proven": quant_status == "completed",
        "candidates": [
            {
                "symbol": item["symbol"],
                "selection_status": item["selection_status"],
                "hard_filters": item["hard_filters"],
            }
            for item in sorted(candidates, key=lambda value: value["symbol"])
        ],
        "filter_version": "quant-selector-filter-v1.4",
        "q_threshold": 65.0,
        "ranking": ranking,
        "top_n": 30,
        "top_symbols": list(symbols) if quant_status == "completed" else [],
    }
    quant = {
        "status": quant_status,
        "boundary_proven": quant_status == "completed",
        "filter_version": "quant-selector-filter-v1.4",
        "data_as_of": DATA_AS_OF.isoformat(),
        "official_close": "2026-07-24T20:00:00.000Z",
        "candidates": candidates,
        "quant_ranking": ranking,
        "ai_candidate_symbols": manifest["top_symbols"],
        "selection_manifest": manifest,
        "selection_manifest_hash": canonical_sha256(manifest),
    }
    contexts = {item["symbol"]: _context(item) for item in selected}
    return CapturedQuantRun(
        data_as_of=DATA_AS_OF,
        quant_selection=quant,
        ai_contexts=contexts,
        input_snapshots=[
            CapturedInputSnapshot(
                snapshot_kind="security_catalog",
                source="longbridge",
                schema_version="catalog-v1",
                captured_at=NOW,
                data_as_of=NOW,
                payload={"revision": snapshot_revision, "symbols": [*symbols, "SPY.US"]},
            ),
        ],
        required_inputs_complete=required_inputs_complete,
        errors=[] if required_inputs_complete else ["required_snapshot_missing"],
    )


class QuantSelectionServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.factory = _ConnectionFactory()
        self.addCleanup(self.factory.close)
        self.repository = QuantSelectionRunRepository(
            connection_factory=self.factory,
            clock=lambda: NOW,
        )

    def _service(self, captured, ai_provider=None, *, runtime_id="runtime-a"):
        provider = ai_provider or _AIProvider()
        service = QuantSelectionService(
            _InputProvider(captured),
            QuantAISelectionService(provider, max_attempts=2, max_workers=1),
            repository=self.repository,
            runtime_id=runtime_id,
            clock=lambda: NOW,
        )
        return service, provider

    def test_completed_run_persists_all_candidates_and_integrity(self) -> None:
        service, provider = self._service(_captured())
        created = service.create_run()
        completed = service.execute_run(created["run_id"])

        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["candidate_count"], 2)
        self.assertEqual(completed["ai_planned_count"], 1)
        self.assertEqual(completed["ai_completed_count"], 1)
        self.assertEqual(completed["final_count"], 1)
        self.assertTrue(self.repository.verify_integrity(created["run_id"]))
        results = self.repository.get_results(created["run_id"])
        self.assertEqual([item["symbol"] for item in results["results"]], ["AAA.US"])
        self.assertEqual(len(results["candidates"]), 2)
        self.assertEqual(provider.complete_calls, 1)

    def test_results_only_include_published_final_candidates(self) -> None:
        service, _provider = self._service(_captured())
        run_id = service.create_run()["run_id"]
        service.execute_run(run_id)

        with self.factory() as connection:
            connection.execute(
                """
                UPDATE quant_selection_candidates
                SET final_selected = FALSE
                WHERE run_id = ? AND symbol = 'AAA.US'
                """,
                [run_id],
            )

        results = self.repository.get_results(run_id)
        self.assertEqual(results["results"], [])

    def test_display_scores_are_rounded_without_changing_hash_inputs(self) -> None:
        captured = _captured()
        candidate = captured.quant_selection["candidates"][0]
        candidate["quant_score"].update({
            "total": 90.123456789,
            "liquidity": 24.123456789,
            "trend": 23.987654321,
        })
        captured.ai_contexts["AAA.US"].quant_score.update(
            candidate["quant_score"]
        )
        service, _provider = self._service(captured)

        result = service.execute_run(service.create_run()["run_id"])
        persisted = self.repository.get_results(result["run_id"])
        aaa = next(
            item for item in persisted["candidates"]
            if item["symbol"] == "AAA.US"
        )

        self.assertEqual(aaa["quant_score"]["total"], 90.123457)
        self.assertEqual(aaa["quant_score"]["liquidity"], 24.123457)
        self.assertEqual(aaa["quant_score"]["trend"], 23.987654)
        self.assertEqual(aaa["result"]["quant_score"], 90.123457)
        self.assertEqual(aaa["result"]["ai_score"], 90.0)
        self.assertEqual(aaa["result"]["final_score"], 90.08642)
        raw_candidate = next(
            item for item in result["quant_manifest"]["candidates"]
            if item["symbol"] == "AAA.US"
        )
        self.assertEqual(raw_candidate["quant_score"]["total"], 90.123456789)

    def test_cache_reuse_keeps_new_run_identity_and_new_snapshots(self) -> None:
        service, provider = self._service(_captured())
        first = service.create_run()
        first_done = service.execute_run(first["run_id"])
        second = service.create_run()
        second_done = service.execute_run(second["run_id"])

        self.assertNotEqual(first_done["run_id"], second_done["run_id"])
        self.assertEqual(second_done["reused_from_run_id"], first_done["run_id"])
        self.assertEqual(second_done["run_input_hash"], first_done["run_input_hash"])
        self.assertEqual(provider.complete_calls, 1)
        count = self.factory.connection.execute(
            "SELECT COUNT(*) FROM quant_selection_input_snapshots WHERE run_id = ?",
            [second_done["run_id"]],
        ).fetchone()[0]
        self.assertEqual(count, 1)
        self.assertTrue(self.repository.verify_integrity(second_done["run_id"]))

    def test_force_refresh_preserves_hashes_but_bypasses_cache(self) -> None:
        service, provider = self._service(_captured())
        first = service.execute_run(service.create_run()["run_id"])
        forced = service.execute_run(
            service.create_run(force_refresh=True)["run_id"]
        )

        self.assertIsNone(forced["reused_from_run_id"])
        self.assertEqual(forced["run_input_hash"], first["run_input_hash"])
        self.assertEqual(forced["cache_key"], first["cache_key"])
        self.assertEqual(provider.complete_calls, 2)

    def test_changed_snapshot_changes_root_hash_and_cache_key(self) -> None:
        service, provider = self._service(_captured(snapshot_revision=1))
        first = service.execute_run(service.create_run()["run_id"])
        service.input_provider.captured = _captured(snapshot_revision=2)
        second = service.execute_run(service.create_run()["run_id"])

        self.assertNotEqual(first["quant_input_hash"], second["quant_input_hash"])
        self.assertNotEqual(first["run_input_hash"], second["run_input_hash"])
        self.assertNotEqual(first["cache_key"], second["cache_key"])
        self.assertIsNone(second["reused_from_run_id"])
        self.assertEqual(provider.complete_calls, 2)

    def test_corrupted_snapshot_prevents_cache_reuse(self) -> None:
        service, provider = self._service(_captured())
        first = service.execute_run(service.create_run()["run_id"])
        self.factory.connection.execute(
            """
            UPDATE quant_selection_input_snapshots SET payload = '{\"tampered\":true}'
            WHERE run_id = ? AND snapshot_kind = 'security_catalog'
            """,
            [first["run_id"]],
        )
        second = service.execute_run(service.create_run()["run_id"])

        self.assertFalse(self.repository.verify_integrity(first["run_id"]))
        self.assertIsNone(second["reused_from_run_id"])
        self.assertEqual(provider.complete_calls, 2)

    def test_quant_partial_saves_diagnostics_without_ai_or_results(self) -> None:
        service, provider = self._service(
            _captured(quant_status="partial", required_inputs_complete=False)
        )
        result = service.execute_run(service.create_run()["run_id"])

        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["ai_planned_count"], 0)
        self.assertEqual(provider.resolve_calls, 0)
        self.assertEqual(provider.complete_calls, 0)
        persisted = self.repository.get_results(result["run_id"])
        self.assertEqual(persisted["results"], [])
        self.assertEqual(len(persisted["candidates"]), 2)

    def test_zero_ai_candidates_is_a_cacheable_completed_run(self) -> None:
        service, provider = self._service(_captured(symbols=()))
        first = service.execute_run(service.create_run()["run_id"])
        second = service.execute_run(service.create_run()["run_id"])

        self.assertEqual(first["status"], "completed")
        self.assertEqual(first["ai_planned_count"], 0)
        self.assertEqual(first["final_count"], 0)
        self.assertEqual(second["reused_from_run_id"], first["run_id"])
        self.assertEqual(provider.complete_calls, 0)
        self.assertTrue(self.repository.verify_integrity(second["run_id"]))

    def test_deleted_candidate_or_snapshot_invalidates_integrity(self) -> None:
        service, _provider = self._service(_captured())
        first = service.execute_run(service.create_run()["run_id"])
        self.factory.connection.execute(
            "DELETE FROM quant_selection_candidates WHERE run_id = ? AND symbol = 'SPY.US'",
            [first["run_id"]],
        )
        self.assertFalse(self.repository.verify_integrity(first["run_id"]))

        second = service.execute_run(service.create_run(force_refresh=True)["run_id"])
        self.factory.connection.execute(
            """
            DELETE FROM quant_selection_input_snapshots
            WHERE run_id = ? AND snapshot_id = (
                SELECT snapshot_id FROM quant_selection_input_snapshots
                WHERE run_id = ? LIMIT 1
            )
            """,
            [second["run_id"], second["run_id"]],
        )
        self.assertFalse(self.repository.verify_integrity(second["run_id"]))

    def test_snapshot_identity_includes_kind_and_source(self) -> None:
        common = {
            "captured_at": NOW,
            "data_as_of": NOW,
            "schema_version": "v1",
            "payload": {"same": True},
        }
        first = CapturedInputSnapshot(
            snapshot_kind="catalog",
            source="source-a",
            **common,
        ).normalized()
        second = CapturedInputSnapshot(
            snapshot_kind="metadata",
            source="source-a",
            **common,
        ).normalized()
        third = CapturedInputSnapshot(
            snapshot_kind="catalog",
            source="source-b",
            **common,
        ).normalized()
        self.assertEqual(first["payload_hash"], second["payload_hash"])
        self.assertEqual(first["payload_hash"], third["payload_hash"])
        self.assertEqual(len({first["snapshot_id"], second["snapshot_id"], third["snapshot_id"]}), 3)

    def test_one_planned_ai_failure_makes_run_partial(self) -> None:
        ai_provider = _AIProvider(fail_symbols={"BBB.US"})
        service, provider = self._service(
            _captured(symbols=("AAA.US", "BBB.US")),
            ai_provider,
        )
        result = service.execute_run(service.create_run()["run_id"])

        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["ai_planned_count"], 2)
        self.assertEqual(result["ai_completed_count"], 1)
        self.assertEqual(result["final_count"], 0)
        self.assertEqual(self.repository.get_results(result["run_id"])["results"], [])
        self.assertEqual(provider.complete_calls, 3)

    def test_capture_failure_is_failed_not_partial(self) -> None:
        provider = _AIProvider()
        service = QuantSelectionService(
            _InputProvider(error=RuntimeError("catalog unavailable")),
            QuantAISelectionService(provider, max_workers=1),
            repository=self.repository,
            runtime_id="runtime-a",
            clock=lambda: NOW,
        )
        result = service.execute_run(service.create_run()["run_id"])
        self.assertEqual(result["status"], "failed")
        self.assertIn("catalog unavailable", result["error_summary"][0])

    def test_state_machine_rejects_invalid_transition_and_terminates_stale(self) -> None:
        service, _provider = self._service(_captured())
        queued = service.create_run()
        with self.assertRaises(QuantSelectionRunConflict):
            self.repository.transition(queued["run_id"], "completed")

        service.repository.transition(
            queued["run_id"], "loading_universe", started_at=NOW
        )
        other_service, _ = self._service(_captured(), runtime_id="runtime-b")
        stale = other_service.terminate_stale_run(queued["run_id"])
        self.assertEqual(stale["status"], "failed")
        self.assertEqual(stale["error_summary"], ["runtime_changed_before_run_completed"])

    def test_manifest_mismatch_fails_before_candidate_persistence(self) -> None:
        captured = _captured()
        broken = dict(captured.quant_selection)
        broken["selection_manifest_hash"] = "0" * 64
        service, _ = self._service(CapturedQuantRun(
            data_as_of=captured.data_as_of,
            quant_selection=broken,
            ai_contexts=captured.ai_contexts,
            input_snapshots=captured.input_snapshots,
        ))
        result = service.execute_run(service.create_run()["run_id"])
        self.assertEqual(result["status"], "failed")
        self.assertIn("manifest hash mismatch", result["error_summary"][0])


if __name__ == "__main__":
    unittest.main()
