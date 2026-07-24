from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
import json
import math
from statistics import mean, median, pstdev
from typing import Any, Callable, Dict, Iterable, List, Optional

from .db import get_connection
from .services import get_cached_candlesticks
from .stock_picker import StockPickerService, get_stock_picker_service
from .stock_screener import DEFAULT_MARKET_BENCHMARKS


EXECUTION_COST_MODEL_VERSION = "square-root-daily-volatility-v1"
MARKET_CURRENCIES = {
    "US": "USD",
    "HK": "HKD",
    "CN": "CNY",
    "SG": "SGD",
}


class StockPickerBacktestService:
    """Walk-forward evaluation for the deterministic stock-picker score."""

    def __init__(
        self,
        stock_picker: Optional[StockPickerService] = None,
        bar_loader: Callable[..., List[Dict[str, Any]]] = get_cached_candlesticks,
        connection_factory: Callable = get_connection,
    ) -> None:
        self.stock_picker = stock_picker or get_stock_picker_service()
        self.bar_loader = bar_loader
        self.connection_factory = connection_factory

    def run(
        self,
        pool_type: str,
        symbols: Optional[List[str]] = None,
        horizons: Iterable[int] = (5, 10, 20),
        lookback: int = 250,
        max_bars: int = 1000,
        min_history: int = 60,
        step: int = 5,
        top_n: int = 5,
        train_ratio: float = 0.7,
        walk_forward_folds: int = 3,
        transaction_cost_bps: float = 10.0,
        order_notional: Optional[float] = None,
        max_participation_rate: float = 0.1,
        impact_coefficient: float = 0.5,
        impact_volatility_lookback: int = 20,
        persist: bool = True,
        report_metadata: Optional[Dict[str, Any]] = None,
        data_as_of: Optional[str] = None,
        top_n_filter: Optional[Callable[[Dict[str, Any]], bool]] = None,
    ) -> Dict[str, Any]:
        direction = self.stock_picker._validate_pool_type(pool_type)
        normalized_horizons = sorted(set(int(value) for value in horizons))
        normalized_data_as_of = self._normalize_data_as_of(data_as_of)
        self._validate_parameters(
            normalized_horizons,
            lookback,
            max_bars,
            min_history,
            step,
            top_n,
            train_ratio,
            walk_forward_folds,
            transaction_cost_bps,
            order_notional,
            max_participation_rate,
            impact_coefficient,
            impact_volatility_lookback,
        )
        selected_symbols = self._resolve_symbols(direction, symbols)
        if not selected_symbols:
            raise ValueError(f"{direction} 股票池没有可回测的股票")

        bars_by_symbol = {
            symbol: self._load_bars(
                symbol,
                max_bars,
                normalized_data_as_of,
            )
            for symbol in selected_symbols
        }
        market_benchmarks = {
            self._market_for_symbol(symbol): DEFAULT_MARKET_BENCHMARKS[
                self._market_for_symbol(symbol)
            ]
            for symbol in selected_symbols
        }
        benchmark_bars = {
            benchmark: self._load_bars(
                benchmark,
                max_bars,
                normalized_data_as_of,
            )
            for benchmark in sorted(set(market_benchmarks.values()))
        }
        benchmark_closes = {
            benchmark: {
                self._bar_date(bar): self._positive_close(bar)
                for bar in bars
                if self._positive_close(bar) is not None
            }
            for benchmark, bars in benchmark_bars.items()
        }

        records: List[Dict[str, Any]] = []
        skipped_symbols: Dict[str, str] = {}
        max_horizon = max(normalized_horizons)
        for symbol, bars in bars_by_symbol.items():
            if len(bars) < min_history + max_horizon:
                skipped_symbols[symbol] = (
                    f"数据不足: {len(bars)} < {min_history + max_horizon}"
                )
                continue
            benchmark = market_benchmarks[self._market_for_symbol(symbol)]
            symbol_records = self._evaluate_symbol(
                symbol,
                direction,
                bars,
                benchmark,
                benchmark_closes.get(benchmark, {}),
                normalized_horizons,
                lookback,
                min_history,
                step,
                order_notional,
                max_participation_rate,
                impact_coefficient,
                impact_volatility_lookback,
            )
            if symbol_records:
                records.extend(symbol_records)
            else:
                skipped_symbols[symbol] = "没有可评估的信号日期"

        if not records:
            raise ValueError("没有足够的历史数据生成回测样本")

        records.sort(key=lambda item: (item["signal_date"], item["symbol"]))
        execution_eligible_records = [
            record
            for record in records
            if record["execution"]["eligible"]
        ]
        eligible_records = (
            [
                record
                for record in execution_eligible_records
                if top_n_filter(record)
            ]
            if top_n_filter is not None
            else execution_eligible_records
        )
        top_records = self._select_top_n(eligible_records, top_n)
        signal_dates = sorted({record["signal_date"] for record in records})
        train_dates, validation_dates = self._split_dates(
            signal_dates,
            train_ratio,
        )
        cost_rate = transaction_cost_bps / 10000

        report = {
            "score_version": self.stock_picker.SCORE_VERSION,
            "pool_type": direction,
            "parameters": {
                "symbols": selected_symbols,
                "horizons": normalized_horizons,
                "lookback": lookback,
                "max_bars": max_bars,
                "min_history": min_history,
                "step": step,
                "top_n": top_n,
                "train_ratio": train_ratio,
                "walk_forward_folds": walk_forward_folds,
                "transaction_cost_bps": transaction_cost_bps,
                "order_notional": order_notional,
                "max_participation_rate": max_participation_rate,
                "impact_coefficient": impact_coefficient,
                "impact_volatility_lookback": impact_volatility_lookback,
                "data_as_of": normalized_data_as_of,
                "market_benchmarks": market_benchmarks,
            },
            "data": {
                "signal_start": signal_dates[0],
                "signal_end": signal_dates[-1],
                "data_as_of": max(
                    self._bar_date(bar)
                    for bars in bars_by_symbol.values()
                    for bar in bars
                ),
                "symbols_requested": len(selected_symbols),
                "symbols_evaluated": len({
                    record["symbol"]
                    for record in records
                }),
                "skipped_symbols": skipped_symbols,
                "sample_count": len(records),
                "top_n_sample_count": len(top_records),
                "benchmark_coverage": self._benchmark_coverage(
                    execution_eligible_records
                ),
            },
            "execution": self._execution_summary(
                records,
                order_notional,
            ),
            "selection": {
                "all": self._selection_summary(
                    records,
                    eligible_records,
                    set(signal_dates),
                    top_n,
                ),
                "validation": self._selection_summary(
                    records,
                    eligible_records,
                    set(validation_dates),
                    top_n,
                ),
            },
            "periods": {
                "train": self._summarize_period(
                    execution_eligible_records,
                    top_records,
                    set(train_dates),
                    normalized_horizons,
                    cost_rate,
                ),
                "validation": self._summarize_period(
                    execution_eligible_records,
                    top_records,
                    set(validation_dates),
                    normalized_horizons,
                    cost_rate,
                ),
                "all": self._summarize_period(
                    execution_eligible_records,
                    top_records,
                    set(signal_dates),
                    normalized_horizons,
                    cost_rate,
                ),
            },
            "walk_forward": self._walk_forward_report(
                execution_eligible_records,
                top_records,
                signal_dates,
                validation_dates,
                normalized_horizons,
                cost_rate,
                walk_forward_folds,
            ),
            "methodology": {
                "no_lookahead": (
                    "每个信号仅使用该交易日及之前最多 lookback 根日 K"
                ),
                "directional_return": (
                    "LONG 使用未来涨幅，SHORT 使用未来涨幅的相反数"
                ),
                "top_n": "每个信号日按方向化技术机会分降序选择",
                "transaction_cost": "每条信号收益一次性扣减 transaction_cost_bps",
                "execution_cost": (
                    "order_notional 为 null 时不启用动态成本；启用时仅使用"
                    "信号日及之前的成交额，以及最近指定窗口收盘到收盘日收益的"
                    "非年化总体标准差，按平方根参与率模型估算冲击成本，并在"
                    "Top N 前排除缺失或参与率超限样本"
                ),
                "industry_or_market_calibration": (
                    "本报告只评估当前固定评分版本，不使用验证期调参"
                ),
                "overlap_warning": (
                    "step 小于持有期时样本会重叠；最大回撤为信号日组合近似值"
                ),
            },
        }
        if report_metadata:
            report["metadata"] = dict(report_metadata)
        if persist:
            report["id"] = self._save_report(report)
        return report

    def get_history(self, limit: int = 20) -> List[Dict[str, Any]]:
        safe_limit = min(100, max(1, int(limit)))
        with self.connection_factory() as connection:
            rows = connection.execute(
                f"""
                SELECT
                    id, created_at, pool_type, score_version,
                    parameters, result, data_as_of
                FROM stock_picker_backtests
                ORDER BY created_at DESC, id DESC
                LIMIT {safe_limit}
                """
            ).fetchall()
        return [
            {
                "id": row[0],
                "created_at": str(row[1]),
                "pool_type": row[2],
                "score_version": row[3],
                "parameters": json.loads(row[4]),
                "result": json.loads(row[5]),
                "data_as_of": str(row[6]) if row[6] else None,
            }
            for row in rows
        ]

    def _resolve_symbols(
        self,
        pool_type: str,
        symbols: Optional[List[str]],
    ) -> List[str]:
        if symbols:
            return list(dict.fromkeys(
                self.stock_picker._normalize_symbol(symbol)
                for symbol in symbols
            ))
        pools = self.stock_picker.get_pools(pool_type)
        key = "long_pool" if pool_type == "LONG" else "short_pool"
        return [
            self.stock_picker._normalize_symbol(item["symbol"])
            for item in pools[key]
            if item.get("is_active", True)
        ]

    def _load_bars(
        self,
        symbol: str,
        limit: int,
        data_as_of: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        bars = self.bar_loader(
            symbol,
            period="day",
            limit=limit,
            end_date=data_as_of,
        ) or []
        return sorted(
            (
                bar
                for bar in bars
                if self._positive_close(bar) is not None
            ),
            key=self._bar_date,
        )

    @staticmethod
    def _normalize_data_as_of(value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        try:
            return date.fromisoformat(str(value)).isoformat()
        except ValueError as exc:
            raise ValueError("data_as_of 必须是 YYYY-MM-DD 日期") from exc

    def _evaluate_symbol(
        self,
        symbol: str,
        pool_type: str,
        bars: List[Dict[str, Any]],
        benchmark_symbol: str,
        benchmark_closes: Dict[str, float],
        horizons: List[int],
        lookback: int,
        min_history: int,
        step: int,
        order_notional: Optional[float] = None,
        max_participation_rate: float = 0.1,
        impact_coefficient: float = 0.5,
        impact_volatility_lookback: int = 20,
    ) -> List[Dict[str, Any]]:
        direction = 1 if pool_type == "LONG" else -1
        max_horizon = max(horizons)
        records = []
        for end_index in range(
            min_history - 1,
            len(bars) - max_horizon,
            step,
        ):
            history = bars[
                max(0, end_index - lookback + 1):end_index + 1
            ]
            score = self.stock_picker._calculate_advanced_score_v2(
                history,
                pool_type,
            )
            current = self._positive_close(bars[end_index])
            if current is None:
                continue
            signal_date = self._bar_date(bars[end_index])
            features = self._market_relative_strength_features(
                bars,
                end_index,
                benchmark_closes,
                direction,
            )
            execution = self._execution_features(
                bars,
                end_index,
                order_notional,
                max_participation_rate,
                impact_coefficient,
                impact_volatility_lookback,
            )
            horizon_returns = {}
            for horizon in horizons:
                future_bar = bars[end_index + horizon]
                future = self._positive_close(future_bar)
                if future is None:
                    continue
                gross = direction * (future / current - 1)
                future_date = self._bar_date(future_bar)
                benchmark_current = benchmark_closes.get(signal_date)
                benchmark_future = benchmark_closes.get(future_date)
                excess = None
                if (
                    benchmark_current is not None
                    and benchmark_future is not None
                    and benchmark_current > 0
                ):
                    benchmark_directional = direction * (
                        benchmark_future / benchmark_current - 1
                    )
                    excess = gross - benchmark_directional
                horizon_returns[str(horizon)] = {
                    "gross_return": gross,
                    "excess_return": excess,
                    "future_date": future_date,
                }
            if len(horizon_returns) != len(horizons):
                continue
            records.append({
                "symbol": symbol,
                "signal_date": signal_date,
                "score": float(score["total"]),
                "grade": score["grade"],
                "benchmark_symbol": benchmark_symbol,
                "features": features,
                "execution": execution,
                "returns": horizon_returns,
            })
        return records

    def _execution_features(
        self,
        bars: List[Dict[str, Any]],
        end_index: int,
        order_notional: Optional[float],
        max_participation_rate: float,
        impact_coefficient: float,
        volatility_lookback: int,
    ) -> Dict[str, Any]:
        if order_notional is None:
            return {
                "enabled": False,
                "eligible": True,
                "exclusion_reason": None,
                "order_notional": None,
                "signal_turnover": None,
                "turnover_source": None,
                "participation_rate": None,
                "historical_daily_volatility": None,
                "dynamic_cost_rate": 0.0,
            }

        signal_bar = bars[end_index]
        signal_turnover = self._positive_number(
            signal_bar.get("turnover")
        )
        turnover_source = "turnover" if signal_turnover is not None else None
        if signal_turnover is None:
            close = self._positive_close(signal_bar)
            volume = self._positive_number(signal_bar.get("volume"))
            if close is not None and volume is not None:
                signal_turnover = close * volume
                turnover_source = "close_x_volume"

        volatility = self._historical_daily_volatility(
            bars,
            end_index,
            volatility_lookback,
        )
        participation_rate = (
            order_notional / signal_turnover
            if signal_turnover is not None
            else None
        )
        dynamic_cost_rate = (
            impact_coefficient
            * volatility
            * math.sqrt(participation_rate)
            if (
                volatility is not None
                and participation_rate is not None
            )
            else None
        )

        exclusion_reason = None
        if signal_turnover is None:
            exclusion_reason = "missing_turnover"
        elif volatility is None:
            exclusion_reason = "insufficient_volatility_history"
        elif participation_rate is None or not math.isfinite(participation_rate):
            exclusion_reason = "invalid_participation_rate"
        elif participation_rate > max_participation_rate:
            exclusion_reason = "participation_rate_exceeded"

        return {
            "enabled": True,
            "eligible": exclusion_reason is None,
            "exclusion_reason": exclusion_reason,
            "order_notional": order_notional,
            "signal_turnover": signal_turnover,
            "turnover_source": turnover_source,
            "participation_rate": participation_rate,
            "historical_daily_volatility": volatility,
            "dynamic_cost_rate": (
                dynamic_cost_rate
                if exclusion_reason is None and dynamic_cost_rate is not None
                else None
            ),
        }

    def _historical_daily_volatility(
        self,
        bars: List[Dict[str, Any]],
        end_index: int,
        lookback: int,
    ) -> Optional[float]:
        start_index = end_index - lookback
        if start_index < 0:
            return None
        closes = [
            self._positive_close(bars[index])
            for index in range(start_index, end_index + 1)
        ]
        if any(value is None for value in closes):
            return None
        returns = [
            closes[index] / closes[index - 1] - 1
            for index in range(1, len(closes))
        ]
        if len(returns) != lookback or not all(
            math.isfinite(value)
            for value in returns
        ):
            return None
        return pstdev(returns)

    def _execution_summary(
        self,
        records: List[Dict[str, Any]],
        order_notional: Optional[float],
    ) -> Dict[str, Any]:
        enabled = order_notional is not None
        total = len(records)
        executable = [
            record
            for record in records
            if record["execution"]["eligible"]
        ]
        markets = sorted({
            self._market_for_symbol(record["symbol"])
            for record in records
        })
        summary: Dict[str, Any] = {
            "enabled": enabled,
            "model_version": (
                EXECUTION_COST_MODEL_VERSION
                if enabled
                else None
            ),
            "order_notional": order_notional,
            "order_currency_by_market": {
                market: MARKET_CURRENCIES[market]
                for market in markets
            },
            "sample_count": total,
            "executable_sample_count": len(executable),
            "executable_coverage": len(executable) / total if total else 0,
            "turnover_coverage": None,
            "volatility_coverage": None,
            "turnover_source_counts": {},
            "exclusion_counts": {},
            "participation_rate": {
                "average": None,
                "median": None,
                "p95": None,
                "maximum": None,
            },
            "dynamic_cost_rate": {
                "average": None,
                "median": None,
                "p95": None,
                "maximum": None,
            },
        }
        if not enabled:
            return summary

        turnover_values = [
            record["execution"]["signal_turnover"]
            for record in records
            if record["execution"]["signal_turnover"] is not None
        ]
        volatility_values = [
            record["execution"]["historical_daily_volatility"]
            for record in records
            if record["execution"]["historical_daily_volatility"] is not None
        ]
        participation_values = [
            record["execution"]["participation_rate"]
            for record in records
            if record["execution"]["participation_rate"] is not None
        ]
        dynamic_cost_values = [
            record["execution"]["dynamic_cost_rate"]
            for record in executable
            if record["execution"]["dynamic_cost_rate"] is not None
        ]
        source_counts: Dict[str, int] = defaultdict(int)
        exclusion_counts: Dict[str, int] = defaultdict(int)
        for record in records:
            execution = record["execution"]
            if execution["turnover_source"]:
                source_counts[execution["turnover_source"]] += 1
            if execution["exclusion_reason"]:
                exclusion_counts[execution["exclusion_reason"]] += 1

        summary.update({
            "turnover_coverage": (
                len(turnover_values) / total
                if total
                else 0
            ),
            "volatility_coverage": (
                len(volatility_values) / total
                if total
                else 0
            ),
            "turnover_source_counts": dict(sorted(source_counts.items())),
            "exclusion_counts": dict(sorted(exclusion_counts.items())),
            "participation_rate": self._distribution_summary(
                participation_values
            ),
            "dynamic_cost_rate": self._distribution_summary(
                dynamic_cost_values
            ),
        })
        return summary

    def _market_relative_strength_features(
        self,
        bars: List[Dict[str, Any]],
        end_index: int,
        benchmark_closes: Dict[str, float],
        direction: int,
    ) -> Dict[str, Optional[float]]:
        current = self._positive_close(bars[end_index])
        signal_date = self._bar_date(bars[end_index])
        benchmark_current = benchmark_closes.get(signal_date)
        features: Dict[str, Optional[float]] = {}
        for feature_name, window in (
            ("market_rs_10d", 10),
            ("market_rs_half_year", 120),
        ):
            value = None
            if (
                current is not None
                and benchmark_current is not None
                and end_index >= window
            ):
                prior = self._positive_close(bars[end_index - window])
                prior_date = self._bar_date(bars[end_index - window])
                benchmark_prior = benchmark_closes.get(prior_date)
                if (
                    prior is not None
                    and benchmark_prior is not None
                    and benchmark_prior > 0
                ):
                    stock_return = current / prior - 1
                    benchmark_return = (
                        benchmark_current / benchmark_prior - 1
                    )
                    value = direction * (
                        stock_return - benchmark_return
                    )
            features[feature_name] = value
        return features

    @staticmethod
    def _select_top_n(
        records: List[Dict[str, Any]],
        top_n: int,
    ) -> List[Dict[str, Any]]:
        by_date: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for record in records:
            by_date[record["signal_date"]].append(record)
        selected = []
        for signal_date in sorted(by_date):
            selected.extend(sorted(
                by_date[signal_date],
                key=lambda item: (-item["score"], item["symbol"]),
            )[:top_n])
        return selected

    @staticmethod
    def _selection_summary(
        records: List[Dict[str, Any]],
        eligible_records: List[Dict[str, Any]],
        dates: set[str],
        top_n: int,
    ) -> Dict[str, Any]:
        period_records = [
            record
            for record in records
            if record["signal_date"] in dates
        ]
        period_eligible = [
            record
            for record in eligible_records
            if record["signal_date"] in dates
        ]
        eligible_by_date: Dict[str, int] = defaultdict(int)
        for record in period_eligible:
            eligible_by_date[record["signal_date"]] += 1
        return {
            "sample_count": len(period_records),
            "eligible_sample_count": len(period_eligible),
            "eligible_coverage": (
                len(period_eligible) / len(period_records)
                if period_records
                else 0
            ),
            "signal_dates": len(dates),
            "eligible_signal_dates": sum(
                1
                for signal_date in dates
                if eligible_by_date.get(signal_date, 0) > 0
            ),
            "underfilled_signal_dates": sum(
                1
                for signal_date in dates
                if eligible_by_date.get(signal_date, 0) < top_n
            ),
        }

    @staticmethod
    def _split_dates(
        dates: List[str],
        train_ratio: float,
    ) -> tuple[List[str], List[str]]:
        if len(dates) < 2:
            return dates, []
        split = min(
            len(dates) - 1,
            max(1, int(len(dates) * train_ratio)),
        )
        return dates[:split], dates[split:]

    def _summarize_period(
        self,
        all_records: List[Dict[str, Any]],
        top_records: List[Dict[str, Any]],
        dates: set[str],
        horizons: List[int],
        cost_rate: float,
    ) -> Dict[str, Any]:
        return {
            "signal_start": min(dates) if dates else None,
            "signal_end": max(dates) if dates else None,
            "signal_dates": len(dates),
            "all": self._summarize_records(
                [record for record in all_records if record["signal_date"] in dates],
                horizons,
                cost_rate,
            ),
            "top_n": self._summarize_records(
                [record for record in top_records if record["signal_date"] in dates],
                horizons,
                cost_rate,
            ),
        }

    def _summarize_records(
        self,
        records: List[Dict[str, Any]],
        horizons: List[int],
        cost_rate: float,
    ) -> Dict[str, Any]:
        metrics = {}
        for horizon in horizons:
            key = str(horizon)
            samples = [
                (record, record["returns"][key])
                for record in records
                if key in record["returns"]
            ]
            gross = [
                sample["gross_return"]
                for _, sample in samples
            ]
            dynamic_costs = [
                self._dynamic_cost_rate(record)
                for record, _ in samples
            ]
            total_costs = [
                cost_rate + dynamic_cost
                for dynamic_cost in dynamic_costs
            ]
            net = [
                sample["gross_return"] - total_cost
                for (_, sample), total_cost in zip(samples, total_costs)
            ]
            excess = [
                sample["excess_return"]
                for _, sample in samples
                if sample["excess_return"] is not None
            ]
            date_returns: Dict[str, List[float]] = defaultdict(list)
            for record in records:
                if key in record["returns"]:
                    date_returns[record["signal_date"]].append(
                        record["returns"][key]["gross_return"]
                        - cost_rate
                        - self._dynamic_cost_rate(record)
                    )
            portfolio_returns = [
                mean(date_returns[signal_date])
                for signal_date in sorted(date_returns)
            ]
            metrics[key] = {
                "sample_count": len(samples),
                "avg_gross_return": self._safe_mean(gross),
                "avg_net_return": self._safe_mean(net),
                "median_net_return": self._safe_median(net),
                "hit_rate": (
                    sum(1 for value in net if value > 0) / len(net)
                    if net
                    else None
                ),
                "avg_excess_return": self._safe_mean(excess),
                "excess_coverage": (
                    len(excess) / len(samples)
                    if samples
                    else 0
                ),
                "avg_fixed_cost_rate": (
                    cost_rate
                    if samples
                    else None
                ),
                "avg_dynamic_cost_rate": self._safe_mean(dynamic_costs),
                "avg_total_cost_rate": self._safe_mean(total_costs),
                "estimated_fixed_cost_sum": cost_rate * len(samples),
                "estimated_dynamic_cost_sum": sum(dynamic_costs),
                "estimated_cost_sum": sum(total_costs),
                "max_drawdown": self._max_drawdown(portfolio_returns),
            }
        return {
            "sample_count": len(records),
            "avg_score": self._safe_mean([
                record["score"]
                for record in records
            ]),
            "horizons": metrics,
        }

    def _walk_forward_report(
        self,
        records: List[Dict[str, Any]],
        top_records: List[Dict[str, Any]],
        all_dates: List[str],
        validation_dates: List[str],
        horizons: List[int],
        cost_rate: float,
        folds: int,
    ) -> List[Dict[str, Any]]:
        if not validation_dates:
            return []
        fold_size = max(1, math.ceil(len(validation_dates) / folds))
        reports = []
        for index in range(0, len(validation_dates), fold_size):
            fold_dates = validation_dates[index:index + fold_size]
            train_end_index = all_dates.index(fold_dates[0])
            fold_train_dates = all_dates[:train_end_index]
            reports.append({
                "fold": len(reports) + 1,
                "train_start": fold_train_dates[0] if fold_train_dates else None,
                "train_end": fold_train_dates[-1] if fold_train_dates else None,
                "validation_start": fold_dates[0],
                "validation_end": fold_dates[-1],
                "train_metrics": self._summarize_period(
                    records,
                    top_records,
                    set(fold_train_dates),
                    horizons,
                    cost_rate,
                ),
                "validation_metrics": self._summarize_period(
                    records,
                    top_records,
                    set(fold_dates),
                    horizons,
                    cost_rate,
                ),
            })
        return reports

    def _save_report(self, report: Dict[str, Any]) -> int:
        result_payload = {
            key: value
            for key, value in report.items()
            if key not in {"parameters", "id"}
        }
        with self.connection_factory() as connection:
            row = connection.execute(
                """
                INSERT INTO stock_picker_backtests (
                    pool_type, score_version, parameters, result, data_as_of
                ) VALUES (?, ?, ?, ?, ?)
                RETURNING id
                """,
                (
                    report["pool_type"],
                    report["score_version"],
                    json.dumps(
                        report["parameters"],
                        ensure_ascii=False,
                        default=str,
                    ),
                    json.dumps(
                        result_payload,
                        ensure_ascii=False,
                        default=str,
                    ),
                    report["data"]["data_as_of"],
                ),
            ).fetchone()
        return int(row[0])

    @staticmethod
    def _validate_parameters(
        horizons: List[int],
        lookback: int,
        max_bars: int,
        min_history: int,
        step: int,
        top_n: int,
        train_ratio: float,
        folds: int,
        transaction_cost_bps: float,
        order_notional: Optional[float],
        max_participation_rate: float,
        impact_coefficient: float,
        impact_volatility_lookback: int,
    ) -> None:
        if not horizons or any(value < 1 or value > 60 for value in horizons):
            raise ValueError("horizons 必须包含 1～60 的交易日")
        if not 30 <= min_history <= lookback <= max_bars <= 5000:
            raise ValueError(
                "必须满足 30 <= min_history <= lookback <= max_bars <= 5000"
            )
        if not 1 <= step <= 60:
            raise ValueError("step 必须在 1～60 之间")
        if not 1 <= top_n <= 100:
            raise ValueError("top_n 必须在 1～100 之间")
        if not 0.5 <= train_ratio <= 0.9:
            raise ValueError("train_ratio 必须在 0.5～0.9 之间")
        if not 1 <= folds <= 10:
            raise ValueError("walk_forward_folds 必须在 1～10 之间")
        if not 0 <= transaction_cost_bps <= 1000:
            raise ValueError("transaction_cost_bps 必须在 0～1000 之间")
        if order_notional is not None and (
            isinstance(order_notional, bool)
            or not math.isfinite(order_notional)
            or order_notional <= 0
        ):
            raise ValueError("order_notional 必须是大于 0 的有限数值或 null")
        if (
            isinstance(max_participation_rate, bool)
            or not math.isfinite(max_participation_rate)
            or not 0 < max_participation_rate <= 1
        ):
            raise ValueError("max_participation_rate 必须在 0～1 之间")
        if (
            isinstance(impact_coefficient, bool)
            or not math.isfinite(impact_coefficient)
            or not 0 <= impact_coefficient <= 10
        ):
            raise ValueError("impact_coefficient 必须在 0～10 之间")
        if not 2 <= impact_volatility_lookback <= 252:
            raise ValueError(
                "impact_volatility_lookback 必须在 2～252 之间"
            )

    @staticmethod
    def _market_for_symbol(symbol: str) -> str:
        if symbol.endswith(".HK"):
            return "HK"
        if symbol.endswith((".SH", ".SZ", ".CN")):
            return "CN"
        if symbol.endswith(".SG"):
            return "SG"
        return "US"

    @staticmethod
    def _bar_date(bar: Dict[str, Any]) -> str:
        value = bar.get("ts") or bar.get("timestamp") or bar.get("date")
        if isinstance(value, datetime):
            return value.date().isoformat()
        if isinstance(value, date):
            return value.isoformat()
        text = str(value or "")
        return text[:10] if len(text) >= 10 else text

    @staticmethod
    def _positive_close(bar: Dict[str, Any]) -> Optional[float]:
        try:
            value = float(bar.get("close"))
        except (TypeError, ValueError):
            return None
        return value if value > 0 and math.isfinite(value) else None

    @staticmethod
    def _positive_number(value: Any) -> Optional[float]:
        try:
            normalized = float(value)
        except (TypeError, ValueError):
            return None
        return (
            normalized
            if normalized > 0 and math.isfinite(normalized)
            else None
        )

    @staticmethod
    def _dynamic_cost_rate(record: Dict[str, Any]) -> float:
        execution = record.get("execution") or {}
        value = execution.get("dynamic_cost_rate", 0)
        try:
            normalized = float(value)
        except (TypeError, ValueError):
            return 0.0
        return (
            normalized
            if normalized >= 0 and math.isfinite(normalized)
            else 0.0
        )

    def _distribution_summary(
        self,
        values: List[float],
    ) -> Dict[str, Optional[float]]:
        return {
            "average": self._safe_mean(values),
            "median": self._safe_median(values),
            "p95": self._percentile(values, 0.95),
            "maximum": max(values) if values else None,
        }

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
        lower_index = math.floor(position)
        upper_index = math.ceil(position)
        if lower_index == upper_index:
            return ordered[lower_index]
        weight = position - lower_index
        return (
            ordered[lower_index] * (1 - weight)
            + ordered[upper_index] * weight
        )

    @staticmethod
    def _benchmark_coverage(records: List[Dict[str, Any]]) -> float:
        total = 0
        covered = 0
        for record in records:
            for sample in record["returns"].values():
                total += 1
                if sample["excess_return"] is not None:
                    covered += 1
        return covered / total if total else 0

    @staticmethod
    def _safe_mean(values: List[float]) -> Optional[float]:
        return mean(values) if values else None

    @staticmethod
    def _safe_median(values: List[float]) -> Optional[float]:
        return median(values) if values else None

    @staticmethod
    def _max_drawdown(returns: List[float]) -> Optional[float]:
        if not returns:
            return None
        equity = 1.0
        peak = 1.0
        maximum = 0.0
        for period_return in returns:
            equity *= max(0.0, 1 + period_return)
            peak = max(peak, equity)
            drawdown = (peak - equity) / peak if peak else 0
            maximum = max(maximum, drawdown)
        return maximum


_stock_picker_backtest_service: Optional[StockPickerBacktestService] = None


def get_stock_picker_backtest_service() -> StockPickerBacktestService:
    global _stock_picker_backtest_service
    if _stock_picker_backtest_service is None:
        _stock_picker_backtest_service = StockPickerBacktestService()
    return _stock_picker_backtest_service
