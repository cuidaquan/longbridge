"""Live Longbridge capture for the quantitative selector source bundle."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

from .exceptions import LongbridgeAPIError, LongbridgeDependencyMissing
from .repositories import load_credentials
from .security_catalog import SecurityCatalogService
from .services import _quote_context
from .stock_candidate_data import (
    get_security_static_info,
    get_security_tradeability,
)
from .quant_stock_selector_data import SOURCE_BUNDLE_SCHEMA_VERSION
from .quant_stock_selector_symbols import normalize_symbol


CATALOG_SOURCE_VERSION = "longbridge-security-list-static-info-v1"
BAR_SOURCE_VERSION = "longbridge-history-candlestick-v1"
CALENDAR_SOURCE_VERSION = "longbridge-trading-days-v1"
DEFAULT_BATCH_SIZE = 500
NEW_YORK = ZoneInfo("America/New_York")
ELIGIBLE_EXCHANGES = frozenset({"NYSE", "NASDAQ", "NYSE AMERICAN"})


def _credentials() -> Mapping[str, str]:
    credentials = load_credentials()
    required = (
        "LONGPORT_APP_KEY",
        "LONGPORT_APP_SECRET",
        "LONGPORT_ACCESS_TOKEN",
    )
    if not credentials or any(not credentials.get(key) for key in required):
        raise ValueError("请先在基础配置中保存完整的 Longbridge 凭据")
    return credentials


def _timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, time.min, tzinfo=timezone.utc)
    elif isinstance(value, (int, float, Decimal)):
        parsed = datetime.fromtimestamp(float(value), tz=timezone.utc)
    else:
        text = str(value or "").strip()
        if not text:
            raise ValueError("candlestick timestamp is required")
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _bar_row(value: Any) -> dict[str, Any]:
    timestamp = _timestamp(getattr(value, "timestamp", None))
    return {
        "ts": timestamp.isoformat().replace("+00:00", "Z"),
        "open": _float(getattr(value, "open", None)),
        "high": _float(getattr(value, "high", None)),
        "low": _float(getattr(value, "low", None)),
        "close": _float(getattr(value, "close", None)),
        "volume": _float(getattr(value, "volume", None)),
        "turnover": _float(getattr(value, "turnover", None)),
    }


def load_longbridge_daily_bars(
    symbols: Sequence[str],
    *,
    data_as_of: date,
    count: int = 90,
) -> Mapping[str, Mapping[str, Any]]:
    """Load independent adjusted and raw daily series without shared OHLC writes."""
    try:
        from longbridge.openapi import AdjustType, Period
    except ModuleNotFoundError as exc:  # pragma: no cover - environment dependent
        raise LongbridgeDependencyMissing(
            "未找到 longbridge Python SDK，请先运行 `pip install longbridge`。"
        ) from exc
    start = data_as_of - timedelta(days=max(180, count * 2))
    result: dict[str, Mapping[str, Any]] = {}
    try:
        with _quote_context(dict(_credentials())) as context:
            for raw_symbol in symbols:
                symbol = normalize_symbol(raw_symbol)
                try:
                    adjusted = context.history_candlesticks_by_date(
                        symbol,
                        Period.Day,
                        AdjustType.ForwardAdjust,
                        start,
                        data_as_of,
                    )
                    raw = context.history_candlesticks_by_date(
                        symbol,
                        Period.Day,
                        AdjustType.NoAdjust,
                        start,
                        data_as_of,
                    )
                    adjusted_rows = sorted(
                        (_bar_row(item) for item in list(adjusted or [])),
                        key=lambda item: item["ts"],
                    )[-count:]
                    raw_rows = sorted(
                        (_bar_row(item) for item in list(raw or [])),
                        key=lambda item: item["ts"],
                    )[-count:]
                    result[symbol] = {
                        "forward_adjusted_bars": adjusted_rows,
                        "unadjusted_bars": raw_rows,
                        "error": None,
                    }
                except Exception as exc:
                    if "301607" in str(exc):
                        raise LongbridgeAPIError(
                            "Longbridge 历史 K 线月度唯一证券额度已用尽"
                            "（301607 Permission limit）"
                        ) from exc
                    result[symbol] = {
                        "forward_adjusted_bars": [],
                        "unadjusted_bars": [],
                        "error": f"{type(exc).__name__}: {exc}",
                    }
    except (ValueError, LongbridgeDependencyMissing, LongbridgeAPIError):
        raise
    except Exception as exc:
        raise LongbridgeAPIError(f"获取 Longbridge 日 K 失败: {exc}") from exc
    return result


def load_longbridge_latest_completed_us_session(
    *,
    now: datetime | None = None,
) -> Mapping[str, Any]:
    """Resolve the latest completed XNYS session using Longbridge trade-day evidence."""
    captured_at = now or datetime.now(timezone.utc)
    if captured_at.tzinfo is None or captured_at.utcoffset() is None:
        raise ValueError("calendar capture time must be timezone-aware")
    captured_at = captured_at.astimezone(timezone.utc)
    local_today = captured_at.astimezone(NEW_YORK).date()
    start = local_today - timedelta(days=28)
    try:
        from longbridge.openapi import Market
    except ModuleNotFoundError as exc:  # pragma: no cover - environment dependent
        raise LongbridgeDependencyMissing(
            "未找到 longbridge Python SDK，请先运行 `pip install longbridge`。"
        ) from exc
    try:
        with _quote_context(dict(_credentials())) as context:
            response = context.trading_days(Market.US, start, local_today)
    except (ValueError, LongbridgeDependencyMissing):
        raise
    except Exception as exc:
        raise LongbridgeAPIError(f"获取 Longbridge 美国交易日失败: {exc}") from exc
    trading_days = {
        value if isinstance(value, date) else date.fromisoformat(str(value))
        for value in list(getattr(response, "trading_days", None) or [])
    }
    half_days = {
        value if isinstance(value, date) else date.fromisoformat(str(value))
        for value in list(getattr(response, "half_trading_days", None) or [])
    }
    sessions = sorted(trading_days | half_days, reverse=True)
    resolved = None
    for session_date in sessions:
        close_time = time(13, 0) if session_date in half_days else time(16, 0)
        close = datetime.combine(session_date, close_time, tzinfo=NEW_YORK)
        if close.astimezone(timezone.utc) <= captured_at:
            resolved = (session_date, close)
            break
    if resolved is None:
        raise LongbridgeAPIError("最近 28 天没有可确认的已完成美国交易日")
    session_date, close = resolved
    open_at = datetime.combine(session_date, time(9, 30), tzinfo=NEW_YORK)
    return {
        "source": "Longbridge QuoteContext.trading_days",
        "source_version": CALENDAR_SOURCE_VERSION,
        "license": "Longbridge OpenAPI account entitlement",
        "historical_semantics": "point_in_time",
        "market_calendar": "XNYS",
        "session_date": session_date.isoformat(),
        "official_open": open_at.astimezone(timezone.utc).isoformat().replace(
            "+00:00", "Z"
        ),
        "official_close": close.astimezone(timezone.utc).isoformat().replace(
            "+00:00", "Z"
        ),
        "session_status": "completed",
        "is_latest_completed_session": True,
        "half_trade_day": session_date in half_days,
        "captured_at": captured_at.isoformat().replace("+00:00", "Z"),
    }


def _chunks(values: Sequence[str], size: int) -> Iterable[Sequence[str]]:
    for index in range(0, len(values), size):
        yield values[index:index + size]


def _board(value: Any) -> str:
    return "".join(str(value or "").upper().split()).replace("_", "")


def _exchange(value: Any) -> str:
    normalized = " ".join(str(value or "").strip().upper().split())
    aliases = {
        "NASD": "NASDAQ",
        "AMEX": "NYSE AMERICAN",
        "NYSEAMERICAN": "NYSE AMERICAN",
        "NYSE AMEX": "NYSE AMERICAN",
        "NASDAQ GLOBAL MARKET": "NASDAQ",
        "NASDAQ CAPITAL MARKET": "NASDAQ",
        "NASDAQ GLOBAL SELECT": "NASDAQ",
    }
    return aliases.get(normalized, normalized)


class LongbridgeQuantSourceBundleCollector:
    """Capture Longbridge facts into the same immutable v3 bundle used for replay."""

    def __init__(
        self,
        *,
        catalog_loader: Callable[[], Sequence[Mapping[str, Any]]] | None = None,
        static_info_loader: Callable[[Iterable[str]], Sequence[Mapping[str, Any]]] = get_security_static_info,
        tradeability_loader: Callable[[Iterable[str]], Mapping[str, Mapping[str, Any]]] = get_security_tradeability,
        calendar_loader: Callable[..., Mapping[str, Any]] = load_longbridge_latest_completed_us_session,
        bar_loader: Callable[..., Mapping[str, Mapping[str, Any]]] = load_longbridge_daily_bars,
        clock: Callable[[], datetime] | None = None,
        batch_size: int = DEFAULT_BATCH_SIZE,
    ) -> None:
        self.catalog_loader = catalog_loader or (
            lambda: SecurityCatalogService(fetch_attempts=2).refresh("US")
        )
        self.static_info_loader = static_info_loader
        self.tradeability_loader = tradeability_loader
        self.calendar_loader = calendar_loader
        self.bar_loader = bar_loader
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        if batch_size < 1 or batch_size > 500:
            raise ValueError("batch_size must be between 1 and 500")
        self.batch_size = batch_size

    def _load_static(self, symbols: Sequence[str]) -> dict[str, Mapping[str, Any]]:
        records = {}
        for batch in _chunks(symbols, self.batch_size):
            for item in self.static_info_loader(batch):
                symbol = normalize_symbol(item.get("symbol"))
                if symbol not in batch:
                    raise ValueError(f"unexpected static info symbol: {symbol}")
                if symbol in records:
                    raise ValueError(f"duplicate static info symbol: {symbol}")
                records[symbol] = item
        return records

    def _load_tradeability(
        self,
        symbols: Sequence[str],
    ) -> dict[str, Mapping[str, Any]]:
        records = {}
        for batch in _chunks(symbols, self.batch_size):
            loaded = self.tradeability_loader(batch)
            for raw_symbol, item in loaded.items():
                symbol = normalize_symbol(raw_symbol)
                if symbol not in batch:
                    raise ValueError(f"unexpected quote symbol: {symbol}")
                if symbol in records:
                    raise ValueError(f"duplicate quote symbol: {symbol}")
                records[symbol] = item
        return records

    def capture(self) -> Mapping[str, Any]:
        captured_at = self.clock()
        if captured_at.tzinfo is None or captured_at.utcoffset() is None:
            raise ValueError("collector clock must be timezone-aware")
        captured_at = captured_at.astimezone(timezone.utc)
        calendar = dict(self.calendar_loader(now=captured_at))
        data_as_of = date.fromisoformat(str(calendar["session_date"]))

        raw_catalog = [dict(item) for item in self.catalog_loader()]
        by_symbol = {}
        for item in raw_catalog:
            symbol = normalize_symbol(item.get("symbol"))
            if symbol in by_symbol:
                raise ValueError(f"duplicate catalog symbol: {symbol}")
            by_symbol[symbol] = item
        by_symbol.setdefault("SPY.US", {
            "symbol": "SPY.US",
            "name": "SPDR S&P 500 ETF Trust",
            "name_en": "SPDR S&P 500 ETF Trust",
            "market": "US",
        })
        symbols = sorted(by_symbol)
        static = self._load_static(symbols)
        catalog = []
        eligible_symbols = []
        for symbol in symbols:
            source = by_symbol[symbol]
            detail = static.get(symbol, {})
            board = str(detail.get("board") or "")
            exchange = _exchange(detail.get("exchange"))
            catalog.append({
                "symbol": symbol,
                "name": str(
                    source.get("name")
                    or source.get("name_en")
                    or symbol
                ).strip(),
                "name_en": str(source.get("name_en") or "").strip(),
                "market": "US",
                "board": board,
                "exchange": exchange,
            })
            if _board(board) == "USMAIN" and exchange in ELIGIBLE_EXCHANGES:
                eligible_symbols.append(symbol)

        tradeability = self._load_tradeability(eligible_symbols)
        history_symbols = []
        for symbol in eligible_symbols:
            quote = tradeability.get(symbol, {})
            try:
                last_done = float(quote.get("last_done"))
            except (TypeError, ValueError):
                last_done = 0.0
            if (
                symbol == "SPY.US"
                or (
                    str(quote.get("trade_status") or "").strip().lower()
                    == "normal"
                    and last_done >= 5.0
                )
            ):
                history_symbols.append(symbol)
        bar_results = self.bar_loader(
            history_symbols,
            data_as_of=data_as_of,
        )
        capture_errors = []
        market_data = {}
        for symbol in eligible_symbols:
            quote = dict(tradeability.get(symbol, {}))
            bars = dict(bar_results.get(symbol, {}))
            error = str(bars.get("error") or "").strip()
            if error:
                capture_errors.append(f"bars:{symbol}:{error}")
            adjusted = list(bars.get("forward_adjusted_bars") or [])
            raw = list(bars.get("unadjusted_bars") or [])
            raw_latest = max(raw, key=lambda item: str(item.get("ts") or "")) if raw else None
            raw_date = (
                _timestamp(raw_latest.get("ts")).date()
                if raw_latest is not None else None
            )
            market_data[symbol] = {
                "trade_status": quote.get("trade_status"),
                "last_price": (
                    raw_latest.get("close")
                    if raw_latest is not None else quote.get("last_done")
                ),
                "price_data_as_of": raw_date.isoformat() if raw_date else None,
                "bar_data_as_of": raw_date.isoformat() if raw_date else None,
                "forward_adjusted_bars": adjusted,
                "unadjusted_bars": raw,
                "news": {
                    "status": "unavailable",
                    "source": "not_collected_by_longbridge_bundle",
                    "news_items": [],
                },
                "events": {
                    "status": "unavailable",
                    "source": "not_collected_by_longbridge_bundle",
                    "events": [],
                },
            }
        return {
            "schema_version": SOURCE_BUNDLE_SCHEMA_VERSION,
            "captured_at": captured_at.isoformat().replace("+00:00", "Z"),
            "source_capture": {
                "status": "partial" if capture_errors else "complete",
                "source": "Longbridge OpenAPI",
                "errors": capture_errors,
                "catalog_count": len(catalog),
                "eligible_market_data_count": len(eligible_symbols),
                "history_symbol_count": len(history_symbols),
            },
            "data_as_of": data_as_of.isoformat(),
            "official_close": calendar["official_close"],
            "exchange_calendar": calendar,
            "catalog_source": "Longbridge security_list + static_info",
            "catalog_source_version": CATALOG_SOURCE_VERSION,
            "catalog_captured_at": captured_at.isoformat().replace("+00:00", "Z"),
            "tradeability_source": "Longbridge QuoteContext.quote",
            "bar_source": BAR_SOURCE_VERSION,
            "catalog": catalog,
            "market_data": market_data,
        }
