"""Run orchestration, immutable persistence and cache identity for quant selection."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timezone
import json
from typing import Any, Callable, Mapping, Protocol, Sequence
from uuid import uuid4

from .db import get_connection
from .quant_stock_selector import SCORE_VERSION
from .quant_stock_selector_ai import (
    AI_PROMPT_VERSION,
    MODEL_POLICY_VERSION,
    AICandidateContext,
    QuantAISelectionService,
    build_ai_input_snapshot,
)
from .quant_stock_selector_hashing import canonical_json, canonical_sha256
from .quant_stock_selector_metadata import normalize_symbol
from .quant_stock_selector_snapshots import MarketBarSnapshotStore
from .quant_stock_selector_universe import FILTER_VERSION
from .runtime import get_runtime_metadata
from .stock_picker_ai_snapshots import sanitize_error


UNIVERSE_VERSION = "quant-selector-universe-v1.1"
RUN_SCHEMA_VERSION = "quant-selector-run-v1"
INPUT_SNAPSHOT_SCHEMA_VERSION = "quant-selector-input-snapshot-v1"
TERMINAL_STATUSES = frozenset({"completed", "partial", "failed", "cancelled"})
RUN_STATUSES = frozenset({
    "queued",
    "loading_universe",
    "scoring_quant",
    "analyzing_ai",
    *TERMINAL_STATUSES,
})
_TRANSITIONS = {
    "queued": {"loading_universe", "cancelled", "failed"},
    "loading_universe": {"scoring_quant", "partial", "failed", "cancelled"},
    "scoring_quant": {"analyzing_ai", "completed", "partial", "failed", "cancelled"},
    "analyzing_ai": {"completed", "partial", "failed", "cancelled"},
}


class QuantSelectionRunError(RuntimeError):
    pass


class QuantSelectionRunNotFound(QuantSelectionRunError):
    pass


class QuantSelectionRunConflict(QuantSelectionRunError):
    pass


def _utc(value: datetime | date | str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, time.min, tzinfo=timezone.utc)
    else:
        text = str(value).strip()
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            parsed = datetime.combine(date.fromisoformat(text), time.min)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    return _utc(value).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _json(value: Any) -> str:
    return canonical_json(value)


def _display_score(value: Any) -> float | None:
    if value is None:
        return None
    return round(float(value), 6)


def _candidate_display_payload(value: Mapping[str, Any]) -> dict[str, Any]:
    payload = json.loads(_json(value))
    quant_score = payload.get("quant_score")
    if isinstance(quant_score, dict):
        for key in (
            "total",
            "liquidity",
            "trend",
            "relative_strength",
            "momentum",
            "risk",
        ):
            if key in quant_score:
                quant_score[key] = _display_score(quant_score[key])
    result = payload.get("result")
    if isinstance(result, dict):
        for key in ("quant_score", "ai_score", "final_score"):
            if key in result:
                result[key] = _display_score(result[key])
    return payload


def _row_dict(cursor: Any, row: Sequence[Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {item[0]: value for item, value in zip(cursor.description, row)}


@dataclass(frozen=True)
class CapturedInputSnapshot:
    snapshot_kind: str
    source: str
    schema_version: str
    captured_at: datetime
    data_as_of: datetime
    payload: Mapping[str, Any] | Sequence[Any]
    symbol: str | None = None

    def normalized(self) -> dict[str, Any]:
        kind = str(self.snapshot_kind).strip()
        source = str(self.source).strip()
        schema = str(self.schema_version).strip()
        if not kind or not source or not schema:
            raise QuantSelectionRunError(
                "snapshot_kind, source and schema_version are required"
            )
        payload = json.loads(_json(self.payload))
        payload_hash = canonical_sha256(payload)
        symbol = normalize_symbol(self.symbol) if self.symbol else None
        snapshot_identity = canonical_sha256({
            "payload_hash": payload_hash,
            "schema_version": schema,
            "snapshot_kind": kind,
            "source": source,
            "symbol": symbol,
        })
        return {
            "snapshot_id": f"qis_{snapshot_identity}",
            "snapshot_kind": kind,
            "symbol": symbol,
            "source": source,
            "schema_version": schema,
            "captured_at": _timestamp(_utc(self.captured_at)),
            "data_as_of": _timestamp(_utc(self.data_as_of)),
            "payload_hash": payload_hash,
            "payload": payload,
            "reference": {
                "payload_hash": payload_hash,
                "schema_version": schema,
                "source": source,
            },
        }


@dataclass(frozen=True)
class CapturedQuantRun:
    data_as_of: date
    quant_selection: Mapping[str, Any]
    ai_contexts: Mapping[str, AICandidateContext]
    input_snapshots: Sequence[CapturedInputSnapshot]
    required_inputs_complete: bool = True
    errors: Sequence[str] = ()


class QuantRunInputProvider(Protocol):
    def capture(self) -> CapturedQuantRun:
        ...


class QuantSelectionRunRepository:
    def __init__(
        self,
        *,
        connection_factory: Callable[..., Any] = get_connection,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.connection_factory = connection_factory
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def create_run(
        self,
        run_id: str,
        *,
        runtime_id: str,
        force_refresh: bool,
        model_alias: str,
    ) -> dict[str, Any]:
        now = _utc(self.clock())
        with self.connection_factory() as connection:
            connection.execute(
                """
                INSERT INTO quant_selection_runs (
                    run_id, runtime_id, status, force_refresh, created_at,
                    universe_version, filter_version, score_version,
                    prompt_version, model_policy_version, model_alias
                ) VALUES (?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    run_id,
                    runtime_id,
                    bool(force_refresh),
                    now,
                    UNIVERSE_VERSION,
                    FILTER_VERSION,
                    SCORE_VERSION,
                    AI_PROMPT_VERSION,
                    MODEL_POLICY_VERSION,
                    model_alias,
                ],
            )
        return self.get_run(run_id)

    def transition(
        self,
        run_id: str,
        status: str,
        **fields: Any,
    ) -> dict[str, Any]:
        if status not in RUN_STATUSES:
            raise QuantSelectionRunConflict(f"invalid run status: {status}")
        current = self.get_run(run_id)
        if current["status"] != status and status not in _TRANSITIONS.get(
            current["status"], set()
        ):
            raise QuantSelectionRunConflict(
                f"invalid transition: {current['status']} -> {status}"
            )
        allowed = {
            "started_at",
            "completed_at",
            "data_as_of",
            "resolved_model_id",
            "candidate_count",
            "ai_planned_count",
            "ai_completed_count",
            "final_count",
            "error_summary",
            "quant_manifest",
            "run_manifest",
            "quant_input_hash",
            "run_input_hash",
            "cache_key",
            "reused_from_run_id",
        }
        unexpected = set(fields) - allowed
        if unexpected:
            raise QuantSelectionRunConflict(
                f"unsupported run fields: {sorted(unexpected)}"
            )
        values = {"status": status, **fields}
        if status in TERMINAL_STATUSES and "completed_at" not in values:
            values["completed_at"] = _utc(self.clock())
        encoded = {
            key: _json(value) if key in {"error_summary", "quant_manifest", "run_manifest"}
            else value
            for key, value in values.items()
        }
        assignments = ", ".join(f"{key} = ?" for key in encoded)
        with self.connection_factory() as connection:
            connection.execute(
                f"UPDATE quant_selection_runs SET {assignments} WHERE run_id = ?",
                [*encoded.values(), run_id],
            )
        return self.get_run(run_id)

    def save_input_snapshots(
        self,
        run_id: str,
        snapshots: Sequence[Mapping[str, Any]],
    ) -> None:
        with self.connection_factory() as connection:
            connection.execute("BEGIN TRANSACTION")
            try:
                for snapshot in snapshots:
                    connection.execute(
                        """
                        INSERT INTO quant_selection_input_snapshots (
                            run_id, snapshot_id, snapshot_kind, symbol, source,
                            schema_version, captured_at, data_as_of,
                            payload_hash, payload
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        [
                            run_id,
                            snapshot["snapshot_id"],
                            snapshot["snapshot_kind"],
                            snapshot["symbol"],
                            snapshot["source"],
                            snapshot["schema_version"],
                            _utc(snapshot["captured_at"]),
                            _utc(snapshot["data_as_of"]),
                            snapshot["payload_hash"],
                            _json(snapshot["payload"]),
                        ],
                    )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def save_candidates(
        self,
        run_id: str,
        quant_selection: Mapping[str, Any],
    ) -> None:
        ranks = {
            normalize_symbol(item["symbol"]): int(item["rank"])
            for item in quant_selection.get("quant_ranking", [])
        }
        with self.connection_factory() as connection:
            connection.execute("BEGIN TRANSACTION")
            try:
                for raw in quant_selection.get("candidates", []):
                    candidate = _candidate_display_payload(raw)
                    symbol = normalize_symbol(candidate["symbol"])
                    score = candidate.get("quant_score") or {}
                    connection.execute(
                        """
                        INSERT INTO quant_selection_candidates (
                            run_id, symbol, selection_status,
                            candidate_quant_input_hash, payload_hash, payload,
                            quant_score, quant_rank, selected_for_ai
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        [
                            run_id,
                            symbol,
                            candidate["selection_status"],
                            candidate["candidate_quant_input_hash"],
                            canonical_sha256(candidate),
                            _json(candidate),
                            _display_score(score.get("total")),
                            ranks.get(symbol),
                            bool(candidate.get("selected_for_ai")),
                        ],
                    )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def save_ai_result(
        self,
        run_id: str,
        ai_result: Mapping[str, Any],
        prepared_inputs: Mapping[str, Mapping[str, Any]],
    ) -> None:
        snapshots = {
            normalize_symbol(item["symbol"]): item
            for item in ai_result.get("snapshots", [])
        }
        provisional = {
            normalize_symbol(item["symbol"]): item
            for item in ai_result.get("provisional_results", [])
        }
        final_ranks = {
            normalize_symbol(item["symbol"]): rank
            for rank, item in enumerate(ai_result.get("final_results", []), 1)
        }
        completed_run = ai_result.get("status") == "completed"
        with self.connection_factory() as connection:
            for symbol in sorted(prepared_inputs):
                snapshot = snapshots.get(symbol, {})
                input_snapshot = snapshot.get("input_snapshot") or prepared_inputs[symbol]
                ai_input_hash = snapshot.get("ai_input_hash") or prepared_inputs[symbol]["ai_input_hash"]
                connection.execute(
                    """
                    INSERT INTO quant_selection_ai_snapshots (
                        run_id, symbol, request_status, attempts,
                        ai_input_hash, input_snapshot, raw_output,
                        raw_attempt_outputs, parsed_output, model_alias,
                        resolved_model_id, prompt_version,
                        model_policy_version, error
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        run_id,
                        symbol,
                        snapshot.get("status", "failed"),
                        int(snapshot.get("attempts", 0)),
                        ai_input_hash,
                        _json(input_snapshot),
                        snapshot.get("raw_output"),
                        _json(snapshot.get("raw_attempt_outputs", [])),
                        _json(snapshot["parsed_output"])
                        if snapshot.get("parsed_output") is not None else None,
                        ai_result["model_alias"],
                        snapshot.get("resolved_model_id"),
                        ai_result["prompt_version"],
                        ai_result["model_policy_version"],
                        snapshot.get("error"),
                    ],
                )
                row = connection.execute(
                    "SELECT payload FROM quant_selection_candidates WHERE run_id = ? AND symbol = ?",
                    [run_id, symbol],
                ).fetchone()
                if row is None:
                    continue
                payload = json.loads(row[0])
                item = provisional.get(symbol)
                payload["ai"] = {
                    "request_status": snapshot.get("status", "failed"),
                    "attempts": int(snapshot.get("attempts", 0)),
                    "ai_input_hash": ai_input_hash,
                    "decision": snapshot.get("parsed_output"),
                    "error": snapshot.get("error"),
                }
                if item is not None:
                    payload["result"] = json.loads(_json(item))
                payload = _candidate_display_payload(payload)
                connection.execute(
                    """
                    UPDATE quant_selection_candidates
                    SET payload = ?, payload_hash = ?, ai_score = ?,
                        final_score = ?, final_rank = ?, final_selected = ?
                    WHERE run_id = ? AND symbol = ?
                    """,
                    [
                        _json(payload),
                        canonical_sha256(payload),
                        _display_score(item.get("ai_score")) if item else None,
                        _display_score(item.get("final_score")) if item else None,
                        final_ranks.get(symbol) if completed_run else None,
                        completed_run and symbol in final_ranks,
                        run_id,
                        symbol,
                    ],
                )

    def copy_cached_outputs(
        self,
        source_run_id: str,
        target_run_id: str,
    ) -> None:
        with self.connection_factory() as connection:
            source_rows = connection.execute(
                """
                SELECT symbol, payload, ai_score, final_score, final_rank,
                       final_selected
                FROM quant_selection_candidates WHERE run_id = ?
                """,
                [source_run_id],
            ).fetchall()
            for symbol, source_payload_text, ai_score, final_score, final_rank, final_selected in source_rows:
                target = connection.execute(
                    "SELECT payload FROM quant_selection_candidates WHERE run_id = ? AND symbol = ?",
                    [target_run_id, symbol],
                ).fetchone()
                if target is None:
                    raise QuantSelectionRunError("cached candidate set mismatch")
                source_payload = json.loads(source_payload_text)
                target_payload = json.loads(target[0])
                for key in ("ai", "result"):
                    if key in source_payload:
                        target_payload[key] = source_payload[key]
                connection.execute(
                    """
                    UPDATE quant_selection_candidates
                    SET payload = ?, payload_hash = ?, ai_score = ?,
                        final_score = ?, final_rank = ?, final_selected = ?
                    WHERE run_id = ? AND symbol = ?
                    """,
                    [
                        _json(target_payload),
                        canonical_sha256(target_payload),
                        ai_score,
                        final_score,
                        final_rank,
                        final_selected,
                        target_run_id,
                        symbol,
                    ],
                )
            connection.execute(
                """
                INSERT INTO quant_selection_ai_snapshots (
                    run_id, symbol, request_status, attempts, ai_input_hash,
                    input_snapshot, raw_output, raw_attempt_outputs,
                    parsed_output, model_alias, resolved_model_id,
                    prompt_version, model_policy_version, error,
                    reused_from_run_id
                )
                SELECT ?, symbol, 'reused', 0, ai_input_hash,
                       input_snapshot, raw_output, raw_attempt_outputs,
                       parsed_output, model_alias, resolved_model_id,
                       prompt_version, model_policy_version, error, ?
                FROM quant_selection_ai_snapshots WHERE run_id = ?
                """,
                [target_run_id, source_run_id, source_run_id],
            )

    def get_run(self, run_id: str) -> dict[str, Any]:
        with self.connection_factory() as connection:
            cursor = connection.execute(
                "SELECT * FROM quant_selection_runs WHERE run_id = ?",
                [run_id],
            )
            result = _row_dict(cursor, cursor.fetchone())
        if result is None:
            raise QuantSelectionRunNotFound(run_id)
        for key in ("error_summary", "quant_manifest", "run_manifest"):
            if result.get(key) is not None:
                result[key] = json.loads(result[key])
        for key in ("created_at", "started_at", "completed_at"):
            result[key] = _timestamp(result.get(key))
        if result.get("data_as_of") is not None:
            result["data_as_of"] = result["data_as_of"].isoformat()
        return result

    def list_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        with self.connection_factory() as connection:
            rows = connection.execute(
                "SELECT run_id FROM quant_selection_runs ORDER BY created_at DESC, run_id DESC LIMIT ?",
                [limit],
            ).fetchall()
        return [self.get_run(row[0]) for row in rows]

    def latest_completed(self) -> dict[str, Any] | None:
        with self.connection_factory() as connection:
            row = connection.execute(
                """
                SELECT run_id FROM quant_selection_runs
                WHERE status = 'completed'
                ORDER BY completed_at DESC, run_id DESC LIMIT 1
                """
            ).fetchone()
        return self.get_run(row[0]) if row else None

    def get_results(self, run_id: str) -> dict[str, Any]:
        run = self.get_run(run_id)
        with self.connection_factory() as connection:
            rows = connection.execute(
                """
                SELECT payload FROM quant_selection_candidates
                WHERE run_id = ?
                ORDER BY final_selected DESC, final_rank ASC NULLS LAST,
                         quant_rank ASC NULLS LAST, symbol ASC
                """,
                [run_id],
            ).fetchall()
        candidates = [json.loads(row[0]) for row in rows]
        final_results = [
            item["result"] for item in candidates
            if run["status"] == "completed" and item.get("result")
            and item["result"].get("eligible")
        ]
        return {"run": run, "results": final_results, "candidates": candidates}

    def find_valid_cache(self, cache_key: str, *, exclude_run_id: str) -> dict[str, Any] | None:
        with self.connection_factory() as connection:
            rows = connection.execute(
                """
                SELECT run_id FROM quant_selection_runs
                WHERE cache_key = ? AND status = 'completed' AND run_id <> ?
                ORDER BY completed_at DESC, run_id DESC
                """,
                [cache_key, exclude_run_id],
            ).fetchall()
        for (run_id,) in rows:
            if self.verify_integrity(run_id):
                return self.get_run(run_id)
        return None

    def verify_integrity(self, run_id: str) -> bool:
        try:
            run = self.get_run(run_id)
            if run["status"] != "completed":
                return False
            if canonical_sha256(run["quant_manifest"]) != run["quant_input_hash"]:
                return False
            if canonical_sha256(run["run_manifest"]) != run["run_input_hash"]:
                return False
            expected_cache = canonical_sha256({
                "run_input_hash": run["run_input_hash"],
                "universe_version": run["universe_version"],
                "filter_version": run["filter_version"],
                "score_version": run["score_version"],
                "prompt_version": run["prompt_version"],
                "model_policy_version": run["model_policy_version"],
                "resolved_model_id": run["resolved_model_id"],
            })
            if expected_cache != run["cache_key"]:
                return False
            with self.connection_factory() as connection:
                inputs = connection.execute(
                    """
                    SELECT snapshot_kind, source, schema_version,
                           payload_hash, payload
                    FROM quant_selection_input_snapshots WHERE run_id = ?
                    """,
                    [run_id],
                ).fetchall()
                candidates = connection.execute(
                    """
                    SELECT payload_hash, payload, final_selected
                    FROM quant_selection_candidates WHERE run_id = ?
                    """,
                    [run_id],
                ).fetchall()
                ai_rows = connection.execute(
                    """
                    SELECT ai_input_hash, input_snapshot, request_status
                    FROM quant_selection_ai_snapshots WHERE run_id = ?
                    """,
                    [run_id],
                ).fetchall()
            actual_input_references = sorted(
                (
                    {
                        "source": source,
                        "schema_version": schema,
                        "payload_hash": digest,
                    }
                    for _kind, source, schema, digest, _payload in inputs
                ),
                key=lambda item: (
                    item["source"], item["schema_version"], item["payload_hash"]
                ),
            )
            if actual_input_references != run["quant_manifest"].get("input_snapshots"):
                return False
            if any(
                canonical_sha256(json.loads(payload)) != digest
                for _kind, _source, _schema, digest, payload in inputs
            ):
                return False
            market_store = MarketBarSnapshotStore(
                connection_factory=self.connection_factory
            )
            for kind, _source, _schema, _digest, payload in inputs:
                if kind != "market_bar_reference":
                    continue
                reference = json.loads(payload)
                detail = market_store.get(str(reference.get("snapshot_id") or ""))
                if detail["reference"] != reference.get("reference"):
                    return False
            if len(candidates) != run["candidate_count"]:
                return False
            if sum(bool(selected) for _digest, _payload, selected in candidates) != run["final_count"]:
                return False
            if any(
                canonical_sha256(json.loads(payload)) != digest
                for digest, payload, _selected in candidates
            ):
                return False
            if len(ai_rows) != run["ai_planned_count"]:
                return False
            if sum(status in {"completed", "reused"} for _digest, _payload, status in ai_rows) != run["ai_completed_count"]:
                return False
            for digest, payload, _status in ai_rows:
                value = json.loads(payload)
                embedded = value.pop("ai_input_hash", None)
                if canonical_sha256(value) != digest or embedded not in {None, digest}:
                    return False
            return True
        except Exception:
            return False


class QuantSelectionService:
    def __init__(
        self,
        input_provider: QuantRunInputProvider,
        ai_service: QuantAISelectionService,
        *,
        repository: QuantSelectionRunRepository | None = None,
        runtime_id: str | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.input_provider = input_provider
        self.ai_service = ai_service
        self.repository = repository or QuantSelectionRunRepository()
        self.runtime_id = runtime_id or get_runtime_metadata()["runtime_id"]
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def create_run(self, *, force_refresh: bool = False) -> dict[str, Any]:
        run_id = f"qsr_{uuid4().hex}"
        return self.repository.create_run(
            run_id,
            runtime_id=self.runtime_id,
            force_refresh=force_refresh,
            model_alias=self.ai_service.provider.model_alias,
        )

    def _resolve_model_id(self) -> str:
        resolver = getattr(self.ai_service.provider, "resolve_model_id", None)
        if not callable(resolver):
            raise QuantSelectionRunError(
                "AI provider cannot resolve an immutable model ID"
            )
        resolved = str(resolver() or "").strip()
        if not resolved:
            raise QuantSelectionRunError("resolved model ID is empty")
        return resolved

    def execute_run(self, run_id: str) -> dict[str, Any]:
        run = self.repository.get_run(run_id)
        if run["status"] != "queued":
            raise QuantSelectionRunConflict("only queued runs can be executed")
        quant_saved = False
        try:
            self.repository.transition(
                run_id,
                "loading_universe",
                started_at=_utc(self.clock()),
            )
            captured = self.input_provider.capture()
            snapshots = [item.normalized() for item in captured.input_snapshots]
            self.repository.save_input_snapshots(run_id, snapshots)
            self.repository.transition(
                run_id,
                "scoring_quant",
                data_as_of=captured.data_as_of,
            )
            quant_selection = json.loads(_json(captured.quant_selection))
            selection_manifest = quant_selection.get("selection_manifest")
            if not isinstance(selection_manifest, dict):
                raise QuantSelectionRunError("selection manifest is required")
            if canonical_sha256(selection_manifest) != quant_selection.get(
                "selection_manifest_hash"
            ):
                raise QuantSelectionRunError("selection manifest hash mismatch")
            if [normalize_symbol(value) for value in selection_manifest.get("top_symbols", [])] != [
                normalize_symbol(value)
                for value in quant_selection.get("ai_candidate_symbols", [])
            ]:
                raise QuantSelectionRunError("AI candidate manifest mismatch")
            self.repository.save_candidates(run_id, quant_selection)
            quant_saved = True

            candidate_payloads = sorted(
                quant_selection.get("candidates", []),
                key=lambda item: normalize_symbol(item["symbol"]),
            )
            quant_manifest = {
                "schema_version": RUN_SCHEMA_VERSION,
                "data_as_of": captured.data_as_of,
                "official_close": quant_selection.get("official_close"),
                "filter_version": quant_selection.get("filter_version"),
                "score_version": SCORE_VERSION,
                "selection_manifest": quant_selection.get("selection_manifest"),
                "selection_manifest_hash": quant_selection.get("selection_manifest_hash"),
                "candidates": candidate_payloads,
                "input_snapshots": sorted(
                    (item["reference"] for item in snapshots),
                    key=lambda item: (item["source"], item["schema_version"], item["payload_hash"]),
                ),
            }
            quant_input_hash = canonical_sha256(quant_manifest)
            planned = [normalize_symbol(value) for value in quant_selection.get("ai_candidate_symbols", [])]
            if len(planned) != len(set(planned)):
                raise QuantSelectionRunError("AI candidate symbols must be unique")
            normalized_contexts = {
                normalize_symbol(symbol): context
                for symbol, context in captured.ai_contexts.items()
            }
            prepared_inputs = {}
            preflight_errors = []
            for symbol in planned:
                context = normalized_contexts.get(symbol)
                try:
                    if context is None:
                        raise QuantSelectionRunError("ai_context_missing")
                    prepared_inputs[symbol] = build_ai_input_snapshot(
                        context,
                        model_alias=self.ai_service.provider.model_alias,
                        temperature=self.ai_service.provider.temperature,
                    )
                except Exception as exc:
                    error = sanitize_error(exc)
                    preflight_errors.append(f"{symbol}:{error}")
                    placeholder = {
                        "input_schema_version": INPUT_SNAPSHOT_SCHEMA_VERSION,
                        "symbol": symbol,
                        "preflight_error": error,
                    }
                    prepared_inputs[symbol] = {
                        **placeholder,
                        "ai_input_hash": canonical_sha256(placeholder),
                    }
            run_manifest = {
                "schema_version": RUN_SCHEMA_VERSION,
                "quant_input_hash": quant_input_hash,
                "selection_manifest_hash": quant_selection.get("selection_manifest_hash"),
                "ai_inputs": [
                    {"symbol": symbol, "ai_input_hash": prepared_inputs[symbol]["ai_input_hash"]}
                    for symbol in sorted(prepared_inputs)
                ],
            }
            run_input_hash = canonical_sha256(run_manifest)
            base_fields = {
                "candidate_count": len(candidate_payloads),
                "ai_planned_count": len(planned),
                "quant_manifest": quant_manifest,
                "run_manifest": run_manifest,
                "quant_input_hash": quant_input_hash,
                "run_input_hash": run_input_hash,
            }
            incomplete = (
                not captured.required_inputs_complete
                or quant_selection.get("status") != "completed"
                or bool(preflight_errors)
            )
            if incomplete:
                errors = [*captured.errors, *preflight_errors]
                if quant_selection.get("status") != "completed":
                    errors.append("quant_candidate_boundary_unproven")
                return self.repository.transition(
                    run_id,
                    "partial",
                    **base_fields,
                    error_summary=errors,
                )

            resolved_model_id = self._resolve_model_id()
            cache_key = canonical_sha256({
                "run_input_hash": run_input_hash,
                "universe_version": UNIVERSE_VERSION,
                "filter_version": FILTER_VERSION,
                "score_version": SCORE_VERSION,
                "prompt_version": AI_PROMPT_VERSION,
                "model_policy_version": MODEL_POLICY_VERSION,
                "resolved_model_id": resolved_model_id,
            })
            hash_fields = {
                **base_fields,
                "resolved_model_id": resolved_model_id,
                "cache_key": cache_key,
            }
            if not run["force_refresh"]:
                cached = self.repository.find_valid_cache(
                    cache_key,
                    exclude_run_id=run_id,
                )
                if cached is not None:
                    self.repository.copy_cached_outputs(cached["run_id"], run_id)
                    return self.repository.transition(
                        run_id,
                        "completed",
                        **hash_fields,
                        ai_completed_count=cached["ai_completed_count"],
                        final_count=cached["final_count"],
                        error_summary=[],
                        reused_from_run_id=cached["run_id"],
                    )

            if not planned:
                return self.repository.transition(
                    run_id,
                    "completed",
                    **hash_fields,
                    ai_completed_count=0,
                    final_count=0,
                    error_summary=[],
                )

            self.repository.transition(run_id, "analyzing_ai", **hash_fields)
            ai_result = self.ai_service.analyze(quant_selection, normalized_contexts)
            self.repository.save_ai_result(run_id, ai_result, prepared_inputs)
            errors = []
            if ai_result.get("resolved_model_id") != resolved_model_id:
                errors.append("resolved_model_id_changed_during_run")
            if ai_result.get("status") != "completed":
                errors.append(str(ai_result.get("error") or "planned_ai_call_incomplete"))
            status = "completed" if not errors else "partial"
            final_count = len(ai_result.get("final_results", [])) if status == "completed" else 0
            return self.repository.transition(
                run_id,
                status,
                **hash_fields,
                ai_completed_count=int(ai_result.get("completed_count", 0)),
                final_count=final_count,
                error_summary=errors,
            )
        except Exception as exc:
            error = sanitize_error(exc)
            current = self.repository.get_run(run_id)
            if current["status"] in TERMINAL_STATUSES:
                return current
            terminal = "partial" if quant_saved else "failed"
            return self.repository.transition(
                run_id,
                terminal,
                error_summary=[error],
            )

    def cancel_run(self, run_id: str) -> dict[str, Any]:
        run = self.repository.get_run(run_id)
        if run["status"] in TERMINAL_STATUSES:
            return run
        return self.repository.transition(
            run_id,
            "cancelled",
            error_summary=["run_cancelled"],
        )

    def terminate_stale_run(self, run_id: str) -> dict[str, Any]:
        run = self.repository.get_run(run_id)
        if run["status"] in TERMINAL_STATUSES or run["runtime_id"] == self.runtime_id:
            return run
        return self.repository.transition(
            run_id,
            "failed",
            error_summary=["runtime_changed_before_run_completed"],
        )
