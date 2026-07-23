from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from typing import Any, Dict, Iterable, List, Optional

from .services import sync_history_candlesticks
from .stock_picker_backtest import (
    StockPickerBacktestService,
    get_stock_picker_backtest_service,
)
from .stock_screener import DEFAULT_MARKET_BENCHMARKS


BASELINE_VERSION = "stock-picker-baseline-v1"
BASELINE_DATA_AS_OF = "2026-07-23"
BASELINE_UNIVERSES = {
    "US": {
        "name": "us-large-cap-10",
        "symbols": [
            "AAPL.US",
            "MSFT.US",
            "GOOGL.US",
            "AMZN.US",
            "META.US",
            "NVDA.US",
            "TSLA.US",
            "JPM.US",
            "XOM.US",
            "JNJ.US",
        ],
    },
    "HK": {
        "name": "hk-large-cap-10",
        "symbols": [
            "700.HK",
            "9988.HK",
            "3690.HK",
            "1299.HK",
            "941.HK",
            "5.HK",
            "388.HK",
            "2318.HK",
            "1211.HK",
            "1810.HK",
        ],
    },
}
BASELINE_PARAMETERS = {
    "horizons": [5, 10, 20],
    "lookback": 250,
    "max_bars": 1000,
    "min_history": 250,
    "step": 20,
    "top_n": 3,
    "train_ratio": 0.7,
    "walk_forward_folds": 3,
    "transaction_cost_bps": 10,
    "data_as_of": BASELINE_DATA_AS_OF,
}
BASELINE_LIMITATIONS = [
    "股票宇宙按当前大盘股名单冻结，不是历史时点成分股，存在幸存者偏差",
    "行情使用数据库日 K 收盘价，不含分红再投资，复权口径未单独版本化",
    "只评估固定技术评分，不包含 AI、新闻、Screener RS、Fundamental 或执行风险过滤",
    "交易成本只按每条信号一次性扣减，不含点差、冲击成本、借券费和资金占用",
]


def baseline_history_symbols() -> List[str]:
    symbols: List[str] = []
    for market, universe in BASELINE_UNIVERSES.items():
        symbols.extend(universe["symbols"])
        symbols.append(DEFAULT_MARKET_BENCHMARKS[market])
    return list(dict.fromkeys(symbols))


def sync_stock_picker_baseline_history(
    count: int = 1000,
) -> Dict[str, int]:
    return sync_history_candlesticks(
        symbols=baseline_history_symbols(),
        period="day",
        adjust_type="no_adjust",
        count=count,
        continue_on_error=True,
    )


def run_stock_picker_baselines(
    service: Optional[StockPickerBacktestService] = None,
    persist: bool = True,
) -> List[Dict[str, Any]]:
    _validate_baseline_definition()
    evaluator = service or get_stock_picker_backtest_service()
    reports = []
    for market, universe in BASELINE_UNIVERSES.items():
        for pool_type in ("LONG", "SHORT"):
            reports.append(evaluator.run(
                pool_type,
                symbols=list(universe["symbols"]),
                persist=persist,
                report_metadata={
                    "baseline_version": BASELINE_VERSION,
                    "market": market,
                    "universe_name": universe["name"],
                    "universe_selection": (
                        "当前大盘股固定名单；非历史时点成分股"
                    ),
                    "limitations": list(BASELINE_LIMITATIONS),
                },
                **BASELINE_PARAMETERS,
            ))
    return reports


def build_stock_picker_baseline_snapshot(
    reports: Iterable[Dict[str, Any]],
) -> Dict[str, Any]:
    report_list = list(reports)
    score_versions = sorted({
        str(report["score_version"])
        for report in report_list
    })
    return {
        "baseline_version": BASELINE_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "score_versions": score_versions,
        "parameters": dict(BASELINE_PARAMETERS),
        "limitations": list(BASELINE_LIMITATIONS),
        "reports": [
            _summarize_report(report)
            for report in report_list
        ],
    }


def _summarize_report(report: Dict[str, Any]) -> Dict[str, Any]:
    validation = report["periods"]["validation"]
    metadata = report.get("metadata") or {}
    horizons = {}
    for horizon in report["parameters"]["horizons"]:
        key = str(horizon)
        all_metrics = validation["all"]["horizons"][key]
        top_metrics = validation["top_n"]["horizons"][key]
        all_net = all_metrics["avg_net_return"]
        top_net = top_metrics["avg_net_return"]
        horizons[key] = {
            "sample_count": top_metrics["sample_count"],
            "all_avg_net_return": all_net,
            "top_n_avg_net_return": top_net,
            "top_n_lift": (
                top_net - all_net
                if top_net is not None and all_net is not None
                else None
            ),
            "top_n_hit_rate": top_metrics["hit_rate"],
            "top_n_avg_excess_return": (
                top_metrics["avg_excess_return"]
            ),
            "top_n_excess_coverage": top_metrics["excess_coverage"],
            "top_n_max_drawdown": top_metrics["max_drawdown"],
        }
    return {
        "id": report.get("id"),
        "market": metadata.get("market"),
        "pool_type": report["pool_type"],
        "score_version": report["score_version"],
        "universe_name": metadata.get("universe_name"),
        "symbols": list(report["parameters"]["symbols"]),
        "data": dict(report["data"]),
        "validation_signal_dates": validation["signal_dates"],
        "validation": horizons,
        "walk_forward": [
            {
                "fold": fold["fold"],
                "validation_start": fold["validation_start"],
                "validation_end": fold["validation_end"],
                "top_n_avg_net_return": {
                    str(horizon): (
                        fold["validation_metrics"]["top_n"][
                            "horizons"
                        ][str(horizon)]["avg_net_return"]
                    )
                    for horizon in report["parameters"]["horizons"]
                },
            }
            for fold in report["walk_forward"]
        ],
    }


def _validate_baseline_definition() -> None:
    horizons = BASELINE_PARAMETERS["horizons"]
    if BASELINE_PARAMETERS["step"] < max(horizons):
        raise ValueError("基线 step 必须大于等于最长持有期，避免样本重叠")
    for market, universe in BASELINE_UNIVERSES.items():
        symbols = universe["symbols"]
        if not symbols or len(symbols) != len(set(symbols)):
            raise ValueError(f"{market} 基线股票宇宙为空或包含重复代码")
        if BASELINE_PARAMETERS["top_n"] > len(symbols):
            raise ValueError(f"{market} 基线 Top N 超过股票宇宙大小")
        benchmark = DEFAULT_MARKET_BENCHMARKS.get(market)
        if not benchmark:
            raise ValueError(f"{market} 缺少市场基准")
        if benchmark in symbols:
            raise ValueError(f"{market} 市场基准不能进入选股宇宙")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="运行可复现的智能选股 US/HK LONG/SHORT 基线",
    )
    parser.add_argument(
        "--sync-history",
        action="store_true",
        help="运行基线前同步固定股票宇宙与市场基准的 1000 根日 K",
    )
    parser.add_argument(
        "--no-persist",
        action="store_true",
        help="只计算并输出结果，不写入 stock_picker_backtests",
    )
    args = parser.parse_args()
    sync_result = (
        sync_stock_picker_baseline_history()
        if args.sync_history
        else None
    )
    reports = run_stock_picker_baselines(
        persist=not args.no_persist,
    )
    snapshot = build_stock_picker_baseline_snapshot(reports)
    if sync_result is not None:
        snapshot["history_sync"] = sync_result
    print(json.dumps(snapshot, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
