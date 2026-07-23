from __future__ import annotations

from contextlib import contextmanager
import logging
import math
from statistics import median
from typing import Any, Callable, Dict, Iterator, List, Optional

from .exceptions import LongbridgeAPIError, LongbridgeDependencyMissing
from .external_service_resilience import (
    ExternalServiceTimeoutError,
    run_external_call,
)
from .longbridge_compat import close_longbridge_context
from .repositories import load_credentials
from .services import get_security_calc_indexes, get_short_risk_metrics
from .stock_candidate_data import (
    get_fundamental_profiles,
    get_margin_requirements,
    get_security_tradeability,
)


logger = logging.getLogger(__name__)

SUPPORTED_SCREENER_MARKETS = ("US", "HK", "CN", "SG")
_REQUIRED_CREDENTIALS = (
    "LONGPORT_APP_KEY",
    "LONGPORT_APP_SECRET",
    "LONGPORT_ACCESS_TOKEN",
)
_STRATEGY_ID_KEYS = ("id", "strategy_id", "strategyId")
_STRATEGY_NAME_KEYS = (
    "name",
    "title",
    "strategy_name",
    "strategyName",
)
_SYMBOL_KEYS = ("symbol", "stock_symbol", "stockSymbol", "stock_code", "stockCode")
_NAME_KEYS = ("name", "stock_name", "stockName", "name_cn", "name_en")
DEFAULT_MARKET_BENCHMARKS = {
    "US": "SPY.US",
    "HK": "2800.HK",
    "CN": "510300.SH",
    "SG": "ES3.SG",
}
INDEX_FILTER_KEYS = {
    "min_turnover",
    "min_market_value",
    "min_turnover_rate",
    "min_pe_ttm",
    "max_pe_ttm",
    "max_pb",
    "min_capital_flow",
    "min_volume_ratio",
    "min_market_rs_10d",
    "min_market_rs_half_year",
    "min_industry_rs_10d",
    "min_industry_rs_half_year",
}
SHORT_RISK_FILTER_KEYS = {
    "max_days_to_cover",
    "max_short_ratio",
    "max_short_ratio_change",
}
TRADEABILITY_FILTER_KEYS = {
    "max_spread_bps",
    "min_top_of_book_notional",
}
FUNDAMENTAL_FILTER_KEYS = {
    "min_revenue_yoy",
    "max_revenue_yoy",
    "min_net_profit_yoy",
    "max_net_profit_yoy",
    "min_operating_cash_flow_yoy",
    "min_analyst_alignment",
    "min_eps_revision_alignment",
    "min_days_to_financial_event",
    "min_days_to_corporate_action",
}
MARGIN_FILTER_KEYS = {
    "max_initial_margin_ratio",
}


def _retry_candidate_batch(error: BaseException) -> bool:
    """Retry transient failures, but never duplicate a timed-out full batch."""
    return not isinstance(error, ExternalServiceTimeoutError)


class StockScreenerService:
    """Normalize Longbridge Screener's raw JSON responses for the stock picker."""

    def __init__(
        self,
        index_loader: Callable[
            [List[str]],
            Dict[str, Dict[str, Optional[float]]],
        ] = get_security_calc_indexes,
        short_risk_loader: Callable[
            [List[str]],
            Dict[str, Dict[str, object]],
        ] = get_short_risk_metrics,
        tradeability_loader: Callable[..., Dict[str, Dict[str, Any]]] = (
            get_security_tradeability
        ),
        fundamental_loader: Callable[..., Dict[str, Dict[str, Any]]] = (
            get_fundamental_profiles
        ),
        margin_loader: Callable[..., Dict[str, Dict[str, Any]]] = (
            get_margin_requirements
        ),
    ) -> None:
        self._index_loader = index_loader
        self._short_risk_loader = short_risk_loader
        self._tradeability_loader = tradeability_loader
        self._fundamental_loader = fundamental_loader
        self._margin_loader = margin_loader

    @staticmethod
    def normalize_market(market: str) -> str:
        normalized = market.strip().upper()
        if normalized not in SUPPORTED_SCREENER_MARKETS:
            raise ValueError("market 必须是 US、HK、CN 或 SG")
        return normalized

    @contextmanager
    def _context(self):
        credentials = load_credentials()
        if not credentials or any(
            not credentials.get(key) for key in _REQUIRED_CREDENTIALS
        ):
            raise ValueError("请先在基础配置中保存完整的 Longbridge 凭据")

        try:
            from longbridge.openapi import Config, ScreenerContext
        except ModuleNotFoundError as exc:  # pragma: no cover - environment dependent
            raise LongbridgeDependencyMissing(
                "未找到 longbridge Python SDK，请先运行 `pip install longbridge`。"
            ) from exc

        try:
            config = Config.from_apikey(
                credentials["LONGPORT_APP_KEY"],
                credentials["LONGPORT_APP_SECRET"],
                credentials["LONGPORT_ACCESS_TOKEN"],
            )
            context = ScreenerContext(config)
        except Exception as exc:
            raise LongbridgeAPIError(f"创建 Longbridge Screener 连接失败: {exc}") from exc

        try:
            yield context
        finally:
            try:
                close_longbridge_context(context)
            except Exception:  # noqa: S110 - best effort cleanup
                pass

    def list_strategies(
        self,
        market: str,
        include_user: bool = True,
    ) -> Dict[str, Any]:
        normalized_market = self.normalize_market(market)

        def fetch_strategies():
            with self._context() as context:
                recommended = context.screener_recommend_strategies(
                    normalized_market
                ).data
                user = []
                if include_user:
                    try:
                        user = context.screener_user_strategies(
                            normalized_market
                        ).data
                    except Exception as exc:
                        logger.warning(
                            "Longbridge user screener strategies unavailable for %s: %s",
                            normalized_market,
                            exc,
                        )
                return recommended, user

        try:
            recommended, user = run_external_call(
                "screener",
                "list_strategies",
                fetch_strategies,
            )
        except (ValueError, LongbridgeDependencyMissing, LongbridgeAPIError):
            raise
        except Exception as exc:
            raise LongbridgeAPIError(f"获取 Longbridge 选股策略失败: {exc}") from exc

        strategies = self._normalize_strategies(
            recommended,
            source="recommended",
            default_market=normalized_market,
        )
        strategies.extend(
            self._normalize_strategies(
                user,
                source="user",
                default_market=normalized_market,
            )
        )

        deduplicated: List[Dict[str, Any]] = []
        seen_ids = set()
        for strategy in strategies:
            strategy_id = strategy["id"]
            if strategy_id in seen_ids:
                continue
            seen_ids.add(strategy_id)
            deduplicated.append(strategy)

        return {
            "market": normalized_market,
            "source": "longbridge-screener",
            "items": deduplicated,
        }

    def search(
        self,
        market: str,
        strategy_id: int,
        page: int = 0,
        size: int = 20,
        filters: Optional[Dict[str, Any]] = None,
        include_indexes: bool = True,
        target_direction: str = "LONG",
        benchmark_symbol: Optional[str] = None,
        include_short_risk: bool = True,
        include_tradeability: bool = True,
        require_normal_trade_status: bool = True,
        include_fundamentals: bool = False,
        include_margin_requirements: bool = False,
        fundamental_event_window_days: int = 30,
        include_corporate_actions: bool = False,
    ) -> Dict[str, Any]:
        normalized_market = self.normalize_market(market)
        normalized_direction = target_direction.strip().upper()
        if normalized_direction not in {"LONG", "SHORT"}:
            raise ValueError("target_direction 必须是 LONG 或 SHORT")
        benchmark = (
            benchmark_symbol.strip().upper()
            if benchmark_symbol and benchmark_symbol.strip()
            else DEFAULT_MARKET_BENCHMARKS[normalized_market]
        )
        if isinstance(strategy_id, bool) or int(strategy_id) <= 0:
            raise ValueError("strategy_id 必须是正整数")
        if page < 0:
            raise ValueError("page 不能小于 0")
        if not 1 <= size <= 100:
            raise ValueError("size 必须在 1～100 之间")
        if not 1 <= fundamental_event_window_days <= 365:
            raise ValueError(
                "fundamental_event_window_days 必须在 1～365 之间"
            )
        normalized_filters = self._normalize_filters(filters or {})
        if (
            normalized_direction != "SHORT"
            and SHORT_RISK_FILTER_KEYS.intersection(normalized_filters)
        ):
            raise ValueError("做空拥挤度过滤仅适用于 SHORT 方向")
        if (
            not include_tradeability
            and (
                require_normal_trade_status
                or TRADEABILITY_FILTER_KEYS.intersection(
                    normalized_filters
                )
            )
        ):
            raise ValueError(
                "交易状态或盘口过滤要求 include_tradeability=true"
            )

        def fetch_candidates():
            with self._context() as context:
                return context.screener_search(
                    normalized_market,
                    int(strategy_id),
                    [],
                    [],
                    page,
                    size,
                ).data

        try:
            payload = run_external_call(
                "screener",
                "search",
                fetch_candidates,
            )
        except (ValueError, LongbridgeDependencyMissing, LongbridgeAPIError):
            raise
        except Exception as exc:
            raise LongbridgeAPIError(f"Longbridge 主动选股失败: {exc}") from exc

        candidates, container = self._normalize_candidates(
            payload,
            normalized_market,
            page,
            size,
        )
        enrichment = {
            "status": "disabled",
            "error": None,
        }
        hard_index_filters = INDEX_FILTER_KEYS.intersection(
            normalized_filters
        )
        if candidates and (include_indexes or hard_index_filters):
            try:
                index_symbols = [
                    candidate["symbol"]
                    for candidate in candidates
                ]
                if benchmark not in index_symbols:
                    index_symbols.append(benchmark)
                indexes = run_external_call(
                    "quote",
                    "calc_indexes",
                    self._index_loader,
                    index_symbols,
                )
            except Exception as exc:
                if hard_index_filters:
                    if isinstance(exc, LongbridgeAPIError):
                        raise
                    raise LongbridgeAPIError(
                        f"无法应用候选过滤条件: {exc}"
                    ) from exc
                logger.warning("Longbridge calc_indexes unavailable: %s", exc)
                enrichment = {
                    "status": "fallback",
                    "error": str(exc),
                }
            else:
                for candidate in candidates:
                    candidate["indexes"] = indexes.get(
                        candidate["symbol"],
                        {},
                    )
                self._attach_relative_strength(
                    candidates,
                    indexes.get(benchmark, {}),
                    normalized_direction,
                    benchmark,
                )
                enrichment = {
                    "status": "available",
                    "error": None,
                }
        else:
            for candidate in candidates:
                candidate["indexes"] = {}
                candidate["relative_strength"] = self._empty_relative_strength(
                    benchmark,
                    normalized_direction,
                )
        for candidate in candidates:
            candidate.setdefault("indexes", {})
            candidate.setdefault(
                "relative_strength",
                self._empty_relative_strength(
                    benchmark,
                    normalized_direction,
                ),
            )

        relative_filter_keys = {
            "min_market_rs_10d",
            "min_market_rs_half_year",
        }
        for filter_key in relative_filter_keys.intersection(
            normalized_filters
        ):
            metric_key = filter_key.replace("min_", "")
            if candidates and not any(
                (candidate.get("relative_strength") or {}).get(metric_key)
                is not None
                for candidate in candidates
            ):
                raise LongbridgeAPIError(
                    f"市场基准 {benchmark} 的 {metric_key} 数据不可用"
                )

        short_risk_status = {
            "status": "not_applicable"
            if normalized_direction != "SHORT"
            else "disabled",
            "error": None,
        }
        needs_short_risk = (
            normalized_direction == "SHORT"
            and (
                include_short_risk
                or bool(
                    SHORT_RISK_FILTER_KEYS.intersection(
                        normalized_filters
                    )
                )
            )
        )
        if needs_short_risk and candidates:
            try:
                short_risk = run_external_call(
                    "quote",
                    "short_positions",
                    self._short_risk_loader,
                    [
                        candidate["symbol"]
                        for candidate in candidates
                    ],
                )
            except Exception as exc:
                if SHORT_RISK_FILTER_KEYS.intersection(
                    normalized_filters
                ):
                    if isinstance(exc, LongbridgeAPIError):
                        raise
                    raise LongbridgeAPIError(
                        f"无法应用做空拥挤度过滤: {exc}"
                    ) from exc
                logger.warning("Longbridge short-risk metrics unavailable: %s", exc)
                short_risk_status = {
                    "status": "fallback",
                    "error": str(exc),
                }
            else:
                for candidate in candidates:
                    candidate["short_risk"] = short_risk.get(
                        candidate["symbol"],
                        {
                            "status": "no_data",
                            "error": None,
                        },
                    )
                short_risk_status = {
                    "status": "available",
                    "error": None,
                }
        for candidate in candidates:
            candidate.setdefault("short_risk", {
                "status": "not_applicable"
                if normalized_direction != "SHORT"
                else short_risk_status["status"],
                "error": short_risk_status["error"],
            })

        candidate_symbols = [
            candidate["symbol"]
            for candidate in candidates
        ]
        hard_tradeability = bool(
            require_normal_trade_status
            or TRADEABILITY_FILTER_KEYS.intersection(normalized_filters)
        )
        include_depth = bool(
            TRADEABILITY_FILTER_KEYS.intersection(normalized_filters)
        )
        tradeability_status = {
            "status": "disabled",
            "error": None,
            "require_normal_trade_status": require_normal_trade_status,
            "depth_included": include_depth,
        }
        if include_tradeability and candidates:
            try:
                tradeability = run_external_call(
                    "quote",
                    "candidate_tradeability",
                    self._tradeability_loader,
                    candidate_symbols,
                    include_depth,
                    retry_if=_retry_candidate_batch,
                )
            except Exception as exc:
                if hard_tradeability:
                    raise LongbridgeAPIError(
                        f"无法应用交易可执行性过滤: {exc}"
                    ) from exc
                logger.warning(
                    "Longbridge candidate tradeability unavailable: %s",
                    exc,
                )
                tradeability_status["status"] = "fallback"
                tradeability_status["error"] = str(exc)
            else:
                for candidate in candidates:
                    candidate["tradeability"] = tradeability.get(
                        candidate["symbol"],
                        {
                            "status": "no_data",
                            "error": None,
                            "trade_status": None,
                            "is_tradable": None,
                            "spread_bps": None,
                            "top_of_book_notional": None,
                            "impact_cost_status": (
                                "requires_order_size"
                            ),
                        },
                    )
                tradeability_status["status"] = "available"
        for candidate in candidates:
            candidate.setdefault("tradeability", {
                "status": tradeability_status["status"],
                "error": tradeability_status["error"],
                "trade_status": None,
                "is_tradable": None,
                "spread_bps": None,
                "top_of_book_notional": None,
                "impact_cost_status": "requires_order_size",
            })

        event_thresholds = [
            normalized_filters[key]
            for key in (
                "min_days_to_financial_event",
                "min_days_to_corporate_action",
            )
            if key in normalized_filters
        ]
        effective_event_window = max([
            fundamental_event_window_days,
            *(
                math.ceil(value)
                for value in event_thresholds
            ),
        ])
        corporate_actions_requested = bool(
            include_corporate_actions
            or "min_days_to_corporate_action" in normalized_filters
        )
        hard_fundamental_filters = (
            FUNDAMENTAL_FILTER_KEYS.intersection(normalized_filters)
        )
        needs_fundamentals = bool(
            include_fundamentals or hard_fundamental_filters
        )
        fundamental_status = {
            "status": "disabled",
            "error": None,
            "event_window_days": effective_event_window,
            "corporate_actions_included": (
                corporate_actions_requested
            ),
        }
        if needs_fundamentals and candidates:
            try:
                fundamentals = run_external_call(
                    "fundamental",
                    "candidate_profiles",
                    self._fundamental_loader,
                    candidate_symbols,
                    normalized_market,
                    normalized_direction,
                    effective_event_window,
                    corporate_actions_requested,
                    retry_if=_retry_candidate_batch,
                )
            except Exception as exc:
                if hard_fundamental_filters:
                    raise LongbridgeAPIError(
                        f"无法应用财务或事件过滤: {exc}"
                    ) from exc
                logger.warning(
                    "Longbridge candidate fundamentals unavailable: %s",
                    exc,
                )
                fundamental_status["status"] = "fallback"
                fundamental_status["error"] = str(exc)
            else:
                for candidate in candidates:
                    candidate["fundamentals"] = fundamentals.get(
                        candidate["symbol"],
                        {
                            "status": "no_data",
                            "errors": [],
                        },
                    )
                fundamental_status["status"] = "available"
        for candidate in candidates:
            candidate.setdefault("fundamentals", {
                "status": fundamental_status["status"],
                "errors": (
                    [fundamental_status["error"]]
                    if fundamental_status["error"]
                    else []
                ),
            })

        hard_margin_filters = MARGIN_FILTER_KEYS.intersection(
            normalized_filters
        )
        needs_margin = bool(
            include_margin_requirements or hard_margin_filters
        )
        margin_status = {
            "status": "disabled",
            "error": None,
            "borrow_availability": "unknown",
        }
        if needs_margin and candidates:
            try:
                margin_requirements = run_external_call(
                    "trade",
                    "margin_requirements",
                    self._margin_loader,
                    candidate_symbols,
                    retry_if=_retry_candidate_batch,
                )
            except Exception as exc:
                if hard_margin_filters:
                    raise LongbridgeAPIError(
                        f"无法应用保证金比例过滤: {exc}"
                    ) from exc
                logger.warning(
                    "Longbridge margin requirements unavailable: %s",
                    exc,
                )
                margin_status["status"] = "fallback"
                margin_status["error"] = str(exc)
            else:
                for candidate in candidates:
                    candidate[
                        "margin_requirements"
                    ] = margin_requirements.get(
                        candidate["symbol"],
                        {
                            "status": "no_data",
                            "error": None,
                            "borrow_availability": "unknown",
                            "borrow_fee_rate": None,
                        },
                    )
                margin_status["status"] = "available"
        for candidate in candidates:
            candidate.setdefault("margin_requirements", {
                "status": margin_status["status"],
                "error": margin_status["error"],
                "borrow_availability": "unknown",
                "borrow_fee_rate": None,
            })

        before_filter_count = len(candidates)
        candidates, exclusion_reasons = self._apply_candidate_filters(
            candidates,
            normalized_filters,
            require_normal_trade_status,
        )
        total = self._read_int(
            container,
            ("total", "total_count", "totalCount", "count"),
        )
        if total is None:
            total = self._read_int(
                payload,
                ("total", "total_count", "totalCount", "count"),
            )
        if total is None:
            total = page * size + before_filter_count

        has_more = self._read_bool(
            container,
            ("has_more", "hasMore", "more"),
        )
        if has_more is None:
            has_more = (page + 1) * size < total

        return {
            "market": normalized_market,
            "strategy_id": int(strategy_id),
            "source": "longbridge-screener",
            "page": page,
            "size": size,
            "total": total,
            "has_more": has_more,
            "enrichment": enrichment,
            "relative_strength": {
                "benchmark_symbol": benchmark,
                "target_direction": normalized_direction,
                "industry_basis": "current_page_industry_median",
            },
            "short_risk": short_risk_status,
            "tradeability": tradeability_status,
            "fundamentals": fundamental_status,
            "margin_requirements": margin_status,
            "filters": {
                "applied": {
                    **normalized_filters,
                    "require_normal_trade_status": (
                        require_normal_trade_status
                    ),
                },
                "before": before_filter_count,
                "after": len(candidates),
                "excluded": before_filter_count - len(candidates),
                "reasons": exclusion_reasons,
            },
            "items": candidates,
        }

    def _normalize_filters(self, filters: Dict[str, Any]) -> Dict[str, float]:
        supported = {
            "min_turnover": (0, None),
            "min_market_value": (0, None),
            "min_turnover_rate": (0, None),
            "min_pe_ttm": (0, None),
            "max_pe_ttm": (0, None),
            "max_pb": (0, None),
            "min_capital_flow": (None, None),
            "min_volume_ratio": (0, None),
            "min_market_rs_10d": (None, None),
            "min_market_rs_half_year": (None, None),
            "min_industry_rs_10d": (None, None),
            "min_industry_rs_half_year": (None, None),
            "max_days_to_cover": (0, None),
            "max_short_ratio": (0, None),
            "max_short_ratio_change": (None, None),
            "max_spread_bps": (0, None),
            "min_top_of_book_notional": (0, None),
            "min_revenue_yoy": (None, None),
            "max_revenue_yoy": (None, None),
            "min_net_profit_yoy": (None, None),
            "max_net_profit_yoy": (None, None),
            "min_operating_cash_flow_yoy": (None, None),
            "min_analyst_alignment": (-1, 1),
            "min_eps_revision_alignment": (-1, 1),
            "min_days_to_financial_event": (0, 365),
            "min_days_to_corporate_action": (0, 365),
            "max_initial_margin_ratio": (0, None),
        }
        unknown = set(filters) - set(supported)
        if unknown:
            raise ValueError(
                f"不支持的候选过滤项: {', '.join(sorted(unknown))}"
            )

        normalized: Dict[str, float] = {}
        for key, value in filters.items():
            if value is None or value == "":
                continue
            try:
                number = float(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{key} 必须是数字") from exc
            if not math.isfinite(number):
                raise ValueError(f"{key} 必须是有限数字")
            minimum, maximum = supported[key]
            if minimum is not None and number < minimum:
                raise ValueError(f"{key} 不能小于 {minimum}")
            if maximum is not None and number > maximum:
                raise ValueError(f"{key} 不能大于 {maximum}")
            normalized[key] = number
        if (
            "min_pe_ttm" in normalized
            and "max_pe_ttm" in normalized
            and normalized["min_pe_ttm"] > normalized["max_pe_ttm"]
        ):
            raise ValueError("min_pe_ttm 不能大于 max_pe_ttm")
        for minimum_key, maximum_key in (
            ("min_revenue_yoy", "max_revenue_yoy"),
            ("min_net_profit_yoy", "max_net_profit_yoy"),
        ):
            if (
                minimum_key in normalized
                and maximum_key in normalized
                and normalized[minimum_key] > normalized[maximum_key]
            ):
                raise ValueError(
                    f"{minimum_key} 不能大于 {maximum_key}"
                )
        return normalized

    def _apply_candidate_filters(
        self,
        candidates: List[Dict[str, Any]],
        filters: Dict[str, float],
        require_normal_trade_status: bool,
    ) -> tuple[List[Dict[str, Any]], Dict[str, int]]:
        if not filters and not require_normal_trade_status:
            return candidates, {}

        rules = (
            ("min_turnover", "indexes", "turnover", "min"),
            ("min_market_value", "indexes", "total_market_value", "min"),
            ("min_turnover_rate", "indexes", "turnover_rate", "min"),
            ("min_pe_ttm", "indexes", "pe_ttm_ratio", "min"),
            ("max_pe_ttm", "indexes", "pe_ttm_ratio", "max"),
            ("max_pb", "indexes", "pb_ratio", "max"),
            ("min_capital_flow", "indexes", "capital_flow", "min"),
            ("min_volume_ratio", "indexes", "volume_ratio", "min"),
            ("min_market_rs_10d", "relative_strength", "market_rs_10d", "min"),
            (
                "min_market_rs_half_year",
                "relative_strength",
                "market_rs_half_year",
                "min",
            ),
            (
                "min_industry_rs_10d",
                "relative_strength",
                "industry_rs_10d",
                "min",
            ),
            (
                "min_industry_rs_half_year",
                "relative_strength",
                "industry_rs_half_year",
                "min",
            ),
            ("max_days_to_cover", "short_risk", "days_to_cover", "max"),
            ("max_short_ratio", "short_risk", "short_ratio", "max"),
            (
                "max_short_ratio_change",
                "short_risk",
                "short_ratio_change",
                "max",
            ),
            (
                "max_spread_bps",
                "tradeability",
                "spread_bps",
                "max",
            ),
            (
                "min_top_of_book_notional",
                "tradeability",
                "top_of_book_notional",
                "min",
            ),
            (
                "min_revenue_yoy",
                "fundamentals",
                "revenue_yoy",
                "min",
            ),
            (
                "max_revenue_yoy",
                "fundamentals",
                "revenue_yoy",
                "max",
            ),
            (
                "min_net_profit_yoy",
                "fundamentals",
                "net_profit_yoy",
                "min",
            ),
            (
                "max_net_profit_yoy",
                "fundamentals",
                "net_profit_yoy",
                "max",
            ),
            (
                "min_operating_cash_flow_yoy",
                "fundamentals",
                "operating_cash_flow_yoy",
                "min",
            ),
            (
                "min_analyst_alignment",
                "fundamentals",
                "analyst_alignment",
                "min",
            ),
            (
                "min_eps_revision_alignment",
                "fundamentals",
                "eps_revision_alignment",
                "min",
            ),
            (
                "min_days_to_financial_event",
                "fundamentals",
                "days_to_financial_event",
                "min",
            ),
            (
                "min_days_to_corporate_action",
                "fundamentals",
                "days_to_corporate_action",
                "min",
            ),
            (
                "max_initial_margin_ratio",
                "margin_requirements",
                "initial_margin_ratio",
                "max",
            ),
        )
        kept = []
        reasons: Dict[str, int] = {}
        for candidate in candidates:
            failure_reason = None
            if require_normal_trade_status:
                tradeability = candidate.get("tradeability") or {}
                if tradeability.get("is_tradable") is not True:
                    trade_status = tradeability.get("trade_status")
                    failure_reason = (
                        f"trade_status_{trade_status}"
                        if trade_status
                        else "missing_trade_status"
                    )
            for filter_key, section, metric_key, comparison in rules:
                if failure_reason:
                    break
                threshold = filters.get(filter_key)
                if threshold is None:
                    continue
                value = (candidate.get(section) or {}).get(metric_key)
                if value is None:
                    failure_reason = f"missing_{metric_key}"
                    break
                if comparison == "min" and value < threshold:
                    failure_reason = f"below_{filter_key}"
                    break
                if comparison == "max" and value > threshold:
                    failure_reason = f"above_{filter_key}"
                    break
            if failure_reason:
                reasons[failure_reason] = reasons.get(failure_reason, 0) + 1
            else:
                kept.append(candidate)
        return kept, reasons

    def _attach_relative_strength(
        self,
        candidates: List[Dict[str, Any]],
        benchmark_indexes: Dict[str, Optional[float]],
        target_direction: str,
        benchmark_symbol: str,
    ) -> None:
        direction = 1 if target_direction == "LONG" else -1
        horizons = {
            "10d": "ten_day_change_rate",
            "half_year": "half_year_change_rate",
        }
        industry_groups: Dict[str, Dict[str, List[float]]] = {}
        for candidate in candidates:
            industry = str(
                candidate.get("indicators", {}).get("industry") or ""
            ).strip()
            if not industry:
                continue
            industry_groups.setdefault(
                industry,
                {horizon: [] for horizon in horizons},
            )
            for horizon, metric in horizons.items():
                value = (candidate.get("indexes") or {}).get(metric)
                if value is not None:
                    industry_groups[industry][horizon].append(value)

        industry_medians = {
            industry: {
                horizon: median(values) if values else None
                for horizon, values in by_horizon.items()
            }
            for industry, by_horizon in industry_groups.items()
        }
        for candidate in candidates:
            indexes = candidate.get("indexes") or {}
            industry = str(
                candidate.get("indicators", {}).get("industry") or ""
            ).strip()
            peer_count = max(
                (
                    len(values)
                    for values in industry_groups.get(industry, {}).values()
                ),
                default=0,
            )
            relative = self._empty_relative_strength(
                benchmark_symbol,
                target_direction,
            )
            relative["industry"] = industry or None
            relative["industry_peer_count"] = peer_count
            for horizon, metric in horizons.items():
                stock_return = indexes.get(metric)
                market_return = benchmark_indexes.get(metric)
                industry_return = (
                    industry_medians.get(industry, {}).get(horizon)
                    if (
                        industry
                        and len(
                            industry_groups.get(industry, {}).get(
                                horizon,
                                [],
                            )
                        ) >= 2
                    )
                    else None
                )
                if stock_return is not None and market_return is not None:
                    relative[f"market_rs_{horizon}"] = round(
                        direction * (stock_return - market_return),
                        6,
                    )
                if stock_return is not None and industry_return is not None:
                    relative[f"industry_rs_{horizon}"] = round(
                        direction * (stock_return - industry_return),
                        6,
                    )
            candidate["relative_strength"] = relative

    @staticmethod
    def _empty_relative_strength(
        benchmark_symbol: str,
        target_direction: str,
    ) -> Dict[str, Any]:
        return {
            "benchmark_symbol": benchmark_symbol,
            "target_direction": target_direction,
            "industry": None,
            "industry_peer_count": 0,
            "market_rs_10d": None,
            "market_rs_half_year": None,
            "industry_rs_10d": None,
            "industry_rs_half_year": None,
        }

    def _normalize_strategies(
        self,
        payload: Any,
        source: str,
        default_market: str,
    ) -> List[Dict[str, Any]]:
        strategies: List[Dict[str, Any]] = []
        for record in self._walk_dicts(payload):
            strategy_id = self._first_int(record, _STRATEGY_ID_KEYS)
            name = self._first_text(record, _STRATEGY_NAME_KEYS)
            if strategy_id is None or strategy_id <= 0 or not name:
                continue
            market = self._first_text(record, ("market",)) or default_market
            description = self._first_text(
                record,
                ("description", "desc", "summary", "strategy_description"),
            )
            strategies.append({
                "id": strategy_id,
                "name": name,
                "description": description,
                "market": self._response_market(market, default_market),
                "source": source,
            })
        return strategies

    def _normalize_candidates(
        self,
        payload: Any,
        default_market: str,
        page: int,
        size: int,
    ) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
        container = self._find_candidate_container(
            payload,
            default_market,
        )
        records: List[Dict[str, Any]] = []
        if container:
            for key in ("items", "list", "results", "stocks", "securities"):
                value = container.get(key)
                if isinstance(value, list):
                    records = [item for item in value if isinstance(item, dict)]
                    break
        elif isinstance(payload, list):
            records = [item for item in payload if isinstance(item, dict)]

        if not records:
            records = [
                record
                for record in self._walk_dicts(payload)
                if self._candidate_symbol(record, default_market)
            ]

        candidates: List[Dict[str, Any]] = []
        seen_symbols = set()
        for record in records:
            symbol = self._candidate_symbol(record, default_market)
            if not symbol:
                continue
            symbol = symbol.strip().upper()
            if symbol in seen_symbols:
                continue
            seen_symbols.add(symbol)

            indicators = self._normalize_indicators(record.get("indicators"))
            for key in (
                "prevclose",
                "prevchg",
                "marketcap",
                "salesgrowthyoy",
                "pettm",
                "pbmrq",
                "industry",
            ):
                if key not in indicators:
                    value = record.get(key)
                    if self._is_scalar(value):
                        indicators[key] = value

            candidate_market = (
                self._candidate_text(record, ("market",))
                or self._market_from_symbol(symbol)
                or default_market
            )
            candidates.append({
                "rank": page * size + len(candidates) + 1,
                "symbol": symbol,
                "name": self._candidate_text(record, _NAME_KEYS) or symbol,
                "market": self._response_market(
                    candidate_market,
                    default_market,
                ),
                "indicators": indicators,
                "indexes": {},
            })

        return candidates, container or {}

    def _find_candidate_container(
        self,
        payload: Any,
        default_market: str,
    ) -> Optional[Dict[str, Any]]:
        for record in self._walk_dicts(payload):
            for key in ("items", "list", "results", "stocks", "securities"):
                value = record.get(key)
                if (
                    isinstance(value, list)
                    and any(
                        isinstance(item, dict)
                        and self._candidate_symbol(
                            item,
                            default_market,
                        )
                        for item in value
                    )
                ):
                    return record
        return None

    def _candidate_text(
        self,
        record: Dict[str, Any],
        keys: tuple[str, ...],
    ) -> Optional[str]:
        value = self._first_text(record, keys)
        if value:
            return value
        for nested_key in ("security", "stock", "quote", "basic"):
            nested = record.get(nested_key)
            if isinstance(nested, dict):
                value = self._first_text(nested, keys)
                if value:
                    return value
        return None

    def _candidate_symbol(
        self,
        record: Dict[str, Any],
        default_market: str,
    ) -> Optional[str]:
        symbol = self._candidate_text(record, _SYMBOL_KEYS)
        if symbol:
            return symbol
        counter_id = self._candidate_text(
            record,
            ("counter_id", "counterId"),
        )
        if not counter_id:
            return None
        parts = [
            part.strip()
            for part in counter_id.split("/")
            if part.strip()
        ]
        if len(parts) < 3 or parts[0].upper() != "ST":
            return None
        market = parts[1].upper()
        code = "/".join(parts[2:]).upper()
        if not code:
            return None
        suffix = {
            "US": "US",
            "HK": "HK",
            "SG": "SG",
            "SH": "SH",
            "SZ": "SZ",
            "CN": default_market.upper(),
        }.get(market)
        if suffix not in {"US", "HK", "SG", "SH", "SZ"}:
            return None
        if suffix == "HK":
            code = code.lstrip("0") or "0"
        return f"{code}.{suffix}"

    @staticmethod
    def _market_from_symbol(symbol: str) -> Optional[str]:
        suffix = symbol.rsplit(".", 1)[-1].upper()
        if suffix in {"SH", "SZ"}:
            return "CN"
        if suffix in SUPPORTED_SCREENER_MARKETS:
            return suffix
        return None

    def _normalize_indicators(self, value: Any) -> Dict[str, Any]:
        if isinstance(value, dict):
            return {
                str(key).removeprefix("filter_"): item
                for key, item in value.items()
                if self._is_scalar(item)
            }
        if not isinstance(value, list):
            return {}

        indicators: Dict[str, Any] = {}
        for item in value:
            if not isinstance(item, dict):
                continue
            key = self._first_text(item, ("key", "indicator", "name"))
            if not key:
                continue
            indicator_value = self._first_scalar(
                item,
                ("value", "display_value", "displayValue", "text"),
            )
            indicators[key.removeprefix("filter_")] = indicator_value
        return indicators

    @classmethod
    def _response_market(cls, value: str, default_market: str) -> str:
        normalized = value.strip().upper()
        if normalized in SUPPORTED_SCREENER_MARKETS:
            return normalized
        return cls.normalize_market(default_market)

    def _walk_dicts(self, payload: Any) -> Iterator[Dict[str, Any]]:
        if isinstance(payload, dict):
            yield payload
            for value in payload.values():
                yield from self._walk_dicts(value)
        elif isinstance(payload, list):
            for value in payload:
                yield from self._walk_dicts(value)

    def _read_int(
        self,
        payload: Any,
        keys: tuple[str, ...],
    ) -> Optional[int]:
        if isinstance(payload, dict):
            value = self._first_int(payload, keys)
            if value is not None:
                return value
            for nested in payload.values():
                value = self._read_int(nested, keys)
                if value is not None:
                    return value
        elif isinstance(payload, list):
            for nested in payload:
                value = self._read_int(nested, keys)
                if value is not None:
                    return value
        return None

    def _read_bool(
        self,
        payload: Any,
        keys: tuple[str, ...],
    ) -> Optional[bool]:
        if not isinstance(payload, dict):
            return None
        for key in keys:
            value = payload.get(key)
            if isinstance(value, bool):
                return value
        return None

    @staticmethod
    def _first_int(
        record: Dict[str, Any],
        keys: tuple[str, ...],
    ) -> Optional[int]:
        for key in keys:
            value = record.get(key)
            try:
                if value is not None and not isinstance(value, bool):
                    return int(value)
            except (TypeError, ValueError):
                continue
        return None

    @classmethod
    def _first_text(
        cls,
        record: Dict[str, Any],
        keys: tuple[str, ...],
    ) -> Optional[str]:
        for key in keys:
            value = record.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, dict):
                nested = cls._first_text(
                    value,
                    ("zh_cn", "zh-CN", "en", "value", "text", "name"),
                )
                if nested:
                    return nested
        return None

    @classmethod
    def _first_scalar(
        cls,
        record: Dict[str, Any],
        keys: tuple[str, ...],
    ) -> Any:
        for key in keys:
            value = record.get(key)
            if cls._is_scalar(value):
                return value
        return None

    @staticmethod
    def _is_scalar(value: Any) -> bool:
        return value is None or isinstance(value, (str, int, float, bool))


_stock_screener_service: Optional[StockScreenerService] = None


def get_stock_screener_service() -> StockScreenerService:
    global _stock_screener_service
    if _stock_screener_service is None:
        _stock_screener_service = StockScreenerService()
    return _stock_screener_service
