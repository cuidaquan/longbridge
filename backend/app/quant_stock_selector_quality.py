"""Operational quality metrics for quantitative selection runs."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from itertools import combinations
import json
import math
from typing import Any, Callable, Mapping, Sequence

from .db import get_connection
from .quant_stock_selector_hashing import canonical_sha256


QUALITY_REPORT_VERSION = "quant-selector-quality-v1"
AI_COMPLETION_SLO = 0.95
RUN_DURATION_P95_SLO_SECONDS = 300.0
_AUDITABLE_STATUSES = {"completed", "partial"}
_PRE_AI_SNAPSHOT_KINDS = {"news", "events"}


def _ratio(numerator: int, denominator: int, *, empty: float | None) -> float | None:
    return numerator / denominator if denominator else empty


def _coverage_metric(
    numerator: int,
    denominator: int,
    *,
    target: float | None,
    empty: float | None = 1.0,
) -> dict[str, Any]:
    value = _ratio(numerator, denominator, empty=empty)
    return {
        "value": value,
        "numerator": numerator,
        "denominator": denominator,
        "target": target,
        "passes": None if target is None or value is None else value >= target,
    }


def _json_value(value: Any) -> Any:
    if value is None:
        return None
    try:
        return json.loads(value) if isinstance(value, str) else value
    except (TypeError, json.JSONDecodeError):
        return None


def _json_object(value: Any) -> dict[str, Any] | None:
    parsed = _json_value(value)
    return parsed if isinstance(parsed, dict) else None


def _nearest_rank_percentile(values: Sequence[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[index]


class QuantSelectionQualityService:
    def __init__(
        self,
        *,
        connection_factory: Callable[..., Any] = get_connection,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.connection_factory = connection_factory
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def report(self, *, limit: int = 100) -> dict[str, Any]:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        runs = self._load_runs(limit)
        run_ids = [item["run_id"] for item in runs]
        candidates = self._load_candidates(run_ids)
        snapshots = self._load_snapshots(run_ids)
        ai_snapshots = self._load_ai_snapshots(run_ids)

        selected_candidates = [
            item for item in candidates if item["selected_for_ai"]
        ]
        product_evidence = sum(
            self._has_product_scope_evidence(item["payload"])
            for item in selected_candidates
        )
        etfs = [
            item for item in selected_candidates
            if (item["payload"].get("metadata") or {}).get("asset_class")
            == "equity_etf"
        ]
        etf_direction = sum(
            str((item["payload"].get("metadata") or {}).get(
                "exposure_direction"
            ) or "").lower() not in {"", "unknown"}
            for item in etfs
        )
        etf_leverage = sum(
            (item["payload"].get("metadata") or {}).get("leverage") is not None
            for item in etfs
        )

        hard_filtered = [
            item for item in candidates if self._is_hard_filtered(item["payload"])
        ]
        hard_filter_reasons = sum(
            bool(item["payload"].get("exclusion_reasons"))
            for item in hard_filtered
        )

        planned = sum(item["ai_planned_count"] for item in runs)
        completed = sum(item["ai_completed_count"] for item in runs)
        auditable_runs = [
            item for item in runs
            if item["status"] in _AUDITABLE_STATUSES
            and item["quant_manifest"] is not None
        ]
        completed_auditable_runs = [
            item for item in auditable_runs if item["status"] == "completed"
        ]
        proven_boundaries = sum(
            bool(
                (item["quant_manifest"].get("selection_manifest") or {}).get(
                    "boundary_proven"
                )
            )
            for item in completed_auditable_runs
        )
        valid_hash_runs = sum(
            self._hashes_are_valid(
                item,
                snapshots.get(item["run_id"], []),
                ai_snapshots.get(item["run_id"], []),
                [
                    candidate
                    for candidate in candidates
                    if candidate["run_id"] == item["run_id"]
                ],
            )
            for item in auditable_runs
        )

        consistency_numerator, consistency_denominator = self._consistency(
            completed_auditable_runs,
            snapshots,
        )
        durations = [
            (item["completed_at"] - item["started_at"]).total_seconds()
            for item in runs
            if item["started_at"] is not None
            and item["completed_at"] is not None
            and item["completed_at"] >= item["started_at"]
        ]
        duration_p95 = _nearest_rank_percentile(durations, 0.95)

        metrics = {
            "product_scope_evidence_coverage": _coverage_metric(
                product_evidence,
                len(selected_candidates),
                target=1.0,
            ),
            "etf_direction_coverage": _coverage_metric(
                etf_direction,
                len(etfs),
                target=None,
                empty=None,
            ),
            "etf_leverage_coverage": _coverage_metric(
                etf_leverage,
                len(etfs),
                target=None,
                empty=None,
            ),
            "hard_filter_reason_coverage": _coverage_metric(
                hard_filter_reasons,
                len(hard_filtered),
                target=1.0,
            ),
            "ai_completion_rate": _coverage_metric(
                completed,
                planned,
                target=AI_COMPLETION_SLO,
            ),
            "exact_candidate_boundary_rate": _coverage_metric(
                proven_boundaries,
                len(completed_auditable_runs),
                target=1.0,
                empty=None,
            ),
            "input_hash_validity_rate": _coverage_metric(
                valid_hash_runs,
                len(auditable_runs),
                target=1.0,
                empty=None,
            ),
            "quant_replay_consistency_rate": _coverage_metric(
                consistency_numerator,
                consistency_denominator,
                target=1.0,
                empty=None,
            ),
            "run_duration_p95_seconds": {
                "value": duration_p95,
                "sample_count": len(durations),
                "target": RUN_DURATION_P95_SLO_SECONDS,
                "passes": (
                    None
                    if duration_p95 is None
                    else duration_p95 < RUN_DURATION_P95_SLO_SECONDS
                ),
            },
        }
        gate_reasons = [
            name
            for name, metric in metrics.items()
            if metric["target"] is not None and metric["passes"] is False
        ]
        if not auditable_runs:
            gate_reasons.append("insufficient_auditable_runs")
        if consistency_denominator == 0:
            gate_reasons.append("insufficient_quant_replay_pairs")
        if not durations:
            gate_reasons.append("insufficient_duration_samples")
        return {
            "quality_report_version": QUALITY_REPORT_VERSION,
            "generated_at": self.clock().astimezone(timezone.utc).isoformat(
                timespec="milliseconds"
            ).replace("+00:00", "Z"),
            "ready": not gate_reasons,
            "gate_reasons": gate_reasons,
            "sample": {
                "terminal_runs": len(runs),
                "auditable_runs": len(auditable_runs),
                "candidate_records": len(candidates),
                "selected_for_ai_records": len(selected_candidates),
                "quant_replay_pairs": consistency_denominator,
            },
            "metrics": metrics,
        }

    def _load_runs(self, limit: int) -> list[dict[str, Any]]:
        with self.connection_factory() as connection:
            cursor = connection.execute(
                """
                SELECT run_id, status, started_at, completed_at, data_as_of,
                       universe_version, filter_version, score_version,
                       candidate_count, ai_planned_count, ai_completed_count,
                       quant_manifest,
                       run_manifest, quant_input_hash, run_input_hash
                FROM quant_selection_runs
                WHERE status IN ('completed', 'partial', 'failed', 'cancelled')
                ORDER BY completed_at DESC NULLS LAST, created_at DESC, run_id DESC
                LIMIT ?
                """,
                [limit],
            )
            rows = cursor.fetchall()
        keys = [item[0] for item in cursor.description]
        result = []
        for row in rows:
            item = dict(zip(keys, row))
            item["quant_manifest"] = _json_object(item["quant_manifest"])
            item["run_manifest"] = _json_object(item["run_manifest"])
            result.append(item)
        return result

    def _load_candidates(self, run_ids: Sequence[str]) -> list[dict[str, Any]]:
        if not run_ids:
            return []
        placeholders = ", ".join("?" for _ in run_ids)
        with self.connection_factory() as connection:
            rows = connection.execute(
                f"""
                SELECT run_id, selected_for_ai, payload_hash, payload
                FROM quant_selection_candidates
                WHERE run_id IN ({placeholders})
                """,
                list(run_ids),
            ).fetchall()
        return [
            {
                "run_id": run_id,
                "selected_for_ai": bool(selected_for_ai),
                "payload_hash": payload_hash,
                "payload": _json_object(payload) or {},
            }
            for run_id, selected_for_ai, payload_hash, payload in rows
        ]

    def _load_snapshots(
        self,
        run_ids: Sequence[str],
    ) -> dict[str, list[dict[str, Any]]]:
        if not run_ids:
            return {}
        placeholders = ", ".join("?" for _ in run_ids)
        with self.connection_factory() as connection:
            rows = connection.execute(
                f"""
                SELECT run_id, snapshot_kind, symbol, source, schema_version,
                       payload_hash, payload
                FROM quant_selection_input_snapshots
                WHERE run_id IN ({placeholders})
                """,
                list(run_ids),
            ).fetchall()
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for run_id, kind, symbol, source, schema, digest, payload in rows:
            grouped[run_id].append({
                "snapshot_kind": kind,
                "symbol": symbol,
                "source": source,
                "schema_version": schema,
                "payload_hash": digest,
                "payload": _json_value(payload),
            })
        return grouped

    def _load_ai_snapshots(
        self,
        run_ids: Sequence[str],
    ) -> dict[str, list[dict[str, Any]]]:
        if not run_ids:
            return {}
        placeholders = ", ".join("?" for _ in run_ids)
        with self.connection_factory() as connection:
            rows = connection.execute(
                f"""
                SELECT run_id, ai_input_hash, input_snapshot
                FROM quant_selection_ai_snapshots
                WHERE run_id IN ({placeholders})
                """,
                list(run_ids),
            ).fetchall()
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for run_id, digest, payload in rows:
            grouped[run_id].append({
                "ai_input_hash": digest,
                "input_snapshot": _json_object(payload),
            })
        return grouped

    @staticmethod
    def _has_product_scope_evidence(payload: Mapping[str, Any]) -> bool:
        metadata = payload.get("metadata")
        return (
            isinstance(metadata, Mapping)
            and metadata.get("asset_class") in {"common_stock", "equity_etf"}
            and bool(str(metadata.get("source") or "").strip())
            and bool(str(metadata.get("source_version") or "").strip())
        )

    @staticmethod
    def _is_hard_filtered(payload: Mapping[str, Any]) -> bool:
        filters = payload.get("hard_filters")
        if not isinstance(filters, Mapping):
            return False
        return any(
            isinstance(item, Mapping)
            and item.get("status") in {"fail", "unresolved"}
            for item in filters.values()
        )

    @staticmethod
    def _hashes_are_valid(
        run: Mapping[str, Any],
        snapshots: Sequence[Mapping[str, Any]],
        ai_snapshots: Sequence[Mapping[str, Any]],
        candidates: Sequence[Mapping[str, Any]],
    ) -> bool:
        quant_manifest = run.get("quant_manifest")
        run_manifest = run.get("run_manifest")
        if not isinstance(quant_manifest, Mapping) or not isinstance(
            run_manifest, Mapping
        ):
            return False
        if canonical_sha256(quant_manifest) != run.get("quant_input_hash"):
            return False
        if canonical_sha256(run_manifest) != run.get("run_input_hash"):
            return False
        if len(candidates) != run.get("candidate_count"):
            return False
        actual_references = sorted(
            (
                {
                    "source": snapshot["source"],
                    "schema_version": snapshot["schema_version"],
                    "payload_hash": snapshot["payload_hash"],
                }
                for snapshot in snapshots
            ),
            key=lambda item: (
                item["source"],
                item["schema_version"],
                item["payload_hash"],
            ),
        )
        if actual_references != quant_manifest.get("input_snapshots"):
            return False
        for snapshot in snapshots:
            payload = snapshot.get("payload")
            if payload is None or canonical_sha256(payload) != snapshot.get(
                "payload_hash"
            ):
                return False
        for candidate in candidates:
            if canonical_sha256(candidate["payload"]) != candidate["payload_hash"]:
                return False
        for snapshot in ai_snapshots:
            payload = snapshot.get("input_snapshot")
            if payload is None:
                return False
            unhashed = dict(payload)
            embedded = unhashed.pop("ai_input_hash", None)
            digest = snapshot.get("ai_input_hash")
            if canonical_sha256(unhashed) != digest or embedded not in {None, digest}:
                return False
        if run.get("status") == "completed":
            if len(ai_snapshots) != run.get("ai_planned_count"):
                return False
            actual_ai_hashes = sorted(
                snapshot["ai_input_hash"] for snapshot in ai_snapshots
            )
            manifest_ai_hashes = sorted(
                item.get("ai_input_hash")
                for item in run_manifest.get("ai_inputs", [])
                if isinstance(item, Mapping)
            )
            if actual_ai_hashes != manifest_ai_hashes:
                return False
        return True

    @staticmethod
    def _consistency(
        runs: Sequence[Mapping[str, Any]],
        snapshots: Mapping[str, Sequence[Mapping[str, Any]]],
    ) -> tuple[int, int]:
        groups: dict[str, list[str]] = defaultdict(list)
        signatures = {}
        for run in runs:
            references = sorted(
                (
                    {
                        "snapshot_kind": item["snapshot_kind"],
                        "symbol": item["symbol"],
                        "source": item["source"],
                        "schema_version": item["schema_version"],
                        "payload_hash": item["payload_hash"],
                    }
                    for item in snapshots.get(run["run_id"], [])
                    if item["snapshot_kind"] not in _PRE_AI_SNAPSHOT_KINDS
                ),
                key=lambda item: (
                    item["snapshot_kind"],
                    item["symbol"] or "",
                    item["source"],
                    item["schema_version"],
                    item["payload_hash"],
                ),
            )
            if not references:
                continue
            replay_key = canonical_sha256({
                "data_as_of": run["data_as_of"],
                "universe_version": run["universe_version"],
                "filter_version": run["filter_version"],
                "score_version": run["score_version"],
                "input_snapshots": references,
            })
            groups[replay_key].append(run["run_id"])
            manifest = run["quant_manifest"] or {}
            signatures[run["run_id"]] = canonical_sha256({
                "candidates": manifest.get("candidates"),
                "selection_manifest": manifest.get("selection_manifest"),
            })
        numerator = 0
        denominator = 0
        for run_ids in groups.values():
            for left, right in combinations(run_ids, 2):
                denominator += 1
                numerator += signatures[left] == signatures[right]
        return numerator, denominator
