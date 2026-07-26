from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
from datetime import date, datetime, timedelta
import math
from typing import Any, Dict, Iterable, Iterator, List, Optional

import httpx

from .exceptions import LongbridgeAPIError, LongbridgeDependencyMissing
from .external_service_resilience import (
    ExternalServiceBusyError,
    ExternalServiceCircuitOpenError,
    ExternalServiceTimeoutError,
)
from .longbridge_compat import close_longbridge_context
from .repositories import _safe_float, load_credentials
from .stock_picker_ai_snapshots import sanitize_error


_REQUIRED_CREDENTIALS = (
    "LONGPORT_APP_KEY",
    "LONGPORT_APP_SECRET",
    "LONGPORT_ACCESS_TOKEN",
)

SHORT_CAPACITY_FAILURE_CATEGORIES = frozenset({
    "credentials_missing",
    "dependency_missing",
    "authentication_failed",
    "rate_limited",
    "timeout",
    "network_error",
    "service_busy",
    "circuit_open",
    "upstream_rejected",
    "response_no_data",
    "unknown_error",
})
_AUTHENTICATION_ERROR_CODES = {401, 401001, 401002, 401003}
_RATE_LIMIT_ERROR_CODES = {429, 429000, 429001}


def _symbols(values: Iterable[str]) -> List[str]:
    return list(dict.fromkeys(
        value.strip().upper()
        for value in values
        if value and value.strip()
    ))


def _credentials() -> Dict[str, str]:
    credentials = load_credentials()
    if not credentials or any(
        not credentials.get(key)
        for key in _REQUIRED_CREDENTIALS
    ):
        raise ValueError("请先在基础配置中保存完整的 Longbridge 凭据")
    return credentials


def classify_short_capacity_failure(error: BaseException) -> Dict[str, str]:
    """Classify only technical failures proven by the exception chain."""
    chain = list(_exception_chain(error))
    texts = [str(item).lower() for item in chain]

    if any(isinstance(item, LongbridgeDependencyMissing) for item in chain):
        category = "dependency_missing"
    elif any(
        isinstance(item, ValueError)
        and "longbridge" in text
        and "凭据" in text
        for item, text in zip(chain, texts)
    ):
        category = "credentials_missing"
    elif any(isinstance(item, ExternalServiceTimeoutError) for item in chain):
        category = "timeout"
    elif any(isinstance(item, httpx.TimeoutException) for item in chain):
        category = "timeout"
    elif any(isinstance(item, ExternalServiceBusyError) for item in chain):
        category = "service_busy"
    elif any(
        isinstance(item, ExternalServiceCircuitOpenError)
        for item in chain
    ):
        category = "circuit_open"
    elif any(_is_authentication_error(item) for item in chain):
        category = "authentication_failed"
    elif any(_is_rate_limit_error(item) for item in chain):
        category = "rate_limited"
    elif any(
        isinstance(item, (httpx.NetworkError, ConnectionError))
        or _exception_kind(item) == "http"
        for item in chain
    ):
        category = "network_error"
    elif any(_exception_kind(item) == "openapi" for item in chain):
        category = "upstream_rejected"
    else:
        category = "unknown_error"

    return {
        "failure_category": category,
        "error": sanitize_error(error),
    }


def _exception_chain(error: BaseException) -> Iterator[BaseException]:
    current: Optional[BaseException] = error
    seen = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def _exception_code(error: BaseException) -> Optional[int]:
    try:
        return int(getattr(error, "code", None))
    except (TypeError, ValueError):
        return None


def _exception_kind(error: BaseException) -> str:
    kind = str(getattr(error, "kind", "") or "").lower()
    return kind.rsplit(".", 1)[-1]


def _is_authentication_error(error: BaseException) -> bool:
    return (
        _exception_kind(error) == "oauth"
        or _exception_code(error) in _AUTHENTICATION_ERROR_CODES
    )


def _is_rate_limit_error(error: BaseException) -> bool:
    code = _exception_code(error)
    return code in _RATE_LIMIT_ERROR_CODES or (
        code is not None and code // 1000 == 429
    )


@contextmanager
def _context(kind: str) -> Iterator[Any]:
    credentials = _credentials()
    try:
        from longbridge.openapi import (
            CalendarContext,
            Config,
            FundamentalContext,
            QuoteContext,
            TradeContext,
        )
    except ModuleNotFoundError as exc:  # pragma: no cover - environment dependent
        raise LongbridgeDependencyMissing(
            "未找到 longbridge Python SDK，请先运行 `pip install longbridge`。"
        ) from exc

    context_types = {
        "calendar": CalendarContext,
        "fundamental": FundamentalContext,
        "quote": QuoteContext,
        "trade": TradeContext,
    }
    try:
        context_type = context_types[kind]
    except KeyError as exc:
        raise ValueError(f"未知 Longbridge context: {kind}") from exc

    try:
        config = Config.from_apikey(
            credentials["LONGPORT_APP_KEY"],
            credentials["LONGPORT_APP_SECRET"],
            credentials["LONGPORT_ACCESS_TOKEN"],
        )
        context = context_type(config)
    except Exception as exc:
        raise LongbridgeAPIError(
            f"创建 Longbridge {kind} 连接失败: {exc}"
        ) from exc
    try:
        yield context
    finally:
        try:
            close_longbridge_context(context)
        except Exception:  # noqa: S110 - best effort cleanup
            pass


def is_market_trading_day(
    market: str,
    trading_date: date,
) -> bool:
    normalized_market = market.strip().upper()
    if normalized_market not in {"US", "HK", "CN", "SG"}:
        raise ValueError("market 必须是 US、HK、CN 或 SG")
    if not isinstance(trading_date, date):
        raise ValueError("trading_date 必须是 date")

    try:
        from longbridge.openapi import Market
    except ModuleNotFoundError as exc:  # pragma: no cover - environment dependent
        raise LongbridgeDependencyMissing(
            "未找到 longbridge Python SDK，请先运行 `pip install longbridge`。"
        ) from exc

    with _context("quote") as context:
        response = context.trading_days(
            getattr(Market, normalized_market),
            trading_date,
            trading_date,
        )
    if response is None or not any(
        hasattr(response, field)
        for field in ("trading_days", "half_trading_days")
    ):
        raise LongbridgeAPIError("Longbridge 交易日历返回无效")
    trading_days = [
        *list(getattr(response, "trading_days", None) or []),
        *list(getattr(response, "half_trading_days", None) or []),
    ]
    return trading_date in {
        parsed
        for value in trading_days
        if (parsed := _parse_date(value)) is not None
    }


def get_security_static_info(
    symbols: Iterable[str],
    *,
    context: Optional[Any] = None,
) -> List[Dict[str, Any]]:
    """Load classification fields exposed by Longbridge static_info."""
    symbol_list = _symbols(symbols)
    if not symbol_list:
        return []

    try:
        context_scope = (
            _context("quote") if context is None else nullcontext(context)
        )
        with context_scope as active_context:
            response = active_context.static_info(symbol_list)
    except (ValueError, LongbridgeDependencyMissing, LongbridgeAPIError):
        raise
    except Exception as exc:
        raise LongbridgeAPIError(
            f"获取证券静态分类信息失败: {exc}"
        ) from exc

    items = []
    requested = set(symbol_list)
    for value in list(response or []):
        symbol = str(getattr(value, "symbol", "") or "").strip().upper()
        if not symbol or symbol not in requested:
            continue
        board_value = getattr(value, "board", None)
        board_raw = (
            board_value
            if isinstance(board_value, str)
            else getattr(board_value, "__name__", None)
            or getattr(board_value, "name", None)
            or str(board_value or "")
        )
        lot_size = getattr(value, "lot_size", None)
        try:
            normalized_lot_size = (
                int(lot_size) if lot_size is not None else None
            )
        except (TypeError, ValueError):
            normalized_lot_size = None
        if normalized_lot_size is not None and normalized_lot_size <= 0:
            normalized_lot_size = None
        items.append({
            "symbol": symbol,
            "board": _enum_name(board_value),
            "board_raw": str(board_raw).strip() or None,
            "exchange": str(
                getattr(value, "exchange", "") or ""
            ).strip(),
            "currency": str(
                getattr(value, "currency", "") or ""
            ).strip().upper(),
            "lot_size": normalized_lot_size,
        })
    return items


def get_security_tradeability(
    symbols: Iterable[str],
    include_depth: bool = False,
    *,
    context: Optional[Any] = None,
) -> Dict[str, Dict[str, Any]]:
    symbol_list = _symbols(symbols)
    if not symbol_list:
        return {}

    try:
        context_scope = (
            _context("quote") if context is None else nullcontext(context)
        )
        with context_scope as active_context:
            quotes = active_context.quote(symbol_list)
            result = {
                symbol: {
                    "status": "no_data",
                    "error": None,
                    "trade_status": None,
                    "is_tradable": None,
                    "spread_bps": None,
                    "best_bid": None,
                    "best_ask": None,
                    "top_of_book_notional": None,
                    "impact_cost_status": "requires_order_size",
                }
                for symbol in symbol_list
            }
            for quote in quotes:
                symbol = str(getattr(quote, "symbol", "") or "").upper()
                if symbol not in result:
                    continue
                status = _enum_name(getattr(quote, "trade_status", None))
                result[symbol].update({
                    "status": "available",
                    "trade_status": status,
                    "is_tradable": status == "normal",
                    "last_done": _safe_float(
                        getattr(quote, "last_done", None)
                    ),
                    "volume": _safe_float(getattr(quote, "volume", None)),
                    "turnover": _safe_float(
                        getattr(quote, "turnover", None)
                    ),
                    "data_as_of": str(
                        getattr(quote, "timestamp", "") or ""
                    ),
                })

            if include_depth:
                for symbol in symbol_list:
                    try:
                        depth = active_context.depth(symbol)
                        bid_price, bid_volume = _best_depth(
                            getattr(depth, "bids", []) or [],
                            highest=True,
                        )
                        ask_price, ask_volume = _best_depth(
                            getattr(depth, "asks", []) or [],
                            highest=False,
                        )
                        spread_bps = None
                        if (
                            bid_price is not None
                            and ask_price is not None
                            and ask_price >= bid_price
                        ):
                            midpoint = (bid_price + ask_price) / 2
                            if midpoint > 0:
                                spread_bps = (
                                    (ask_price - bid_price)
                                    / midpoint
                                    * 10000
                                )
                        top_notional = None
                        if (
                            bid_price is not None
                            and ask_price is not None
                            and bid_volume is not None
                            and ask_volume is not None
                        ):
                            top_notional = min(
                                bid_price * bid_volume,
                                ask_price * ask_volume,
                            )
                        result[symbol].update({
                            "best_bid": bid_price,
                            "best_ask": ask_price,
                            "spread_bps": spread_bps,
                            "top_of_book_notional": top_notional,
                        })
                    except Exception as exc:
                        result[symbol]["depth_error"] = str(exc)
    except (ValueError, LongbridgeDependencyMissing, LongbridgeAPIError):
        raise
    except Exception as exc:
        raise LongbridgeAPIError(
            f"获取候选股票交易状态失败: {exc}"
        ) from exc
    return result


def get_margin_requirements(
    symbols: Iterable[str],
) -> Dict[str, Dict[str, Any]]:
    symbol_list = _symbols(symbols)
    if not symbol_list:
        return {}
    results = {}
    try:
        with _context("trade") as context:
            for symbol in symbol_list:
                try:
                    ratio = context.margin_ratio(symbol)
                    results[symbol] = {
                        "status": "available",
                        "error": None,
                        "initial_margin_ratio": _safe_float(
                            getattr(ratio, "im_factor", None)
                        ),
                        "maintenance_margin_ratio": _safe_float(
                            getattr(ratio, "mm_factor", None)
                        ),
                        "forced_close_margin_ratio": _safe_float(
                            getattr(ratio, "fm_factor", None)
                        ),
                        "borrow_availability": "unknown",
                        "borrow_fee_rate": None,
                        "note": (
                            "保证金比例不代表实时券源可借或融券费率"
                        ),
                    }
                except Exception as exc:
                    results[symbol] = {
                        "status": "error",
                        "error": str(exc),
                        "borrow_availability": "unknown",
                        "borrow_fee_rate": None,
                    }
    except (ValueError, LongbridgeDependencyMissing, LongbridgeAPIError):
        raise
    except Exception as exc:
        raise LongbridgeAPIError(
            f"获取候选股票保证金比例失败: {exc}"
        ) from exc
    return results


def get_short_selling_capacity(
    symbols: Iterable[str],
) -> Dict[str, Dict[str, Any]]:
    symbol_list = _symbols(symbols)
    if not symbol_list:
        return {}

    results = {
        symbol: {
            "status": "unsupported",
            "error": None,
            "failure_category": None,
            "cash_max_qty": None,
            "margin_max_qty": None,
            "short_selling_max_qty": None,
            "availability": "unknown",
            "borrow_fee_rate": None,
            "recall_risk": "unknown",
            "source": "longbridge_estimate_max_purchase_quantity",
            "note": "账户点时预估不包含融券费率或召回风险",
        }
        for symbol in symbol_list
        if not symbol.endswith(".US")
    }
    us_symbols = [
        symbol
        for symbol in symbol_list
        if symbol.endswith(".US")
    ]
    if not us_symbols:
        return results

    try:
        from longbridge.openapi import OrderSide, OrderType
    except ModuleNotFoundError as exc:  # pragma: no cover - environment dependent
        raise LongbridgeDependencyMissing(
            "未找到 longbridge Python SDK，请先运行 `pip install longbridge`。"
        ) from exc

    try:
        with _context("trade") as context:
            for symbol in us_symbols:
                try:
                    response = context.estimate_max_purchase_quantity(
                        symbol,
                        OrderType.LO,
                        OrderSide.Sell,
                    )
                    cash_max_qty = _non_negative_float(
                        getattr(response, "cash_max_qty", None)
                    )
                    margin_max_qty = _non_negative_float(
                        getattr(response, "margin_max_qty", None)
                    )
                    results[symbol] = {
                        "status": (
                            "available"
                            if margin_max_qty is not None
                            else "no_data"
                        ),
                        "error": None,
                        "failure_category": (
                            None
                            if margin_max_qty is not None
                            else "response_no_data"
                        ),
                        "cash_max_qty": cash_max_qty,
                        "margin_max_qty": margin_max_qty,
                        "short_selling_max_qty": margin_max_qty,
                        "availability": (
                            "available"
                            if margin_max_qty is not None
                            and margin_max_qty > 0
                            else "unavailable"
                            if margin_max_qty == 0
                            else "unknown"
                        ),
                        "borrow_fee_rate": None,
                        "recall_risk": "unknown",
                        "source": (
                            "longbridge_estimate_max_purchase_quantity"
                        ),
                        "note": (
                            "Longbridge 账户风险控制点时预估；"
                            "不包含融券费率或召回风险"
                        ),
                    }
                except Exception as exc:
                    diagnostic = classify_short_capacity_failure(exc)
                    results[symbol] = {
                        "status": "error",
                        **diagnostic,
                        "cash_max_qty": None,
                        "margin_max_qty": None,
                        "short_selling_max_qty": None,
                        "availability": "unknown",
                        "borrow_fee_rate": None,
                        "recall_risk": "unknown",
                        "source": (
                            "longbridge_estimate_max_purchase_quantity"
                        ),
                        "note": (
                            "查询失败不能区分账户权限、风险控制或临时服务错误"
                        ),
                    }
    except (ValueError, LongbridgeDependencyMissing, LongbridgeAPIError):
        raise
    except Exception as exc:
        raise LongbridgeAPIError(
            f"获取账户预估可卖空数量失败: {exc}"
        ) from exc
    return results


def get_fundamental_profiles(
    symbols: Iterable[str],
    market: str,
    target_direction: str,
    event_window_days: int = 30,
    include_corporate_actions: bool = False,
    today: Optional[date] = None,
) -> Dict[str, Dict[str, Any]]:
    symbol_list = _symbols(symbols)
    if not symbol_list:
        return {}
    if not 1 <= event_window_days <= 365:
        raise ValueError("event_window_days 必须在 1～365 之间")
    normalized_direction = target_direction.strip().upper()
    if normalized_direction not in {"LONG", "SHORT"}:
        raise ValueError("target_direction 必须是 LONG 或 SHORT")
    direction = 1 if normalized_direction == "LONG" else -1
    current_date = today or date.today()
    results = {
        symbol: {
            "status": "available",
            "errors": [],
            "revenue_yoy": None,
            "net_profit_yoy": None,
            "operating_cash_flow_yoy": None,
            "analyst_alignment": None,
            "analyst_total": None,
            "target_price": None,
            "eps_revision_alignment": None,
            "days_to_financial_event": None,
            "financial_event": None,
            "days_to_corporate_action": None,
            "corporate_action": None,
        }
        for symbol in symbol_list
    }

    worker_count = min(3, len(symbol_list))
    symbol_chunks = [
        symbol_list[index::worker_count]
        for index in range(worker_count)
    ]
    try:
        with ThreadPoolExecutor(
            max_workers=worker_count,
            thread_name_prefix="candidate-fundamental",
        ) as executor:
            futures = [
                executor.submit(
                    _load_fundamental_chunk,
                    chunk,
                    results,
                    direction,
                    current_date,
                    event_window_days,
                    include_corporate_actions,
                )
                for chunk in symbol_chunks
                if chunk
            ]
            for future in futures:
                future.result()
    except (ValueError, LongbridgeDependencyMissing, LongbridgeAPIError):
        raise
    except Exception as exc:
        raise LongbridgeAPIError(
            f"获取候选股票基本面失败: {exc}"
        ) from exc

    try:
        _attach_financial_calendar(
            results,
            market,
            current_date,
            event_window_days,
        )
    except Exception as exc:
        for profile in results.values():
            profile["errors"].append(f"calendar: {exc}")

    for profile in results.values():
        if len(profile["errors"]) >= (
            4 if include_corporate_actions else 3
        ):
            profile["status"] = "error"
        elif profile["errors"]:
            profile["status"] = "partial"
    return results


def _load_fundamental_chunk(
    symbols: List[str],
    results: Dict[str, Dict[str, Any]],
    direction: int,
    current_date: date,
    event_window_days: int,
    include_corporate_actions: bool,
) -> None:
    with _context("fundamental") as context:
        for symbol in symbols:
            profile = results[symbol]
            _load_operating(context, symbol, profile)
            _load_analyst(context, symbol, profile, direction)
            _load_forecast(context, symbol, profile, direction)
            if include_corporate_actions:
                _load_corporate_actions(
                    context,
                    symbol,
                    profile,
                    current_date,
                    event_window_days,
                )


def _load_operating(context, symbol: str, profile: Dict[str, Any]) -> None:
    try:
        response = context.operating(symbol)
        items = list(getattr(response, "list", []) or [])
        latest = next(
            (item for item in items if getattr(item, "latest", False)),
            items[0] if items else None,
        )
        indicators = (
            getattr(getattr(latest, "financial", None), "indicators", [])
            if latest
            else []
        )
        for indicator in indicators or []:
            key = str(getattr(indicator, "field_name", "") or "").lower()
            yoy = _ratio(getattr(indicator, "yoy", None))
            if yoy is None:
                continue
            if (
                profile["revenue_yoy"] is None
                and "revenue" in key
                and "cost" not in key
            ):
                profile["revenue_yoy"] = yoy
            if (
                profile["net_profit_yoy"] is None
                and any(token in key for token in (
                    "net_profit",
                    "net_income",
                    "profit_attributable",
                ))
            ):
                profile["net_profit_yoy"] = yoy
            if (
                profile["operating_cash_flow_yoy"] is None
                and "operating" in key
                and "cash" in key
            ):
                profile["operating_cash_flow_yoy"] = yoy
    except Exception as exc:
        profile["errors"].append(f"operating: {exc}")


def _load_analyst(
    context,
    symbol: str,
    profile: Dict[str, Any],
    direction: int,
) -> None:
    try:
        response = context.institution_rating(symbol)
        evaluate = getattr(getattr(response, "latest", None), "evaluate", None)
        if evaluate is not None:
            total = int(getattr(evaluate, "total", 0) or 0)
            bullish = int(getattr(evaluate, "buy", 0) or 0) + int(
                getattr(evaluate, "over", 0) or 0
            )
            bearish = int(getattr(evaluate, "sell", 0) or 0) + int(
                getattr(evaluate, "under", 0) or 0
            )
            profile["analyst_total"] = total
            if total > 0:
                profile["analyst_alignment"] = (
                    direction * (bullish - bearish) / total
                )
        profile["target_price"] = _safe_float(
            getattr(getattr(response, "summary", None), "target", None)
        )
    except Exception as exc:
        profile["errors"].append(f"analyst: {exc}")


def _load_forecast(
    context,
    symbol: str,
    profile: Dict[str, Any],
    direction: int,
) -> None:
    try:
        items = list(
            getattr(context.forecast_eps(symbol), "items", []) or []
        )
        if not items:
            return
        latest = max(
            items,
            key=lambda item: str(
                getattr(item, "forecast_end_date", "") or ""
            ),
        )
        total = int(getattr(latest, "institution_total", 0) or 0)
        raised = int(getattr(latest, "institution_up", 0) or 0)
        lowered = int(getattr(latest, "institution_down", 0) or 0)
        if total > 0:
            profile["eps_revision_alignment"] = (
                direction * (raised - lowered) / total
            )
    except Exception as exc:
        profile["errors"].append(f"forecast: {exc}")


def _load_corporate_actions(
    context,
    symbol: str,
    profile: Dict[str, Any],
    current_date: date,
    window_days: int,
) -> None:
    try:
        items = list(
            getattr(context.corp_action(symbol), "items", []) or []
        )
        upcoming = []
        for item in items:
            event_date = _parse_date(getattr(item, "date", None))
            if event_date is None:
                continue
            days = (event_date - current_date).days
            if 0 <= days <= window_days:
                upcoming.append((days, item))
        if upcoming:
            days, item = min(upcoming, key=lambda value: value[0])
            profile["days_to_corporate_action"] = days
            profile["corporate_action"] = {
                "date": str(getattr(item, "date", "") or ""),
                "type": str(getattr(item, "act_type", "") or ""),
                "description": str(
                    getattr(item, "act_desc", "") or ""
                ),
            }
        else:
            profile["days_to_corporate_action"] = window_days + 1
    except Exception as exc:
        profile["errors"].append(f"corp_action: {exc}")


def _attach_financial_calendar(
    results: Dict[str, Dict[str, Any]],
    market: str,
    current_date: date,
    window_days: int,
) -> None:
    try:
        from longbridge.openapi import CalendarCategory
    except ModuleNotFoundError as exc:  # pragma: no cover
        raise LongbridgeDependencyMissing(
            "未找到 longbridge Python SDK"
        ) from exc
    end_date = current_date + timedelta(days=window_days)
    with _context("calendar") as context:
        query_start = current_date.isoformat()
        visited_starts = set()
        for _ in range(100):
            if query_start in visited_starts:
                break
            visited_starts.add(query_start)
            response = context.finance_calendar(
                CalendarCategory.Report,
                query_start,
                end_date.isoformat(),
                market,
            )
            for group in getattr(response, "list", []) or []:
                group_date = _parse_date(getattr(group, "date", None))
                for info in getattr(group, "infos", []) or []:
                    symbol = str(
                        getattr(info, "symbol", "") or ""
                    ).upper()
                    if symbol not in results:
                        continue
                    event_date = (
                        _parse_date(getattr(info, "date", None))
                        or group_date
                    )
                    if event_date is None:
                        continue
                    days = (event_date - current_date).days
                    current = results[symbol][
                        "days_to_financial_event"
                    ]
                    if 0 <= days <= window_days and (
                        current is None or days < current
                    ):
                        results[symbol][
                            "days_to_financial_event"
                        ] = days
                        results[symbol]["financial_event"] = {
                            "date": event_date.isoformat(),
                            "content": str(
                                getattr(info, "content", "") or ""
                            ),
                            "market_time": str(
                                getattr(
                                    info,
                                    "financial_market_time",
                                    "",
                                ) or ""
                            ),
                            "star": int(
                                getattr(info, "star", 0) or 0
                            ),
                        }
            next_start = str(
                getattr(response, "next_date", "") or ""
            ).strip()
            next_date = _parse_date(next_start)
            if (
                not next_start
                or next_date is None
                or next_date > end_date
            ):
                break
            query_start = next_date.isoformat()

    for symbol in results:
        if results[symbol]["days_to_financial_event"] is None:
            results[symbol]["days_to_financial_event"] = window_days + 1


def _best_depth(
    levels: Iterable[Any],
    highest: bool,
) -> tuple[Optional[float], Optional[float]]:
    values = []
    for level in levels:
        price = _safe_float(getattr(level, "price", None))
        volume = _safe_float(getattr(level, "volume", None))
        if price is not None and price > 0:
            values.append((price, volume))
    if not values:
        return None, None
    best_price = (
        max(price for price, _volume in values)
        if highest
        else min(price for price, _volume in values)
    )
    best_volumes = [
        volume
        for price, volume in values
        if price == best_price and volume is not None
    ]
    return (
        best_price,
        sum(best_volumes) if best_volumes else None,
    )


def _enum_name(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        text = value
    elif isinstance(value, type):
        text = value.__name__
    else:
        text = getattr(value, "name", None) or str(value)
        if (
            not text
            or (
                str(text).startswith("<")
                and str(text).endswith(">")
            )
        ):
            text = value.__class__.__name__
    normalized = str(text).split(".")[-1].strip().lower()
    return normalized or None


def _non_negative_float(value: Any) -> Optional[float]:
    number = _safe_float(value)
    if number is None or number < 0:
        return None
    return number


def _ratio(value: Any) -> Optional[float]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    percentage = text.endswith("%")
    if percentage:
        text = text[:-1]
    try:
        number = float(text)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number / 100 if percentage else number


def _parse_date(value: Any) -> Optional[date]:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    for pattern in ("%Y-%m-%d", "%Y.%m.%d", "%Y%m%d"):
        try:
            return datetime.strptime(text[:10], pattern).date()
        except ValueError:
            continue
    return None
