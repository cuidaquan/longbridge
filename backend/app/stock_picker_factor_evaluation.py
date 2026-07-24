from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
import json
import math
import random
from statistics import mean, median
from typing import Any, Callable, Dict, Iterable, List, Optional
from zoneinfo import ZoneInfo

from .db import get_connection
from .services import get_cached_candlesticks
from .stock_picker_ai_snapshots import canonical_json
from .stock_picker_factor_snapshots import (
    FACTOR_REQUIREMENTS,
    MAX_SNAPSHOT_AGE_HOURS,
    MARKET_TIMEZONES,
    MIN_DISTINCT_SYMBOLS,
    MIN_FACTOR_COVERAGE,
    MIN_OBSERVATION_DATES,
    SNAPSHOT_VERSION,
    StockPickerFactorSnapshotService,
)


FACTOR_INCREMENT_EVALUATION_VERSION = "stock-picker-factor-increment-v1"
BLOCK_BOOTSTRAP_METHOD = "circular-moving-block-bootstrap-v1"
DEFAULT_BOOTSTRAP_SAMPLES = 2000
DEFAULT_BOOTSTRAP_CONFIDENCE_LEVEL = 0.95
DEFAULT_BOOTSTRAP_SEED = 20260724
MINIMUM_BOOTSTRAP_DATES = 4

FROZEN_THRESHOLDS = {
    "min_revenue_yoy": 0.0,
    "min_net_profit_yoy": 0.0,
    "min_operating_cash_flow_yoy": 0.0,
    "min_analyst_alignment": 0.0,
    "min_eps_revision_alignment": 0.0,
    "min_days_to_financial_event": 5.0,
    "min_days_to_corporate_action": 5.0,
    "max_spread_bps": 50.0,
    "min_top_of_book_notional": 100_000.0,
    "max_initial_margin_ratio": 0.6,
    "max_days_to_cover": 5.0,
    "max_short_ratio": 0.2,
    "min_short_selling_quantity": 1.0,
}

FACTOR_VARIANTS = {
    "fundamental_quality": (
        "min_revenue_yoy",
        "min_net_profit_yoy",
        "min_operating_cash_flow_yoy",
        "min_analyst_alignment",
        "min_eps_revision_alignment",
    ),
    "event_safety": (
        "min_days_to_financial_event",
        "min_days_to_corporate_action",
    ),
    "execution_risk": (
        "require_tradable",
        "max_spread_bps",
        "min_top_of_book_notional",
        "max_initial_margin_ratio",
        "max_days_to_cover",
        "max_short_ratio",
        "min_short_selling_quantity",
    ),
    "fundamental_and_execution": (
        "min_revenue_yoy",
        "min_net_profit_yoy",
        "min_operating_cash_flow_yoy",
        "min_analyst_alignment",
        "min_eps_revision_alignment",
        "min_days_to_financial_event",
        "min_days_to_corporate_action",
        "require_tradable",
        "max_spread_bps",
        "min_top_of_book_notional",
        "max_initial_margin_ratio",
        "max_days_to_cover",
        "max_short_ratio",
        "min_short_selling_quantity",
    ),
}

_RULES = {
    "min_revenue_yoy": ("fundamentals", "revenue_yoy", "min"),
    "min_net_profit_yoy": ("fundamentals", "net_profit_yoy", "min"),
    "min_operating_cash_flow_yoy": (
        "fundamentals",
        "operating_cash_flow_yoy",
        "min",
    ),
    "min_analyst_alignment": (
        "fundamentals",
        "analyst_alignment",
        "min",
    ),
    "min_eps_revision_alignment": (
        "fundamentals",
        "eps_revision_alignment",
        "min",
    ),
    "min_days_to_financial_event": (
        "fundamentals",
        "days_to_financial_event",
        "min",
    ),
    "min_days_to_corporate_action": (
        "fundamentals",
        "days_to_corporate_action",
        "min",
    ),
    "max_spread_bps": ("tradeability", "spread_bps", "max"),
    "min_top_of_book_notional": (
        "tradeability",
        "top_of_book_notional",
        "min",
    ),
    "max_initial_margin_ratio": (
        "margin_requirements",
        "initial_margin_ratio",
        "max",
    ),
    "max_days_to_cover": ("short_risk", "days_to_cover", "max"),
    "max_short_ratio": ("short_risk", "short_ratio", "max"),
    "min_short_selling_quantity": (
        "short_capacity",
        "short_selling_max_qty",
        "min",
    ),
}


class StockPickerFactorIncrementEvaluationService:
    """Evaluate frozen point-in-time factor filters after coverage is ready."""

    def __init__(
        self,
        bar_loader: Callable[..., List[Dict[str, Any]]] = (
            get_cached_candlesticks
        ),
        connection_factory: Callable = get_connection,
        now_provider: Callable[[], datetime] = (
            lambda: datetime.now(timezone.utc)
        ),
    ) -> None:
        self.bar_loader = bar_loader
        self.connection_factory = connection_factory
        self.now_provider = now_provider

    def run(
        self,
        market: str,
        pool_type: str,
        horizons: Iterable[int] = (5, 10, 20),
        lookback_days: int = 730,
        max_bars: int = 5000,
        minimum_observation_dates: int = MIN_OBSERVATION_DATES,
        minimum_distinct_symbols: int = MIN_DISTINCT_SYMBOLS,
        minimum_factor_coverage: float = MIN_FACTOR_COVERAGE,
        maximum_snapshot_age_hours: float = MAX_SNAPSHOT_AGE_HOURS,
        minimum_label_coverage: float = 0.9,
        minimum_paired_dates: int = 40,
        minimum_selected_per_date: int = 3,
        bootstrap_samples: int = DEFAULT_BOOTSTRAP_SAMPLES,
        bootstrap_confidence_level: float = (
            DEFAULT_BOOTSTRAP_CONFIDENCE_LEVEL
        ),
        bootstrap_block_size: Optional[int] = None,
        bootstrap_seed: int = DEFAULT_BOOTSTRAP_SEED,
        persist: bool = True,
    ) -> Dict[str, Any]:
        normalized_market = self._normalize_market(market)
        direction = self._normalize_direction(pool_type)
        normalized_horizons = self._validate_parameters(
            horizons=horizons,
            lookback_days=lookback_days,
            max_bars=max_bars,
            minimum_observation_dates=minimum_observation_dates,
            minimum_distinct_symbols=minimum_distinct_symbols,
            minimum_factor_coverage=minimum_factor_coverage,
            maximum_snapshot_age_hours=maximum_snapshot_age_hours,
            minimum_label_coverage=minimum_label_coverage,
            minimum_paired_dates=minimum_paired_dates,
            minimum_selected_per_date=minimum_selected_per_date,
            bootstrap_samples=bootstrap_samples,
            bootstrap_confidence_level=bootstrap_confidence_level,
            bootstrap_block_size=bootstrap_block_size,
            bootstrap_seed=bootstrap_seed,
        )
        now = self._as_utc(self.now_provider())
        cutoff = now - timedelta(days=lookback_days)
        raw_rows = self._load_rows(normalized_market, direction, cutoff)
        rows, exclusions = self._normalize_rows(raw_rows, now)
        dates = sorted({row["observation_date"] for row in rows})
        symbols = sorted({row["symbol"] for row in rows})
        latest = max(
            (row["observed_at"] for row in rows),
            default=None,
        )
        snapshot_age_hours = (
            (now - latest).total_seconds() / 3600
            if latest is not None
            else None
        )

        applicable_factors = self._applicable_factors(
            normalized_market,
            direction,
        )
        factor_coverage = self._factor_coverage(rows, applicable_factors)
        labels, label_exclusions, latest_label_date = self._build_labels(
            rows,
            normalized_horizons,
            max_bars,
        )
        exclusions.update(label_exclusions)
        label_counts = {
            str(horizon): sum(
                1 for row in rows if horizon in labels.get(row["key"], {})
            )
            for horizon in normalized_horizons
        }
        label_coverage = {
            key: self._safe_ratio(count, len(rows))
            for key, count in label_counts.items()
        }

        selection = self._selection_summary(
            rows,
            normalized_market,
            direction,
        )
        comparisons = self._build_comparisons(
            rows,
            labels,
            normalized_horizons,
            normalized_market,
            direction,
            minimum_selected_per_date,
        )
        paired_counts = {
            variant: {
                str(horizon): len(comparisons[variant][horizon])
                for horizon in normalized_horizons
            }
            for variant in FACTOR_VARIANTS
        }
        gate_reasons = self._gate_reasons(
            observation_dates=len(dates),
            distinct_symbols=len(symbols),
            snapshot_age_hours=snapshot_age_hours,
            factor_coverage=factor_coverage,
            label_coverage=label_coverage,
            paired_counts=paired_counts,
            horizons=normalized_horizons,
            minimum_observation_dates=minimum_observation_dates,
            minimum_distinct_symbols=minimum_distinct_symbols,
            minimum_factor_coverage=minimum_factor_coverage,
            maximum_snapshot_age_hours=maximum_snapshot_age_hours,
            minimum_label_coverage=minimum_label_coverage,
            minimum_paired_dates=minimum_paired_dates,
        )
        ready = not gate_reasons
        metrics = (
            {
                variant: {
                    str(horizon): self._summarize_comparisons(
                        comparisons[variant][horizon],
                        bootstrap_samples=bootstrap_samples,
                        confidence_level=bootstrap_confidence_level,
                        requested_block_size=bootstrap_block_size,
                        seed=(
                            bootstrap_seed
                            + horizon
                            + list(FACTOR_VARIANTS).index(variant) * 1000
                        ) % 4294967296,
                    )
                    for horizon in normalized_horizons
                }
                for variant in FACTOR_VARIANTS
            }
            if ready
            else None
        )

        report: Dict[str, Any] = {
            "evaluation_version": FACTOR_INCREMENT_EVALUATION_VERSION,
            "market": normalized_market,
            "pool_type": direction,
            "ready": ready,
            "parameters": {
                "horizons": normalized_horizons,
                "lookback_days": lookback_days,
                "max_bars": max_bars,
                "minimum_observation_dates": minimum_observation_dates,
                "minimum_distinct_symbols": minimum_distinct_symbols,
                "minimum_factor_coverage": minimum_factor_coverage,
                "maximum_snapshot_age_hours": maximum_snapshot_age_hours,
                "minimum_label_coverage": minimum_label_coverage,
                "minimum_paired_dates": minimum_paired_dates,
                "minimum_selected_per_date": minimum_selected_per_date,
                "bootstrap_samples": bootstrap_samples,
                "bootstrap_confidence_level": bootstrap_confidence_level,
                "bootstrap_block_size": bootstrap_block_size,
                "bootstrap_seed": bootstrap_seed,
                "snapshot_version": SNAPSHOT_VERSION,
                "thresholds": dict(FROZEN_THRESHOLDS),
                "variants": {
                    key: list(value)
                    for key, value in FACTOR_VARIANTS.items()
                },
            },
            "coverage": {
                "raw_snapshot_rows": len(raw_rows),
                "eligible_snapshot_rows": len(rows),
                "observation_dates": len(dates),
                "distinct_symbols": len(symbols),
                "latest_observed_at": (
                    latest.isoformat() if latest is not None else None
                ),
                "snapshot_age_hours": snapshot_age_hours,
                "applicable_factors": applicable_factors,
                "factor_coverage": factor_coverage,
                "labeled_records_by_horizon": label_counts,
                "label_coverage_by_horizon": label_coverage,
                "paired_dates_by_variant_horizon": paired_counts,
                "selection": selection,
                "excluded_rows": dict(sorted(exclusions.items())),
                "latest_label_date": latest_label_date,
                "gate_reasons": gate_reasons,
            },
            "metrics": metrics,
            "methodology": {
                "comparison": (
                    "每个市场日期等权比较全部可用快照股票与冻结过滤变体的"
                    "方向收益；不同日期再次等权汇总"
                ),
                "no_lookahead": (
                    "信号价必须是 observation_date 当日收盘价，收益标签仅使用"
                    "其后第 N 个交易日收盘价；缺少当日价或未来价时不平移日期"
                ),
                "deduplication": (
                    "同一市场、方向、股票和 observation_date 只保留 observed_at"
                    " 最新的 post_close v2 快照"
                ),
                "snapshot_integrity": (
                    "拒绝未来 observed_at，以及 observation_date 与 observed_at"
                    " 所在市场本地日期不一致的记录"
                ),
                "gate": (
                    "日期、股票、因子覆盖、快照新鲜度、标签覆盖或任一变体的"
                    "配对日期不足时 metrics 为 null"
                ),
                "inference": (
                    "按 observation_date 排序，使用循环移动块 bootstrap 估计"
                    "配对平均增量置信区间"
                ),
                "causal_limit": (
                    "过滤不是随机分配；报告描述关联增量，不构成 Fundamental"
                    " 或执行风险因子的独立因果证明，也未校正多变体和多持有期"
                    "比较"
                ),
                "costs": (
                    "收益未扣点差、冲击成本、借券费或资金占用；执行风险快照"
                    "只用于冻结资格过滤"
                ),
                "price_limit": (
                    "收益使用本地日 K 收盘价；分红再投资和复权口径未在快照中"
                    "单独版本化"
                ),
            },
        }
        if persist:
            report["id"] = self._save_report(report, latest_label_date)
        return report

    def get_history(self, limit: int = 20) -> List[Dict[str, Any]]:
        safe_limit = min(100, max(1, int(limit)))
        with self.connection_factory() as connection:
            rows = connection.execute(
                f"""
                SELECT
                    id, created_at, market, pool_type, evaluation_version,
                    parameters, result, ready, data_as_of
                FROM stock_picker_factor_evaluations
                ORDER BY created_at DESC, id DESC
                LIMIT {safe_limit}
                """
            ).fetchall()
        history = []
        for row in rows:
            try:
                parameters = json.loads(row[5])
                result = json.loads(row[6])
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            history.append({
                "id": int(row[0]),
                "created_at": str(row[1]),
                "market": row[2],
                "pool_type": row[3],
                "evaluation_version": row[4],
                "parameters": parameters,
                "result": result,
                "ready": bool(row[7]),
                "data_as_of": str(row[8]) if row[8] else None,
            })
        return history

    def _load_rows(
        self,
        market: str,
        direction: str,
        cutoff: datetime,
    ) -> List[tuple]:
        with self.connection_factory() as connection:
            return connection.execute(
                """
                SELECT
                    id, request_id, observed_at, observation_date,
                    snapshot_version, market, target_direction, symbol,
                    payload
                FROM stock_picker_factor_snapshots
                WHERE market = ?
                  AND target_direction = ?
                  AND observed_at >= ?
                ORDER BY observed_at, id
                """,
                [market, direction, cutoff.replace(tzinfo=None)],
            ).fetchall()

    def _normalize_rows(
        self,
        raw_rows: List[tuple],
        now: datetime,
    ) -> tuple[List[Dict[str, Any]], Counter[str]]:
        exclusions: Counter[str] = Counter()
        deduplicated: Dict[tuple[str, str], Dict[str, Any]] = {}
        for raw in raw_rows:
            if raw[4] != SNAPSHOT_VERSION:
                exclusions["unsupported_snapshot_version"] += 1
                continue
            try:
                payload = json.loads(raw[8])
            except (TypeError, ValueError, json.JSONDecodeError):
                exclusions["invalid_payload"] += 1
                continue
            if not isinstance(payload, dict):
                exclusions["invalid_payload"] += 1
                continue
            if (payload.get("capture") or {}).get("session_phase") != "post_close":
                exclusions["not_post_close"] += 1
                continue
            observed_at = self._as_utc(raw[2])
            if observed_at > now:
                exclusions["future_observed_at"] += 1
                continue
            observation_date = self._date_string(raw[3])
            if observation_date is None:
                exclusions["invalid_observation_date"] += 1
                continue
            market = str(raw[5]).strip().upper()
            if market not in MARKET_TIMEZONES:
                exclusions["invalid_market"] += 1
                continue
            local_date = observed_at.astimezone(
                ZoneInfo(MARKET_TIMEZONES[market])
            ).date().isoformat()
            if observation_date != local_date:
                exclusions["observation_date_mismatch"] += 1
                continue
            symbol = str(raw[7]).strip().upper()
            if not symbol:
                exclusions["invalid_symbol"] += 1
                continue
            row = {
                "id": int(raw[0]),
                "request_id": raw[1],
                "observed_at": observed_at,
                "observation_date": observation_date,
                "market": market,
                "pool_type": raw[6],
                "symbol": symbol,
                "payload": payload,
            }
            row["key"] = (observation_date, symbol)
            previous = deduplicated.get(row["key"])
            if previous is None or (
                observed_at,
                row["id"],
            ) > (
                previous["observed_at"],
                previous["id"],
            ):
                if previous is not None:
                    exclusions["duplicate_same_day_replaced"] += 1
                deduplicated[row["key"]] = row
            else:
                exclusions["duplicate_same_day_ignored"] += 1
        return sorted(
            deduplicated.values(),
            key=lambda item: (
                item["observation_date"],
                item["symbol"],
                item["id"],
            ),
        ), exclusions

    def _build_labels(
        self,
        rows: List[Dict[str, Any]],
        horizons: List[int],
        max_bars: int,
    ) -> tuple[
        Dict[tuple[str, str], Dict[int, Dict[str, Any]]],
        Counter[str],
        Optional[str],
    ]:
        cache: Dict[str, List[Dict[str, Any]]] = {}
        labels: Dict[tuple[str, str], Dict[int, Dict[str, Any]]] = {}
        exclusions: Counter[str] = Counter()
        latest_label_date = None
        for row in rows:
            symbol = row["symbol"]
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
            signal_index = next(
                (
                    index
                    for index, bar in enumerate(cache[symbol])
                    if self._bar_date(bar) == row["observation_date"]
                ),
                None,
            )
            if signal_index is None:
                exclusions["missing_signal_bar"] += 1
                labels[row["key"]] = {}
                continue
            signal_close = self._positive_close(cache[symbol][signal_index])
            if signal_close is None:
                exclusions["invalid_signal_close"] += 1
                labels[row["key"]] = {}
                continue
            direction = 1 if row["pool_type"] == "LONG" else -1
            row_labels = {}
            for horizon in horizons:
                future_index = signal_index + horizon
                if future_index >= len(cache[symbol]):
                    exclusions[f"missing_future_bar_{horizon}"] += 1
                    continue
                future_bar = cache[symbol][future_index]
                future_close = self._positive_close(future_bar)
                future_date = self._bar_date(future_bar)
                if future_close is None or future_date is None:
                    exclusions[f"invalid_future_bar_{horizon}"] += 1
                    continue
                row_labels[horizon] = {
                    "directional_return": direction * (
                        future_close / signal_close - 1
                    ),
                    "future_date": future_date,
                }
                if latest_label_date is None or future_date > latest_label_date:
                    latest_label_date = future_date
            labels[row["key"]] = row_labels
        return labels, exclusions, latest_label_date

    def _selection_summary(
        self,
        rows: List[Dict[str, Any]],
        market: str,
        direction: str,
    ) -> Dict[str, Any]:
        by_date: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for row in rows:
            by_date[row["observation_date"]].append(row)
        return {
            variant: {
                "selected_records": sum(
                    self._variant_matches(
                        row["payload"], variant, market, direction
                    )
                    for row in rows
                ),
                "total_records": len(rows),
                "selected_dates": sum(
                    any(
                        self._variant_matches(
                            row["payload"], variant, market, direction
                        )
                        for row in date_rows
                    )
                    for date_rows in by_date.values()
                ),
                "total_dates": len(by_date),
            }
            for variant in FACTOR_VARIANTS
        }

    def _build_comparisons(
        self,
        rows: List[Dict[str, Any]],
        labels: Dict[tuple[str, str], Dict[int, Dict[str, Any]]],
        horizons: List[int],
        market: str,
        direction: str,
        minimum_selected_per_date: int,
    ) -> Dict[str, Dict[int, List[Dict[str, Any]]]]:
        by_date: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for row in rows:
            by_date[row["observation_date"]].append(row)
        comparisons = {
            variant: {horizon: [] for horizon in horizons}
            for variant in FACTOR_VARIANTS
        }
        for observation_date, date_rows in sorted(by_date.items()):
            for horizon in horizons:
                if any(
                    horizon not in labels.get(row["key"], {})
                    for row in date_rows
                ):
                    continue
                baseline_values = [
                    labels[row["key"]][horizon]["directional_return"]
                    for row in date_rows
                ]
                baseline_return = mean(baseline_values)
                for variant in FACTOR_VARIANTS:
                    selected = [
                        row
                        for row in date_rows
                        if self._variant_matches(
                            row["payload"], variant, market, direction
                        )
                    ]
                    if len(selected) < minimum_selected_per_date:
                        continue
                    variant_return = mean(
                        labels[row["key"]][horizon]["directional_return"]
                        for row in selected
                    )
                    comparisons[variant][horizon].append({
                        "observation_date": observation_date,
                        "baseline_return": baseline_return,
                        "variant_return": variant_return,
                        "delta": variant_return - baseline_return,
                        "baseline_count": len(date_rows),
                        "selected_count": len(selected),
                    })
        return comparisons

    @classmethod
    def _variant_matches(
        cls,
        payload: Dict[str, Any],
        variant: str,
        market: str,
        direction: str,
    ) -> bool:
        for rule in FACTOR_VARIANTS[variant]:
            if rule in {"max_days_to_cover", "max_short_ratio"}:
                if direction != "SHORT":
                    continue
            if rule == "min_short_selling_quantity":
                if market != "US" or direction != "SHORT":
                    continue
            if rule == "require_tradable":
                if (payload.get("tradeability") or {}).get("is_tradable") is not True:
                    return False
                continue
            section, field, comparison = _RULES[rule]
            value = cls._finite_number((payload.get(section) or {}).get(field))
            threshold = FROZEN_THRESHOLDS[rule]
            if value is None:
                return False
            if comparison == "min" and value < threshold:
                return False
            if comparison == "max" and value > threshold:
                return False
        return True

    @staticmethod
    def _applicable_factors(market: str, direction: str) -> List[str]:
        return [
            factor
            for factor in FACTOR_REQUIREMENTS
            if (factor != "short_risk" or direction == "SHORT")
            and (
                factor != "short_capacity"
                or (market == "US" and direction == "SHORT")
            )
        ]

    @staticmethod
    def _factor_coverage(
        rows: List[Dict[str, Any]],
        factors: List[str],
    ) -> Dict[str, Dict[str, Any]]:
        return {
            factor: {
                "available_count": sum(
                    StockPickerFactorSnapshotService._factor_available(
                        row["payload"], factor
                    )
                    for row in rows
                ),
                "total_count": len(rows),
                "coverage": (
                    sum(
                        StockPickerFactorSnapshotService._factor_available(
                            row["payload"], factor
                        )
                        for row in rows
                    ) / len(rows)
                    if rows
                    else 0.0
                ),
            }
            for factor in factors
        }

    @staticmethod
    def _gate_reasons(
        *,
        observation_dates: int,
        distinct_symbols: int,
        snapshot_age_hours: Optional[float],
        factor_coverage: Dict[str, Dict[str, Any]],
        label_coverage: Dict[str, Optional[float]],
        paired_counts: Dict[str, Dict[str, int]],
        horizons: List[int],
        minimum_observation_dates: int,
        minimum_distinct_symbols: int,
        minimum_factor_coverage: float,
        maximum_snapshot_age_hours: float,
        minimum_label_coverage: float,
        minimum_paired_dates: int,
    ) -> List[str]:
        reasons = []
        if observation_dates < minimum_observation_dates:
            reasons.append("insufficient_observation_dates")
        if distinct_symbols < minimum_distinct_symbols:
            reasons.append("insufficient_distinct_symbols")
        if snapshot_age_hours is None:
            reasons.append("missing_latest_snapshot")
        elif snapshot_age_hours > maximum_snapshot_age_hours:
            reasons.append("stale_latest_snapshot")
        for factor, coverage in factor_coverage.items():
            if coverage["coverage"] < minimum_factor_coverage:
                reasons.append(f"insufficient_factor_coverage:{factor}")
        for horizon in horizons:
            key = str(horizon)
            if (label_coverage[key] or 0.0) < minimum_label_coverage:
                reasons.append(f"insufficient_label_coverage:{horizon}")
            for variant in FACTOR_VARIANTS:
                if paired_counts[variant][key] < minimum_paired_dates:
                    reasons.append(
                        f"insufficient_paired_dates:{variant}:{horizon}"
                    )
        return reasons

    def _summarize_comparisons(
        self,
        comparisons: List[Dict[str, Any]],
        *,
        bootstrap_samples: int,
        confidence_level: float,
        requested_block_size: Optional[int],
        seed: int,
    ) -> Dict[str, Any]:
        baseline = [item["baseline_return"] for item in comparisons]
        variant = [item["variant_return"] for item in comparisons]
        deltas = [item["delta"] for item in comparisons]
        return {
            "paired_dates": len(comparisons),
            "baseline": self._return_summary(baseline),
            "variant": self._return_summary(variant),
            "paired_delta": self._return_summary(deltas),
            "average_baseline_count": mean(
                item["baseline_count"] for item in comparisons
            ),
            "average_selected_count": mean(
                item["selected_count"] for item in comparisons
            ),
            "paired_delta_inference": self._block_bootstrap_inference(
                comparisons,
                bootstrap_samples=bootstrap_samples,
                confidence_level=confidence_level,
                requested_block_size=requested_block_size,
                seed=seed,
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
        ordered = sorted(
            comparisons,
            key=lambda item: item["observation_date"],
        )
        values = [item["delta"] for item in ordered]
        count = len(values)
        block_size = (
            requested_block_size
            if requested_block_size is not None
            else max(2, math.ceil(count ** (1 / 3)))
        )
        result = {
            "ready": False,
            "reason": None,
            "method": BLOCK_BOOTSTRAP_METHOD,
            "cluster_unit": "observation_date",
            "estimate": mean(values) if values else None,
            "confidence_level": confidence_level,
            "lower": None,
            "upper": None,
            "standard_error": None,
            "interval_direction": None,
            "bootstrap_samples": bootstrap_samples,
            "requested_block_size": requested_block_size,
            "effective_block_size": block_size,
            "distinct_observation_dates": count,
            "seed": seed,
        }
        if count < MINIMUM_BOOTSTRAP_DATES:
            result["reason"] = "insufficient_distinct_observation_dates"
            return result
        if block_size >= count:
            result["reason"] = "block_size_not_less_than_date_count"
            return result
        generator = random.Random(seed)
        bootstrap_means = []
        for _ in range(bootstrap_samples):
            sampled = []
            while len(sampled) < count:
                start = generator.randrange(count)
                take = min(block_size, count - len(sampled))
                sampled.extend(
                    values[(start + offset) % count]
                    for offset in range(take)
                )
            bootstrap_means.append(mean(sampled))
        alpha = 1 - confidence_level
        lower = self._percentile(bootstrap_means, alpha / 2)
        upper = self._percentile(bootstrap_means, 1 - alpha / 2)
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

    @classmethod
    def _return_summary(cls, values: List[float]) -> Dict[str, Any]:
        return {
            "sample_count": len(values),
            "average": mean(values) if values else None,
            "median": median(values) if values else None,
            "positive_rate": cls._safe_ratio(
                sum(value > 0 for value in values),
                len(values),
            ),
            "p05": cls._percentile(values, 0.05),
            "p95": cls._percentile(values, 0.95),
        }

    def _save_report(
        self,
        report: Dict[str, Any],
        data_as_of: Optional[str],
    ) -> int:
        result_payload = {
            key: value
            for key, value in report.items()
            if key not in {"parameters", "id"}
        }
        with self.connection_factory() as connection:
            row = connection.execute(
                """
                INSERT INTO stock_picker_factor_evaluations (
                    market, pool_type, evaluation_version, parameters,
                    result, ready, data_as_of
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                RETURNING id
                """,
                [
                    report["market"],
                    report["pool_type"],
                    report["evaluation_version"],
                    canonical_json(report["parameters"]),
                    canonical_json(result_payload),
                    report["ready"],
                    data_as_of,
                ],
            ).fetchone()
        return int(row[0])

    @staticmethod
    def _normalize_market(value: str) -> str:
        market = str(value).strip().upper()
        if market not in {"US", "HK"}:
            raise ValueError("market 必须是 US 或 HK")
        return market

    @staticmethod
    def _normalize_direction(value: str) -> str:
        direction = str(value).strip().upper()
        if direction not in {"LONG", "SHORT"}:
            raise ValueError("pool_type 必须是 LONG 或 SHORT")
        return direction

    @staticmethod
    def _validate_parameters(**parameters) -> List[int]:
        horizons = list(parameters["horizons"])
        if not horizons or any(
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 1 <= value <= 252
            for value in horizons
        ):
            raise ValueError("horizons 必须包含 1～252 的整数交易日")
        integer_bounds = {
            "lookback_days": (1, 3650),
            "max_bars": (2, 10000),
            "minimum_observation_dates": (1, 3650),
            "minimum_distinct_symbols": (1, 10000),
            "minimum_paired_dates": (1, 3650),
            "minimum_selected_per_date": (1, 10000),
            "bootstrap_samples": (200, 100000),
            "bootstrap_seed": (0, 4294967295),
        }
        for name, (lower, upper) in integer_bounds.items():
            value = parameters[name]
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not lower <= value <= upper
            ):
                raise ValueError(f"{name} 必须在 {lower}～{upper} 之间")
        for name in ("minimum_factor_coverage", "minimum_label_coverage"):
            value = parameters[name]
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not 0 <= value <= 1
            ):
                raise ValueError(f"{name} 必须在 0～1 之间")
        age = parameters["maximum_snapshot_age_hours"]
        if (
            isinstance(age, bool)
            or not isinstance(age, (int, float))
            or not 1 <= age <= 87600
        ):
            raise ValueError("maximum_snapshot_age_hours 必须在 1～87600 之间")
        confidence = parameters["bootstrap_confidence_level"]
        if (
            isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not 0.8 <= confidence <= 0.99
        ):
            raise ValueError("bootstrap_confidence_level 必须在 0.8～0.99 之间")
        block_size = parameters["bootstrap_block_size"]
        if block_size is not None and (
            isinstance(block_size, bool)
            or not isinstance(block_size, int)
            or not 2 <= block_size <= 3650
        ):
            raise ValueError("bootstrap_block_size 必须为空或在 2～3650 之间")
        return sorted(set(horizons))

    @staticmethod
    def _as_utc(value: Any) -> datetime:
        if not isinstance(value, datetime):
            value = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    @staticmethod
    def _date_string(value: Any) -> Optional[str]:
        if isinstance(value, datetime):
            return value.date().isoformat()
        if isinstance(value, date):
            return value.isoformat()
        try:
            return date.fromisoformat(str(value)).isoformat()
        except (TypeError, ValueError):
            return None

    @classmethod
    def _bar_date(cls, bar: Dict[str, Any]) -> Optional[str]:
        value = bar.get("ts") or bar.get("timestamp") or bar.get("date")
        if isinstance(value, datetime):
            return value.date().isoformat()
        return cls._date_string(value)

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
    def _safe_ratio(numerator: int, denominator: int) -> Optional[float]:
        return numerator / denominator if denominator else None

    @staticmethod
    def _percentile(values: List[float], probability: float) -> Optional[float]:
        if not values:
            return None
        ordered = sorted(values)
        if len(ordered) == 1:
            return ordered[0]
        position = probability * (len(ordered) - 1)
        lower = math.floor(position)
        upper = math.ceil(position)
        if lower == upper:
            return ordered[lower]
        fraction = position - lower
        return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


_stock_picker_factor_evaluation_service: Optional[
    StockPickerFactorIncrementEvaluationService
] = None


def get_stock_picker_factor_evaluation_service(
) -> StockPickerFactorIncrementEvaluationService:
    global _stock_picker_factor_evaluation_service
    if _stock_picker_factor_evaluation_service is None:
        _stock_picker_factor_evaluation_service = (
            StockPickerFactorIncrementEvaluationService()
        )
    return _stock_picker_factor_evaluation_service
