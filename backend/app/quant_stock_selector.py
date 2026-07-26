"""Deterministic indicators and scoring for the quantitative selector."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
import math
from statistics import median
from typing import Any, Mapping, Sequence

import numpy as np


SCORE_VERSION = "quant-selector-score-v1.2"
MIN_DAILY_BARS = 85


class QuantInputError(ValueError):
    """A deterministic data exclusion with a machine-readable reason."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class QuantIndicators:
    data_as_of: str
    close: float
    median_turnover_20d: float
    ma20: float
    ma60: float
    ma20_slope_10d: float
    rs20: float
    rs60: float
    rsi14: float
    macd_line: float
    macd_signal: float
    macd_histogram: float
    macd_histogram_previous_1: float
    macd_histogram_previous_2: float
    atr14: float
    atr14_close: float
    volatility_20d: float
    max_drawdown_60d: float
    return_5d: float
    volume_ratio_5_20: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class QuantScore:
    score_version: str
    total: float
    liquidity: float
    trend: float
    relative_strength: float
    momentum: float
    risk: float
    turnover_score: float
    moving_average_score: float
    ma20_slope_score: float
    rs20_score: float
    rs60_score: float
    rsi14_score: float
    macd_histogram_score: float
    volume_confirmation_score: float
    atr_close_score: float
    volatility_score: float
    max_drawdown_score: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _bar_date(value: Any) -> date:
    if isinstance(value, datetime):
        if value.tzinfo is not None and value.utcoffset() is not None:
            value = value.astimezone(timezone.utc)
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        raise QuantInputError("invalid_timestamp", "bar timestamp is required")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is not None and parsed.utcoffset() is not None:
            parsed = parsed.astimezone(timezone.utc)
        return parsed.date()
    except ValueError:
        try:
            return date.fromisoformat(text)
        except ValueError as exc:
            raise QuantInputError(
                "invalid_timestamp",
                f"invalid bar timestamp: {text}",
            ) from exc


def _finite_number(
    value: Any,
    *,
    reason: str,
    field: str,
    positive: bool = False,
    nonnegative: bool = False,
) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise QuantInputError(reason, f"{field} must be numeric") from exc
    if not math.isfinite(number):
        raise QuantInputError(reason, f"{field} must be finite")
    if positive and number <= 0:
        raise QuantInputError(reason, f"{field} must be positive")
    if nonnegative and number < 0:
        raise QuantInputError(reason, f"{field} must be nonnegative")
    return number


def _normalize_price_bars(
    bars: Sequence[Mapping[str, Any]],
    *,
    label: str,
) -> list[dict[str, Any]]:
    normalized = []
    seen_dates = set()
    for bar in bars:
        trading_date = _bar_date(bar.get("ts"))
        if trading_date in seen_dates:
            raise QuantInputError(
                "duplicate_trading_date",
                f"{label} contains duplicate date {trading_date}",
            )
        seen_dates.add(trading_date)
        row = {"date": trading_date}
        for field in ("open", "high", "low", "close"):
            row[field] = _finite_number(
                bar.get(field),
                reason="invalid_price",
                field=f"{label}.{field}",
                positive=True,
            )
        if row["high"] < max(row["open"], row["close"], row["low"]):
            raise QuantInputError("invalid_ohlc", f"{label} high is inconsistent")
        if row["low"] > min(row["open"], row["close"], row["high"]):
            raise QuantInputError("invalid_ohlc", f"{label} low is inconsistent")
        normalized.append(row)
    normalized.sort(key=lambda item: item["date"])
    return normalized


def _normalize_raw_bars(
    bars: Sequence[Mapping[str, Any]],
) -> dict[date, dict[str, float]]:
    normalized = {}
    for bar in bars:
        trading_date = _bar_date(bar.get("ts"))
        if trading_date in normalized:
            raise QuantInputError(
                "duplicate_trading_date",
                f"raw bars contain duplicate date {trading_date}",
            )
        close = _finite_number(
            bar.get("close"),
            reason="invalid_raw_price",
            field="raw.close",
            positive=True,
        )
        volume = _finite_number(
            bar.get("volume"),
            reason="invalid_volume",
            field="raw.volume",
            nonnegative=True,
        )
        turnover_value = bar.get("turnover")
        turnover = (
            close * volume
            if turnover_value is None
            else _finite_number(
                turnover_value,
                reason="invalid_turnover",
                field="raw.turnover",
                nonnegative=True,
            )
        )
        normalized[trading_date] = {
            "close": close,
            "volume": volume,
            "turnover": turnover,
        }
    return normalized


def _ema(values: Sequence[float], period: int) -> list[float | None]:
    if len(values) < period:
        raise QuantInputError(
            "insufficient_history",
            f"EMA{period} requires {period} observations",
        )
    result: list[float | None] = [None] * len(values)
    seed = float(np.mean(np.asarray(values[:period], dtype=np.float64)))
    result[period - 1] = seed
    alpha = 2.0 / (period + 1.0)
    previous = seed
    for index in range(period, len(values)):
        previous = alpha * values[index] + (1.0 - alpha) * previous
        result[index] = previous
    return result


def _wilder_rsi(closes: Sequence[float], period: int = 14) -> float:
    if len(closes) < period + 1:
        raise QuantInputError(
            "insufficient_history",
            f"RSI{period} requires {period + 1} closes",
        )
    changes = np.diff(np.asarray(closes, dtype=np.float64))
    gains = np.maximum(changes, 0.0)
    losses = np.maximum(-changes, 0.0)
    avg_gain = float(np.mean(gains[:period]))
    avg_loss = float(np.mean(losses[:period]))
    for index in range(period, len(changes)):
        avg_gain = (avg_gain * (period - 1) + float(gains[index])) / period
        avg_loss = (avg_loss * (period - 1) + float(losses[index])) / period
    if avg_gain == 0.0 and avg_loss == 0.0:
        return 50.0
    if avg_loss == 0.0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def _macd(closes: Sequence[float]) -> tuple[float, float, list[float]]:
    ema12 = _ema(closes, 12)
    ema26 = _ema(closes, 26)
    valid_indexes = [
        index
        for index, (fast, slow) in enumerate(zip(ema12, ema26))
        if fast is not None and slow is not None
    ]
    dif_values = [
        float(ema12[index]) - float(ema26[index])
        for index in valid_indexes
    ]
    signal_values = _ema(dif_values, 9)
    histogram = [
        dif - float(signal)
        for dif, signal in zip(dif_values, signal_values)
        if signal is not None
    ]
    if len(histogram) < 3:
        raise QuantInputError(
            "insufficient_history",
            "MACD histogram requires at least three valid observations",
        )
    return dif_values[-1], float(signal_values[-1]), histogram


def _wilder_atr(bars: Sequence[Mapping[str, Any]], period: int = 14) -> float:
    if len(bars) < period + 1:
        raise QuantInputError(
            "insufficient_history",
            f"ATR{period} requires {period + 1} bars",
        )
    true_ranges = []
    for index in range(1, len(bars)):
        current = bars[index]
        previous_close = float(bars[index - 1]["close"])
        true_ranges.append(max(
            float(current["high"]) - float(current["low"]),
            abs(float(current["high"]) - previous_close),
            abs(float(current["low"]) - previous_close),
        ))
    atr = float(np.mean(np.asarray(true_ranges[:period], dtype=np.float64)))
    for value in true_ranges[period:]:
        atr = (atr * (period - 1) + value) / period
    return atr


def calculate_quant_indicators(
    adjusted_bars: Sequence[Mapping[str, Any]],
    raw_bars: Sequence[Mapping[str, Any]],
    spy_adjusted_bars: Sequence[Mapping[str, Any]],
) -> QuantIndicators:
    """Calculate every v1.1 indicator without intermediate rounding."""
    adjusted = _normalize_price_bars(adjusted_bars, label="adjusted")
    spy = _normalize_price_bars(spy_adjusted_bars, label="spy")
    raw_by_date = _normalize_raw_bars(raw_bars)
    if len(adjusted) < MIN_DAILY_BARS:
        raise QuantInputError(
            "insufficient_history",
            f"adjusted bars require at least {MIN_DAILY_BARS} observations",
        )
    if not spy or adjusted[-1]["date"] != spy[-1]["date"]:
        raise QuantInputError(
            "benchmark_date_mismatch",
            "candidate and SPY must share data_as_of",
        )

    closes = [float(row["close"]) for row in adjusted]
    ma20 = float(np.mean(np.asarray(closes[-20:], dtype=np.float64)))
    ma60 = float(np.mean(np.asarray(closes[-60:], dtype=np.float64)))
    ma20_previous = float(
        np.mean(np.asarray(closes[-30:-10], dtype=np.float64))
    )
    ma20_slope = ma20 / ma20_previous - 1.0

    candidate_close_by_date = {
        row["date"]: float(row["close"]) for row in adjusted
    }
    spy_close_by_date = {row["date"]: float(row["close"]) for row in spy}
    common_dates = sorted(set(candidate_close_by_date) & set(spy_close_by_date))
    if len(common_dates) < 61 or common_dates[-1] != adjusted[-1]["date"]:
        raise QuantInputError(
            "benchmark_alignment_missing",
            "RS60 requires 61 common NYSE trading dates ending data_as_of",
        )

    def relative_strength(period: int) -> float:
        end = common_dates[-1]
        start = common_dates[-period - 1]
        candidate_return = (
            candidate_close_by_date[end] / candidate_close_by_date[start] - 1.0
        )
        spy_return = spy_close_by_date[end] / spy_close_by_date[start] - 1.0
        return candidate_return - spy_return

    macd_line, macd_signal, histogram = _macd(closes)
    atr14 = _wilder_atr(adjusted)
    simple_returns = (
        np.diff(np.asarray(closes[-21:], dtype=np.float64))
        / np.asarray(closes[-21:-1], dtype=np.float64)
    )
    volatility = float(np.std(simple_returns, ddof=1) * math.sqrt(252.0))

    peak = closes[-60]
    max_drawdown = 0.0
    for close in closes[-60:]:
        peak = max(peak, close)
        max_drawdown = max(max_drawdown, 1.0 - close / peak)

    volume_dates = [row["date"] for row in adjusted[-20:]]
    try:
        raw_window = [raw_by_date[trading_date] for trading_date in volume_dates]
    except KeyError as exc:
        raise QuantInputError(
            "raw_bar_alignment_missing",
            f"raw bar missing for {exc.args[0]}",
        ) from exc
    volumes = [row["volume"] for row in raw_window]
    average_volume_20 = float(np.mean(np.asarray(volumes, dtype=np.float64)))
    if average_volume_20 == 0.0:
        raise QuantInputError(
            "zero_volume_denominator",
            "20-day average volume must be positive",
        )

    return QuantIndicators(
        data_as_of=adjusted[-1]["date"].isoformat(),
        close=closes[-1],
        median_turnover_20d=float(median(
            row["turnover"] for row in raw_window
        )),
        ma20=ma20,
        ma60=ma60,
        ma20_slope_10d=ma20_slope,
        rs20=relative_strength(20),
        rs60=relative_strength(60),
        rsi14=_wilder_rsi(closes),
        macd_line=macd_line,
        macd_signal=macd_signal,
        macd_histogram=histogram[-1],
        macd_histogram_previous_1=histogram[-2],
        macd_histogram_previous_2=histogram[-3],
        atr14=atr14,
        atr14_close=atr14 / closes[-1],
        volatility_20d=volatility,
        max_drawdown_60d=max_drawdown,
        return_5d=closes[-1] / closes[-6] - 1.0,
        volume_ratio_5_20=(
            float(np.mean(np.asarray(volumes[-5:], dtype=np.float64)))
            / average_volume_20
        ),
    )


def _turnover_score(value: float) -> float:
    if value >= 100_000_000:
        return 100.0
    if value >= 50_000_000:
        return 85.0
    if value >= 20_000_000:
        return 70.0
    if value >= 10_000_000:
        return 50.0
    raise QuantInputError("h5_min_turnover", "median turnover is below 10M USD")


def score_quantitative(indicators: QuantIndicators) -> QuantScore:
    """Apply the frozen v1.2 score thresholds to unrounded indicators."""
    for field, value in indicators.to_dict().items():
        if field == "data_as_of":
            continue
        if not math.isfinite(float(value)):
            raise QuantInputError(
                "invalid_indicator",
                f"{field} must be finite",
            )
    if indicators.atr14_close < 0 or indicators.volatility_20d < 0:
        raise QuantInputError(
            "invalid_indicator",
            "risk ratios must be nonnegative",
        )
    if not 0 <= indicators.max_drawdown_60d <= 1:
        raise QuantInputError(
            "invalid_indicator",
            "max_drawdown_60d must be between 0 and 1",
        )
    if not 0 <= indicators.rsi14 <= 100:
        raise QuantInputError(
            "invalid_indicator",
            "rsi14 must be between 0 and 100",
        )
    turnover_score = _turnover_score(indicators.median_turnover_20d)
    liquidity = turnover_score

    if indicators.close > indicators.ma20 > indicators.ma60:
        moving_average_score = 100.0
    elif indicators.close > indicators.ma20 and indicators.ma20 <= indicators.ma60:
        moving_average_score = 70.0
    elif indicators.close <= indicators.ma20 and indicators.ma20 > indicators.ma60:
        moving_average_score = 40.0
    else:
        moving_average_score = 0.0

    slope = indicators.ma20_slope_10d
    ma20_slope_score = (
        100.0 if slope >= 0.04 else
        80.0 if slope >= 0.02 else
        60.0 if slope >= 0.0 else
        30.0 if slope >= -0.02 else
        0.0
    )
    trend = 0.6 * moving_average_score + 0.4 * ma20_slope_score

    rs20_score = (
        100.0 if indicators.rs20 >= 0.10 else
        85.0 if indicators.rs20 >= 0.05 else
        65.0 if indicators.rs20 >= 0.0 else
        35.0 if indicators.rs20 >= -0.05 else
        0.0
    )
    rs60_score = (
        100.0 if indicators.rs60 >= 0.15 else
        85.0 if indicators.rs60 >= 0.07 else
        65.0 if indicators.rs60 >= 0.0 else
        35.0 if indicators.rs60 >= -0.07 else
        0.0
    )
    relative_strength = 0.6 * rs20_score + 0.4 * rs60_score

    rsi = indicators.rsi14
    rsi14_score = (
        100.0 if 50.0 <= rsi <= 65.0 else
        70.0 if 40.0 <= rsi < 50.0 else
        60.0 if 65.0 < rsi <= 70.0 else
        35.0 if 30.0 <= rsi < 40.0 else
        25.0 if 70.0 < rsi <= 75.0 else
        0.0
    )
    histogram_rising = (
        indicators.macd_histogram
        > indicators.macd_histogram_previous_1
        > indicators.macd_histogram_previous_2
    )
    macd_histogram_score = (
        100.0 if indicators.macd_histogram > 0 and histogram_rising else
        70.0 if indicators.macd_histogram > 0 else
        40.0 if histogram_rising else
        0.0
    )
    volume_confirmation_score = (
        100.0
        if indicators.return_5d > 0 and indicators.volume_ratio_5_20 >= 1.2
        else 70.0
        if indicators.return_5d > 0 and indicators.volume_ratio_5_20 >= 0.8
        else 40.0
        if indicators.return_5d > 0
        else 0.0
    )
    momentum = (
        0.4 * rsi14_score
        + 0.4 * macd_histogram_score
        + 0.2 * volume_confirmation_score
    )

    atr_ratio = indicators.atr14_close
    atr_close_score = (
        100.0 if 0.015 <= atr_ratio <= 0.04 else
        70.0 if 0.01 <= atr_ratio < 0.015 else
        60.0 if 0.04 < atr_ratio <= 0.06 else
        40.0 if atr_ratio < 0.01 else
        30.0 if atr_ratio <= 0.08 else
        0.0
    )
    volatility = indicators.volatility_20d
    volatility_score = (
        100.0 if 0.20 <= volatility <= 0.50 else
        70.0 if 0.15 <= volatility < 0.20 or 0.50 < volatility <= 0.60 else
        40.0 if 0.10 <= volatility < 0.15 or 0.60 < volatility <= 0.70 else
        0.0
    )
    drawdown = indicators.max_drawdown_60d
    max_drawdown_score = (
        100.0 if drawdown <= 0.10 else
        70.0 if drawdown <= 0.20 else
        30.0 if drawdown <= 0.30 else
        0.0
    )
    risk = (
        0.4 * atr_close_score
        + 0.3 * volatility_score
        + 0.3 * max_drawdown_score
    )
    total = (
        0.20 * liquidity
        + 0.30 * trend
        + 0.20 * relative_strength
        + 0.15 * momentum
        + 0.15 * risk
    )
    return QuantScore(
        score_version=SCORE_VERSION,
        total=total,
        liquidity=liquidity,
        trend=trend,
        relative_strength=relative_strength,
        momentum=momentum,
        risk=risk,
        turnover_score=turnover_score,
        moving_average_score=moving_average_score,
        ma20_slope_score=ma20_slope_score,
        rs20_score=rs20_score,
        rs60_score=rs60_score,
        rsi14_score=rsi14_score,
        macd_histogram_score=macd_histogram_score,
        volume_confirmation_score=volume_confirmation_score,
        atr_close_score=atr_close_score,
        volatility_score=volatility_score,
        max_drawdown_score=max_drawdown_score,
    )
