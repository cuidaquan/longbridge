"""Unified US-main universe filtering and deterministic Top-30 selection."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
import math
from typing import Any, Mapping, Sequence

from .quant_stock_selector import (
    MIN_DAILY_BARS,
    QuantIndicators,
    QuantInputError,
    QuantScore,
    score_quantitative,
)
from .quant_stock_selector_hashing import canonical_sha256
from .quant_stock_selector_symbols import normalize_symbol


FILTER_VERSION = "quant-selector-filter-v1.4"
AI_QUANT_THRESHOLD = 65.0
AI_CANDIDATE_LIMIT = 30
BENCHMARK_SYMBOLS = frozenset({"SPY"})
ELIGIBLE_EXCHANGES = frozenset({"NASDAQ"})
ELIGIBLE_BOARD = "USMAIN"
MIN_PRICE = 5.0
MAX_PRICE = 500.0
MIN_CURRENT_TURNOVER = 10_000_000.0
MIN_TOTAL_MARKET_VALUE = 1_000_000_000.0
MIN_VOLUME_RATIO = 0.8
MAX_PE_TTM = 50.0


class UniverseSelectionError(ValueError):
    pass


@dataclass(frozen=True)
class QuantUniverseCandidate:
    symbol: str
    name: str
    market: str
    board: str
    exchange: str
    catalog_source: str
    catalog_version: str
    catalog_captured_at: str
    trade_status: str | None
    last_price: float | None
    price_data_as_of: date | None
    bar_data_as_of: date | None
    valid_daily_bars: int
    indicators: QuantIndicators | None
    indicator_error: str | None = None
    current_turnover: float | None = None
    total_market_value: float | None = None
    volume_ratio: float | None = None
    pe_ttm_ratio: float | None = None
    ten_day_change_rate: float | None = None
    ten_day_relative_strength: float | None = None

    def catalog_evidence(self) -> dict[str, str]:
        return {
            "board": _normalize_board(self.board),
            "captured_at": str(self.catalog_captured_at),
            "exchange": _normalize_exchange(self.exchange),
            "market": str(self.market).strip().upper(),
            "source": str(self.catalog_source).strip(),
            "source_version": str(self.catalog_version).strip(),
        }


@dataclass(frozen=True)
class CandidateMarketData:
    trade_status: str | None
    last_price: float | None
    price_data_as_of: date | None
    bar_data_as_of: date | None
    valid_daily_bars: int
    indicators: QuantIndicators | None
    indicator_error: str | None = None
    current_turnover: float | None = None
    total_market_value: float | None = None
    volume_ratio: float | None = None
    pe_ttm_ratio: float | None = None
    ten_day_change_rate: float | None = None
    ten_day_relative_strength: float | None = None


@dataclass(frozen=True)
class SelectionPolicy:
    q_threshold: float = AI_QUANT_THRESHOLD
    top_n: int = AI_CANDIDATE_LIMIT

    def __post_init__(self) -> None:
        if not 0 <= self.q_threshold <= 100:
            raise UniverseSelectionError("q_threshold must be between 0 and 100")
        if self.top_n < 1:
            raise UniverseSelectionError("top_n must be positive")


def _normalize_board(value: Any) -> str:
    return "".join(str(value or "").strip().upper().split())


def _normalize_exchange(value: Any) -> str:
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


def _symbol_root(symbol: str) -> str:
    normalized = normalize_symbol(symbol)
    return normalized[:-3] if normalized.endswith(".US") else normalized


def _date_value(value: date | None) -> str | None:
    return value.isoformat() if value is not None else None


def _filter(status: str, reason: str | None = None) -> dict[str, str | None]:
    return {"status": status, "reason": reason}


def build_unified_candidate_pool(
    catalog_items: Sequence[Mapping[str, Any]],
    *,
    market_data: Mapping[str, CandidateMarketData],
    catalog_source: str,
    catalog_version: str,
    catalog_captured_at: str,
) -> list[QuantUniverseCandidate]:
    """Join the complete point-in-time US-main catalog to market facts."""
    if not str(catalog_source).strip():
        raise UniverseSelectionError("catalog_source is required")
    if not str(catalog_version).strip():
        raise UniverseSelectionError("catalog_version is required")
    if not str(catalog_captured_at).strip():
        raise UniverseSelectionError("catalog_captured_at is required")
    normalized_market_data = {
        normalize_symbol(symbol): facts for symbol, facts in market_data.items()
    }
    candidates = []
    seen = set()
    for item in catalog_items:
        symbol = normalize_symbol(item.get("symbol"))
        if symbol in seen:
            raise UniverseSelectionError(f"duplicate catalog symbol: {symbol}")
        seen.add(symbol)
        facts = normalized_market_data.get(symbol)
        candidates.append(QuantUniverseCandidate(
            symbol=symbol,
            name=str(item.get("name") or item.get("name_en") or symbol).strip(),
            market=str(item.get("market") or "").strip().upper(),
            board=str(item.get("board") or "").strip(),
            exchange=str(item.get("exchange") or "").strip(),
            catalog_source=str(catalog_source).strip(),
            catalog_version=str(catalog_version).strip(),
            catalog_captured_at=str(catalog_captured_at).strip(),
            trade_status=facts.trade_status if facts is not None else None,
            last_price=facts.last_price if facts is not None else None,
            price_data_as_of=(
                facts.price_data_as_of if facts is not None else None
            ),
            bar_data_as_of=facts.bar_data_as_of if facts is not None else None,
            valid_daily_bars=facts.valid_daily_bars if facts is not None else 0,
            indicators=facts.indicators if facts is not None else None,
            indicator_error=(
                facts.indicator_error
                if facts is not None
                else "market_data_missing"
            ),
            current_turnover=(
                facts.current_turnover if facts is not None else None
            ),
            total_market_value=(
                facts.total_market_value if facts is not None else None
            ),
            volume_ratio=facts.volume_ratio if facts is not None else None,
            pe_ttm_ratio=facts.pe_ttm_ratio if facts is not None else None,
            ten_day_change_rate=(
                facts.ten_day_change_rate if facts is not None else None
            ),
            ten_day_relative_strength=(
                facts.ten_day_relative_strength if facts is not None else None
            ),
        ))
    return candidates


def evaluate_hard_filters(
    candidate: QuantUniverseCandidate,
    *,
    data_as_of: date,
) -> tuple[dict[str, dict[str, str | None]], list[str]]:
    """Evaluate the versioned H1-H13 hard-filter contract."""
    symbol = normalize_symbol(candidate.symbol)
    filters: dict[str, dict[str, str | None]] = {}
    filters["H1"] = (
        _filter("pass")
        if str(candidate.market).strip().upper() == "US"
        and _normalize_board(candidate.board) == ELIGIBLE_BOARD
        else _filter("fail", "market_or_catalog_not_usmain")
    )
    filters["H2"] = (
        _filter("pass")
        if _normalize_exchange(candidate.exchange) in ELIGIBLE_EXCHANGES
        else _filter("fail", "ineligible_exchange")
    )
    filters["H3"] = (
        _filter("pass")
        if str(candidate.trade_status or "").strip().lower() == "normal"
        else _filter("fail", "trade_status_not_normal")
    )
    try:
        last_price = float(candidate.last_price)
    except (TypeError, ValueError):
        last_price = float("nan")
    filters["H4"] = (
        _filter("pass")
        if math.isfinite(last_price) and MIN_PRICE <= last_price <= MAX_PRICE
        else _filter("fail", "price_outside_5_to_500_or_missing")
    )
    if candidate.indicators is None:
        filters["H5"] = _filter("fail", "turnover_missing")
    elif candidate.indicators.median_turnover_20d < 10_000_000:
        filters["H5"] = _filter("fail", "median_turnover_below_minimum")
    else:
        filters["H5"] = _filter("pass")
    filters["H6"] = (
        _filter("pass")
        if candidate.valid_daily_bars >= MIN_DAILY_BARS
        and candidate.indicators is not None
        else _filter(
            "fail",
            candidate.indicator_error or "daily_bar_history_incomplete",
        )
    )
    filters["H7"] = (
        _filter("pass")
        if candidate.price_data_as_of == data_as_of
        and candidate.bar_data_as_of == data_as_of
        else _filter("fail", "data_as_of_mismatch")
    )
    filters["H8"] = (
        _filter("fail", "benchmark_symbol")
        if _symbol_root(symbol) in BENCHMARK_SYMBOLS
        else _filter("pass")
    )
    filters["H9"] = (
        _filter("pass")
        if candidate.current_turnover is not None
        and candidate.current_turnover >= MIN_CURRENT_TURNOVER
        else _filter("fail", "current_turnover_below_minimum_or_missing")
    )
    filters["H10"] = (
        _filter("pass")
        if candidate.total_market_value is not None
        and candidate.total_market_value >= MIN_TOTAL_MARKET_VALUE
        else _filter("fail", "market_value_below_minimum_or_missing")
    )
    filters["H11"] = (
        _filter("pass")
        if candidate.volume_ratio is not None
        and candidate.volume_ratio >= MIN_VOLUME_RATIO
        else _filter("fail", "volume_ratio_below_minimum_or_missing")
    )
    filters["H12"] = (
        _filter("pass")
        if candidate.ten_day_relative_strength is not None
        and candidate.ten_day_relative_strength >= 0.0
        else _filter("fail", "ten_day_relative_strength_below_spy_or_missing")
    )
    filters["H13"] = (
        _filter("pass")
        if candidate.pe_ttm_ratio is not None
        and 0.0 < candidate.pe_ttm_ratio < MAX_PE_TTM
        else _filter("fail", "pe_ttm_outside_0_to_50_or_missing")
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


class QuantUniverseSelector:
    def __init__(self, *, policy: SelectionPolicy | None = None) -> None:
        self.policy = policy or SelectionPolicy()

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
        eligible: list[dict[str, Any]] = []
        for candidate in candidates:
            filters, reasons = evaluate_hard_filters(
                candidate,
                data_as_of=data_as_of,
            )
            state = {
                "candidate": candidate,
                "filters": filters,
                "exclusion_reasons": reasons,
                "quant_score": None,
                "selection_status": "hard_filter_failed" if reasons else "scoring",
            }
            states.append(state)
            if reasons:
                continue
            try:
                score = score_quantitative(candidate.indicators)
            except QuantInputError as exc:
                state["filters"]["H6"] = _filter("fail", exc.reason)
                state["exclusion_reasons"].append(exc.reason)
                state["selection_status"] = "hard_filter_failed"
                continue
            state["quant_score"] = score
            if score.total >= self.policy.q_threshold:
                state["selection_status"] = "quant_eligible"
                eligible.append(state)
            else:
                state["selection_status"] = "quant_score_below_threshold"
                state["exclusion_reasons"].append("quant_score_below_threshold")

        ranked = sorted(eligible, key=_quant_rank_key)
        top_states = ranked[:self.policy.top_n]
        top_symbols = {
            normalize_symbol(item["candidate"].symbol) for item in top_states
        }
        candidate_payloads = []
        for state in sorted(
            states,
            key=lambda item: normalize_symbol(item["candidate"].symbol),
        ):
            candidate: QuantUniverseCandidate = state["candidate"]
            symbol = normalize_symbol(candidate.symbol)
            candidate_input = {
                "bar_data_as_of": _date_value(candidate.bar_data_as_of),
                "catalog_evidence": candidate.catalog_evidence(),
                "indicator_error": candidate.indicator_error,
                "indicators": (
                    candidate.indicators.to_dict()
                    if candidate.indicators is not None else None
                ),
                "last_price": candidate.last_price,
                "current_turnover": candidate.current_turnover,
                "total_market_value": candidate.total_market_value,
                "volume_ratio": candidate.volume_ratio,
                "pe_ttm_ratio": candidate.pe_ttm_ratio,
                "ten_day_change_rate": candidate.ten_day_change_rate,
                "ten_day_relative_strength": candidate.ten_day_relative_strength,
                "name": candidate.name,
                "price_data_as_of": _date_value(candidate.price_data_as_of),
                "quant_input_schema": "quant-selector-candidate-input-v2",
                "symbol": symbol,
                "trade_status": candidate.trade_status,
                "valid_daily_bars": candidate.valid_daily_bars,
            }
            candidate_payloads.append({
                **candidate_input,
                "candidate_quant_input_hash": canonical_sha256(candidate_input),
                "exclusion_reasons": list(state["exclusion_reasons"]),
                "filter_version": FILTER_VERSION,
                "hard_filters": state["filters"],
                "quant_score": (
                    state["quant_score"].to_dict()
                    if state["quant_score"] is not None else None
                ),
                "selected_for_ai": symbol in top_symbols,
                "selection_status": state["selection_status"],
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
        ranking_by_symbol = {item["symbol"]: item for item in ranking}
        manifest_candidates = []
        for item in candidate_payloads:
            rank_item = ranking_by_symbol.get(item["symbol"])
            manifest_candidates.append({
                "final_q": rank_item["q"] if rank_item is not None else None,
                "final_rank": rank_item["rank"] if rank_item is not None else None,
                "hard_filters": item["hard_filters"],
                "selection_status": item["selection_status"],
                "stable_sort_key": {
                    "median_turnover_20d_desc": (
                        rank_item["median_turnover_20d"]
                        if rank_item is not None else None
                    ),
                    "q_desc": rank_item["q"] if rank_item is not None else None,
                    "symbol_asc": item["symbol"],
                },
                "symbol": item["symbol"],
            })
        selection_manifest = {
            "candidate_set_method": "deterministic-full-score-v1.4",
            "candidates": manifest_candidates,
            "filter_version": FILTER_VERSION,
            "q_threshold": self.policy.q_threshold,
            "ranking": ranking,
            "top_n": self.policy.top_n,
            "top_symbols": [
                normalize_symbol(item["candidate"].symbol) for item in top_states
            ],
        }
        return {
            "status": "completed",
            "filter_version": FILTER_VERSION,
            "data_as_of": data_as_of.isoformat(),
            "official_close": official_close.astimezone(timezone.utc).isoformat(
                timespec="milliseconds"
            ).replace("+00:00", "Z"),
            "candidates": candidate_payloads,
            "quant_ranking": ranking,
            "ai_candidate_symbols": selection_manifest["top_symbols"],
            "selection_manifest": selection_manifest,
            "selection_manifest_hash": canonical_sha256(selection_manifest),
        }
