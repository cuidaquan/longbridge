from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from typing import Any, Callable, Dict, List, Optional

from .db import get_connection
from .stock_picker_backtest import (
    StockPickerBacktestService,
    get_stock_picker_backtest_service,
)
from .stock_picker_baseline import (
    BASELINE_PARAMETERS,
    BASELINE_UNIVERSES,
    BASELINE_VERSION,
)


EXPERIMENT_VERSION = "stock-picker-market-rs-ablation-v1"
MARKET_RS_VARIANTS = (
    {
        "name": "quant",
        "label": "纯量化 Top N",
        "required_features": [],
    },
    {
        "name": "market_rs_10d",
        "label": "量化 Top N + 10 日市场 RS 非负",
        "required_features": ["market_rs_10d"],
    },
    {
        "name": "market_rs_half_year",
        "label": "量化 Top N + 半年市场 RS 非负",
        "required_features": ["market_rs_half_year"],
    },
    {
        "name": "market_rs_both",
        "label": "量化 Top N + 10 日及半年市场 RS 均非负",
        "required_features": [
            "market_rs_10d",
            "market_rs_half_year",
        ],
    },
)
EXPERIMENT_LIMITATIONS = [
    "本实验只改变 Top N 候选资格，不修改生产评分权重",
    "市场 RS 使用信号日前 10 和 120 个交易日的股票收益减基准收益",
    "半年窗口固定为 120 个交易日，与上游实时指标口径可能存在细微差异",
    (
        "行业 RS 依赖历史时点行业成员，现有数据库无法无偏重建，"
        "本实验不评估"
    ),
    (
        "Fundamental、盘口、交易状态和保证金缺少历史点时快照，"
        "本实验不使用当前值回填"
    ),
]


class StockPickerFactorExperimentService:
    def __init__(
        self,
        backtest_service: Optional[StockPickerBacktestService] = None,
        connection_factory: Callable = get_connection,
    ) -> None:
        self.backtest_service = (
            backtest_service or get_stock_picker_backtest_service()
        )
        self.connection_factory = connection_factory

    def run(self, persist: bool = True) -> Dict[str, Any]:
        groups = []
        score_versions = set()
        for market, universe in BASELINE_UNIVERSES.items():
            for pool_type in ("LONG", "SHORT"):
                reports = []
                for variant in MARKET_RS_VARIANTS:
                    required = list(variant["required_features"])
                    report = self.backtest_service.run(
                        pool_type,
                        symbols=list(universe["symbols"]),
                        persist=False,
                        report_metadata={
                            "experiment_version": EXPERIMENT_VERSION,
                            "baseline_version": BASELINE_VERSION,
                            "market": market,
                            "variant": variant["name"],
                            "required_features": required,
                        },
                        top_n_filter=(
                            self._build_filter(required)
                            if required
                            else None
                        ),
                        **BASELINE_PARAMETERS,
                    )
                    score_versions.add(str(report["score_version"]))
                    reports.append((variant, report))
                groups.append(
                    self._summarize_group(
                        market,
                        pool_type,
                        universe,
                        reports,
                    )
                )

        snapshot = {
            "experiment_version": EXPERIMENT_VERSION,
            "baseline_version": BASELINE_VERSION,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "score_versions": sorted(score_versions),
            "parameters": {
                **dict(BASELINE_PARAMETERS),
                "variants": [
                    {
                        "name": variant["name"],
                        "label": variant["label"],
                        "required_features": list(
                            variant["required_features"]
                        ),
                        "minimum_value": (
                            0
                            if variant["required_features"]
                            else None
                        ),
                    }
                    for variant in MARKET_RS_VARIANTS
                ],
            },
            "limitations": list(EXPERIMENT_LIMITATIONS),
            "groups": groups,
        }
        if persist:
            snapshot["id"] = self._save_snapshot(snapshot)
        return snapshot

    def get_history(self, limit: int = 20) -> List[Dict[str, Any]]:
        safe_limit = min(100, max(1, int(limit)))
        with self.connection_factory() as connection:
            rows = connection.execute(
                f"""
                SELECT
                    id, created_at, experiment_version, baseline_version,
                    score_version, data_as_of, parameters, result
                FROM stock_picker_factor_experiments
                ORDER BY created_at DESC, id DESC
                LIMIT {safe_limit}
                """
            ).fetchall()
        return [
            {
                "id": row[0],
                "created_at": str(row[1]),
                "experiment_version": row[2],
                "baseline_version": row[3],
                "score_version": row[4],
                "data_as_of": str(row[5]),
                "parameters": json.loads(row[6]),
                "result": json.loads(row[7]),
            }
            for row in rows
        ]

    @staticmethod
    def _build_filter(
        required_features: List[str],
    ) -> Callable[[Dict[str, Any]], bool]:
        def passes(record: Dict[str, Any]) -> bool:
            features = record.get("features") or {}
            return all(
                features.get(feature) is not None
                and features[feature] >= 0
                for feature in required_features
            )

        return passes

    def _summarize_group(
        self,
        market: str,
        pool_type: str,
        universe: Dict[str, Any],
        reports: List[tuple[Dict[str, Any], Dict[str, Any]]],
    ) -> Dict[str, Any]:
        baseline_report = reports[0][1]
        baseline_metrics = self._validation_metrics(baseline_report)
        variants = []
        for variant, report in reports:
            metrics = self._validation_metrics(report)
            deltas = {
                horizon: {
                    "avg_net_return": self._difference(
                        values["avg_net_return"],
                        baseline_metrics[horizon]["avg_net_return"],
                    ),
                    "avg_excess_return": self._difference(
                        values["avg_excess_return"],
                        baseline_metrics[horizon][
                            "avg_excess_return"
                        ],
                    ),
                    "top_n_lift": self._difference(
                        values["top_n_lift"],
                        baseline_metrics[horizon]["top_n_lift"],
                    ),
                }
                for horizon, values in metrics.items()
            }
            variants.append({
                "name": variant["name"],
                "label": variant["label"],
                "required_features": list(
                    variant["required_features"]
                ),
                "selection": dict(report["selection"]["validation"]),
                "validation": metrics,
                "delta_vs_quant": deltas,
                "passes_frozen_return_gate": all(
                    values["avg_net_return"] is not None
                    and values["avg_net_return"] > 0
                    and values["avg_excess_return"] is not None
                    and values["avg_excess_return"] > 0
                    and values["top_n_lift"] is not None
                    and values["top_n_lift"] > 0
                    and values["positive_walk_forward_folds"] >= 2
                    for values in metrics.values()
                ),
            })
        data = baseline_report["data"]
        return {
            "market": market,
            "pool_type": pool_type,
            "universe_name": universe["name"],
            "symbols": list(universe["symbols"]),
            "data": dict(data),
            "quality_gate": {
                "benchmark_coverage_at_least_95pct": (
                    data["benchmark_coverage"] >= 0.95
                ),
                "symbol_coverage_at_least_90pct": (
                    data["symbols_evaluated"]
                    / data["symbols_requested"]
                    >= 0.9
                ),
                "validation_signal_dates_at_least_12": (
                    baseline_report["periods"]["validation"][
                        "signal_dates"
                    ] >= 12
                ),
            },
            "variants": variants,
        }

    @staticmethod
    def _validation_metrics(
        report: Dict[str, Any],
    ) -> Dict[str, Dict[str, Any]]:
        validation = report["periods"]["validation"]
        metrics = {}
        for horizon in report["parameters"]["horizons"]:
            key = str(horizon)
            all_metrics = validation["all"]["horizons"][key]
            top_metrics = validation["top_n"]["horizons"][key]
            top_net = top_metrics["avg_net_return"]
            all_net = all_metrics["avg_net_return"]
            fold_returns = [
                fold["validation_metrics"]["top_n"]["horizons"][
                    key
                ]["avg_net_return"]
                for fold in report["walk_forward"]
            ]
            metrics[key] = {
                "sample_count": top_metrics["sample_count"],
                "avg_net_return": top_net,
                "avg_excess_return": top_metrics[
                    "avg_excess_return"
                ],
                "hit_rate": top_metrics["hit_rate"],
                "top_n_lift": (
                    top_net - all_net
                    if top_net is not None and all_net is not None
                    else None
                ),
                "max_drawdown": top_metrics["max_drawdown"],
                "positive_walk_forward_folds": sum(
                    1
                    for value in fold_returns
                    if value is not None and value > 0
                ),
                "walk_forward_folds_with_data": sum(
                    1
                    for value in fold_returns
                    if value is not None
                ),
            }
        return metrics

    def _save_snapshot(self, snapshot: Dict[str, Any]) -> int:
        score_version = ",".join(snapshot["score_versions"])
        result = {
            key: value
            for key, value in snapshot.items()
            if key not in {"id", "parameters"}
        }
        with self.connection_factory() as connection:
            row = connection.execute(
                """
                INSERT INTO stock_picker_factor_experiments (
                    experiment_version, baseline_version, score_version,
                    data_as_of, parameters, result
                ) VALUES (?, ?, ?, ?, ?, ?)
                RETURNING id
                """,
                (
                    snapshot["experiment_version"],
                    snapshot["baseline_version"],
                    score_version,
                    snapshot["parameters"]["data_as_of"],
                    json.dumps(
                        snapshot["parameters"],
                        ensure_ascii=False,
                        default=str,
                    ),
                    json.dumps(
                        result,
                        ensure_ascii=False,
                        default=str,
                    ),
                ),
            ).fetchone()
        return int(row[0])

    @staticmethod
    def _difference(
        value: Optional[float],
        baseline: Optional[float],
    ) -> Optional[float]:
        if value is None or baseline is None:
            return None
        return value - baseline


_factor_experiment_service: Optional[
    StockPickerFactorExperimentService
] = None


def get_stock_picker_factor_experiment_service(
) -> StockPickerFactorExperimentService:
    global _factor_experiment_service
    if _factor_experiment_service is None:
        _factor_experiment_service = StockPickerFactorExperimentService()
    return _factor_experiment_service


def main() -> None:
    parser = argparse.ArgumentParser(
        description="运行智能选股市场 RS 样本外消融实验",
    )
    parser.add_argument(
        "--no-persist",
        action="store_true",
        help="只计算并输出结果，不写入实验历史",
    )
    args = parser.parse_args()
    snapshot = get_stock_picker_factor_experiment_service().run(
        persist=not args.no_persist,
    )
    print(json.dumps(snapshot, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
