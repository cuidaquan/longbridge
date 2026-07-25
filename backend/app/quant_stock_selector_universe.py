"""Unified US stock/ETF universe filtering and exact Top-30 selection."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
import math
import time
from typing import Any, Callable, Mapping, Protocol, Sequence

from .quant_stock_selector import (
    MIN_DAILY_BARS,
    QuantIndicators,
    QuantInputError,
    QuantScore,
    score_quantitative,
)
from .quant_stock_selector_hashing import canonical_sha256
from .quant_stock_selector_metadata import (
    ELIGIBLE_ASSET_CLASSES,
    ProductMetadata,
    ProductMetadataBatch,
    normalize_symbol,
)


FILTER_VERSION = "quant-selector-filter-v1.1"
AI_QUANT_THRESHOLD = 65.0
AI_CANDIDATE_LIMIT = 30
BENCHMARK_SYMBOLS = frozenset({"SPY"})
ELIGIBLE_EXCHANGES = frozenset({"NYSE", "NASDAQ", "NYSE AMERICAN"})


class UniverseSelectionError(ValueError):
    pass


@dataclass(frozen=True)
class NbboQuote:
    symbol: str
    status: str
    bid: float | None
    ask: float | None
    quote_timestamp: datetime | None
    session: str | None
    source: str
    error_code: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        if self.quote_timestamp is not None:
            timestamp = self.quote_timestamp
            if timestamp.tzinfo is not None and timestamp.utcoffset() is not None:
                timestamp = timestamp.astimezone(timezone.utc)
                payload["quote_timestamp"] = timestamp.isoformat(
                    timespec="milliseconds"
                ).replace("+00:00", "Z")
            else:
                payload["quote_timestamp"] = timestamp.isoformat()
        return payload


class BatchNbboProvider(Protocol):
    source: str

    def fetch(
        self,
        symbols: Sequence[str],
        *,
        data_as_of: date,
        official_close: datetime,
    ) -> Mapping[str, NbboQuote]:
        ...


@dataclass(frozen=True)
class QuantUniverseCandidate:
    symbol: str
    name: str
    market: str
    exchange: str
    metadata: ProductMetadata | None
    trade_status: str | None
    directly_buyable: bool
    last_price: float | None
    price_data_as_of: date | None
    bar_data_as_of: date | None
    valid_daily_bars: int
    indicators: QuantIndicators | None
    indicator_error: str | None = None


@dataclass(frozen=True)
class CandidateMarketData:
    trade_status: str | None
    directly_buyable: bool
    last_price: float | None
    price_data_as_of: date | None
    bar_data_as_of: date | None
    valid_daily_bars: int
    indicators: QuantIndicators | None
    indicator_error: str | None = None


def build_unified_candidate_pool(
    catalog_items: Sequence[Mapping[str, Any]],
    *,
    metadata_batch: ProductMetadataBatch,
    market_data: Mapping[str, CandidateMarketData],
    data_as_of: date,
) -> list[QuantUniverseCandidate]:
    """Join the full catalog to point-in-time metadata and market facts."""
    if metadata_batch.data_as_of != data_as_of.isoformat():
        raise UniverseSelectionError(
            "product metadata data_as_of does not match candidate run"
        )
    normalized_market_data = {
        normalize_symbol(symbol): facts for symbol, facts in market_data.items()
    }
    candidates = []
    seen = set()
    for item in catalog_items:
        symbol = normalize_symbol(item.get("symbol"))
        if symbol in seen:
            raise UniverseSelectionError(
                f"duplicate catalog symbol: {symbol}"
            )
        seen.add(symbol)
        facts = normalized_market_data.get(symbol)
        candidates.append(QuantUniverseCandidate(
            symbol=symbol,
            name=str(
                item.get("name")
                or item.get("name_en")
                or symbol
            ).strip(),
            market=str(item.get("market") or "").strip().upper(),
            exchange=str(item.get("exchange") or "").strip().upper(),
            metadata=metadata_batch.records.get(symbol),
            trade_status=facts.trade_status if facts is not None else None,
            directly_buyable=(
                facts.directly_buyable if facts is not None else False
            ),
            last_price=facts.last_price if facts is not None else None,
            price_data_as_of=(
                facts.price_data_as_of if facts is not None else None
            ),
            bar_data_as_of=(
                facts.bar_data_as_of if facts is not None else None
            ),
            valid_daily_bars=(
                facts.valid_daily_bars if facts is not None else 0
            ),
            indicators=facts.indicators if facts is not None else None,
            indicator_error=(
                facts.indicator_error
                if facts is not None
                else "market_data_missing"
            ),
        ))
    return candidates


@dataclass(frozen=True)
class SelectionPolicy:
    q_threshold: float = AI_QUANT_THRESHOLD
    top_n: int = AI_CANDIDATE_LIMIT
    max_nbbo_request_units: int = 500
    batch_size: int = 50
    max_attempts: int = 2
    deadline_seconds: float = 90.0
    maximum_spread_bps: float = 30.0
    quote_staleness_seconds: float = 300.0

    def __post_init__(self) -> None:
        if not 0 <= self.q_threshold <= 100:
            raise UniverseSelectionError("q_threshold must be between 0 and 100")
        if self.top_n < 1:
            raise UniverseSelectionError("top_n must be positive")
        if self.max_nbbo_request_units < 0:
            raise UniverseSelectionError(
                "max_nbbo_request_units must be nonnegative"
            )
        if self.batch_size < 1 or self.max_attempts < 1:
            raise UniverseSelectionError("batch_size and max_attempts must be positive")
        if self.deadline_seconds <= 0 or self.quote_staleness_seconds < 0:
            raise UniverseSelectionError("invalid NBBO timing policy")


def _normalize_exchange(value: Any) -> str:
    normalized = " ".join(str(value or "").strip().upper().split())
    aliases = {
        "AMEX": "NYSE AMERICAN",
        "NYSEAMERICAN": "NYSE AMERICAN",
        "NYSE AMEX": "NYSE AMERICAN",
        "NASDAQ GLOBAL MARKET": "NASDAQ",
        "NASDAQ CAPITAL MARKET": "NASDAQ",
        "NASDAQ GLOBAL SELECT": "NASDAQ",
    }
    return aliases.get(normalized, normalized)


def _symbol_root(symbol: str) -> str:
    normalized = normalize_symbol(symbol)
    return normalized[:-3] if normalized.endswith(".US") else normalized


def _date_value(value: date | None) -> str | None:
    return value.isoformat() if value is not None else None


def _filter(status: str, reason: str | None = None) -> dict[str, str | None]:
    return {"status": status, "reason": reason}


def evaluate_pre_nbbo_filters(
    candidate: QuantUniverseCandidate,
    *,
    data_as_of: date,
) -> tuple[dict[str, dict[str, str | None]], list[str]]:
    """Evaluate H1-H7, H9, H10 preconditions and H11."""
    symbol = normalize_symbol(candidate.symbol)
    filters: dict[str, dict[str, str | None]] = {}

    filters["H1"] = (
        _filter("pass")
        if str(candidate.market).strip().upper() == "US"
        else _filter("fail", "market_not_us")
    )
    if candidate.metadata is None:
        filters["H2"] = _filter("fail", "asset_class_unknown")
    elif candidate.metadata.asset_class not in ELIGIBLE_ASSET_CLASSES:
        filters["H2"] = _filter("fail", "ineligible_asset_class")
    else:
        filters["H2"] = _filter("pass")

    exchange = _normalize_exchange(
        candidate.metadata.exchange
        if candidate.metadata is not None
        else candidate.exchange
    )
    filters["H3"] = (
        _filter("pass")
        if exchange in ELIGIBLE_EXCHANGES
        else _filter("fail", "ineligible_exchange")
    )
    filters["H4"] = (
        _filter("pass")
        if str(candidate.trade_status or "").strip().lower() == "normal"
        else _filter("fail", "trade_status_not_normal")
    )
    try:
        last_price = float(candidate.last_price)
    except (TypeError, ValueError):
        last_price = float("nan")
    filters["H5"] = (
        _filter("pass")
        if math.isfinite(last_price) and last_price >= 5.0
        else _filter("fail", "price_below_minimum_or_missing")
    )
    filters["H6"] = (
        _filter("pass")
        if candidate.directly_buyable
        else _filter("fail", "not_directly_buyable")
    )
    if candidate.indicators is None:
        filters["H7"] = _filter("fail", "turnover_missing")
    elif candidate.indicators.median_turnover_20d < 10_000_000:
        filters["H7"] = _filter("fail", "median_turnover_below_minimum")
    else:
        filters["H7"] = _filter("pass")
    filters["H8"] = _filter("pending")
    filters["H9"] = (
        _filter("pass")
        if candidate.valid_daily_bars >= MIN_DAILY_BARS
        and candidate.indicators is not None
        else _filter(
            "fail",
            candidate.indicator_error or "daily_bar_history_incomplete",
        )
    )
    if (
        candidate.price_data_as_of == data_as_of
        and candidate.bar_data_as_of == data_as_of
    ):
        filters["H10"] = _filter("pending")
    else:
        filters["H10"] = _filter("fail", "data_as_of_mismatch")
    filters["H11"] = (
        _filter("fail", "benchmark_symbol")
        if _symbol_root(symbol) in BENCHMARK_SYMBOLS
        else _filter("pass")
    )
    reasons = [
        str(filters[code]["reason"])
        for code in sorted(filters, key=lambda item: int(item[1:]))
        if filters[code]["status"] == "fail"
    ]
    return filters, reasons


def _quant_rank_key(state: Mapping[str, Any]) -> tuple[float, float, str]:
    score: QuantScore = state["quant_score"]
    candidate: QuantUniverseCandidate = state["candidate"]
    return (
        -score.total,
        -candidate.indicators.median_turnover_20d,
        normalize_symbol(candidate.symbol),
    )


def _upper_rank_key(state: Mapping[str, Any]) -> tuple[float, float, str]:
    candidate: QuantUniverseCandidate = state["candidate"]
    return (
        -float(state["q_upper_bound"]),
        -candidate.indicators.median_turnover_20d,
        normalize_symbol(candidate.symbol),
    )


def _obstacles_cannot_change_frontier(
    selected: Sequence[Mapping[str, Any]],
    obstacles: Sequence[Mapping[str, Any]],
    *,
    policy: SelectionPolicy,
) -> bool:
    if not obstacles:
        return True
    if len(selected) < policy.top_n:
        return all(
            float(item["q_upper_bound"]) < policy.q_threshold
            for item in obstacles
        )
    ranked = sorted(selected, key=_quant_rank_key)
    frontier = _quant_rank_key(ranked[policy.top_n - 1])
    best_obstacle = min(_upper_rank_key(item) for item in obstacles)
    return best_obstacle >= frontier


def _validate_nbbo(
    quote: NbboQuote,
    *,
    official_close: datetime,
    policy: SelectionPolicy,
) -> tuple[str, str | None, float | None]:
    status = str(quote.status or "").strip().lower()
    if status == "error":
        return "unresolved", quote.error_code or "nbbo_provider_error", None
    if status == "no_data":
        return "failed", "nbbo_no_data", None
    if status != "available":
        return "unresolved", "nbbo_status_unknown", None
    if str(quote.session or "").strip().lower() != "regular":
        return "failed", "nbbo_not_regular_session", None
    if quote.quote_timestamp is None:
        return "failed", "nbbo_timestamp_missing", None
    timestamp = quote.quote_timestamp
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        return "failed", "nbbo_timestamp_not_timezone_aware", None
    timestamp = timestamp.astimezone(timezone.utc)
    close_utc = official_close.astimezone(timezone.utc)
    earliest = close_utc - timedelta(seconds=policy.quote_staleness_seconds)
    if timestamp < earliest or timestamp > close_utc:
        return "failed", "nbbo_outside_close_window", None
    try:
        bid = float(quote.bid)
        ask = float(quote.ask)
    except (TypeError, ValueError):
        return "failed", "nbbo_invalid_prices", None
    if (
        not math.isfinite(bid)
        or not math.isfinite(ask)
        or bid <= 0
        or ask < bid
    ):
        return "failed", "nbbo_invalid_prices", None
    midpoint = (ask + bid) / 2.0
    spread_bps = (ask - bid) / midpoint * 10_000.0
    if spread_bps > policy.maximum_spread_bps:
        return "failed", "spread_above_maximum", spread_bps
    return "valid", None, spread_bps


class QuantUniverseSelector:
    def __init__(
        self,
        nbbo_provider: BatchNbboProvider,
        *,
        policy: SelectionPolicy | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.nbbo_provider = nbbo_provider
        self.policy = policy or SelectionPolicy()
        self.monotonic = monotonic

    def select(
        self,
        candidates: Sequence[QuantUniverseCandidate],
        *,
        data_as_of: date,
        official_close: datetime,
    ) -> dict[str, Any]:
        if official_close.tzinfo is None or official_close.utcoffset() is None:
            raise UniverseSelectionError("official_close must be timezone-aware")
        if official_close.date() != data_as_of:
            raise UniverseSelectionError(
                "official_close local date must equal data_as_of"
            )
        normalized_symbols = [normalize_symbol(item.symbol) for item in candidates]
        if len(normalized_symbols) != len(set(normalized_symbols)):
            raise UniverseSelectionError("candidate symbols must be unique")

        states: list[dict[str, Any]] = []
        pending: list[dict[str, Any]] = []
        for candidate in candidates:
            filters, reasons = evaluate_pre_nbbo_filters(
                candidate,
                data_as_of=data_as_of,
            )
            state = {
                "candidate": candidate,
                "filters": filters,
                "exclusion_reasons": reasons,
                "q_upper_bound": None,
                "quant_score": None,
                "nbbo": None,
                "spread_bps": None,
                "selection_status": (
                    "hard_filter_failed" if reasons else "pending_nbbo"
                ),
                "nbbo_error": None,
            }
            states.append(state)
            if reasons:
                continue
            try:
                upper_score = score_quantitative(
                    candidate.indicators,
                    spread_bps=0.0,
                )
            except QuantInputError as exc:
                state["filters"]["H9"] = _filter("fail", exc.reason)
                state["exclusion_reasons"].append(exc.reason)
                state["selection_status"] = "hard_filter_failed"
                continue
            state["q_upper_bound"] = upper_score.total
            pending.append(state)

        pending.sort(key=_upper_rank_key)
        unresolved: list[dict[str, Any]] = []
        selected: list[dict[str, Any]] = []
        request_units = 0
        batch_calls = 0
        started = self.monotonic()

        while not _obstacles_cannot_change_frontier(
            selected,
            pending + unresolved,
            policy=self.policy,
        ):
            elapsed = self.monotonic() - started
            remaining_budget = self.policy.max_nbbo_request_units - request_units
            if not pending or remaining_budget <= 0 or elapsed >= self.policy.deadline_seconds:
                break
            batch_size = min(
                self.policy.batch_size,
                len(pending),
                remaining_budget,
            )
            batch = pending[:batch_size]
            del pending[:batch_size]
            retry_states = list(batch)
            resolved_quotes: dict[str, NbboQuote] = {}
            last_error_code: dict[str, str] = {}

            for _attempt in range(self.policy.max_attempts):
                elapsed = self.monotonic() - started
                remaining_budget = (
                    self.policy.max_nbbo_request_units - request_units
                )
                if (
                    not retry_states
                    or remaining_budget <= 0
                    or elapsed >= self.policy.deadline_seconds
                ):
                    break
                attempt_states = retry_states[:remaining_budget]
                symbols = [
                    normalize_symbol(item["candidate"].symbol)
                    for item in attempt_states
                ]
                request_units += len(symbols)
                batch_calls += 1
                try:
                    response = self.nbbo_provider.fetch(
                        symbols,
                        data_as_of=data_as_of,
                        official_close=official_close,
                    )
                except Exception as exc:
                    response = {}
                    error_code = f"provider_exception:{type(exc).__name__}"
                    for symbol in symbols:
                        last_error_code[symbol] = error_code
                next_retry = list(retry_states[len(attempt_states):])
                for state in attempt_states:
                    symbol = normalize_symbol(state["candidate"].symbol)
                    quote = response.get(symbol)
                    if quote is None:
                        last_error_code.setdefault(symbol, "provider_missing_symbol")
                        next_retry.append(state)
                        continue
                    try:
                        response_symbol = normalize_symbol(quote.symbol)
                    except Exception:
                        response_symbol = ""
                    if response_symbol != symbol:
                        last_error_code[symbol] = "provider_symbol_mismatch"
                        next_retry.append(state)
                        continue
                    if str(quote.status).strip().lower() == "error":
                        last_error_code[symbol] = (
                            quote.error_code or "nbbo_provider_error"
                        )
                        next_retry.append(state)
                        continue
                    resolved_quotes[symbol] = quote
                retry_states = next_retry

            unresolved_ids = {id(item) for item in retry_states}
            for state in batch:
                candidate = state["candidate"]
                symbol = normalize_symbol(candidate.symbol)
                if id(state) in unresolved_ids:
                    state["nbbo_error"] = last_error_code.get(
                        symbol,
                        "nbbo_unresolved",
                    )
                    state["filters"]["H8"] = _filter(
                        "unresolved",
                        state["nbbo_error"],
                    )
                    state["filters"]["H10"] = _filter("not_evaluated")
                    state["exclusion_reasons"].append(state["nbbo_error"])
                    state["selection_status"] = "nbbo_unresolved"
                    unresolved.append(state)
                    continue
                quote = resolved_quotes[symbol]
                state["nbbo"] = quote
                quote_status, reason, spread_bps = _validate_nbbo(
                    quote,
                    official_close=official_close,
                    policy=self.policy,
                )
                state["spread_bps"] = spread_bps
                if quote_status == "unresolved":
                    state["nbbo_error"] = reason
                    state["filters"]["H8"] = _filter("unresolved", reason)
                    state["filters"]["H10"] = _filter("not_evaluated")
                    state["exclusion_reasons"].append(reason)
                    state["selection_status"] = "nbbo_unresolved"
                    unresolved.append(state)
                    continue
                if quote_status == "failed":
                    filter_code = (
                        "H10"
                        if (
                            reason is not None
                            and (
                                "timestamp" in reason
                                or reason == "nbbo_outside_close_window"
                            )
                        )
                        else "H8"
                    )
                    state["filters"][filter_code] = _filter("fail", reason)
                    other_code = "H8" if filter_code == "H10" else "H10"
                    if state["filters"][other_code]["status"] == "pending":
                        state["filters"][other_code] = _filter("not_evaluated")
                    state["exclusion_reasons"].append(reason)
                    state["selection_status"] = "hard_filter_failed"
                    continue
                state["filters"]["H8"] = _filter("pass")
                state["filters"]["H10"] = _filter("pass")
                try:
                    score = score_quantitative(
                        candidate.indicators,
                        spread_bps=float(spread_bps),
                    )
                except QuantInputError as exc:
                    state["filters"]["H8"] = _filter("fail", exc.reason)
                    state["exclusion_reasons"].append(exc.reason)
                    state["selection_status"] = "hard_filter_failed"
                    continue
                state["quant_score"] = score
                if score.total >= self.policy.q_threshold:
                    state["selection_status"] = "quant_eligible"
                    selected.append(state)
                else:
                    state["selection_status"] = "quant_score_below_threshold"
                    state["exclusion_reasons"].append(
                        "quant_score_below_threshold"
                    )

        boundary_proven = _obstacles_cannot_change_frontier(
            selected,
            pending + unresolved,
            policy=self.policy,
        )
        if not boundary_proven:
            elapsed = self.monotonic() - started
            pending_reason = (
                "nbbo_deadline_exceeded"
                if elapsed >= self.policy.deadline_seconds
                else "nbbo_budget_exhausted"
                if request_units >= self.policy.max_nbbo_request_units
                else "nbbo_boundary_unproven"
            )
            for state in pending:
                state["nbbo_error"] = pending_reason
                state["filters"]["H8"] = _filter(
                    "unresolved",
                    pending_reason,
                )
                state["filters"]["H10"] = _filter("not_evaluated")
                state["exclusion_reasons"].append(pending_reason)
                state["selection_status"] = "nbbo_not_evaluated"
        ranked = sorted(selected, key=_quant_rank_key)
        top_states = ranked[: self.policy.top_n] if boundary_proven else []
        top_symbols = {
            normalize_symbol(item["candidate"].symbol) for item in top_states
        }

        if boundary_proven:
            prune_reason = (
                "quant_upper_bound_below_threshold"
                if len(selected) < self.policy.top_n
                else "quant_upper_bound_below_frontier"
            )
            for state in pending + unresolved:
                state["selection_status"] = prune_reason
                state["exclusion_reasons"] = [prune_reason]
                state["filters"]["H8"] = _filter("not_evaluated")
                state["filters"]["H10"] = _filter("not_evaluated")
            unresolved = []

        candidate_payloads = []
        for state in sorted(
            states,
            key=lambda item: normalize_symbol(item["candidate"].symbol),
        ):
            candidate: QuantUniverseCandidate = state["candidate"]
            symbol = normalize_symbol(candidate.symbol)
            quote = state["nbbo"]
            candidate_input = {
                "bar_data_as_of": _date_value(candidate.bar_data_as_of),
                "directly_buyable": candidate.directly_buyable,
                "exchange": _normalize_exchange(candidate.exchange),
                "indicator_error": candidate.indicator_error,
                "indicators": (
                    candidate.indicators.to_dict()
                    if candidate.indicators is not None else None
                ),
                "last_price": candidate.last_price,
                "market": str(candidate.market).strip().upper(),
                "metadata": (
                    candidate.metadata.to_dict()
                    if candidate.metadata is not None else None
                ),
                "name": candidate.name,
                "nbbo": quote.to_dict() if quote is not None else None,
                "nbbo_error": state["nbbo_error"],
                "price_data_as_of": _date_value(candidate.price_data_as_of),
                "quant_input_schema": "quant-selector-candidate-input-v1",
                "symbol": symbol,
                "valid_daily_bars": candidate.valid_daily_bars,
            }
            base_payload = {
                **candidate_input,
                "filter_version": FILTER_VERSION,
                "hard_filters": state["filters"],
                "q_upper_bound": state["q_upper_bound"],
                "quant_score": (
                    state["quant_score"].to_dict()
                    if state["quant_score"] is not None else None
                ),
                "selection_status": state["selection_status"],
                "spread_bps": state["spread_bps"],
            }
            candidate_payloads.append({
                **base_payload,
                "candidate_quant_input_hash": canonical_sha256(candidate_input),
                "exclusion_reasons": list(state["exclusion_reasons"]),
                "selected_for_ai": boundary_proven and symbol in top_symbols,
            })

        ranking = [
            {
                "median_turnover_20d": (
                    item["candidate"].indicators.median_turnover_20d
                ),
                "q": item["quant_score"].total,
                "rank": index + 1,
                "symbol": normalize_symbol(item["candidate"].symbol),
            }
            for index, item in enumerate(ranked)
        ]
        selection_manifest = {
            "boundary_proven": boundary_proven,
            "candidates": [
                {
                    "q_upper_bound": item["q_upper_bound"],
                    "selection_status": item["selection_status"],
                    "symbol": item["symbol"],
                }
                for item in candidate_payloads
            ],
            "filter_version": FILTER_VERSION,
            "q_threshold": self.policy.q_threshold,
            "ranking": ranking,
            "top_n": self.policy.top_n,
            "top_symbols": [
                normalize_symbol(item["candidate"].symbol)
                for item in top_states
            ],
        }
        return {
            "status": "completed" if boundary_proven else "partial",
            "filter_version": FILTER_VERSION,
            "data_as_of": data_as_of.isoformat(),
            "official_close": official_close.astimezone(timezone.utc).isoformat(
                timespec="milliseconds"
            ).replace("+00:00", "Z"),
            "boundary_proven": boundary_proven,
            "nbbo_provider": str(getattr(self.nbbo_provider, "source", "unknown")),
            "nbbo_request_units": request_units,
            "nbbo_batch_calls": batch_calls,
            "unresolved_symbols": [
                normalize_symbol(item["candidate"].symbol)
                for item in unresolved
            ],
            "candidates": candidate_payloads,
            "quant_ranking": ranking,
            "ai_candidate_symbols": selection_manifest["top_symbols"],
            "selection_manifest": selection_manifest,
            "selection_manifest_hash": canonical_sha256(selection_manifest),
        }
