"""Paired, non-production Flash/Pro evaluation on frozen AI inputs."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import json
import math
from statistics import mean
import time
from typing import Any, Callable, Mapping, Sequence
from uuid import uuid4

from .db import get_connection
from .quant_stock_selector_ai import (
    AICompletionProvider,
    parse_ai_decision,
)
from .quant_stock_selector_hashing import canonical_json, canonical_sha256
from .quant_stock_selector_metadata import normalize_symbol
from .stock_picker_ai_snapshots import sanitize_error


SHADOW_EVALUATION_VERSION = "quant-selector-flash-pro-shadow-v1"
FLASH_MODEL_ALIAS = "deepseek-v4-flash"
PRO_MODEL_ALIAS = "deepseek-v4-pro"


class QuantShadowEvaluationError(ValueError):
    pass


def _utc_timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")


def _percentile(values: Sequence[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(percentile * len(ordered)) - 1)]


def _safe_error(provider: AICompletionProvider, error: Exception) -> str:
    callback = getattr(provider, "safe_error", None)
    if callable(callback):
        return str(callback(error))
    return sanitize_error(error)


class QuantShadowEvaluationRepository:
    def __init__(
        self,
        *,
        connection_factory: Callable[..., Any] = get_connection,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.connection_factory = connection_factory
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def create(
        self,
        source_run_id: str,
        *,
        flash_model_alias: str,
        pro_model_alias: str,
    ) -> dict[str, Any]:
        shadow_id = f"qse_{uuid4().hex}"
        with self.connection_factory() as connection:
            connection.execute(
                """
                INSERT INTO quant_selection_shadow_evaluations (
                    shadow_id, source_run_id, status, created_at,
                    flash_model_alias, pro_model_alias
                ) VALUES (?, ?, 'running', ?, ?, ?)
                """,
                [
                    shadow_id,
                    source_run_id,
                    self.clock(),
                    flash_model_alias,
                    pro_model_alias,
                ],
            )
        return self.get(shadow_id)

    def save_observations(
        self,
        shadow_id: str,
        observations: Sequence[Mapping[str, Any]],
    ) -> None:
        with self.connection_factory() as connection:
            connection.execute("BEGIN TRANSACTION")
            try:
                for item in observations:
                    connection.execute(
                        """
                        INSERT INTO quant_selection_shadow_observations (
                            shadow_id, symbol, model_role, model_alias,
                            resolved_model_id, production_ai_input_hash,
                            paired_input_hash, request_status, attempts,
                            latency_ms, input_tokens, output_tokens,
                            raw_attempt_outputs, parsed_output, error
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        [
                            shadow_id,
                            item["symbol"],
                            item["model_role"],
                            item["model_alias"],
                            item.get("resolved_model_id"),
                            item["production_ai_input_hash"],
                            item["paired_input_hash"],
                            item["request_status"],
                            item["attempts"],
                            item["latency_ms"],
                            item.get("input_tokens"),
                            item.get("output_tokens"),
                            canonical_json(item["raw_attempt_outputs"]),
                            (
                                canonical_json(item["parsed_output"])
                                if item.get("parsed_output") is not None
                                else None
                            ),
                            item.get("error"),
                        ],
                    )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def finish(
        self,
        shadow_id: str,
        *,
        status: str,
        flash_resolved_model_id: str | None,
        pro_resolved_model_id: str | None,
        paired_input_count: int,
        flash_completed_count: int,
        pro_completed_count: int,
        result: Mapping[str, Any] | None,
        errors: Sequence[str],
    ) -> dict[str, Any]:
        if status not in {"completed", "partial", "failed"}:
            raise QuantShadowEvaluationError("invalid terminal shadow status")
        with self.connection_factory() as connection:
            connection.execute(
                """
                UPDATE quant_selection_shadow_evaluations
                SET status = ?, completed_at = ?, flash_resolved_model_id = ?,
                    pro_resolved_model_id = ?, paired_input_count = ?,
                    flash_completed_count = ?, pro_completed_count = ?,
                    result = ?, error_summary = ?
                WHERE shadow_id = ?
                """,
                [
                    status,
                    self.clock(),
                    flash_resolved_model_id,
                    pro_resolved_model_id,
                    paired_input_count,
                    flash_completed_count,
                    pro_completed_count,
                    canonical_json(result) if result is not None else None,
                    canonical_json(list(errors)),
                    shadow_id,
                ],
            )
        return self.get(shadow_id)

    def get(self, shadow_id: str) -> dict[str, Any]:
        with self.connection_factory() as connection:
            cursor = connection.execute(
                """
                SELECT * FROM quant_selection_shadow_evaluations
                WHERE shadow_id = ?
                """,
                [shadow_id],
            )
            row = cursor.fetchone()
            if row is None:
                raise QuantShadowEvaluationError("shadow evaluation not found")
            item = dict(zip((value[0] for value in cursor.description), row))
        for key in ("created_at", "completed_at"):
            if item.get(key) is not None:
                item[key] = _utc_timestamp(item[key])
        item["result"] = json.loads(item["result"]) if item.get("result") else None
        item["error_summary"] = json.loads(item["error_summary"])
        return item

    def list(self, *, limit: int = 20) -> list[dict[str, Any]]:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        with self.connection_factory() as connection:
            rows = connection.execute(
                """
                SELECT shadow_id FROM quant_selection_shadow_evaluations
                ORDER BY created_at DESC, shadow_id DESC LIMIT ?
                """,
                [limit],
            ).fetchall()
        return [self.get(row[0]) for row in rows]

    def load_source_inputs(self, source_run_id: str) -> list[dict[str, Any]]:
        with self.connection_factory() as connection:
            run = connection.execute(
                """
                SELECT status, prompt_version FROM quant_selection_runs
                WHERE run_id = ?
                """,
                [source_run_id],
            ).fetchone()
            if run is None:
                raise QuantShadowEvaluationError("source run not found")
            if run[0] != "completed":
                raise QuantShadowEvaluationError("source run must be completed")
            rows = connection.execute(
                """
                SELECT symbol, ai_input_hash, input_snapshot
                FROM quant_selection_ai_snapshots
                WHERE run_id = ? AND request_status IN ('completed', 'reused')
                ORDER BY symbol
                """,
                [source_run_id],
            ).fetchall()
        result = []
        for symbol, digest, raw_payload in rows:
            try:
                payload = json.loads(raw_payload)
            except (TypeError, json.JSONDecodeError) as exc:
                raise QuantShadowEvaluationError(
                    f"invalid frozen AI input for {symbol}"
                ) from exc
            if not isinstance(payload, dict):
                raise QuantShadowEvaluationError(
                    f"invalid frozen AI input for {symbol}"
                )
            embedded = payload.pop("ai_input_hash", None)
            if canonical_sha256(payload) != digest or embedded not in {None, digest}:
                raise QuantShadowEvaluationError(
                    f"frozen AI input hash mismatch for {symbol}"
                )
            if payload.get("prompt_version") != run[1]:
                raise QuantShadowEvaluationError(
                    f"frozen prompt version mismatch for {symbol}"
                )
            for field in (
                "system_prompt",
                "user_prompt",
                "response_schema",
                "temperature",
            ):
                if field not in payload:
                    raise QuantShadowEvaluationError(
                        f"frozen AI input missing {field} for {symbol}"
                    )
            paired_input = {
                key: payload.get(key)
                for key in (
                    "input_schema_version",
                    "prompt_version",
                    "system_prompt",
                    "user_prompt",
                    "response_schema",
                    "temperature",
                    "facts",
                )
            }
            result.append({
                "symbol": normalize_symbol(symbol),
                "production_ai_input_hash": digest,
                "paired_input_hash": canonical_sha256(paired_input),
                "input": payload,
            })
        return result

    def get_observations(self, shadow_id: str) -> list[dict[str, Any]]:
        with self.connection_factory() as connection:
            rows = connection.execute(
                """
                SELECT symbol, model_role, production_ai_input_hash,
                       paired_input_hash, request_status, parsed_output
                FROM quant_selection_shadow_observations
                WHERE shadow_id = ? ORDER BY symbol, model_role
                """,
                [shadow_id],
            ).fetchall()
        return [
            {
                "symbol": symbol,
                "model_role": role,
                "production_ai_input_hash": production_hash,
                "paired_input_hash": paired_hash,
                "request_status": status,
                "parsed_output": json.loads(parsed) if parsed else None,
            }
            for symbol, role, production_hash, paired_hash, status, parsed in rows
        ]


class QuantShadowEvaluationService:
    def __init__(
        self,
        flash_provider: AICompletionProvider,
        pro_provider: AICompletionProvider,
        *,
        repository: QuantShadowEvaluationRepository | None = None,
        max_attempts: int = 2,
        max_workers: int = 4,
        monotonic: Callable[[], float] | None = None,
        pro_latency_budget_ms: float | None = None,
        pro_cost_budget_usd: float | None = None,
        pro_input_cost_per_million_usd: float | None = None,
        pro_output_cost_per_million_usd: float | None = None,
    ) -> None:
        if flash_provider.model_alias != FLASH_MODEL_ALIAS:
            raise QuantShadowEvaluationError("flash provider alias is invalid")
        if pro_provider.model_alias != PRO_MODEL_ALIAS:
            raise QuantShadowEvaluationError("pro provider alias is invalid")
        if max_attempts < 1 or max_workers < 1:
            raise QuantShadowEvaluationError("attempt and worker limits must be positive")
        self.flash_provider = flash_provider
        self.pro_provider = pro_provider
        self.repository = repository or QuantShadowEvaluationRepository()
        self.max_attempts = max_attempts
        self.max_workers = max_workers
        self.monotonic = monotonic or time.monotonic
        self.pro_latency_budget_ms = pro_latency_budget_ms
        self.pro_cost_budget_usd = pro_cost_budget_usd
        self.pro_input_cost_per_million_usd = pro_input_cost_per_million_usd
        self.pro_output_cost_per_million_usd = pro_output_cost_per_million_usd

    def start(self, source_run_id: str) -> dict[str, Any]:
        self.repository.load_source_inputs(source_run_id)
        return self.repository.create(
            source_run_id,
            flash_model_alias=self.flash_provider.model_alias,
            pro_model_alias=self.pro_provider.model_alias,
        )

    def run(self, source_run_id: str) -> dict[str, Any]:
        evaluation = self.start(source_run_id)
        return self.execute(evaluation["shadow_id"])

    def execute(self, shadow_id: str) -> dict[str, Any]:
        evaluation = self.repository.get(shadow_id)
        if evaluation["status"] != "running":
            raise QuantShadowEvaluationError(
                "only running shadow evaluations can be executed"
            )
        source_run_id = evaluation["source_run_id"]
        frozen_inputs = self.repository.load_source_inputs(source_run_id)
        shadow_id = evaluation["shadow_id"]
        flash_model_id = None
        pro_model_id = None
        try:
            flash_model_id = self._resolve(self.flash_provider)
            pro_model_id = self._resolve(self.pro_provider)
            observations = self._evaluate_all(
                frozen_inputs,
                flash_model_id=flash_model_id,
                pro_model_id=pro_model_id,
            )
            self.repository.save_observations(shadow_id, observations)
            result = self._summarize(observations, len(frozen_inputs))
            errors = [
                f"{item['symbol']}:{item['model_role']}:{item['error']}"
                for item in observations
                if item["request_status"] != "completed"
            ]
            status = "completed" if not errors else "partial"
            return self.repository.finish(
                shadow_id,
                status=status,
                flash_resolved_model_id=flash_model_id,
                pro_resolved_model_id=pro_model_id,
                paired_input_count=len(frozen_inputs),
                flash_completed_count=result["flash"]["completed_count"],
                pro_completed_count=result["pro"]["completed_count"],
                result=result,
                errors=errors,
            )
        except Exception as exc:
            return self.repository.finish(
                shadow_id,
                status="failed",
                flash_resolved_model_id=flash_model_id,
                pro_resolved_model_id=pro_model_id,
                paired_input_count=len(frozen_inputs),
                flash_completed_count=0,
                pro_completed_count=0,
                result=None,
                errors=[sanitize_error(exc)],
            )

    @staticmethod
    def _resolve(provider: AICompletionProvider) -> str:
        resolved = str(provider.resolve_model_id() or "").strip()
        if not resolved:
            raise QuantShadowEvaluationError("resolved model ID is empty")
        return resolved

    def _evaluate_all(
        self,
        frozen_inputs: Sequence[Mapping[str, Any]],
        *,
        flash_model_id: str,
        pro_model_id: str,
    ) -> list[dict[str, Any]]:
        futures = {}
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            for item in frozen_inputs:
                for role, provider, model_id in (
                    ("flash", self.flash_provider, flash_model_id),
                    ("pro", self.pro_provider, pro_model_id),
                ):
                    future = executor.submit(
                        self._evaluate_one,
                        item,
                        role=role,
                        provider=provider,
                        expected_model_id=model_id,
                    )
                    futures[future] = (item["symbol"], role)
            observations = [future.result() for future in as_completed(futures)]
        return sorted(
            observations,
            key=lambda item: (item["symbol"], item["model_role"]),
        )

    def _evaluate_one(
        self,
        frozen: Mapping[str, Any],
        *,
        role: str,
        provider: AICompletionProvider,
        expected_model_id: str,
    ) -> dict[str, Any]:
        raw_outputs = []
        error = None
        started = self.monotonic()
        for attempt in range(1, self.max_attempts + 1):
            try:
                request = frozen["input"]
                if float(provider.temperature) != float(request["temperature"]):
                    raise QuantShadowEvaluationError(
                        "provider temperature differs from frozen input"
                    )
                completion = provider.complete(
                    system_prompt=request["system_prompt"],
                    user_prompt=request["user_prompt"],
                    response_schema=request["response_schema"],
                )
                raw_outputs.append(completion.raw_text)
                if completion.resolved_model_id != expected_model_id:
                    raise QuantShadowEvaluationError(
                        "resolved model ID changed during shadow evaluation"
                    )
                parsed = parse_ai_decision(completion.raw_text)
                return {
                    "symbol": frozen["symbol"],
                    "model_role": role,
                    "model_alias": provider.model_alias,
                    "resolved_model_id": completion.resolved_model_id,
                    "production_ai_input_hash": frozen["production_ai_input_hash"],
                    "paired_input_hash": frozen["paired_input_hash"],
                    "request_status": "completed",
                    "attempts": attempt,
                    "latency_ms": (self.monotonic() - started) * 1000.0,
                    "input_tokens": completion.input_tokens,
                    "output_tokens": completion.output_tokens,
                    "raw_attempt_outputs": raw_outputs,
                    "parsed_output": parsed,
                    "error": None,
                }
            except Exception as exc:
                error = _safe_error(provider, exc)
        return {
            "symbol": frozen["symbol"],
            "model_role": role,
            "model_alias": provider.model_alias,
            "resolved_model_id": None,
            "production_ai_input_hash": frozen["production_ai_input_hash"],
            "paired_input_hash": frozen["paired_input_hash"],
            "request_status": "failed",
            "attempts": self.max_attempts,
            "latency_ms": (self.monotonic() - started) * 1000.0,
            "input_tokens": None,
            "output_tokens": None,
            "raw_attempt_outputs": raw_outputs,
            "parsed_output": None,
            "error": error or "shadow model call failed",
        }

    def _summarize(
        self,
        observations: Sequence[Mapping[str, Any]],
        paired_input_count: int,
    ) -> dict[str, Any]:
        by_role = {
            role: [item for item in observations if item["model_role"] == role]
            for role in ("flash", "pro")
        }
        role_metrics = {}
        for role, items in by_role.items():
            completed = [
                item for item in items if item["request_status"] == "completed"
            ]
            input_tokens = [
                item["input_tokens"]
                for item in completed
                if item.get("input_tokens") is not None
            ]
            output_tokens = [
                item["output_tokens"]
                for item in completed
                if item.get("output_tokens") is not None
            ]
            if paired_input_count == 0:
                input_token_total = 0
                output_token_total = 0
            else:
                input_token_total = (
                    sum(input_tokens)
                    if completed and len(input_tokens) == len(completed)
                    else None
                )
                output_token_total = (
                    sum(output_tokens)
                    if completed and len(output_tokens) == len(completed)
                    else None
                )
            role_metrics[role] = {
                "planned_count": paired_input_count,
                "completed_count": len(completed),
                "completion_rate": (
                    len(completed) / paired_input_count
                    if paired_input_count else 1.0
                ),
                "structured_output_valid_rate": (
                    len(completed) / paired_input_count
                    if paired_input_count else 1.0
                ),
                "latency_p95_ms": _percentile(
                    [float(item["latency_ms"]) for item in items],
                    0.95,
                ),
                "input_tokens": input_token_total,
                "output_tokens": output_token_total,
            }
        pro_input_tokens = role_metrics["pro"]["input_tokens"]
        pro_output_tokens = role_metrics["pro"]["output_tokens"]
        pro_cost = (
            (
                pro_input_tokens * self.pro_input_cost_per_million_usd
                + pro_output_tokens * self.pro_output_cost_per_million_usd
            )
            / 1_000_000.0
            if pro_input_tokens is not None
            and pro_output_tokens is not None
            and self.pro_input_cost_per_million_usd is not None
            and self.pro_output_cost_per_million_usd is not None
            else None
        )
        role_metrics["pro"]["estimated_cost_usd"] = pro_cost
        paired = []
        indexed = {
            (item["symbol"], item["model_role"]): item for item in observations
        }
        for symbol in sorted({item["symbol"] for item in observations}):
            flash = indexed.get((symbol, "flash"))
            pro = indexed.get((symbol, "pro"))
            if not flash or not pro:
                continue
            if flash["request_status"] != "completed" or pro["request_status"] != "completed":
                continue
            paired.append((flash["parsed_output"], pro["parsed_output"]))
        promotion_reasons = ["paired_outcome_or_blind_quality_not_attached"]
        if role_metrics["pro"]["structured_output_valid_rate"] < role_metrics["flash"]["structured_output_valid_rate"]:
            promotion_reasons.append("pro_structured_output_rate_regressed")
        if role_metrics["pro"]["completion_rate"] < 0.95:
            promotion_reasons.append("pro_completion_rate_below_slo")
        pro_latency = role_metrics["pro"]["latency_p95_ms"]
        if self.pro_latency_budget_ms is None:
            promotion_reasons.append("pro_latency_budget_not_configured")
        elif pro_latency is None or pro_latency > self.pro_latency_budget_ms:
            promotion_reasons.append("pro_latency_budget_exceeded")
        if self.pro_cost_budget_usd is None:
            promotion_reasons.append("pro_cost_budget_not_configured")
        elif pro_cost is None:
            promotion_reasons.append("provider_cost_data_unavailable")
        elif pro_cost > self.pro_cost_budget_usd:
            promotion_reasons.append("pro_cost_budget_exceeded")
        return {
            "shadow_evaluation_version": SHADOW_EVALUATION_VERSION,
            **role_metrics,
            "paired": {
                "valid_pair_count": len(paired),
                "decision_agreement_rate": (
                    mean(left["decision"] == right["decision"] for left, right in paired)
                    if paired else None
                ),
                "average_pro_minus_flash_suitability": (
                    mean(
                        right["suitability_score"] - left["suitability_score"]
                        for left, right in paired
                    )
                    if paired else None
                ),
                "average_pro_minus_flash_confidence": (
                    mean(
                        right["confidence"] - left["confidence"]
                        for left, right in paired
                    )
                    if paired else None
                ),
            },
            "promotion_gate": {
                "ready": not promotion_reasons,
                "reasons": promotion_reasons,
            },
        }


def build_configured_quant_shadow_service() -> QuantShadowEvaluationService:
    from .config import get_settings
    from .quant_stock_selector_ai import DeepSeekQuantSelectorProvider
    from .repositories import load_ai_credentials

    settings = get_settings()
    if not settings.quant_selector_shadow_enabled:
        raise QuantShadowEvaluationError("quant selector shadow evaluation is disabled")
    api_key = str(load_ai_credentials().get("DEEPSEEK_API_KEY") or "").strip()
    if not api_key:
        raise QuantShadowEvaluationError("DEEPSEEK_API_KEY is not configured")
    return QuantShadowEvaluationService(
        DeepSeekQuantSelectorProvider(
            api_key,
            model=FLASH_MODEL_ALIAS,
            base_url=settings.deepseek_base_url,
        ),
        DeepSeekQuantSelectorProvider(
            api_key,
            model=PRO_MODEL_ALIAS,
            base_url=settings.deepseek_base_url,
        ),
        pro_latency_budget_ms=settings.quant_selector_shadow_pro_latency_budget_ms,
        pro_cost_budget_usd=settings.quant_selector_shadow_pro_cost_budget_usd,
        pro_input_cost_per_million_usd=(
            settings.quant_selector_shadow_pro_input_cost_per_million_usd
        ),
        pro_output_cost_per_million_usd=(
            settings.quant_selector_shadow_pro_output_cost_per_million_usd
        ),
    )
