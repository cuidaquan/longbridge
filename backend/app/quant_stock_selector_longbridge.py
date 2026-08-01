"""Live Longbridge capture for the quantitative selector source bundle."""

from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
import logging
import multiprocessing
import time as time_module
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
ELIGIBLE_EXCHANGES = frozenset({"NASDAQ"})
MIN_PRICE = 5.0
MAX_PRICE = 500.0
MIN_CURRENT_TURNOVER = 10_000_000.0
MIN_TOTAL_MARKET_VALUE = 1_000_000_000.0
MIN_VOLUME_RATIO = 0.8
MAX_PE_TTM = 50.0
MONTHLY_HISTORY_QUOTA_CATEGORY = "monthly_history_symbol_quota"
HISTORY_REQUEST_RATE_LIMIT_CATEGORY = "history_request_rate_limit"
HISTORY_RATE_LIMIT_MAX_ATTEMPTS = 3
HISTORY_RATE_LIMIT_BACKOFF_SECONDS = 0.5
HISTORY_SKIP_CATEGORIES = frozenset({
    MONTHLY_HISTORY_QUOTA_CATEGORY,
    HISTORY_REQUEST_RATE_LIMIT_CATEGORY,
})

logger = logging.getLogger(__name__)


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


def _history_candlesticks(
    context: Any,
    symbol: str,
    period: Any,
    adjust_type: Any,
    start: date,
    end: date,
) -> Any:
    """Retry transient Longbridge request-rate limits before giving up."""
    for attempt in range(HISTORY_RATE_LIMIT_MAX_ATTEMPTS):
        try:
            return context.history_candlesticks_by_date(
                symbol,
                period,
                adjust_type,
                start,
                end,
            )
        except Exception as exc:
            if "301606" not in str(exc):
                raise
            if attempt + 1 >= HISTORY_RATE_LIMIT_MAX_ATTEMPTS:
                raise
            delay = HISTORY_RATE_LIMIT_BACKOFF_SECONDS * (2 ** attempt)
            logger.warning(
                "Longbridge history request rate limited for %s; retrying in %.1fs",
                symbol,
                delay,
            )
            time_module.sleep(delay)


def load_longbridge_daily_bars(
    symbols: Sequence[str],
    *,
    data_as_of: date,
    count: int = 90,
) -> Mapping[str, Mapping[str, Any]]:
    """Load independent adjusted and raw daily series without shared OHLC writes."""
    try:
        with _quote_context(dict(_credentials())) as context:
            return _load_longbridge_daily_bars_from_context(
                context,
                symbols,
                data_as_of=data_as_of,
                count=count,
            )
    except (ValueError, LongbridgeDependencyMissing, LongbridgeAPIError):
        raise
    except Exception as exc:
        raise LongbridgeAPIError(f"获取 Longbridge 日 K 失败: {exc}") from exc


def _load_longbridge_daily_bars_from_context(
    context: Any,
    symbols: Sequence[str],
    *,
    data_as_of: date,
    count: int = 90,
) -> Mapping[str, Mapping[str, Any]]:
    try:
        from longbridge.openapi import AdjustType, Period
    except ModuleNotFoundError as exc:  # pragma: no cover - environment dependent
        raise LongbridgeDependencyMissing(
            "未找到 longbridge Python SDK，请先运行 `pip install longbridge`。"
        ) from exc
    start = data_as_of - timedelta(days=max(180, count * 2))
    result: dict[str, Mapping[str, Any]] = {}
    for raw_symbol in symbols:
        symbol = normalize_symbol(raw_symbol)
        try:
            adjusted = _history_candlesticks(
                context,
                symbol,
                Period.Day,
                AdjustType.ForwardAdjust,
                start,
                data_as_of,
            )
            raw = _history_candlesticks(
                context,
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
                "error_category": None,
            }
        except Exception as exc:
            if "301607" in str(exc):
                result[symbol] = {
                    "forward_adjusted_bars": [],
                    "unadjusted_bars": [],
                    "error": (
                        "Longbridge 历史 K 线月度唯一证券额度已用尽"
                        "（301607 Permission limit）"
                    ),
                    "error_category": MONTHLY_HISTORY_QUOTA_CATEGORY,
                }
                continue
            if "301606" in str(exc):
                result[symbol] = {
                    "forward_adjusted_bars": [],
                    "unadjusted_bars": [],
                    "error": (
                        "Longbridge 历史 K 线请求频率受限，已跳过该证券"
                        "（301606 request rate limit）"
                    ),
                    "error_category": HISTORY_REQUEST_RATE_LIMIT_CATEGORY,
                }
                continue
            result[symbol] = {
                "forward_adjusted_bars": [],
                "unadjusted_bars": [],
                "error": f"{type(exc).__name__}: {exc}",
                "error_category": "provider_error",
            }
    return result


def load_longbridge_quant_prefilter_indexes(
    symbols: Sequence[str],
) -> Mapping[str, Mapping[str, Any]]:
    try:
        with _quote_context(dict(_credentials())) as context:
            return _load_longbridge_quant_prefilter_indexes_from_context(
                context,
                symbols,
            )
    except (ValueError, LongbridgeDependencyMissing, LongbridgeAPIError):
        raise
    except Exception as exc:
        raise LongbridgeAPIError(
            f"获取 Longbridge 量化预筛选指标失败: {exc}"
        ) from exc


def _load_longbridge_quant_prefilter_indexes_from_context(
    context: Any,
    symbols: Sequence[str],
) -> Mapping[str, Mapping[str, Any]]:
    try:
        from longbridge.openapi import CalcIndex
    except ModuleNotFoundError as exc:  # pragma: no cover - environment dependent
        raise LongbridgeDependencyMissing(
            "未找到 longbridge Python SDK，请先运行 `pip install longbridge`。"
        ) from exc
    requested = [
        CalcIndex.Turnover,
        CalcIndex.TotalMarketValue,
        CalcIndex.VolumeRatio,
        CalcIndex.PeTtmRatio,
        CalcIndex.TenDayChangeRate,
    ]
    rows = context.calc_indexes(list(symbols), requested)
    return {
        normalize_symbol(getattr(row, "symbol", None)): {
            "turnover": _float(getattr(row, "turnover", None)),
            "total_market_value": _float(
                getattr(row, "total_market_value", None)
            ),
            "volume_ratio": _float(getattr(row, "volume_ratio", None)),
            "pe_ttm_ratio": _float(getattr(row, "pe_ttm_ratio", None)),
            "ten_day_change_rate": _float(
                getattr(row, "ten_day_change_rate", None)
            ),
        }
        for row in list(rows or [])
        if getattr(row, "symbol", None)
    }


def load_longbridge_latest_completed_us_session(
    *,
    now: datetime | None = None,
) -> Mapping[str, Any]:
    """Resolve the latest completed XNYS session using Longbridge trade-day evidence."""
    captured_at = now or datetime.now(timezone.utc)
    if captured_at.tzinfo is None or captured_at.utcoffset() is None:
        raise ValueError("calendar capture time must be timezone-aware")
    captured_at = captured_at.astimezone(timezone.utc)
    try:
        with _quote_context(dict(_credentials())) as context:
            return _load_longbridge_latest_completed_us_session_from_context(
                context,
                captured_at=captured_at,
            )
    except (ValueError, LongbridgeDependencyMissing, LongbridgeAPIError):
        raise
    except Exception as exc:
        raise LongbridgeAPIError(f"获取 Longbridge 美国交易日失败: {exc}") from exc


def _load_longbridge_latest_completed_us_session_from_context(
    context: Any,
    *,
    captured_at: datetime,
) -> Mapping[str, Any]:
    local_today = captured_at.astimezone(NEW_YORK).date()
    start = local_today - timedelta(days=28)
    try:
        from longbridge.openapi import Market
    except ModuleNotFoundError as exc:  # pragma: no cover - environment dependent
        raise LongbridgeDependencyMissing(
            "未找到 longbridge Python SDK，请先运行 `pip install longbridge`。"
        ) from exc
    response = context.trading_days(Market.US, start, local_today)
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


def _capture_live_bundle(
    raw_catalog: Sequence[Mapping[str, Any]],
    credentials: Mapping[str, str],
    captured_at: datetime,
    batch_size: int,
) -> Mapping[str, Any]:
    """Run the GIL-blocking SDK capture outside the API server process."""
    try:
        with _quote_context(dict(credentials)) as context:
            collector = LongbridgeQuantSourceBundleCollector(
                catalog_loader=lambda: raw_catalog,
                static_info_loader=lambda symbols: get_security_static_info(
                    symbols,
                    context=context,
                ),
                tradeability_loader=lambda symbols: get_security_tradeability(
                    symbols,
                    context=context,
                ),
                calc_index_loader=lambda symbols: (
                    _load_longbridge_quant_prefilter_indexes_from_context(
                        context,
                        list(symbols),
                    )
                ),
                calendar_loader=lambda *, now: (
                    _load_longbridge_latest_completed_us_session_from_context(
                        context,
                        captured_at=now,
                    )
                ),
                bar_loader=lambda symbols, *, data_as_of: (
                    _load_longbridge_daily_bars_from_context(
                        context,
                        symbols,
                        data_as_of=data_as_of,
                    )
                ),
                clock=lambda: captured_at,
                batch_size=batch_size,
                isolate_live_capture=False,
            )
            return collector.capture()
    except Exception as exc:
        detail = (
            str(exc)
            if isinstance(exc, LongbridgeAPIError)
            else f"{type(exc).__name__}: {exc}"
        )
        raise LongbridgeAPIError(detail) from None


class LongbridgeQuantSourceBundleCollector:
    """Capture Longbridge facts into the same immutable v3 bundle used for replay."""

    def __init__(
        self,
        *,
        catalog_loader: Callable[[], Sequence[Mapping[str, Any]]] | None = None,
        static_info_loader: (
            Callable[[Iterable[str]], Sequence[Mapping[str, Any]]] | None
        ) = None,
        tradeability_loader: (
            Callable[[Iterable[str]], Mapping[str, Mapping[str, Any]]] | None
        ) = None,
        calc_index_loader: (
            Callable[[Iterable[str]], Mapping[str, Mapping[str, Any]]] | None
        ) = None,
        calendar_loader: Callable[..., Mapping[str, Any]] | None = None,
        bar_loader: Callable[..., Mapping[str, Mapping[str, Any]]] | None = None,
        clock: Callable[[], datetime] | None = None,
        batch_size: int = DEFAULT_BATCH_SIZE,
        isolate_live_capture: bool = True,
    ) -> None:
        self.catalog_loader = catalog_loader or (
            lambda: SecurityCatalogService(fetch_attempts=2).refresh("US")
        )
        self._reuse_live_context = all(
            loader is None
            for loader in (
                static_info_loader,
                tradeability_loader,
                calc_index_loader,
                calendar_loader,
                bar_loader,
            )
        )
        self.static_info_loader = static_info_loader or get_security_static_info
        self.tradeability_loader = (
            tradeability_loader or get_security_tradeability
        )
        self.calc_index_loader = (
            calc_index_loader or load_longbridge_quant_prefilter_indexes
        )
        self.calendar_loader = (
            calendar_loader or load_longbridge_latest_completed_us_session
        )
        self.bar_loader = bar_loader or load_longbridge_daily_bars
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        if batch_size < 1 or batch_size > 500:
            raise ValueError("batch_size must be between 1 and 500")
        self.batch_size = batch_size
        self.isolate_live_capture = isolate_live_capture

    @contextmanager
    def _loader_scope(self):
        if not self._reuse_live_context:
            yield (
                self.calendar_loader,
                self.static_info_loader,
                self.tradeability_loader,
                self.calc_index_loader,
                self.bar_loader,
            )
            return

        with _quote_context(dict(_credentials())) as context:
            yield (
                lambda *, now: (
                    _load_longbridge_latest_completed_us_session_from_context(
                        context,
                        captured_at=now,
                    )
                ),
                lambda symbols: get_security_static_info(
                    symbols,
                    context=context,
                ),
                lambda symbols: get_security_tradeability(
                    symbols,
                    context=context,
                ),
                lambda symbols: (
                    _load_longbridge_quant_prefilter_indexes_from_context(
                        context,
                        list(symbols),
                    )
                ),
                lambda symbols, *, data_as_of: (
                    _load_longbridge_daily_bars_from_context(
                        context,
                        symbols,
                        data_as_of=data_as_of,
                    )
                ),
            )

    def _load_static(
        self,
        symbols: Sequence[str],
        *,
        loader: Callable[[Iterable[str]], Sequence[Mapping[str, Any]]] | None = None,
    ) -> dict[str, Mapping[str, Any]]:
        active_loader = loader or self.static_info_loader
        records = {}
        for batch in _chunks(symbols, self.batch_size):
            for item in active_loader(batch):
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
        *,
        loader: (
            Callable[[Iterable[str]], Mapping[str, Mapping[str, Any]]] | None
        ) = None,
    ) -> dict[str, Mapping[str, Any]]:
        active_loader = loader or self.tradeability_loader
        records = {}
        for batch in _chunks(symbols, self.batch_size):
            loaded = active_loader(batch)
            for raw_symbol, item in loaded.items():
                symbol = normalize_symbol(raw_symbol)
                if symbol not in batch:
                    raise ValueError(f"unexpected quote symbol: {symbol}")
                if symbol in records:
                    raise ValueError(f"duplicate quote symbol: {symbol}")
                records[symbol] = item
        return records

    def _load_calc_indexes(
        self,
        symbols: Sequence[str],
        *,
        loader: (
            Callable[[Iterable[str]], Mapping[str, Mapping[str, Any]]] | None
        ) = None,
    ) -> dict[str, Mapping[str, Any]]:
        active_loader = loader or self.calc_index_loader
        records = {}
        for batch in _chunks(symbols, self.batch_size):
            loaded = active_loader(batch)
            for raw_symbol, item in loaded.items():
                symbol = normalize_symbol(raw_symbol)
                if symbol not in batch:
                    raise ValueError(f"unexpected calc index symbol: {symbol}")
                if symbol in records:
                    raise ValueError(f"duplicate calc index symbol: {symbol}")
                records[symbol] = item
        return records

    def capture(self) -> Mapping[str, Any]:
        captured_at = self.clock()
        if captured_at.tzinfo is None or captured_at.utcoffset() is None:
            raise ValueError("collector clock must be timezone-aware")
        captured_at = captured_at.astimezone(timezone.utc)
        if self._reuse_live_context and self.isolate_live_capture:
            raw_catalog = [dict(item) for item in self.catalog_loader()]
            process_context = multiprocessing.get_context("spawn")
            with ProcessPoolExecutor(
                max_workers=1,
                mp_context=process_context,
            ) as executor:
                return executor.submit(
                    _capture_live_bundle,
                    raw_catalog,
                    dict(_credentials()),
                    captured_at,
                    self.batch_size,
                ).result()
        with self._loader_scope() as loaders:
            (
                calendar_loader,
                static_info_loader,
                tradeability_loader,
                calc_index_loader,
                bar_loader,
            ) = loaders
            return self._capture_with_loaders(
                captured_at=captured_at,
                calendar_loader=calendar_loader,
                static_info_loader=static_info_loader,
                tradeability_loader=tradeability_loader,
                calc_index_loader=calc_index_loader,
                bar_loader=bar_loader,
            )

    def _capture_with_loaders(
        self,
        *,
        captured_at: datetime,
        calendar_loader: Callable[..., Mapping[str, Any]],
        static_info_loader: Callable[[Iterable[str]], Sequence[Mapping[str, Any]]],
        tradeability_loader: Callable[
            [Iterable[str]], Mapping[str, Mapping[str, Any]]
        ],
        calc_index_loader: Callable[
            [Iterable[str]], Mapping[str, Mapping[str, Any]]
        ],
        bar_loader: Callable[..., Mapping[str, Mapping[str, Any]]],
    ) -> Mapping[str, Any]:
        calendar = dict(calendar_loader(now=captured_at))
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
        static = self._load_static(symbols, loader=static_info_loader)
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

        quote_symbols = list(eligible_symbols)
        if "SPY.US" not in quote_symbols:
            quote_symbols.append("SPY.US")
        tradeability = self._load_tradeability(
            quote_symbols,
            loader=tradeability_loader,
        )
        price_filtered_symbols = []
        for symbol in eligible_symbols:
            quote = tradeability.get(symbol, {})
            try:
                last_done = float(quote.get("last_done"))
            except (TypeError, ValueError):
                last_done = 0.0
            if (
                str(quote.get("trade_status") or "").strip().lower()
                == "normal"
                and MIN_PRICE <= last_done <= MAX_PRICE
            ):
                price_filtered_symbols.append(symbol)

        calc_symbols = list(price_filtered_symbols)
        if "SPY.US" not in calc_symbols:
            calc_symbols.append("SPY.US")
        calc_indexes = self._load_calc_indexes(
            calc_symbols,
            loader=calc_index_loader,
        )
        spy_ten_day_change = _float(
            calc_indexes.get("SPY.US", {}).get("ten_day_change_rate")
        )
        if spy_ten_day_change is None:
            raise LongbridgeAPIError("SPY.US 十日涨跌幅缺失，无法执行严格预筛选")

        prefiltered_symbols = []
        prefilter_rules = {}
        for symbol in price_filtered_symbols:
            if symbol == "SPY.US":
                continue
            detail = calc_indexes.get(symbol, {})
            turnover = _float(detail.get("turnover"))
            total_market_value = _float(detail.get("total_market_value"))
            volume_ratio = _float(detail.get("volume_ratio"))
            pe_ttm_ratio = _float(detail.get("pe_ttm_ratio"))
            ten_day_change = _float(detail.get("ten_day_change_rate"))
            rules = {
                "current_turnover": (
                    turnover is not None
                    and turnover >= MIN_CURRENT_TURNOVER
                ),
                "total_market_value": (
                    total_market_value is not None
                    and total_market_value >= MIN_TOTAL_MARKET_VALUE
                ),
                "volume_ratio": (
                    volume_ratio is not None
                    and volume_ratio >= MIN_VOLUME_RATIO
                ),
                "pe_ttm_ratio": (
                    pe_ttm_ratio is not None
                    and 0.0 < pe_ttm_ratio < MAX_PE_TTM
                ),
                "ten_day_relative_strength": (
                    ten_day_change is not None
                    and ten_day_change >= spy_ten_day_change
                ),
            }
            prefilter_rules[symbol] = rules
            if all(rules.values()):
                prefiltered_symbols.append(symbol)

        def prefilter_rank(symbol: str) -> tuple[float, float, float, str]:
            detail = calc_indexes[symbol]
            return (
                -float(detail["turnover"]),
                -(
                    float(detail["ten_day_change_rate"])
                    - spy_ten_day_change
                ),
                -float(detail["volume_ratio"]),
                symbol,
            )

        history_symbols = sorted(prefiltered_symbols, key=prefilter_rank)
        history_request_symbols = ["SPY.US", *(
            symbol for symbol in history_symbols if symbol != "SPY.US"
        )]
        logger.info(
            "quant strict prefilter complete: catalog=%d nasdaq_usmain=%d "
            "price_filtered=%d prefiltered=%d history_requests=%d",
            len(catalog),
            len(eligible_symbols),
            len(price_filtered_symbols),
            len(history_symbols),
            len(history_request_symbols),
        )
        bar_results = bar_loader(
            history_request_symbols,
            data_as_of=data_as_of,
        )
        spy_bar_result = dict(bar_results.get("SPY.US", {}))
        capture_errors = []
        history_quota_skips = [
            symbol
            for symbol in history_request_symbols
            if str(
                bar_results.get(symbol, {}).get("error_category") or ""
            ).strip() == MONTHLY_HISTORY_QUOTA_CATEGORY
        ]
        history_rate_limit_skips = [
            symbol
            for symbol in history_request_symbols
            if str(
                bar_results.get(symbol, {}).get("error_category") or ""
            ).strip() == HISTORY_REQUEST_RATE_LIMIT_CATEGORY
        ]
        if (
            spy_bar_result.get("error_category")
            == MONTHLY_HISTORY_QUOTA_CATEGORY
        ):
            capture_errors.append(
                "benchmark:SPY.US:monthly_history_symbol_quota"
            )
        elif (
            spy_bar_result.get("error_category")
            == HISTORY_REQUEST_RATE_LIMIT_CATEGORY
        ):
            capture_errors.append(
                "benchmark:SPY.US:history_request_rate_limit"
            )
        market_data = {}
        market_symbols = list(eligible_symbols)
        if "SPY.US" not in market_symbols:
            market_symbols.append("SPY.US")
        history_rank = {
            symbol: index + 1 for index, symbol in enumerate(history_symbols)
        }
        for symbol in market_symbols:
            quote = dict(tradeability.get(symbol, {}))
            calc_detail = dict(calc_indexes.get(symbol, {}))
            bars = dict(bar_results.get(symbol, {}))
            error = str(bars.get("error") or "").strip()
            error_category = str(
                bars.get("error_category") or ""
            ).strip() or None
            if (
                error_category not in HISTORY_SKIP_CATEGORIES
                and error
            ):
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
                "current_turnover": _float(calc_detail.get("turnover")),
                "total_market_value": _float(
                    calc_detail.get("total_market_value")
                ),
                "volume_ratio": _float(calc_detail.get("volume_ratio")),
                "pe_ttm_ratio": _float(calc_detail.get("pe_ttm_ratio")),
                "ten_day_change_rate": _float(
                    calc_detail.get("ten_day_change_rate")
                ),
                "ten_day_relative_strength": (
                    _float(calc_detail.get("ten_day_change_rate"))
                    - spy_ten_day_change
                    if _float(calc_detail.get("ten_day_change_rate")) is not None
                    else None
                ),
                "prefilter": {
                    "status": (
                        "benchmark"
                        if symbol == "SPY.US"
                        else "selected"
                        if symbol in history_rank
                        else "excluded"
                    ),
                    "rank": history_rank.get(symbol),
                    "rules": prefilter_rules.get(symbol, {}),
                },
                "bar_error": error or None,
                "bar_error_category": error_category,
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
                "price_filtered_count": len(price_filtered_symbols),
                "prefiltered_stock_count": len(history_symbols),
                "history_symbol_count": len(history_request_symbols),
                "history_quota_skipped_count": len(history_quota_skips),
                "history_quota_skipped_symbols": history_quota_skips,
                "history_rate_limit_skipped_count": len(
                    history_rate_limit_skips
                ),
                "history_rate_limit_skipped_symbols": history_rate_limit_skips,
                "prefilter_policy": {
                    "exchange": "NASDAQ",
                    "minimum_price": MIN_PRICE,
                    "maximum_price": MAX_PRICE,
                    "minimum_current_turnover": MIN_CURRENT_TURNOVER,
                    "minimum_total_market_value": MIN_TOTAL_MARKET_VALUE,
                    "minimum_volume_ratio": MIN_VOLUME_RATIO,
                    "minimum_pe_ttm_exclusive": 0.0,
                    "maximum_pe_ttm_exclusive": MAX_PE_TTM,
                    "minimum_ten_day_relative_strength": 0.0,
                    "sort": [
                        "current_turnover_desc",
                        "ten_day_relative_strength_desc",
                        "volume_ratio_desc",
                        "symbol_asc",
                    ],
                    "history_symbol_limit": None,
                },
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
