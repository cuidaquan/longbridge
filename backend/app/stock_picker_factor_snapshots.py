from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
from importlib.metadata import PackageNotFoundError, version
import json
from typing import Any, Callable, Dict, Iterable, List, Optional
from uuid import uuid4
from zoneinfo import ZoneInfo

from .db import get_connection
from .external_service_resilience import (
    ExternalServiceTimeoutError,
    run_external_call,
)
from .services import (
    get_security_calc_indexes,
    get_short_risk_metrics,
)
from .stock_candidate_data import (
    get_fundamental_profiles,
    get_margin_requirements,
    get_security_tradeability,
)
from .stock_picker_baseline import BASELINE_UNIVERSES
from .stock_screener import DEFAULT_MARKET_BENCHMARKS


SNAPSHOT_VERSION = "stock-picker-factor-snapshot-v1"
SNAPSHOT_SOURCE = "longbridge-live"
MIN_OBSERVATION_DATES = 60
MIN_FACTOR_COVERAGE = 0.9
MIN_DISTINCT_SYMBOLS = 10
MAX_SNAPSHOT_AGE_HOURS = 48
EVENT_WINDOW_DAYS = 365
AUTO_CAPTURE_LOCAL_HOUR = 17
MARKET_TIMEZONES = {
    "US": "America/New_York",
    "HK": "Asia/Hong_Kong",
}

FACTOR_REQUIREMENTS = {
    "market_rs": (
        "relative_strength",
        ("market_rs_10d", "market_rs_half_year"),
    ),
    "fundamental_quality": (
        "fundamentals",
        (
            "revenue_yoy",
            "net_profit_yoy",
            "operating_cash_flow_yoy",
        ),
    ),
    "expectations": (
        "fundamentals",
        ("analyst_alignment", "eps_revision_alignment"),
    ),
    "financial_event": (
        "fundamentals",
        ("days_to_financial_event",),
    ),
    "corporate_action": (
        "fundamentals",
        ("days_to_corporate_action",),
    ),
    "trade_status": (
        "tradeability",
        ("is_tradable",),
    ),
    "depth": (
        "tradeability",
        ("spread_bps", "top_of_book_notional"),
    ),
    "margin": (
        "margin_requirements",
        ("initial_margin_ratio",),
    ),
    "short_risk": (
        "short_risk",
        ("short_ratio", "days_to_cover"),
    ),
}


def _package_version(package: str) -> str:
    try:
        return version(package)
    except PackageNotFoundError:
        return "unknown"


class StockPickerFactorSnapshotService:
    def __init__(
        self,
        index_loader: Callable = get_security_calc_indexes,
        short_risk_loader: Callable = get_short_risk_metrics,
        tradeability_loader: Callable = get_security_tradeability,
        fundamental_loader: Callable = get_fundamental_profiles,
        margin_loader: Callable = get_margin_requirements,
        connection_factory: Callable = get_connection,
        clock: Callable[[], datetime] = (
            lambda: datetime.now(timezone.utc)
        ),
    ) -> None:
        self.index_loader = index_loader
        self.short_risk_loader = short_risk_loader
        self.tradeability_loader = tradeability_loader
        self.fundamental_loader = fundamental_loader
        self.margin_loader = margin_loader
        self.connection_factory = connection_factory
        self.clock = clock

    def capture_baseline(
        self,
        market: Optional[str] = None,
        target_direction: Optional[str] = None,
        persist: bool = True,
    ) -> Dict[str, Any]:
        markets = (
            [self._normalize_market(market)]
            if market
            else list(BASELINE_UNIVERSES)
        )
        directions = (
            [self._normalize_direction(target_direction)]
            if target_direction
            else ["LONG", "SHORT"]
        )
        groups = [
            self.capture_group(
                normalized_market,
                direction,
                BASELINE_UNIVERSES[normalized_market]["symbols"],
                persist=persist,
            )
            for normalized_market in markets
            for direction in directions
        ]
        return {
            "snapshot_version": SNAPSHOT_VERSION,
            "source": SNAPSHOT_SOURCE,
            "persisted": persist,
            "groups": groups,
            "row_count": sum(group["row_count"] for group in groups),
        }

    def capture_group(
        self,
        market: str,
        target_direction: str,
        symbols: Iterable[str],
        persist: bool = True,
        include_payloads: bool = False,
    ) -> Dict[str, Any]:
        normalized_market = self._normalize_market(market)
        direction = self._normalize_direction(target_direction)
        normalized_symbols = list(dict.fromkeys(
            str(symbol).strip().upper()
            for symbol in symbols
            if str(symbol).strip()
        ))
        if not normalized_symbols:
            raise ValueError("symbols 不能为空")
        benchmark = DEFAULT_MARKET_BENCHMARKS[normalized_market]
        request_id = uuid4().hex
        observed_at = self._as_utc(self.clock())
        observation_date = observed_at.astimezone(
            ZoneInfo(MARKET_TIMEZONES[normalized_market])
        ).date()
        session_phase = self._session_phase(
            normalized_market,
            observed_at,
        )
        channel_status: Dict[str, Dict[str, Any]] = {}

        indexes = self._load_channel(
            channel_status,
            "quote",
            "factor_snapshot_indexes",
            self.index_loader,
            [*normalized_symbols, benchmark],
        )
        tradeability = self._load_channel(
            channel_status,
            "quote",
            "factor_snapshot_tradeability",
            self.tradeability_loader,
            normalized_symbols,
            True,
        )
        fundamentals = self._load_channel(
            channel_status,
            "fundamental",
            "factor_snapshot_fundamental",
            self.fundamental_loader,
            normalized_symbols,
            normalized_market,
            direction,
            EVENT_WINDOW_DAYS,
            True,
            observation_date,
        )
        margin = self._load_channel(
            channel_status,
            "trade",
            "factor_snapshot_margin",
            self.margin_loader,
            normalized_symbols,
        )
        short_risk = (
            self._load_channel(
                channel_status,
                "quote",
                "factor_snapshot_short_risk",
                self.short_risk_loader,
                normalized_symbols,
            )
            if direction == "SHORT"
            else {}
        )
        if direction == "LONG":
            channel_status["short_risk"] = {
                "status": "not_applicable",
                "error": None,
            }

        source_versions = {
            "snapshot_schema": SNAPSHOT_VERSION,
            "longbridge_sdk": _package_version("longbridge"),
        }
        rows = []
        for symbol in normalized_symbols:
            payload = {
                "indexes": indexes.get(symbol, {}),
                "relative_strength": self._relative_strength(
                    indexes.get(symbol, {}),
                    indexes.get(benchmark, {}),
                    direction,
                    benchmark,
                ),
                "fundamentals": fundamentals.get(
                    symbol,
                    {
                        "status": channel_status["fundamental"][
                            "status"
                        ],
                        "errors": self._channel_errors(
                            channel_status["fundamental"]
                        ),
                    },
                ),
                "tradeability": tradeability.get(
                    symbol,
                    {
                        "status": channel_status["tradeability"][
                            "status"
                        ],
                        "error": channel_status["tradeability"][
                            "error"
                        ],
                    },
                ),
                "margin_requirements": margin.get(
                    symbol,
                    {
                        "status": channel_status["margin"]["status"],
                        "error": channel_status["margin"]["error"],
                        "borrow_availability": "unknown",
                        "borrow_fee_rate": None,
                    },
                ),
                "short_risk": (
                    short_risk.get(
                        symbol,
                        {
                            "status": channel_status["short_risk"][
                                "status"
                            ],
                            "error": channel_status["short_risk"][
                                "error"
                            ],
                        },
                    )
                    if direction == "SHORT"
                    else {
                        "status": "not_applicable",
                        "error": None,
                    }
                ),
                "capture": {
                    "session_phase": session_phase,
                    "depth_requested": True,
                    "corporate_actions_requested": True,
                    "event_window_days": EVENT_WINDOW_DAYS,
                    "channel_status": channel_status,
                },
            }
            rows.append({
                "request_id": request_id,
                "observed_at": observed_at,
                "observation_date": observation_date.isoformat(),
                "snapshot_version": SNAPSHOT_VERSION,
                "market": normalized_market,
                "target_direction": direction,
                "symbol": symbol,
                "benchmark_symbol": benchmark,
                "source": SNAPSHOT_SOURCE,
                "source_versions": source_versions,
                "payload": payload,
            })

        if persist:
            self._save_rows(rows)
        result = {
            "request_id": request_id,
            "observed_at": observed_at.isoformat(),
            "observation_date": observation_date.isoformat(),
            "session_phase": session_phase,
            "market": normalized_market,
            "target_direction": direction,
            "benchmark_symbol": benchmark,
            "symbols": normalized_symbols,
            "row_count": len(rows),
            "persisted": persist,
            "channel_status": channel_status,
        }
        if include_payloads:
            result["snapshots"] = rows
        return result

    def capture_due_baseline(
        self,
        persist: bool = True,
    ) -> Dict[str, Any]:
        now = self._as_utc(self.clock())
        captured = []
        skipped = []
        for market in BASELINE_UNIVERSES:
            local_date = now.astimezone(
                ZoneInfo(MARKET_TIMEZONES[market])
            ).date().isoformat()
            phase = self._session_phase(market, now)
            for direction in ("LONG", "SHORT"):
                if phase != "post_close":
                    skipped.append({
                        "market": market,
                        "target_direction": direction,
                        "observation_date": local_date,
                        "reason": f"session_phase:{phase}",
                    })
                    continue
                if self._has_post_close_snapshot(
                    market,
                    direction,
                    local_date,
                ):
                    skipped.append({
                        "market": market,
                        "target_direction": direction,
                        "observation_date": local_date,
                        "reason": "already_captured",
                    })
                    continue
                captured.append(self.capture_group(
                    market,
                    direction,
                    BASELINE_UNIVERSES[market]["symbols"],
                    persist=persist,
                ))
        return {
            "snapshot_version": SNAPSHOT_VERSION,
            "persisted": persist,
            "captured": captured,
            "skipped": skipped,
            "row_count": sum(
                group["row_count"]
                for group in captured
            ),
        }

    def get_coverage(
        self,
        days: int = 365,
        current_time: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        if not 1 <= int(days) <= 3650:
            raise ValueError("days 必须在 1～3650 之间")
        now = self._as_utc(
            current_time or self.clock()
        )
        cutoff = now - timedelta(days=int(days))
        with self.connection_factory() as connection:
            rows = connection.execute(
                """
                SELECT
                    observed_at, observation_date,
                    market, target_direction,
                    symbol, payload
                FROM stock_picker_factor_snapshots
                WHERE observed_at >= ?
                ORDER BY observed_at
                """,
                [cutoff.replace(tzinfo=None)],
            ).fetchall()
        raw_rows = [
            {
                "observed_at": self._as_utc(row[0]),
                "observation_date": str(row[1]),
                "market": row[2],
                "target_direction": row[3],
                "symbol": row[4],
                "payload": json.loads(row[5]),
            }
            for row in rows
        ]
        deduplicated = {}
        for row in raw_rows:
            key = (
                row["market"],
                row["target_direction"],
                row["symbol"],
                row["observation_date"],
            )
            previous = deduplicated.get(key)
            if (
                previous is None
                or row["observed_at"] > previous["observed_at"]
            ):
                deduplicated[key] = row
        parsed_rows = list(deduplicated.values())
        groups = []
        for market in BASELINE_UNIVERSES:
            for direction in ("LONG", "SHORT"):
                group_rows = [
                    row
                    for row in parsed_rows
                    if row["market"] == market
                    and row["target_direction"] == direction
                ]
                groups.append(
                    self._coverage_group(
                        market,
                        direction,
                        group_rows,
                        now,
                    )
                )
        evaluation_rows = [
            row
            for row in parsed_rows
            if (
                (row["payload"].get("capture") or {}).get(
                    "session_phase"
                ) == "post_close"
            )
        ]
        return {
            "snapshot_version": SNAPSHOT_VERSION,
            "window_days": int(days),
            "raw_snapshot_count": len(raw_rows),
            "daily_snapshot_count": len(parsed_rows),
            "evaluation_snapshot_count": len(evaluation_rows),
            "minimums": {
                "observation_dates": MIN_OBSERVATION_DATES,
                "factor_coverage": MIN_FACTOR_COVERAGE,
                "distinct_symbols": MIN_DISTINCT_SYMBOLS,
                "max_snapshot_age_hours": MAX_SNAPSHOT_AGE_HOURS,
            },
            "groups": groups,
            "ready_for_return_evaluation": all(
                group["ready_for_return_evaluation"]
                for group in groups
            ),
        }

    def _coverage_group(
        self,
        market: str,
        direction: str,
        rows: List[Dict[str, Any]],
        now: datetime,
    ) -> Dict[str, Any]:
        captured_count = len(rows)
        phase_counts = Counter(
            (row["payload"].get("capture") or {}).get(
                "session_phase",
                "unknown",
            )
            for row in rows
        )
        rows = [
            row
            for row in rows
            if (
                (row["payload"].get("capture") or {}).get(
                    "session_phase"
                ) == "post_close"
            )
        ]
        observation_dates = {
            row["observation_date"]
            for row in rows
        }
        symbols = {row["symbol"] for row in rows}
        latest = max(
            (row["observed_at"] for row in rows),
            default=None,
        )
        age_hours = (
            (now - latest).total_seconds() / 3600
            if latest is not None
            else None
        )
        factor_names = [
            factor
            for factor in FACTOR_REQUIREMENTS
            if factor != "short_risk" or direction == "SHORT"
        ]
        factors = {}
        for factor in factor_names:
            available = [
                row
                for row in rows
                if self._factor_available(row["payload"], factor)
            ]
            missing = [
                row
                for row in rows
                if not self._factor_available(
                    row["payload"],
                    factor,
                )
            ]
            coverage = len(available) / len(rows) if rows else 0
            reasons = Counter(
                self._missing_reason(row["payload"], factor)
                for row in missing
            )
            factors[factor] = {
                "available_count": len(available),
                "total_count": len(rows),
                "coverage": coverage,
                "missing_reasons": dict(reasons),
                "coverage_ready": coverage >= MIN_FACTOR_COVERAGE,
            }
        base_ready = (
            len(observation_dates) >= MIN_OBSERVATION_DATES
            and len(symbols) >= MIN_DISTINCT_SYMBOLS
            and age_hours is not None
            and age_hours <= MAX_SNAPSHOT_AGE_HOURS
        )
        return {
            "market": market,
            "target_direction": direction,
            "captured_daily_snapshot_count": captured_count,
            "session_phase_counts": dict(phase_counts),
            "snapshot_count": len(rows),
            "observation_dates": len(observation_dates),
            "distinct_symbols": len(symbols),
            "latest_observed_at": (
                latest.isoformat()
                if latest is not None
                else None
            ),
            "snapshot_age_hours": age_hours,
            "factors": factors,
            "ready_for_return_evaluation": (
                base_ready
                and all(
                    factor["coverage_ready"]
                    for factor in factors.values()
                )
            ),
        }

    def _has_post_close_snapshot(
        self,
        market: str,
        direction: str,
        observation_date: str,
    ) -> bool:
        with self.connection_factory() as connection:
            rows = connection.execute(
                """
                SELECT payload
                FROM stock_picker_factor_snapshots
                WHERE market = ?
                  AND target_direction = ?
                  AND observation_date = ?
                """,
                [market, direction, observation_date],
            ).fetchall()
        return any(
            (
                (json.loads(row[0]).get("capture") or {}).get(
                    "session_phase"
                ) == "post_close"
            )
            for row in rows
        )

    def _load_channel(
        self,
        status: Dict[str, Dict[str, Any]],
        channel: str,
        operation: str,
        loader: Callable,
        *args: Any,
    ) -> Dict[str, Dict[str, Any]]:
        status_key = operation.removeprefix("factor_snapshot_")
        try:
            result = run_external_call(
                channel,
                operation,
                loader,
                *args,
                retry_if=lambda error: not isinstance(
                    error,
                    ExternalServiceTimeoutError,
                ),
            )
        except Exception as exc:
            status[status_key] = {
                "status": "error",
                "error": str(exc),
            }
            return {}
        status[status_key] = {
            "status": "available",
            "error": None,
        }
        return result or {}

    @staticmethod
    def _relative_strength(
        indexes: Dict[str, Any],
        benchmark_indexes: Dict[str, Any],
        direction: str,
        benchmark: str,
    ) -> Dict[str, Any]:
        multiplier = 1 if direction == "LONG" else -1
        result = {
            "status": None,
            "error": None,
            "benchmark_symbol": benchmark,
            "target_direction": direction,
            "market_rs_10d": None,
            "market_rs_half_year": None,
        }
        for suffix, metric in (
            ("10d", "ten_day_change_rate"),
            ("half_year", "half_year_change_rate"),
        ):
            stock_value = indexes.get(metric)
            benchmark_value = benchmark_indexes.get(metric)
            if stock_value is None or benchmark_value is None:
                continue
            result[f"market_rs_{suffix}"] = multiplier * (
                float(stock_value) - float(benchmark_value)
            )
        result["status"] = (
            "available"
            if any(
                result[key] is not None
                for key in (
                    "market_rs_10d",
                    "market_rs_half_year",
                )
            )
            else "no_data"
        )
        return result

    def _save_rows(self, rows: List[Dict[str, Any]]) -> None:
        with self.connection_factory() as connection:
            connection.executemany(
                """
                INSERT INTO stock_picker_factor_snapshots (
                    request_id, observed_at, observation_date,
                    snapshot_version,
                    market, target_direction, symbol,
                    benchmark_symbol, source, source_versions, payload
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        row["request_id"],
                        row["observed_at"].replace(tzinfo=None),
                        row["observation_date"],
                        row["snapshot_version"],
                        row["market"],
                        row["target_direction"],
                        row["symbol"],
                        row["benchmark_symbol"],
                        row["source"],
                        json.dumps(
                            row["source_versions"],
                            ensure_ascii=False,
                            default=str,
                        ),
                        json.dumps(
                            row["payload"],
                            ensure_ascii=False,
                            default=str,
                        ),
                    )
                    for row in rows
                ],
            )

    @classmethod
    def _factor_available(
        cls,
        payload: Dict[str, Any],
        factor: str,
    ) -> bool:
        section, fields = FACTOR_REQUIREMENTS[factor]
        values = payload.get(section) or {}
        return all(values.get(field) is not None for field in fields)

    @classmethod
    def _missing_reason(
        cls,
        payload: Dict[str, Any],
        factor: str,
    ) -> str:
        section, _ = FACTOR_REQUIREMENTS[factor]
        values = payload.get(section) or {}
        error = values.get("error")
        if not error:
            errors = values.get("errors")
            if isinstance(errors, list) and errors:
                error = "; ".join(str(item) for item in errors)
        if error:
            return f"error:{error}"
        status = values.get("status")
        if status and status != "available":
            return f"status:{status}"
        return "missing_required_values"

    @staticmethod
    def _channel_errors(status: Dict[str, Any]) -> List[str]:
        return [status["error"]] if status.get("error") else []

    @staticmethod
    def _normalize_market(market: str) -> str:
        normalized = market.strip().upper()
        if normalized not in BASELINE_UNIVERSES:
            raise ValueError("market 必须是 US 或 HK")
        return normalized

    @staticmethod
    def _normalize_direction(direction: str) -> str:
        normalized = direction.strip().upper()
        if normalized not in {"LONG", "SHORT"}:
            raise ValueError("target_direction 必须是 LONG 或 SHORT")
        return normalized

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    @staticmethod
    def _session_phase(
        market: str,
        observed_at: datetime,
    ) -> str:
        local = observed_at.astimezone(
            ZoneInfo(MARKET_TIMEZONES[market])
        )
        if local.weekday() >= 5:
            return "non_trading_day"
        if local.hour >= AUTO_CAPTURE_LOCAL_HOUR:
            return "post_close"
        return "intraday"


_factor_snapshot_service: Optional[
    StockPickerFactorSnapshotService
] = None


def get_stock_picker_factor_snapshot_service(
) -> StockPickerFactorSnapshotService:
    global _factor_snapshot_service
    if _factor_snapshot_service is None:
        _factor_snapshot_service = StockPickerFactorSnapshotService()
    return _factor_snapshot_service


def main() -> None:
    parser = argparse.ArgumentParser(
        description="采集智能选股 Fundamental 与执行风险点时快照",
    )
    parser.add_argument("--market", choices=["US", "HK"])
    parser.add_argument(
        "--direction",
        choices=["LONG", "SHORT"],
    )
    parser.add_argument(
        "--no-persist",
        action="store_true",
        help="执行采集但不写入点时快照表",
    )
    parser.add_argument(
        "--coverage",
        action="store_true",
        help="只输出现有快照覆盖率，不调用外部服务",
    )
    args = parser.parse_args()
    service = get_stock_picker_factor_snapshot_service()
    result = (
        service.get_coverage()
        if args.coverage
        else service.capture_baseline(
            market=args.market,
            target_direction=args.direction,
            persist=not args.no_persist,
        )
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
