from __future__ import annotations

from contextlib import contextmanager
import logging
import math
from statistics import median
from typing import Any, Callable, Dict, Iterator, List, Optional

from .exceptions import LongbridgeAPIError, LongbridgeDependencyMissing
from .external_service_resilience import run_external_call
from .longbridge_compat import close_longbridge_context
from .repositories import load_credentials
from .services import get_security_calc_indexes, get_short_risk_metrics


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
    ) -> None:
        self._index_loader = index_loader
        self._short_risk_loader = short_risk_loader

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
        normalized_filters = self._normalize_filters(filters or {})
        short_filter_keys = {
            "max_days_to_cover",
            "max_short_ratio",
            "max_short_ratio_change",
        }
        if (
            normalized_direction != "SHORT"
            and short_filter_keys.intersection(normalized_filters)
        ):
            raise ValueError("做空拥挤度过滤仅适用于 SHORT 方向")

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
        if candidates and (include_indexes or normalized_filters):
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
                if normalized_filters:
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
                or bool(short_filter_keys.intersection(normalized_filters))
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
                if short_filter_keys.intersection(normalized_filters):
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

        before_filter_count = len(candidates)
        candidates, exclusion_reasons = self._apply_index_filters(
            candidates,
            normalized_filters,
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
            "filters": {
                "applied": normalized_filters,
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
        return normalized

    def _apply_index_filters(
        self,
        candidates: List[Dict[str, Any]],
        filters: Dict[str, float],
    ) -> tuple[List[Dict[str, Any]], Dict[str, int]]:
        if not filters:
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
        )
        kept = []
        reasons: Dict[str, int] = {}
        for candidate in candidates:
            failure_reason = None
            for filter_key, section, metric_key, comparison in rules:
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
        container = self._find_candidate_container(payload)
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
                if self._candidate_text(record, _SYMBOL_KEYS)
            ]

        candidates: List[Dict[str, Any]] = []
        seen_symbols = set()
        for record in records:
            symbol = self._candidate_text(record, _SYMBOL_KEYS)
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
                self._candidate_text(record, ("market",)) or default_market
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

    def _find_candidate_container(self, payload: Any) -> Optional[Dict[str, Any]]:
        for record in self._walk_dicts(payload):
            for key in ("items", "list", "results", "stocks", "securities"):
                value = record.get(key)
                if (
                    isinstance(value, list)
                    and any(
                        isinstance(item, dict)
                        and self._candidate_text(item, _SYMBOL_KEYS)
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
