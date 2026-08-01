from __future__ import annotations

import logging
import copy
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from fastapi import HTTPException

from .db import get_connection
from .exceptions import LongbridgeAPIError, LongbridgeDependencyMissing
from .longbridge_compat import close_longbridge_context
from .security_catalog import get_security_catalog_service
from .repositories import (
    fetch_latest_prices,
    fetch_latest_candlestick_timestamp,
    load_credentials,
    load_symbols,
    store_candlesticks,
    _safe_float,
)
from .repositories import fetch_bars_from_ticks

try:
    from .repositories import fetch_candlesticks as _repo_fetch_candlesticks
except ImportError:  # pragma: no cover - fallback when symbol is missing
    _repo_fetch_candlesticks = None


logger = logging.getLogger(__name__)
_candlestick_fallback_warned = False
_portfolio_cache_lock = threading.Lock()
_portfolio_cache_value: Optional[Dict[str, object]] = None
_portfolio_cache_expires_at = 0.0
_PORTFOLIO_CACHE_TTL_SECONDS = 15.0
_INCREMENTAL_CANDLESTICK_MAX_AGE_DAYS = 14

_PERIOD_NAME_MAP = {
    "min1": "Min_1",
    "min5": "Min_5",
    "min15": "Min_15",
    "min30": "Min_30",
    "min60": "Min_60",
    "min240": "Min_240",
    "day": "Day",
    "week": "Week",
    "month": "Month",
    "year": "Year",
}

_ADJUST_NAME_MAP = {
    "no_adjust": "NoAdjust",
    "forward_adjust": "ForwardAdjust",
    "backward_adjust": "BackwardAdjust",
}


@contextmanager
def _quote_context(creds: Dict[str, str]):
    try:
        from longbridge.openapi import QuoteContext, Config
    except ModuleNotFoundError as exc:  # pragma: no cover - depends on environment
        raise LongbridgeDependencyMissing(
            "未找到 longbridge Python SDK，请先运行 `pip install longbridge`。"
        ) from exc

    config = Config.from_apikey(
        creds.get("LONGPORT_APP_KEY", ""),
        creds.get("LONGPORT_APP_SECRET", ""),
        creds.get("LONGPORT_ACCESS_TOKEN", ""),
    )
    ctx = QuoteContext(config)
    try:
        yield ctx
    finally:  # pragma: no branch
        try:
            close_longbridge_context(ctx)
        except Exception:  # noqa: S110 - best effort cleanup
            pass


def verify_quote_access(symbols: Optional[Iterable[str]] = None) -> dict[str, str]:
    creds = load_credentials()
    if not creds or any(not creds.get(key) for key in ("LONGPORT_APP_KEY", "LONGPORT_APP_SECRET", "LONGPORT_ACCESS_TOKEN")):
        raise HTTPException(status_code=400, detail="请先配置完整的 Longbridge 凭据。")

    symbols_list = list(symbols or [])
    if not symbols_list:
        symbols_list = ["700.HK"]

    with _quote_context(creds) as ctx:
        try:
            ctx.quote(symbols_list)
        except Exception as exc:
            raise LongbridgeAPIError(str(exc)) from exc

    return {"status": "ok", "tested_symbols": ",".join(symbols_list)}


def get_security_calc_indexes(
    symbols: Iterable[str],
) -> Dict[str, Dict[str, Optional[float]]]:
    """Fetch stock-picker liquidity, valuation, and momentum indexes in one call."""
    symbol_list = list(dict.fromkeys(
        symbol.strip().upper()
        for symbol in symbols
        if symbol and symbol.strip()
    ))
    if not symbol_list:
        return {}

    credentials = load_credentials()
    if not credentials or any(
        not credentials.get(key)
        for key in (
            "LONGPORT_APP_KEY",
            "LONGPORT_APP_SECRET",
            "LONGPORT_ACCESS_TOKEN",
        )
    ):
        raise ValueError("请先在基础配置中保存完整的 Longbridge 凭据")

    try:
        from longbridge.openapi import CalcIndex
    except ModuleNotFoundError as exc:  # pragma: no cover - environment dependent
        raise LongbridgeDependencyMissing(
            "未找到 longbridge Python SDK，请先运行 `pip install longbridge`。"
        ) from exc

    requested_indexes = [
        CalcIndex.LastDone,
        CalcIndex.ChangeRate,
        CalcIndex.Volume,
        CalcIndex.Turnover,
        CalcIndex.TurnoverRate,
        CalcIndex.TotalMarketValue,
        CalcIndex.CapitalFlow,
        CalcIndex.VolumeRatio,
        CalcIndex.PeTtmRatio,
        CalcIndex.PbRatio,
        CalcIndex.DividendRatioTtm,
        CalcIndex.FiveDayChangeRate,
        CalcIndex.TenDayChangeRate,
        CalcIndex.HalfYearChangeRate,
        CalcIndex.YtdChangeRate,
    ]
    try:
        with _quote_context(credentials) as context:
            rows = context.calc_indexes(symbol_list, requested_indexes)
    except (LongbridgeDependencyMissing, ValueError):
        raise
    except Exception as exc:
        raise LongbridgeAPIError(f"获取 Longbridge 股票指标失败: {exc}") from exc

    fields = (
        "last_done",
        "change_rate",
        "volume",
        "turnover",
        "turnover_rate",
        "total_market_value",
        "capital_flow",
        "volume_ratio",
        "pe_ttm_ratio",
        "pb_ratio",
        "dividend_ratio_ttm",
        "five_day_change_rate",
        "ten_day_change_rate",
        "half_year_change_rate",
        "ytd_change_rate",
    )
    return {
        str(row.symbol).strip().upper(): {
            field: _safe_float(getattr(row, field, None))
            for field in fields
        }
        for row in rows
        if getattr(row, "symbol", None)
    }


def get_short_risk_metrics(
    symbols: Iterable[str],
    count: int = 5,
) -> Dict[str, Dict[str, object]]:
    """Fetch recent US/HK short-interest metrics with one reused quote context."""
    if not 1 <= count <= 100:
        raise ValueError("count 必须在 1～100 之间")
    symbol_list = list(dict.fromkeys(
        symbol.strip().upper()
        for symbol in symbols
        if symbol and symbol.strip()
    ))
    results: Dict[str, Dict[str, object]] = {
        symbol: {
            "status": "unsupported",
            "error": None,
        }
        for symbol in symbol_list
        if not symbol.endswith((".US", ".HK"))
    }
    supported = [
        symbol
        for symbol in symbol_list
        if symbol.endswith((".US", ".HK"))
    ]
    if not supported:
        return results

    credentials = load_credentials()
    if not credentials or any(
        not credentials.get(key)
        for key in (
            "LONGPORT_APP_KEY",
            "LONGPORT_APP_SECRET",
            "LONGPORT_ACCESS_TOKEN",
        )
    ):
        raise ValueError("请先在基础配置中保存完整的 Longbridge 凭据")

    try:
        with _quote_context(credentials) as context:
            for symbol in supported:
                try:
                    response = context.short_positions(symbol, count)
                    rows = sorted(
                        list(getattr(response, "data", []) or []),
                        key=lambda item: str(getattr(item, "timestamp", "")),
                        reverse=True,
                    )
                    if not rows:
                        results[symbol] = {
                            "status": "no_data",
                            "error": None,
                        }
                        continue

                    latest = rows[0]
                    previous = rows[1] if len(rows) > 1 else None
                    latest_rate = _safe_float(getattr(latest, "rate", None))
                    previous_rate = (
                        _safe_float(getattr(previous, "rate", None))
                        if previous
                        else None
                    )
                    results[symbol] = {
                        "status": "available",
                        "error": None,
                        "data_as_of": str(getattr(latest, "timestamp", "") or ""),
                        "short_ratio": latest_rate,
                        "short_ratio_change": (
                            latest_rate - previous_rate
                            if latest_rate is not None
                            and previous_rate is not None
                            else None
                        ),
                        "days_to_cover": _safe_float(
                            getattr(latest, "days_to_cover", None)
                        ),
                        "shares_short": _safe_float(
                            getattr(latest, "current_shares_short", None)
                        ),
                        "avg_daily_volume": _safe_float(
                            getattr(latest, "avg_daily_share_volume", None)
                        ),
                        "short_amount": _safe_float(
                            getattr(latest, "amount", None)
                        ),
                        "short_balance": _safe_float(
                            getattr(latest, "balance", None)
                        ),
                        "short_cost": _safe_float(
                            getattr(latest, "cost", None)
                        ),
                    }
                except Exception as exc:
                    logger.warning(
                        "Short-position metrics unavailable for %s: %s",
                        symbol,
                        exc,
                    )
                    results[symbol] = {
                        "status": "error",
                        "error": str(exc),
                    }
    except (LongbridgeDependencyMissing, ValueError):
        raise
    except Exception as exc:
        raise LongbridgeAPIError(
            f"获取 Longbridge 做空拥挤度指标失败: {exc}"
        ) from exc
    return results


def sync_history_candlesticks(
    symbols: Optional[Iterable[str]] = None,
    period: str = "day",
    adjust_type: str = "no_adjust",
    count: int = 120,
    forward: bool = False,  # Changed to False to get historical data
    incremental: bool = False,
    continue_on_error: bool = False,
) -> Dict[str, int]:
    if count <= 0:
        raise ValueError("count 必须大于 0")
    if count > 1000:
        raise ValueError("count 不能超过 1000 条，以避免超额拉取")

    creds = load_credentials()
    if not creds or any(not creds.get(key) for key in ("LONGPORT_APP_KEY", "LONGPORT_APP_SECRET", "LONGPORT_ACCESS_TOKEN")):
        raise HTTPException(status_code=400, detail="请先配置完整的 Longbridge 凭据。")

    symbol_list = list(symbols or load_symbols())
    if not symbol_list:
        raise HTTPException(status_code=400, detail="请至少配置一只股票代码。")

    try:
        period_enum_name = _PERIOD_NAME_MAP[period.lower()]
    except KeyError as exc:
        raise ValueError(f"不支持的周期类型: {period}") from exc

    try:
        adjust_enum_name = _ADJUST_NAME_MAP[adjust_type.lower()]
    except KeyError as exc:
        raise ValueError(f"不支持的复权类型: {adjust_type}") from exc

    try:
        from longbridge.openapi import Period, AdjustType
    except ModuleNotFoundError as exc:  # pragma: no cover - depends on environment
        raise LongbridgeDependencyMissing(
            "未找到 longbridge Python SDK，请先运行 `pip install longbridge`。"
        ) from exc

    period_enum = getattr(Period, period_enum_name)
    adjust_enum = getattr(AdjustType, adjust_enum_name)

    results: Dict[str, int] = {}

    with _quote_context(creds) as ctx:
        for symbol in symbol_list:
            try:
                latest_timestamp = (
                    fetch_latest_candlestick_timestamp(symbol, period)
                    if incremental
                    else None
                )
                today = datetime.now().date()
                can_increment = (
                    period.lower() == "day"
                    and latest_timestamp is not None
                    and latest_timestamp.date() <= today
                    and (today - latest_timestamp.date()).days
                    <= _INCREMENTAL_CANDLESTICK_MAX_AGE_DAYS
                )

                if can_increment:
                    candles = ctx.history_candlesticks_by_date(
                        symbol,
                        period_enum,
                        adjust_enum,
                        latest_timestamp.date(),
                        today,
                    )
                    logger.info(
                        "Got %s incremental %s candles for %s since %s",
                        len(candles),
                        period,
                        symbol,
                        latest_timestamp.date(),
                    )
                else:
                    # Initial or stale local data: refresh only the required window.
                    candles = ctx.candlesticks(
                        symbol,
                        period_enum,
                        count,
                        adjust_enum,
                    )
                    logger.info(
                        "Got %s candles for %s using candlesticks()",
                        len(candles),
                        symbol,
                    )

                # Empty incremental results simply mean there is no newer bar.
                # Only the initial/full path needs the offset fallback.
                if not candles and not can_increment:
                    logger.info(
                        "No data from candlesticks(), trying history_candlesticks_by_offset()"
                    )
                    candles = ctx.history_candlesticks_by_offset(
                        symbol,
                        period_enum,
                        adjust_enum,
                        forward,
                        count,
                    )
                    logger.info(f"Got {len(candles)} candles for {symbol} using history_candlesticks_by_offset()")
            except Exception as exc:
                if continue_on_error:
                    logger.warning("Failed to sync %s candlesticks: %s", symbol, exc)
                    results[symbol] = 0
                    continue
                raise LongbridgeAPIError(f"{symbol}: {exc}") from exc
            inserted = store_candlesticks(symbol, candles, period)  # 传递 period 参数
            logger.info(f"Inserted {inserted} {period} records for {symbol}")
            results[symbol] = inserted

    return results


def _fetch_candlesticks_from_db(
    symbol: str,
    period: str,
    limit: int,
    end_date: Optional[str] = None,
) -> List[Dict[str, float]]:
    with get_connection() as conn:
        if end_date:
            rows = conn.execute(
                """
                SELECT ts, open, high, low, close, volume, turnover
                FROM ohlc
                WHERE symbol = ?
                  AND period = ?
                  AND CAST(ts AS DATE) <= CAST(? AS DATE)
                ORDER BY ts DESC
                LIMIT ?
                """,
                [symbol, period, end_date, limit],
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT ts, open, high, low, close, volume, turnover
                FROM ohlc
                WHERE symbol = ? AND period = ?
                ORDER BY ts DESC
                LIMIT ?
                """,
                [symbol, period, limit],
            ).fetchall()
    return [
        {
            "ts": row[0],
            "open": row[1],
            "high": row[2],
            "low": row[3],
            "close": row[4],
            "volume": row[5],
            "turnover": row[6],
        }
        for row in rows
    ]


def _bar_timestamp(bar: Dict[str, Any]) -> Optional[datetime]:
    timestamp = bar.get("ts")
    if isinstance(timestamp, datetime):
        parsed = timestamp
    elif isinstance(timestamp, str):
        try:
            parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None

    if parsed.tzinfo is not None:
        return parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _merge_minute_bars(
    historical_bars: List[Dict[str, Any]],
    tick_bars: List[Dict[str, Any]],
    limit: int,
) -> List[Dict[str, Any]]:
    """Merge cached minute candles with newer tick-derived candles chronologically."""
    merged: Dict[datetime, Dict[str, Any]] = {}
    for bar in [*historical_bars, *tick_bars]:
        timestamp = _bar_timestamp(bar)
        if timestamp is not None:
            merged[timestamp] = bar
    return [bar for _, bar in sorted(merged.items())][-limit:]


def get_cached_candlesticks(
    symbol: str,
    period: str = "day",
    limit: int = 200,
    end_date: Optional[str] = None,
) -> List[Dict[str, float]]:
    if limit <= 0:
        raise ValueError("limit 必须大于 0")

    if _repo_fetch_candlesticks is not None:
        bars = _repo_fetch_candlesticks(
            symbol,
            period,
            limit,
            end_date=end_date,
        )
    else:
        global _candlestick_fallback_warned
        if not _candlestick_fallback_warned:
            logger.warning("fetch_candlesticks not exported by app.repositories; using direct DuckDB fallback")
            _candlestick_fallback_warned = True
        bars = _fetch_candlesticks_from_db(
            symbol,
            period,
            limit,
            end_date=end_date,
        )

    # Tick aggregation is specifically one-minute data. Merge it even when
    # historical OHLC exists so the current trading day is not omitted.
    if period.lower() == "min1":
        tick_bars = fetch_bars_from_ticks(symbol, min(limit, 500))
        if end_date:
            tick_bars = [
                bar
                for bar in tick_bars
                if (
                    (timestamp := _bar_timestamp(bar)) is not None
                    and timestamp.date().isoformat() <= end_date
                )
            ]
        return _merge_minute_bars(bars, tick_bars, limit)
    return bars


def _build_longbridge_config(creds: Dict[str, str]):
    missing = [key for key in ("LONGPORT_APP_KEY", "LONGPORT_APP_SECRET", "LONGPORT_ACCESS_TOKEN") if not creds.get(key)]
    if missing:
        raise HTTPException(status_code=400, detail="请先配置完整的 Longbridge 凭据。")

    try:
        from longbridge.openapi import Config
    except ModuleNotFoundError as exc:  # pragma: no cover - depends on environment
        raise LongbridgeDependencyMissing(
            "未找到 longbridge Python SDK，请先运行 `pip install longbridge`。"
        ) from exc

    return Config.from_apikey(
        creds["LONGPORT_APP_KEY"],
        creds["LONGPORT_APP_SECRET"],
        creds["LONGPORT_ACCESS_TOKEN"],
    )


def get_positions() -> List[Dict[str, object]]:
    creds = load_credentials()
    logger.info(
        "get_positions: loaded credentials (keys present: %s)",
        {key: bool(creds.get(key)) for key in ("LONGPORT_APP_KEY", "LONGPORT_APP_SECRET", "LONGPORT_ACCESS_TOKEN")},
    )
    config = _build_longbridge_config(creds)
    logger.info("get_positions: built Longbridge Config, requesting stock positions")

    try:
        from longbridge.openapi import TradeContext
    except ModuleNotFoundError as exc:  # pragma: no cover
        raise LongbridgeDependencyMissing(
            "未找到 longbridge Python SDK，请先运行 `pip install longbridge`。"
        ) from exc

    ctx = TradeContext(config)
    try:
        response = ctx.stock_positions()
        logger.info(
            "get_positions: received response type=%s", type(response).__name__
        )
    except Exception as exc:  # pragma: no cover - network errors
        raise LongbridgeAPIError(f"获取持仓信息失败: {exc}") from exc
    finally:  # ensure context close
        try:
            close_longbridge_context(ctx)
        except Exception:  # noqa: S110 - cleanup best effort
            pass

    positions: List[Dict[str, object]] = []

    if response is None:
        return positions

    accounts: Iterable = []
    if hasattr(response, "to_dict") and callable(response.to_dict):
        data_dict = response.to_dict() or {}
        logger.info("get_positions: response.to_dict keys=%s", list(data_dict.keys()))
        # 部分版本下为 {"channels": [...]}
        if "channels" in data_dict:
            accounts = data_dict.get("channels", []) or []
        else:
            accounts = data_dict.get("list", []) or []
    elif hasattr(response, "channels"):
        accounts = getattr(response, "channels") or []
    elif hasattr(response, "list"):
        accounts = getattr(response, "list") or []
    else:  # fallback to iterable response
        accounts = list(response) if hasattr(response, "__iter__") else []  # type: ignore[arg-type]

    for account in accounts or []:
        logger.debug("get_positions: processing account type=%s", type(account).__name__)
        if isinstance(account, dict):
            account_channel = account.get("account_channel") or account.get("channel")
            stock_items = (
                account.get("positions")
                or account.get("stock_positions")
                or account.get("stock_info")
                or account.get("stock_list")
                or []
            )
        else:
            account_channel = getattr(account, "account_channel", None) or getattr(account, "channel", None)
            stock_items = (
                getattr(account, "positions", None)
                or getattr(account, "stock_positions", None)
                or getattr(account, "stock_info", None)
                or getattr(account, "stock_list", None)
                or []
            )
        logger.info(
            "get_positions: account channel=%s, stock_items type=%s len=%s",
            account_channel,
            type(stock_items).__name__,
            len(stock_items) if hasattr(stock_items, "__len__") else "?",
        )

        for item in stock_items:
            if isinstance(item, dict):
                symbol = item.get("symbol")
                symbol_name = item.get("symbol_name")
                currency = item.get("currency")
                qty = _safe_float(item.get("quantity")) or 0.0
                available = _safe_float(item.get("available_quantity"))
                raw_cost_price = _safe_float(item.get("cost_price")) or 0.0
                market = item.get("market")
            else:
                symbol = getattr(item, "symbol", None)
                symbol_name = getattr(item, "symbol_name", None)
                currency = getattr(item, "currency", None)
                qty = _safe_float(getattr(item, "quantity", None)) or 0.0
                available = _safe_float(getattr(item, "available_quantity", None))
                raw_cost_price = _safe_float(getattr(item, "cost_price", None)) or 0.0
                market = getattr(item, "market", None)

            # Convert market enum to string
            if market and hasattr(market, "name"):
                market = market.name
            elif market:
                market = str(market)

            if not symbol:
                continue

            direction = "short" if raw_cost_price < 0 else "long"
            entry_price = abs(raw_cost_price)
            
            # 计算成本价值、市值、盈亏等字段
            cost_value = entry_price * qty
            # 暂时使用成本价作为市值，后续会通过实时行情更新
            market_value = cost_value
            pnl = 0.0
            pnl_percent = 0.0

            positions.append(
                {
                    "symbol": symbol,
                    "symbol_name": symbol_name,
                    "currency": currency,
                    "market": market,
                    "qty": qty,
                    "available_quantity": available,
                    "avg_price": entry_price,
                    "raw_avg_price": raw_cost_price,
                    "cost_value": cost_value,
                    "market_value": market_value,
                    "pnl": pnl,
                    "pnl_percent": pnl_percent,
                    "direction": direction,
                    "account_channel": account_channel,
                }
            )

    logger.info("get_positions: assembled %d positions", len(positions))
    return positions


def get_watchlists() -> List[Dict[str, object]]:
    """Fetch the user's Longbridge watchlist groups without changing them."""
    creds = load_credentials()
    required = (
        "LONGPORT_APP_KEY",
        "LONGPORT_APP_SECRET",
        "LONGPORT_ACCESS_TOKEN",
    )
    if any(not creds.get(key) for key in required):
        raise LongbridgeAPIError("请先在基础配置中保存完整的 Longbridge 凭据")

    try:
        with _quote_context(creds) as context:
            response = context.watchlist()
    except Exception as exc:
        raise LongbridgeAPIError(f"获取 Longbridge 关注列表失败: {exc}") from exc

    def field(value: object, name: str, default: object = None) -> object:
        if isinstance(value, dict):
            return value.get(name, default)
        return getattr(value, name, default)

    def text(value: object) -> str | None:
        if value is None:
            return None
        label = value.name if hasattr(value, "name") else str(value)
        return str(label).removeprefix("Market.")

    groups: List[Dict[str, object]] = []
    for raw_group in response or []:
        group_id = int(field(raw_group, "id", 0) or 0)
        group_name = str(field(raw_group, "name", "未命名分组") or "未命名分组")
        # Longbridge exposes a special "holdings" watchlist group, but it is
        # not the authoritative account position list and can be empty even
        # when TradeContext.stock_positions() has positions. Keep it out of
        # the watchlist API to avoid presenting it as an account holdings tab.
        if group_id == -6 or group_name.strip().lower() in {"holdings", "持仓"}:
            continue

        securities: List[Dict[str, object]] = []
        for raw_security in field(raw_group, "securities", []) or []:
            symbol = str(field(raw_security, "symbol", "") or "").strip()
            if not symbol:
                continue
            watched_at = field(raw_security, "watched_at")
            securities.append({
                "symbol": symbol,
                "name": str(field(raw_security, "name", "") or "").strip(),
                "name_cn": str(field(raw_security, "name", "") or "").strip(),
                "name_en": str(field(raw_security, "name", "") or "").strip(),
                "market": text(field(raw_security, "market")),
                "is_pinned": bool(field(raw_security, "is_pinned", False)),
                "watched_price": _safe_float(
                    field(raw_security, "watched_price")
                ),
                "watched_at": (
                    watched_at.isoformat()
                    if hasattr(watched_at, "isoformat")
                    else str(watched_at) if watched_at is not None else None
                ),
            })
        groups.append({
            "id": group_id,
            "name": group_name,
            "securities": securities,
        })

    # QuoteContext.watchlist() often returns English names for US securities.
    # Enrich from the existing official catalog when available, but keep the
    # watchlist usable if the catalog refresh is unavailable.
    try:
        catalog = get_security_catalog_service()
        for market in ("US", "HK"):
            market_securities = [
                security
                for group in groups
                for security in group["securities"]
                if security.get("market") == market
            ]
            metadata = catalog.lookup_symbols(
                market,
                [security["symbol"] for security in market_securities],
            )
            for security in market_securities:
                item = metadata.get(str(security["symbol"]).upper())
                if not item:
                    continue
                security["name_cn"] = str(
                    item.get("name") or security.get("name_cn") or ""
                ).strip()
                security["name_en"] = str(
                    item.get("name_en") or security.get("name_en") or ""
                ).strip()
    except Exception as exc:  # pragma: no cover - best-effort enrichment
        logger.warning("Failed to enrich watchlist names: %s", exc)

    return groups


def get_watchlist_quotes(symbols: Iterable[str]) -> Dict[str, Dict[str, Optional[float]]]:
    """Fetch the latest price and day change for watchlist symbols."""
    symbol_list = list(dict.fromkeys(
        symbol.strip().upper()
        for symbol in symbols
        if symbol and symbol.strip()
    ))
    if not symbol_list:
        return {}

    creds = load_credentials()
    required = (
        "LONGPORT_APP_KEY",
        "LONGPORT_APP_SECRET",
        "LONGPORT_ACCESS_TOKEN",
    )
    if any(not creds.get(key) for key in required):
        raise LongbridgeAPIError("请先在基础配置中保存完整的 Longbridge 凭据")

    try:
        with _quote_context(creds) as context:
            rows = context.quote(symbol_list)
    except Exception as exc:
        raise LongbridgeAPIError(f"获取 Longbridge 关注行情失败: {exc}") from exc

    quotes: Dict[str, Dict[str, Optional[float]]] = {}
    for row in rows or []:
        symbol = str(getattr(row, "symbol", "") or "").strip().upper()
        if not symbol:
            continue
        last_done = _safe_float(getattr(row, "last_done", None))
        prev_close = _safe_float(getattr(row, "prev_close", None))
        change_rate = _safe_float(getattr(row, "change_rate", None))
        if change_rate is None and last_done is not None and prev_close:
            change_rate = (last_done - prev_close) / prev_close * 100
        quotes[symbol] = {
            "last_done": last_done,
            "prev_close": prev_close,
            "change_rate": change_rate,
        }
    return quotes


def remove_watchlist_security(symbol: str) -> Dict[str, object]:
    """Remove a security from every Longbridge watchlist group."""
    normalized_symbol = str(symbol or "").strip().upper()
    if not normalized_symbol:
        raise ValueError("证券代码不能为空")

    creds = load_credentials()
    required = (
        "LONGPORT_APP_KEY",
        "LONGPORT_APP_SECRET",
        "LONGPORT_ACCESS_TOKEN",
    )
    if any(not creds.get(key) for key in required):
        raise LongbridgeAPIError("请先在基础配置中保存完整的 Longbridge 凭据")

    try:
        from longbridge.openapi import SecuritiesUpdateMode

        with _quote_context(creds) as context:
            groups = context.watchlist() or []
            group_ids = []
            for group in groups:
                group_id = int(getattr(group, "id", 0) or 0)
                if group_id == -6:
                    continue
                securities = getattr(group, "securities", []) or []
                if any(
                    str(getattr(security, "symbol", "") or "").strip().upper()
                    == normalized_symbol
                    for security in securities
                ):
                    group_ids.append(group_id)

            for group_id in dict.fromkeys(group_ids):
                context.update_watchlist_group(
                    group_id,
                    securities=[normalized_symbol],
                    mode=SecuritiesUpdateMode.Remove,
                )
    except ValueError:
        raise
    except Exception as exc:
        raise LongbridgeAPIError(f"取消 Longbridge 关注失败: {exc}") from exc

    return {
        "symbol": normalized_symbol,
        "removed_from_groups": list(dict.fromkeys(group_ids)),
    }


def update_watchlist_pinned(symbol: str, is_pinned: bool) -> Dict[str, object]:
    """Pin or unpin a security in the Longbridge watchlist."""
    normalized_symbol = str(symbol or "").strip().upper()
    if not normalized_symbol:
        raise ValueError("证券代码不能为空")

    creds = load_credentials()
    required = (
        "LONGPORT_APP_KEY",
        "LONGPORT_APP_SECRET",
        "LONGPORT_ACCESS_TOKEN",
    )
    if any(not creds.get(key) for key in required):
        raise LongbridgeAPIError("请先在基础配置中保存完整的 Longbridge 凭据")

    try:
        from longbridge.openapi import PinnedMode

        mode = PinnedMode.Add if is_pinned else PinnedMode.Remove
        with _quote_context(creds) as context:
            context.update_pinned(mode, [normalized_symbol])
    except ValueError:
        raise
    except Exception as exc:
        action = "置顶" if is_pinned else "取消置顶"
        raise LongbridgeAPIError(f"{action} Longbridge 关注失败: {exc}") from exc

    return {
        "symbol": normalized_symbol,
        "is_pinned": is_pinned,
    }


def get_account_balance() -> Dict[str, object]:
    """获取账户资金余额信息（优先返回 USD；若无 USD 则返回所有币种并标注 usd_missing）"""
    creds = load_credentials()
    config = _build_longbridge_config(creds)

    try:
        from longbridge.openapi import TradeContext
    except ModuleNotFoundError as exc:
        raise LongbridgeDependencyMissing(
            "未找到 longbridge Python SDK，请先运行 `pip install longbridge`。"
        ) from exc

    def _normalize_currency(cur: object) -> str:
        if hasattr(cur, "name"):
            return str(getattr(cur, "name"))
        if hasattr(cur, "value"):
            return str(getattr(cur, "value"))
        return str(cur)

    def _parse_balances(iterable) -> Dict[str, dict]:
        result: Dict[str, dict] = {}
        if not hasattr(iterable, "__iter__"):
            return result
        for balance in iterable:
            currency_raw = getattr(balance, "currency", "UNKNOWN")
            currency = _normalize_currency(currency_raw).upper()

            # 现金明细
            cash_infos = getattr(balance, "cash_infos", []) or []
            available_cash = 0.0
            withdraw_cash = 0.0
            settling_cash = 0.0
            frozen_cash = 0.0
            # 优先使用与该余额同币种的 cash_info；若未命中，且期望 USD，则兜底匹配包含 USD 的条目
            for cash_info in cash_infos:
                info_currency = _normalize_currency(getattr(cash_info, "currency", "")).upper()
                if info_currency == currency:
                    available_cash = float(getattr(cash_info, "available_cash", 0) or 0)
                    withdraw_cash = float(getattr(cash_info, "withdraw_cash", 0) or 0)
                    settling_cash = float(getattr(cash_info, "settling_cash", 0) or 0)
                    frozen_cash = float(getattr(cash_info, "frozen_cash", 0) or 0)
                    break
            # 如果仍未命中，但当前余额币种为 USD，则尝试匹配任何包含 'USD' 的币种标识
            if available_cash == 0.0 and currency == "USD":
                for cash_info in cash_infos:
                    info_currency = _normalize_currency(getattr(cash_info, "currency", "")).upper()
                    if "USD" in info_currency:
                        available_cash = float(getattr(cash_info, "available_cash", 0) or 0)
                        withdraw_cash = float(getattr(cash_info, "withdraw_cash", 0) or 0)
                        settling_cash = float(getattr(cash_info, "settling_cash", 0) or 0)
                        frozen_cash = float(getattr(cash_info, "frozen_cash", 0) or 0)
                        break

            total_cash = float(getattr(balance, "total_cash", 0) or 0)
            # 若 cash_infos 未命中，再尝试 balance 层的 available_cash 字段；不要用 total_cash 回退
            balance_level_available = float(getattr(balance, "available_cash", 0) or 0)
            if available_cash == 0.0 and balance_level_available > 0.0:
                available_cash = balance_level_available

            # 冻结费用（按 USD 汇总）
            frozen_transaction_fee_usd = 0.0
            frozen_tx_fees = getattr(balance, "frozen_transaction_fees", []) or []
            for fee in frozen_tx_fees:
                fee_ccy = _normalize_currency(getattr(fee, "currency", "")).upper()
                if fee_ccy == "USD":
                    frozen_transaction_fee_usd = float(getattr(fee, "frozen_transaction_fee", 0) or 0)
                    break
            max_finance_amount = float(getattr(balance, "max_finance_amount", 0) or 0)
            remaining_finance_amount = float(getattr(balance, "remaining_finance_amount", 0) or 0)

            finance_used = 0.0
            if max_finance_amount > 0 and remaining_finance_amount < max_finance_amount:
                finance_used = max_finance_amount - remaining_finance_amount
            debit = float(getattr(balance, "debit", 0) or 0)
            if debit > 0:
                finance_used = debit

            net_assets = float(getattr(balance, "net_assets", 0) or 0)

            result[currency] = {
                "total_cash": total_cash,
                "available_cash": available_cash,
                "withdraw_cash": withdraw_cash,
                "settling_cash": settling_cash,
                "cash_balance": available_cash,
                "max_finance_amount": max_finance_amount,
                "remaining_finance_amount": remaining_finance_amount,
                "finance_used": finance_used,
                "debit": debit,
                "frozen_cash": frozen_cash,
                "frozen_transaction_fee_usd": frozen_transaction_fee_usd,
                "risk_level": getattr(balance, "risk_level", None),
                "margin_call": float(getattr(balance, "margin_call", 0) or 0),
                "net_assets": net_assets,
                "init_margin": float(getattr(balance, "init_margin", 0) or 0),
                "maintenance_margin": float(getattr(balance, "maintenance_margin", 0) or 0),
                "currency": currency,
            }
        return result

    ctx = TradeContext(config)
    try:
        # 1) 优先拉取 USD
        usd_only = {}
        try:
            resp_usd = ctx.account_balance(currency="USD")
            usd_only = _parse_balances(resp_usd)
        except Exception as e:
            logger.warning(f"account_balance(currency='USD') failed: {e}")

        # 2) 再拉取全币种，做补全
        all_balances = {}
        try:
            resp_all = ctx.account_balance()
            all_balances = _parse_balances(resp_all)
        except Exception as e:
            logger.warning(f"account_balance() failed: {e}")

        # 合并：以 usd_only 为主，其次 all_balances
        merged: Dict[str, dict] = {}
        merged.update(all_balances)
        merged.update(usd_only)  # 确保 USD 优先采用定向查询结果

        if "USD" in merged:
            # 可按需：仅返回 USD；也可以同时透传其他币种
            # 这里选择同时返回，前端严格优先 USD
            return merged

        # 若无 USD，则返回所有币种，并标注 _meta.usd_missing
        available = sorted(list(merged.keys()))
        merged["_meta"] = {"usd_missing": True, "available": available}
        return merged
    except Exception as exc:
        logger.error(f"Failed to get account balance: {exc}")
        raise LongbridgeAPIError(f"获取账户资金失败: {exc}") from exc
    finally:
        try:
            close_longbridge_context(ctx)
        except Exception:
            pass


def _fetch_portfolio_overview() -> Dict[str, object]:
    positions = get_positions()
    if not positions:
        return {
            "positions": [],
            "totals": {
                "cost": 0.0,
                "market_value": 0.0,
                "pnl": 0.0,
                "pnl_percent": 0.0,
            },
        }

    symbols = [pos["symbol"] for pos in positions]

    # First try to get cached prices from database
    latest_map = fetch_latest_prices(symbols)

    # Then fetch real-time prices from Longbridge API for all symbols
    try:
        creds = load_credentials()
        if creds and all(creds.get(key) for key in ("LONGPORT_APP_KEY", "LONGPORT_APP_SECRET", "LONGPORT_ACCESS_TOKEN")):
            with _quote_context(creds) as ctx:
                try:
                    # Get real-time quotes for all symbols
                    quotes = ctx.quote(symbols)
                    for quote in quotes:
                        symbol_upper = quote.symbol.upper()
                        if hasattr(quote, 'last_done') and quote.last_done:
                            latest_map[symbol_upper] = {
                                "price": float(quote.last_done),
                                "ts": quote.timestamp if hasattr(quote, 'timestamp') else None,
                                "volume": float(quote.volume) if hasattr(quote, 'volume') else None,
                                "source": "realtime",
                            }
                            # Store the real-time price to database for future use
                            try:
                                from .repositories import store_tick_event
                                store_tick_event(symbol_upper, quote)
                            except Exception:
                                pass  # Best effort to save, don't fail the request
                except Exception as e:
                    logger.warning(f"Failed to fetch real-time quotes: {e}")
    except Exception as e:
        logger.warning(f"Failed to initialize quote context: {e}")

    enriched: List[Dict[str, object]] = []
    total_cost = 0.0
    total_market = 0.0
    total_pnl = 0.0
    total_day_pnl = 0.0
    total_day_pnl_percent = 0.0

    # Get previous close prices for day P&L calculation
    prev_close_map = {}
    try:
        if symbols:
            # For day P&L, we need yesterday's close price
            # This is a simplified version - in production you'd fetch actual previous close
            pass
    except Exception as e:
        logger.warning(f"Failed to get previous close prices: {e}")

    for pos in positions:
        symbol = pos["symbol"]
        qty_raw = float(pos.get("qty", 0) or 0)
        qty = abs(qty_raw)
        entry_price = float(pos.get("avg_price", 0) or 0)
        direction = pos.get("direction", "long")
        cost_value = entry_price * qty
        # fetch_latest_prices returns uppercase symbols as keys
        latest = latest_map.get(symbol.upper() if symbol else symbol, {})
        last_price = latest.get("price")
        last_ts = latest.get("ts")

        # Only calculate P&L if we have a last_price
        if last_price is not None and last_price > 0:
            market_value = last_price * qty
            pnl = (last_price - entry_price) * qty
            if direction == "short":
                pnl *= -1
            pnl_percent = 0.0
            if cost_value and entry_price:
                pct = (last_price - entry_price) / entry_price * 100
                pnl_percent = pct if direction == "long" else -pct

            # Calculate day P&L (simplified - assumes cost as previous close)
            # In production, you'd use actual previous close price
            day_pnl = pnl * 0.1  # Placeholder: assuming 10% of total P&L is today's
            day_pnl_percent = pnl_percent * 0.1 if pnl_percent else 0.0
        else:
            # No last price available, cannot calculate P&L
            market_value = cost_value  # Use cost as market value when price unavailable
            pnl = 0.0
            pnl_percent = 0.0
            day_pnl = 0.0
            day_pnl_percent = 0.0

        total_cost += cost_value
        total_market += market_value
        total_pnl += pnl
        total_day_pnl += day_pnl

        enriched.append(
            {
                "symbol": symbol,
                "symbol_name": pos.get("symbol_name"),
                "currency": pos.get("currency"),
                "market": pos.get("market"),
                "qty": qty,
                "available_quantity": pos.get("available_quantity"),
                "avg_price": entry_price,
                "direction": direction,
                "cost_value": cost_value,
                "last_price": last_price,
                "last_price_time": last_ts,
                "market_value": market_value,
                "pnl": pnl,
                "pnl_percent": pnl_percent,
                "day_pnl": day_pnl,
                "day_pnl_percent": day_pnl_percent,
                "account_channel": pos.get("account_channel"),
            }
        )

    total_pct = (total_pnl / total_cost * 100) if total_cost else 0.0
    total_day_pct = (total_day_pnl / total_cost * 100) if total_cost else 0.0

    # Get account balance
    account_balance = {}
    try:
        account_balance = get_account_balance()
    except Exception as e:
        logger.warning(f"Failed to get account balance: {e}")

    return {
        "positions": enriched,
        "totals": {
            "cost": total_cost,
            "market_value": total_market,
            "pnl": total_pnl,
            "pnl_percent": total_pct,
            "day_pnl": total_day_pnl,
            "day_pnl_percent": total_day_pct,
        },
        "account_balance": account_balance
    }


def get_portfolio_overview(force_refresh: bool = False) -> Dict[str, object]:
    """Return a short-lived, coalesced portfolio snapshot.

    A single snapshot requires multiple broker API calls. Serializing refreshes and
    caching them briefly prevents several pages/background workers from issuing the
    same expensive request at once and starving unrelated API traffic.
    """
    global _portfolio_cache_value, _portfolio_cache_expires_at

    now = time.monotonic()
    with _portfolio_cache_lock:
        if (
            not force_refresh
            and _portfolio_cache_value is not None
            and now < _portfolio_cache_expires_at
        ):
            return copy.deepcopy(_portfolio_cache_value)

        snapshot = _fetch_portfolio_overview()
        _portfolio_cache_value = copy.deepcopy(snapshot)
        _portfolio_cache_expires_at = time.monotonic() + _PORTFOLIO_CACHE_TTL_SECONDS
        return copy.deepcopy(snapshot)
