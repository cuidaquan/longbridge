from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
import math
import random
from statistics import mean, median
from typing import Any, Callable, Dict, Iterable, List, Optional

from .db import get_connection
from .services import get_cached_candlesticks
from .stock_picker import StockPickerService, get_stock_picker_service
from .stock_picker_ai_snapshots import (
    AI_INPUT_SNAPSHOT_VERSION,
    AI_OUTPUT_SNAPSHOT_VERSION,
    canonical_json,
    parse_snapshot,
    sha256_json,
    snapshot_hash,
)


AI_INCREMENT_EVALUATION_VERSION = "stock-picker-ai-increment-v2"
BLOCK_BOOTSTRAP_METHOD = "circular-moving-block-bootstrap-v1"
DEFAULT_BOOTSTRAP_SAMPLES = 2000
DEFAULT_BOOTSTRAP_CONFIDENCE_LEVEL = 0.95
DEFAULT_BOOTSTRAP_SEED = 20260724
MINIMUM_BOOTSTRAP_DATES = 4


class StockPickerAIIncrementEvaluationService:
    """Evaluate final-score re-ranking after outcome coverage is sufficient."""

    def __init__(
        self,
        stock_picker: Optional[StockPickerService] = None,
        bar_loader: Callable[..., List[Dict[str, Any]]] = (
            get_cached_candlesticks
        ),
        connection_factory: Callable = get_connection,
        now_provider: Callable[[], datetime] = (
            lambda: datetime.now(timezone.utc)
        ),
    ) -> None:
        self.stock_picker = stock_picker or get_stock_picker_service()
        self.bar_loader = bar_loader
        self.connection_factory = connection_factory
        self.now_provider = now_provider

    def run(
        self,
        pool_type: str,
        horizons: Iterable[int] = (5, 10, 20),
        top_k: int = 3,
        lookback_days: int = 365,
        max_bars: int = 5000,
        minimum_complete_batches: int = 20,
        minimum_labeled_records: int = 60,
        minimum_ai_completion_rate: float = 0.9,
        bootstrap_samples: int = DEFAULT_BOOTSTRAP_SAMPLES,
        bootstrap_confidence_level: float = (
            DEFAULT_BOOTSTRAP_CONFIDENCE_LEVEL
        ),
        bootstrap_block_size: Optional[int] = None,
        bootstrap_seed: int = DEFAULT_BOOTSTRAP_SEED,
        score_version: Optional[str] = None,
        prompt_version: Optional[str] = None,
        ai_model: Optional[str] = None,
        news_mode: str = "all",
        persist: bool = True,
    ) -> Dict[str, Any]:
        direction = self.stock_picker._validate_pool_type(pool_type)
        raw_horizons = list(horizons)
        if any(
            isinstance(value, bool) or not isinstance(value, int)
            for value in raw_horizons
        ):
            raise ValueError("horizons 必须包含整数交易日")
        normalized_horizons = sorted(set(raw_horizons))
        effective_score_version = (
            score_version or self.stock_picker.SCORE_VERSION
        )
        effective_prompt_version = (
            prompt_version or self.stock_picker.PROMPT_VERSION
        )
        effective_ai_model = ai_model or self.stock_picker.AI_MODEL
        self._validate_parameters(
            normalized_horizons,
            top_k,
            lookback_days,
            max_bars,
            minimum_complete_batches,
            minimum_labeled_records,
            minimum_ai_completion_rate,
            bootstrap_samples,
            bootstrap_confidence_level,
            bootstrap_block_size,
            bootstrap_seed,
            effective_score_version,
            effective_prompt_version,
            effective_ai_model,
            news_mode,
        )
        cutoff = self._normalize_timestamp(
            self.now_provider()
        ) - timedelta(days=lookback_days)
        rows = self._load_analysis_rows(direction, cutoff)
        batches = self._group_batches(rows)

        exclusions: Counter[str] = Counter()
        structural_batches: List[Dict[str, Any]] = []
        raw_selected_records = 0
        ai_completed_records = 0
        latest_analysis_time = None

        for batch_rows in batches.values():
            batch, reason = self._validate_batch(
                batch_rows,
                top_k,
                effective_score_version,
                effective_prompt_version,
                effective_ai_model,
                news_mode,
            )
            if batch is None:
                exclusions[reason or "invalid_batch"] += 1
                continue
            structural_batches.append(batch)
            raw_selected_records += len(batch["selected"])
            ai_completed_records += sum(
                1
                for item in batch["selected"]
                if self._is_completed_ai(item)
            )
            batch_time = max(
                item["analysis_time"]
                for item in batch["rows"]
            )
            if (
                latest_analysis_time is None
                or batch_time > latest_analysis_time
            ):
                latest_analysis_time = batch_time

        ai_completion_rate = (
            ai_completed_records / raw_selected_records
            if raw_selected_records
            else None
        )
        ai_complete_batches = []
        for batch in structural_batches:
            if not all(
                self._is_completed_ai(item)
                for item in batch["selected"]
            ):
                exclusions["ai_incomplete_batch"] += 1
                continue
            ai_complete_batches.append(batch)

        bars_cache: Dict[str, List[Dict[str, Any]]] = {}
        paired_returns: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
        labeled_records: Counter[int] = Counter()
        latest_label_date = None
        for batch in ai_complete_batches:
            labels_by_horizon: Dict[int, Dict[int, Dict[str, Any]]] = {
                horizon: {}
                for horizon in normalized_horizons
            }
            for item in batch["selected"]:
                labels = self._future_returns(
                    item,
                    normalized_horizons,
                    max_bars,
                    bars_cache,
                )
                for horizon, label in labels.items():
                    labels_by_horizon[horizon][item["pool_id"]] = label
                    label_date = label["future_date"]
                    if (
                        latest_label_date is None
                        or label_date > latest_label_date
                    ):
                        latest_label_date = label_date

            selected_count = len(batch["selected"])
            for horizon in normalized_horizons:
                horizon_labels = labels_by_horizon[horizon]
                labeled_records[horizon] += len(horizon_labels)
                if len(horizon_labels) != selected_count:
                    continue
                paired_returns[horizon].append(
                    self._compare_batch_rankings(
                        batch,
                        horizon_labels,
                        top_k,
                    )
                )

        paired_batch_counts = {
            str(horizon): len(paired_returns[horizon])
            for horizon in normalized_horizons
        }
        labeled_record_counts = {
            str(horizon): labeled_records[horizon]
            for horizon in normalized_horizons
        }
        gate_reasons = self._gate_reasons(
            normalized_horizons,
            len(ai_complete_batches),
            paired_batch_counts,
            labeled_record_counts,
            ai_completion_rate,
            minimum_complete_batches,
            minimum_labeled_records,
            minimum_ai_completion_rate,
        )
        ready = not gate_reasons
        metrics = (
            {
                str(horizon): self._summarize_comparisons(
                    paired_returns[horizon],
                    bootstrap_samples=bootstrap_samples,
                    bootstrap_confidence_level=(
                        bootstrap_confidence_level
                    ),
                    bootstrap_block_size=bootstrap_block_size,
                    bootstrap_seed=(
                        bootstrap_seed + horizon
                    ) % 4294967296,
                )
                for horizon in normalized_horizons
            }
            if ready
            else None
        )

        report: Dict[str, Any] = {
            "evaluation_version": AI_INCREMENT_EVALUATION_VERSION,
            "pool_type": direction,
            "ready": ready,
            "parameters": {
                "horizons": normalized_horizons,
                "top_k": top_k,
                "lookback_days": lookback_days,
                "max_bars": max_bars,
                "minimum_complete_batches": minimum_complete_batches,
                "minimum_labeled_records": minimum_labeled_records,
                "minimum_ai_completion_rate": (
                    minimum_ai_completion_rate
                ),
                "bootstrap_samples": bootstrap_samples,
                "bootstrap_confidence_level": (
                    bootstrap_confidence_level
                ),
                "bootstrap_block_size": bootstrap_block_size,
                "bootstrap_seed": bootstrap_seed,
                "snapshot_version": AI_INPUT_SNAPSHOT_VERSION,
                "score_version": effective_score_version,
                "prompt_version": effective_prompt_version,
                "ai_model": effective_ai_model,
                "news_mode": news_mode,
            },
            "gate": {
                "ready": ready,
                "reasons": gate_reasons,
            },
            "coverage": {
                "raw_analysis_records": len(rows),
                "raw_batches": len(batches),
                "structurally_complete_batches": len(
                    structural_batches
                ),
                "ai_complete_batches": len(ai_complete_batches),
                "selected_records": raw_selected_records,
                "ai_completed_records": ai_completed_records,
                "ai_completion_rate": ai_completion_rate,
                "labeled_records_by_horizon": labeled_record_counts,
                "paired_batches_by_horizon": paired_batch_counts,
                "excluded_batches": dict(sorted(exclusions.items())),
                "latest_analysis_time": self._iso_or_none(
                    latest_analysis_time
                ),
                "latest_label_date": latest_label_date,
            },
            "metrics": metrics,
            "methodology": {
                "comparison": (
                    "每个完整批次仅在同一 AI Top N 候选集合内，比较纯量化"
                    "排名 Top K 与 AI 最终机会分重排 Top K 的等权方向收益"
                ),
                "no_lookahead": (
                    "收益标签仅使用分析 data_as_of 之后第 N 个交易日收盘价"
                ),
                "batch_integrity": (
                    "仅使用输入哈希有效、方向排名完整且 AI Top N 全部成功的"
                    " pool_ranking 批次"
                ),
                "gate": (
                    "完整批次、后验标签和 AI 完成率任一不足时 metrics 为 null"
                ),
                "inference": (
                    "按 data_as_of 日期聚类并排序，使用循环移动块 bootstrap"
                    " 估计配对平均增量的置信区间；同日批次不会拆散，少于"
                    f" {MINIMUM_BOOTSTRAP_DATES} 个不同日期时不输出区间"
                ),
                "inference_limit": (
                    "bootstrap 区间只描述当前 cohort 的采样不确定性，"
                    "未校正多持有期或多 cohort 比较，也不是 AI 或新闻的"
                    "因果证明"
                ),
                "causal_limit": (
                    "该报告评估已部署最终机会分链路的重排增量；最终分还包含"
                    "量化权重和趋势强度，且不是随机对照试验，不能把结果解释为"
                    "模型或新闻的独立因果贡献"
                ),
                "costs": (
                    "比较使用未扣交易成本的方向收益；两组 Top K 数量相同，"
                    "但报告不估算标的间不同的冲击成本、借券费或资金占用"
                ),
            },
        }
        if persist:
            report["id"] = self._save_report(report)
        return report

    def get_history(self, limit: int = 20) -> List[Dict[str, Any]]:
        safe_limit = min(100, max(1, int(limit)))
        with self.connection_factory() as connection:
            rows = connection.execute(
                f"""
                SELECT
                    id, created_at, pool_type, evaluation_version,
                    parameters, result, ready, data_as_of
                FROM stock_picker_ai_evaluations
                ORDER BY created_at DESC, id DESC
                LIMIT {safe_limit}
                """
            ).fetchall()
        history = []
        for row in rows:
            parameters, parameters_error = parse_snapshot(row[4])
            result, result_error = parse_snapshot(row[5])
            if parameters_error or result_error:
                continue
            history.append({
                "id": row[0],
                "created_at": str(row[1]),
                "pool_type": row[2],
                "evaluation_version": row[3],
                "parameters": parameters,
                "result": result,
                "ready": bool(row[6]),
                "data_as_of": str(row[7]) if row[7] else None,
            })
        return history

    def _load_analysis_rows(
        self,
        pool_type: str,
        cutoff: datetime,
    ) -> List[Dict[str, Any]]:
        with self.connection_factory() as connection:
            rows = connection.execute(
                """
                SELECT
                    id, pool_id, symbol, pool_type, analysis_time,
                    current_price, score_total, recommendation_score,
                    ai_action, ai_confidence, ai_status, data_as_of,
                    job_id, ai_input_snapshot, ai_input_hash,
                    ai_output_snapshot
                FROM stock_picker_analysis
                WHERE pool_type = ?
                  AND analysis_time >= ?
                  AND job_id IS NOT NULL
                ORDER BY analysis_time, id
                """,
                (pool_type, cutoff),
            ).fetchall()
        return [
            {
                "id": row[0],
                "pool_id": row[1],
                "symbol": row[2],
                "pool_type": row[3],
                "analysis_time": self._normalize_timestamp(row[4]),
                "current_price": self._finite_number(row[5]),
                "score_total": self._finite_number(row[6]),
                "recommendation_score": self._finite_number(row[7]),
                "ai_action": row[8],
                "ai_confidence": self._finite_number(row[9]),
                "ai_status": row[10],
                "data_as_of": self._date_string(row[11]),
                "job_id": row[12],
                "ai_input_snapshot_raw": row[13],
                "ai_input_hash": row[14],
                "ai_output_snapshot_raw": row[15],
            }
            for row in rows
        ]

    @staticmethod
    def _group_batches(
        rows: List[Dict[str, Any]],
    ) -> Dict[str, List[Dict[str, Any]]]:
        batches: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for row in rows:
            batches[str(row["job_id"])].append(row)
        return batches

    def _validate_batch(
        self,
        batch_rows: List[Dict[str, Any]],
        top_k: int,
        score_version: str,
        prompt_version: str,
        ai_model: str,
        news_mode: str,
    ) -> tuple[Optional[Dict[str, Any]], Optional[str]]:
        latest_by_pool: Dict[int, Dict[str, Any]] = {}
        for row in batch_rows:
            existing = latest_by_pool.get(row["pool_id"])
            if existing is None or (
                row["analysis_time"],
                row["id"],
            ) > (
                existing["analysis_time"],
                existing["id"],
            ):
                latest_by_pool[row["pool_id"]] = row
        rows = list(latest_by_pool.values())

        versions = set()
        expected_rankings = []
        ranking_hashes = set()
        news_enabled_values = set()
        for row in rows:
            snapshot, parse_error = parse_snapshot(
                row["ai_input_snapshot_raw"]
            )
            if parse_error or not isinstance(snapshot, dict):
                return None, "missing_or_invalid_snapshot"
            if snapshot.get("version") != AI_INPUT_SNAPSHOT_VERSION:
                return None, "unsupported_snapshot_version"
            if snapshot.get("selection_context") != "pool_ranking":
                return None, "not_pool_ranking"
            if (
                snapshot.get("score_version") != score_version
                or snapshot.get("prompt_version") != prompt_version
                or snapshot.get("ai_model") != ai_model
            ):
                return None, "version_cohort_mismatch"
            news_enabled = snapshot.get("news_enabled")
            if not isinstance(news_enabled, bool):
                return None, "missing_news_mode"
            news_enabled_values.add(news_enabled)
            if (
                not row["ai_input_hash"]
                or snapshot_hash(snapshot) != row["ai_input_hash"]
            ):
                return None, "input_hash_invalid"
            selection = snapshot.get("selection")
            if not isinstance(selection, dict):
                return None, "missing_selection"
            ranking = selection.get("ranking")
            if not isinstance(ranking, list) or not ranking:
                return None, "missing_ranking"
            selection_version = snapshot.get("selection_version")
            if not selection_version:
                return None, "missing_selection_version"
            expected_selection_version = sha256_json({
                "pool_type": row["pool_type"],
                "ai_top_n_per_pool": selection.get(
                    "ai_top_n_per_pool"
                ),
                "ranking": ranking,
            })
            if selection_version != expected_selection_version:
                return None, "selection_version_invalid"
            versions.add(selection_version)
            expected_rankings.append(ranking)
            ranking_hashes.add(sha256_json(ranking))
            row["input_snapshot"] = snapshot
            row["selection"] = selection
            row["quant_rank"] = self._positive_int(
                selection.get("quant_rank")
            )
            output_snapshot, output_error = parse_snapshot(
                row["ai_output_snapshot_raw"]
            )
            if output_error or not isinstance(output_snapshot, dict):
                return None, "missing_or_invalid_output_snapshot"
            if (
                output_snapshot.get("version")
                != AI_OUTPUT_SNAPSHOT_VERSION
            ):
                return None, "unsupported_output_snapshot_version"
            row["output_snapshot"] = output_snapshot

        if len(versions) != 1:
            return None, "mixed_selection_versions"
        if len(news_enabled_values) != 1:
            return None, "mixed_news_mode"
        news_enabled = next(iter(news_enabled_values))
        if (
            (news_mode == "enabled" and not news_enabled)
            or (news_mode == "disabled" and news_enabled)
        ):
            return None, "news_mode_mismatch"
        if len(ranking_hashes) != 1:
            return None, "inconsistent_ranking"
        if any(
            not isinstance(item, dict)
            or self._positive_int(item.get("pool_id")) is None
            or self._positive_int(item.get("rank")) is None
            for item in expected_rankings[0]
        ):
            return None, "invalid_ranking"
        expected_ids = {
            self._positive_int(item.get("pool_id"))
            for item in expected_rankings[0]
            if isinstance(item, dict)
        }
        expected_ids.discard(None)
        actual_ids = {row["pool_id"] for row in rows}
        if not expected_ids or actual_ids != expected_ids:
            return None, "incomplete_batch"
        if any(row["quant_rank"] is None for row in rows):
            return None, "missing_quant_rank"

        selected = [
            row
            for row in rows
            if row["selection"].get("ai_selected") is True
        ]
        if len(selected) <= top_k:
            return None, "insufficient_ai_candidates"
        observation_dates = {
            row["data_as_of"]
            for row in selected
            if row["data_as_of"] is not None
        }
        if len(observation_dates) != 1:
            return None, "mixed_data_as_of"
        for row in selected:
            indicators = row["input_snapshot"].get(
                "technical_indicators"
            )
            quant_score = row["input_snapshot"].get("quant_score")
            if (
                not isinstance(indicators, dict)
                or not isinstance(quant_score, dict)
                or row["data_as_of"] is None
            ):
                return None, "missing_analysis_values"
            snapshot_price = self._finite_number(
                indicators.get("current_price")
            )
            snapshot_score = self._finite_number(
                quant_score.get("total")
            )
            if (
                snapshot_price is None
                or snapshot_price <= 0
                or snapshot_score is None
                or row["current_price"] is None
                or row["score_total"] is None
                or not math.isclose(
                    snapshot_price,
                    row["current_price"],
                    rel_tol=1e-9,
                    abs_tol=1e-9,
                )
                or not math.isclose(
                    snapshot_score,
                    row["score_total"],
                    rel_tol=1e-9,
                    abs_tol=1e-9,
                )
            ):
                return None, "snapshot_record_mismatch"
            row["current_price"] = snapshot_price
            if self._is_completed_ai(row):
                parsed_response = row["output_snapshot"].get(
                    "parsed_response"
                )
                if not isinstance(parsed_response, dict):
                    return None, "missing_parsed_ai_output"
                ai_confidence = self._finite_number(
                    parsed_response.get("confidence")
                )
                if ai_confidence is None:
                    return None, "invalid_ai_confidence"
                ai_analysis = {
                    **parsed_response,
                    "confidence": ai_confidence,
                    "ai_status": "available",
                }
                recomputed_score = (
                    self.stock_picker._calculate_recommendation_score_v2(
                        quant_score,
                        ai_analysis,
                        row["pool_type"],
                    )
                )
                if (
                    row["recommendation_score"] is None
                    or not math.isclose(
                        recomputed_score,
                        row["recommendation_score"],
                        rel_tol=1e-9,
                        abs_tol=1e-9,
                    )
                ):
                    return None, "derived_score_mismatch"
                row["recommendation_score"] = recomputed_score
        return {
            "job_id": rows[0]["job_id"],
            "selection_version": next(iter(versions)),
            "news_enabled": news_enabled,
            "observation_date": next(iter(observation_dates)),
            "rows": rows,
            "selected": selected,
        }, None

    @staticmethod
    def _is_completed_ai(item: Dict[str, Any]) -> bool:
        return (
            item["input_snapshot"].get("request_status") == "completed"
            and item.get("ai_status") == "available"
            and item["output_snapshot"].get("status") == "completed"
            and isinstance(
                item["output_snapshot"].get("parsed_response"),
                dict,
            )
        )

    def _future_returns(
        self,
        item: Dict[str, Any],
        horizons: List[int],
        max_bars: int,
        cache: Dict[str, List[Dict[str, Any]]],
    ) -> Dict[int, Dict[str, Any]]:
        symbol = item["symbol"]
        if symbol not in cache:
            bars = self.bar_loader(
                symbol,
                period="day",
                limit=max_bars,
            ) or []
            cache[symbol] = sorted(
                (
                    bar
                    for bar in bars
                    if self._bar_date(bar) is not None
                    and self._positive_close(bar) is not None
                ),
                key=lambda bar: self._bar_date(bar) or "",
            )
        future_bars = [
            bar
            for bar in cache[symbol]
            if (self._bar_date(bar) or "") > item["data_as_of"]
        ]
        direction = 1 if item["pool_type"] == "LONG" else -1
        labels = {}
        for horizon in horizons:
            if len(future_bars) < horizon:
                continue
            future_bar = future_bars[horizon - 1]
            future_close = self._positive_close(future_bar)
            if future_close is None:
                continue
            labels[horizon] = {
                "directional_return": direction * (
                    future_close / item["current_price"] - 1
                ),
                "future_date": self._bar_date(future_bar),
            }
        return labels

    @staticmethod
    def _compare_batch_rankings(
        batch: Dict[str, Any],
        labels: Dict[int, Dict[str, Any]],
        top_k: int,
    ) -> Dict[str, Any]:
        quant_ranked = sorted(
            batch["selected"],
            key=lambda item: (
                item["quant_rank"],
                item["symbol"],
                item["pool_id"],
            ),
        )
        ai_ranked = sorted(
            batch["selected"],
            key=lambda item: (
                -item["recommendation_score"],
                item["symbol"],
                item["pool_id"],
            ),
        )
        quant_ids = [item["pool_id"] for item in quant_ranked[:top_k]]
        ai_ids = [item["pool_id"] for item in ai_ranked[:top_k]]
        quant_return = mean(
            labels[pool_id]["directional_return"]
            for pool_id in quant_ids
        )
        ai_return = mean(
            labels[pool_id]["directional_return"]
            for pool_id in ai_ids
        )
        overlap = len(set(quant_ids) & set(ai_ids)) / top_k
        return {
            "job_id": batch["job_id"],
            "observation_date": batch["observation_date"],
            "quant_return": quant_return,
            "ai_return": ai_return,
            "delta": ai_return - quant_return,
            "selection_changed": set(quant_ids) != set(ai_ids),
            "selection_overlap": overlap,
        }

    def _summarize_comparisons(
        self,
        comparisons: List[Dict[str, Any]],
        *,
        bootstrap_samples: int,
        bootstrap_confidence_level: float,
        bootstrap_block_size: Optional[int],
        bootstrap_seed: int,
    ) -> Dict[str, Any]:
        quant = [item["quant_return"] for item in comparisons]
        ai = [item["ai_return"] for item in comparisons]
        deltas = [item["delta"] for item in comparisons]
        return {
            "paired_batches": len(comparisons),
            "quant_top_k": self._return_summary(quant),
            "ai_top_k": self._return_summary(ai),
            "paired_delta": self._return_summary(deltas),
            "paired_delta_inference": self._block_bootstrap_inference(
                comparisons,
                bootstrap_samples=bootstrap_samples,
                confidence_level=bootstrap_confidence_level,
                requested_block_size=bootstrap_block_size,
                seed=bootstrap_seed,
            ),
            "selection_changed_batches": sum(
                1
                for item in comparisons
                if item["selection_changed"]
            ),
            "selection_change_rate": self._safe_ratio(
                sum(
                    1
                    for item in comparisons
                    if item["selection_changed"]
                ),
                len(comparisons),
            ),
            "average_selection_overlap": (
                mean(
                    item["selection_overlap"]
                    for item in comparisons
                )
                if comparisons
                else None
            ),
        }

    def _block_bootstrap_inference(
        self,
        comparisons: List[Dict[str, Any]],
        *,
        bootstrap_samples: int,
        confidence_level: float,
        requested_block_size: Optional[int],
        seed: int,
    ) -> Dict[str, Any]:
        clusters: Dict[str, List[float]] = defaultdict(list)
        for item in comparisons:
            observation_date = self._date_string(
                item.get("observation_date")
            )
            delta = self._finite_number(item.get("delta"))
            if observation_date is None or delta is None:
                continue
            clusters[observation_date].append(delta)

        ordered_clusters = [
            clusters[observation_date]
            for observation_date in sorted(clusters)
        ]
        distinct_dates = len(ordered_clusters)
        effective_block_size = (
            requested_block_size
            if requested_block_size is not None
            else max(2, math.ceil(distinct_dates ** (1 / 3)))
        )
        result: Dict[str, Any] = {
            "ready": False,
            "reason": None,
            "method": BLOCK_BOOTSTRAP_METHOD,
            "cluster_unit": "data_as_of_date",
            "estimate": mean(
                item["delta"]
                for item in comparisons
            ) if comparisons else None,
            "confidence_level": confidence_level,
            "lower": None,
            "upper": None,
            "standard_error": None,
            "interval_direction": None,
            "bootstrap_samples": bootstrap_samples,
            "requested_block_size": requested_block_size,
            "effective_block_size": effective_block_size,
            "distinct_observation_dates": distinct_dates,
            "seed": seed,
        }
        if distinct_dates < MINIMUM_BOOTSTRAP_DATES:
            result["reason"] = "insufficient_distinct_observation_dates"
            return result
        if effective_block_size >= distinct_dates:
            result["reason"] = "block_size_not_less_than_date_count"
            return result

        random_generator = random.Random(seed)
        bootstrap_means = []
        for _ in range(bootstrap_samples):
            sampled_clusters: List[List[float]] = []
            while len(sampled_clusters) < distinct_dates:
                start = random_generator.randrange(distinct_dates)
                remaining = distinct_dates - len(sampled_clusters)
                take = min(effective_block_size, remaining)
                sampled_clusters.extend(
                    ordered_clusters[
                        (start + offset) % distinct_dates
                    ]
                    for offset in range(take)
                )
            sampled_values = [
                value
                for cluster in sampled_clusters
                for value in cluster
            ]
            bootstrap_means.append(mean(sampled_values))

        alpha = 1 - confidence_level
        lower = self._percentile(bootstrap_means, alpha / 2)
        upper = self._percentile(
            bootstrap_means,
            1 - alpha / 2,
        )
        bootstrap_average = mean(bootstrap_means)
        standard_error = math.sqrt(
            sum(
                (value - bootstrap_average) ** 2
                for value in bootstrap_means
            ) / (len(bootstrap_means) - 1)
        )
        result.update({
            "ready": True,
            "lower": lower,
            "upper": upper,
            "standard_error": standard_error,
            "interval_direction": (
                "positive"
                if lower > 0
                else "negative"
                if upper < 0
                else "inconclusive"
            ),
        })
        return result

    def _return_summary(self, values: List[float]) -> Dict[str, Any]:
        return {
            "sample_count": len(values),
            "average": mean(values) if values else None,
            "median": median(values) if values else None,
            "positive_rate": self._safe_ratio(
                sum(1 for value in values if value > 0),
                len(values),
            ),
            "p05": self._percentile(values, 0.05),
            "p95": self._percentile(values, 0.95),
        }

    @staticmethod
    def _gate_reasons(
        horizons: List[int],
        ai_complete_batches: int,
        paired_batch_counts: Dict[str, int],
        labeled_record_counts: Dict[str, int],
        completion_rate: Optional[float],
        minimum_complete_batches: int,
        minimum_labeled_records: int,
        minimum_ai_completion_rate: float,
    ) -> List[str]:
        reasons = []
        if ai_complete_batches < minimum_complete_batches:
            reasons.append("insufficient_ai_complete_batches")
        if (
            completion_rate is None
            or completion_rate < minimum_ai_completion_rate
        ):
            reasons.append("insufficient_ai_completion_rate")
        for horizon in horizons:
            key = str(horizon)
            if paired_batch_counts[key] < minimum_complete_batches:
                reasons.append(
                    f"insufficient_paired_batches_{horizon}d"
                )
            if labeled_record_counts[key] < minimum_labeled_records:
                reasons.append(
                    f"insufficient_labeled_records_{horizon}d"
                )
        return reasons

    def _save_report(self, report: Dict[str, Any]) -> int:
        result_payload = {
            key: value
            for key, value in report.items()
            if key not in {"parameters", "id"}
        }
        data_as_of = report["coverage"]["latest_label_date"]
        with self.connection_factory() as connection:
            row = connection.execute(
                """
                INSERT INTO stock_picker_ai_evaluations (
                    pool_type, evaluation_version, parameters,
                    result, ready, data_as_of
                ) VALUES (?, ?, ?, ?, ?, ?)
                RETURNING id
                """,
                (
                    report["pool_type"],
                    report["evaluation_version"],
                    canonical_json(report["parameters"]),
                    canonical_json(result_payload),
                    report["ready"],
                    data_as_of,
                ),
            ).fetchone()
        return int(row[0])

    @staticmethod
    def _validate_parameters(
        horizons: List[int],
        top_k: int,
        lookback_days: int,
        max_bars: int,
        minimum_complete_batches: int,
        minimum_labeled_records: int,
        minimum_ai_completion_rate: float,
        bootstrap_samples: int,
        bootstrap_confidence_level: float,
        bootstrap_block_size: Optional[int],
        bootstrap_seed: int,
        score_version: str,
        prompt_version: str,
        ai_model: str,
        news_mode: str,
    ) -> None:
        if not horizons or any(value < 1 or value > 60 for value in horizons):
            raise ValueError("horizons 必须包含 1～60 的交易日")
        if (
            isinstance(top_k, bool)
            or not isinstance(top_k, int)
            or not 1 <= top_k <= 100
        ):
            raise ValueError("top_k 必须在 1～100 之间")
        if (
            isinstance(lookback_days, bool)
            or not isinstance(lookback_days, int)
            or not 1 <= lookback_days <= 3650
        ):
            raise ValueError("lookback_days 必须在 1～3650 之间")
        if (
            isinstance(max_bars, bool)
            or not isinstance(max_bars, int)
            or not 2 <= max_bars <= 5000
        ):
            raise ValueError("max_bars 必须在 2～5000 之间")
        if (
            isinstance(minimum_complete_batches, bool)
            or not isinstance(minimum_complete_batches, int)
            or not 1 <= minimum_complete_batches <= 1000
        ):
            raise ValueError(
                "minimum_complete_batches 必须在 1～1000 之间"
            )
        if (
            isinstance(minimum_labeled_records, bool)
            or not isinstance(minimum_labeled_records, int)
            or not 1 <= minimum_labeled_records <= 100000
        ):
            raise ValueError(
                "minimum_labeled_records 必须在 1～100000 之间"
            )
        if (
            isinstance(minimum_ai_completion_rate, bool)
            or not math.isfinite(minimum_ai_completion_rate)
            or not 0 <= minimum_ai_completion_rate <= 1
        ):
            raise ValueError(
                "minimum_ai_completion_rate 必须在 0～1 之间"
            )
        if (
            isinstance(bootstrap_samples, bool)
            or not isinstance(bootstrap_samples, int)
            or not 200 <= bootstrap_samples <= 100000
        ):
            raise ValueError(
                "bootstrap_samples 必须在 200～100000 之间"
            )
        if (
            isinstance(bootstrap_confidence_level, bool)
            or not math.isfinite(bootstrap_confidence_level)
            or not 0.8 <= bootstrap_confidence_level <= 0.99
        ):
            raise ValueError(
                "bootstrap_confidence_level 必须在 0.8～0.99 之间"
            )
        if (
            bootstrap_block_size is not None
            and (
                isinstance(bootstrap_block_size, bool)
                or not isinstance(bootstrap_block_size, int)
                or not 2 <= bootstrap_block_size <= 3650
            )
        ):
            raise ValueError(
                "bootstrap_block_size 必须为空或在 2～3650 之间"
            )
        if (
            isinstance(bootstrap_seed, bool)
            or not isinstance(bootstrap_seed, int)
            or not 0 <= bootstrap_seed <= 4294967295
        ):
            raise ValueError(
                "bootstrap_seed 必须在 0～4294967295 之间"
            )
        for label, value in (
            ("score_version", score_version),
            ("prompt_version", prompt_version),
            ("ai_model", ai_model),
        ):
            if (
                not isinstance(value, str)
                or not value
                or len(value) > 200
            ):
                raise ValueError(f"{label} 必须是 1～200 字符")
        if news_mode not in {"all", "enabled", "disabled"}:
            raise ValueError(
                "news_mode 必须是 all、enabled 或 disabled"
            )

    @staticmethod
    def _normalize_timestamp(value: Any) -> datetime:
        if isinstance(value, datetime):
            parsed = value
        else:
            parsed = datetime.fromisoformat(
                str(value).replace("Z", "+00:00")
            )
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed

    @staticmethod
    def _date_string(value: Any) -> Optional[str]:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value.date().isoformat()
        if isinstance(value, date):
            return value.isoformat()
        try:
            return datetime.fromisoformat(
                str(value).replace("Z", "+00:00")
            ).date().isoformat()
        except ValueError:
            return None

    @classmethod
    def _bar_date(cls, bar: Dict[str, Any]) -> Optional[str]:
        return cls._date_string(
            bar.get("ts")
            or bar.get("timestamp")
            or bar.get("date")
        )

    @staticmethod
    def _finite_number(value: Any) -> Optional[float]:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) else None

    @classmethod
    def _positive_close(cls, bar: Dict[str, Any]) -> Optional[float]:
        value = cls._finite_number(bar.get("close"))
        return value if value is not None and value > 0 else None

    @staticmethod
    def _positive_int(value: Any) -> Optional[int]:
        if isinstance(value, bool):
            return None
        try:
            number = int(value)
        except (TypeError, ValueError):
            return None
        return number if number > 0 else None

    @staticmethod
    def _safe_ratio(numerator: int, denominator: int) -> Optional[float]:
        return numerator / denominator if denominator else None

    @staticmethod
    def _percentile(
        values: List[float],
        percentile: float,
    ) -> Optional[float]:
        if not values:
            return None
        ordered = sorted(values)
        if len(ordered) == 1:
            return ordered[0]
        position = (len(ordered) - 1) * percentile
        lower = math.floor(position)
        upper = math.ceil(position)
        if lower == upper:
            return ordered[lower]
        weight = position - lower
        return ordered[lower] * (1 - weight) + ordered[upper] * weight

    @staticmethod
    def _iso_or_none(value: Optional[datetime]) -> Optional[str]:
        return value.isoformat() if value is not None else None


_stock_picker_ai_evaluation_service: Optional[
    StockPickerAIIncrementEvaluationService
] = None


def get_stock_picker_ai_evaluation_service(
) -> StockPickerAIIncrementEvaluationService:
    global _stock_picker_ai_evaluation_service
    if _stock_picker_ai_evaluation_service is None:
        _stock_picker_ai_evaluation_service = (
            StockPickerAIIncrementEvaluationService()
        )
    return _stock_picker_ai_evaluation_service
