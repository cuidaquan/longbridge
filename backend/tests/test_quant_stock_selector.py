from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import math
from statistics import median
import unittest

import duckdb
import numpy as np

from app.db import _run_migrations
from app.quant_stock_selector import (
    MIN_DAILY_BARS,
    QuantIndicators,
    QuantInputError,
    SCORE_VERSION,
    _bar_date,
    _macd,
    _wilder_atr,
    _wilder_rsi,
    calculate_quant_indicators,
    score_quantitative,
)
from app.quant_stock_selector_hashing import (
    CanonicalInputError,
    canonical_json,
    canonical_sha256,
    immutable_snapshot_reference,
)
from app.quant_stock_selector_snapshots import (
    MARKET_BAR_SNAPSHOT_VERSION,
    MarketBarSnapshotError,
    MarketBarSnapshotIntegrityError,
    MarketBarSnapshotStore,
)


class _ConnectionContext:
    def __init__(self, connection) -> None:
        self.connection = connection

    def __enter__(self):
        return self.connection

    def __exit__(self, exc_type, exc, traceback) -> bool:
        return False


def _dates(count: int) -> list[datetime]:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return [start + timedelta(days=index) for index in range(count)]


def _adjusted_bars(count: int = 90) -> list[dict]:
    rows = []
    for index, timestamp in enumerate(_dates(count)):
        close = 80.0 + index * 0.45 + math.sin(index / 3.0) * 1.8
        rows.append({
            "ts": timestamp,
            "open": close - 0.4,
            "high": close + 1.1,
            "low": close - 1.2,
            "close": close,
            "volume": 1_000_000 + index * 10_000,
            "turnover": close * (1_000_000 + index * 10_000),
        })
    return rows


def _raw_bars(adjusted: list[dict]) -> list[dict]:
    rows = []
    for index, bar in enumerate(adjusted):
        raw_close = float(bar["close"]) * (0.5 if index < 45 else 1.0)
        volume = 300_000.0 + index * 7_000.0
        rows.append({
            "ts": bar["ts"],
            "open": raw_close - 0.2,
            "high": raw_close + 0.7,
            "low": raw_close - 0.8,
            "close": raw_close,
            "volume": volume,
            "turnover": raw_close * volume,
        })
    return rows


def _spy_bars(count: int = 90) -> list[dict]:
    rows = []
    for index, timestamp in enumerate(_dates(count)):
        close = 100.0 + index * 0.2 + math.cos(index / 5.0) * 0.7
        rows.append({
            "ts": timestamp,
            "open": close - 0.3,
            "high": close + 0.8,
            "low": close - 0.9,
            "close": close,
        })
    return rows


class CanonicalHashTests(unittest.TestCase):
    def test_rfc8785_hash_is_order_and_timezone_stable(self) -> None:
        first = {
            "symbol": "AAA.US",
            "captured": datetime(
                2026, 7, 25, 9, 30, tzinfo=timezone.utc
            ),
            "nested": {"b": 2.0, "a": "中文"},
        }
        second = {
            "nested": {"a": "中文", "b": 2},
            "captured": datetime.fromisoformat(
                "2026-07-25T18:30:00+09:00"
            ),
            "symbol": "AAA.US",
        }

        self.assertEqual(canonical_sha256(first), canonical_sha256(second))
        self.assertEqual(
            canonical_json({"b": 2.0, "a": 1}),
            '{"a":1,"b":2}',
        )

    def test_ordered_arrays_are_semantic_and_invalid_values_fail(self) -> None:
        self.assertNotEqual(
            canonical_sha256({"symbols": ["AAA.US", "BBB.US"]}),
            canonical_sha256({"symbols": ["BBB.US", "AAA.US"]}),
        )
        for value in (
            {"number": float("nan")},
            {"number": float("inf")},
            {"timestamp": datetime(2026, 7, 25)},
            {"unordered": {"AAA.US", "BBB.US"}},
        ):
            with self.subTest(value=value):
                with self.assertRaises(CanonicalInputError):
                    canonical_sha256(value)

    def test_snapshot_reference_excludes_location_identity(self) -> None:
        payload_hash = "a" * 64
        reference = immutable_snapshot_reference(
            source="longbridge",
            schema_version="bars-v1",
            payload_hash=payload_hash.upper(),
        )
        self.assertEqual(reference, {
            "payload_hash": payload_hash,
            "schema_version": "bars-v1",
            "source": "longbridge",
        })
        self.assertNotIn("snapshot_id", reference)
        self.assertNotIn("uri", reference)
        with self.assertRaises(CanonicalInputError):
            immutable_snapshot_reference(
                source="longbridge",
                schema_version="bars-v1",
                payload_hash="not-a-hash",
            )


class IndicatorCalculationTests(unittest.TestCase):
    def test_wilder_rsi_handles_reference_and_flat_sequences(self) -> None:
        reference = [
            44.34, 44.09, 44.15, 43.61, 44.33,
            44.83, 45.10, 45.42, 45.84, 46.08,
            45.89, 46.03, 45.61, 46.28, 46.28,
        ]
        self.assertAlmostEqual(_wilder_rsi(reference), 70.4641, places=3)
        self.assertEqual(_wilder_rsi([100.0] * 15), 50.0)
        self.assertEqual(_wilder_rsi(list(range(1, 16))), 100.0)

    def test_macd_uses_sma_seed_and_atr_uses_wilder_smoothing(self) -> None:
        line, signal, histogram = _macd([float(value) for value in range(1, 41)])
        self.assertAlmostEqual(line, 7.0, places=12)
        self.assertAlmostEqual(signal, 7.0, places=12)
        self.assertTrue(all(abs(value) < 1e-12 for value in histogram))

        bars = []
        for index in range(16):
            high = 105.0 if index == 15 else 101.0
            low = 100.0 if index == 15 else 99.0
            bars.append({"high": high, "low": low, "close": 100.0})
        self.assertAlmostEqual(
            _wilder_atr(bars),
            (2.0 * 13.0 + 5.0) / 14.0,
            places=12,
        )

    def test_all_indicators_match_independent_window_formulas(self) -> None:
        adjusted = _adjusted_bars()
        raw = _raw_bars(adjusted)
        spy = _spy_bars()
        result = calculate_quant_indicators(adjusted, raw, spy)

        closes = np.asarray([row["close"] for row in adjusted], dtype=np.float64)
        spy_closes = np.asarray([row["close"] for row in spy], dtype=np.float64)
        volumes = np.asarray([row["volume"] for row in raw[-20:]], dtype=np.float64)
        turnovers = [row["turnover"] for row in raw[-20:]]
        expected_returns = np.diff(closes[-21:]) / closes[-21:-1]

        self.assertEqual(result.data_as_of, adjusted[-1]["ts"].date().isoformat())
        self.assertAlmostEqual(result.ma20, float(np.mean(closes[-20:])), places=12)
        self.assertAlmostEqual(result.ma60, float(np.mean(closes[-60:])), places=12)
        self.assertAlmostEqual(
            result.ma20_slope_10d,
            float(np.mean(closes[-20:]) / np.mean(closes[-30:-10]) - 1.0),
            places=12,
        )
        self.assertAlmostEqual(
            result.rs20,
            (closes[-1] / closes[-21] - 1.0)
            - (spy_closes[-1] / spy_closes[-21] - 1.0),
            places=12,
        )
        self.assertAlmostEqual(
            result.rs60,
            (closes[-1] / closes[-61] - 1.0)
            - (spy_closes[-1] / spy_closes[-61] - 1.0),
            places=12,
        )
        self.assertAlmostEqual(
            result.volatility_20d,
            float(np.std(expected_returns, ddof=1) * math.sqrt(252.0)),
            places=12,
        )
        self.assertAlmostEqual(result.return_5d, closes[-1] / closes[-6] - 1.0)
        self.assertAlmostEqual(result.volume_ratio_5_20, np.mean(volumes[-5:]) / np.mean(volumes))
        self.assertAlmostEqual(result.median_turnover_20d, median(turnovers))

        peak = closes[-60]
        expected_drawdown = 0.0
        for close in closes[-60:]:
            peak = max(peak, close)
            expected_drawdown = max(expected_drawdown, 1.0 - close / peak)
        self.assertAlmostEqual(result.max_drawdown_60d, expected_drawdown)

    def test_timestamp_strings_are_aligned_in_utc(self) -> None:
        self.assertEqual(
            _bar_date("2026-07-25T00:30:00+09:00"),
            _bar_date("2026-07-24T15:30:00Z"),
        )

    def test_raw_turnover_fallback_never_uses_adjusted_close(self) -> None:
        adjusted = _adjusted_bars()
        raw = _raw_bars(adjusted)
        for index, row in enumerate(raw):
            if index >= len(raw) - 20:
                row["close"] *= 0.5
            row["turnover"] = None
        result = calculate_quant_indicators(adjusted, raw, _spy_bars())
        expected = median(
            row["close"] * row["volume"] for row in raw[-20:]
        )
        adjusted_wrong = median(
            adjusted[index]["close"] * raw[index]["volume"]
            for index in range(len(raw) - 20, len(raw))
        )
        self.assertAlmostEqual(result.median_turnover_20d, expected)
        self.assertNotEqual(result.median_turnover_20d, adjusted_wrong)

    def test_missing_and_misaligned_inputs_raise_specific_exclusions(self) -> None:
        adjusted = _adjusted_bars()
        raw = _raw_bars(adjusted)
        spy = _spy_bars()
        cases = [
            (
                adjusted[: MIN_DAILY_BARS - 1],
                raw,
                spy,
                "insufficient_history",
            ),
            (
                adjusted,
                raw[:-1],
                spy,
                "raw_bar_alignment_missing",
            ),
            (
                adjusted,
                raw,
                spy[:-1],
                "benchmark_date_mismatch",
            ),
        ]
        for candidate, raw_rows, benchmark, reason in cases:
            with self.subTest(reason=reason):
                with self.assertRaises(QuantInputError) as raised:
                    calculate_quant_indicators(candidate, raw_rows, benchmark)
                self.assertEqual(raised.exception.reason, reason)


class QuantScoreTests(unittest.TestCase):
    @staticmethod
    def _indicators(**overrides) -> QuantIndicators:
        values = {
            "data_as_of": "2026-07-24",
            "close": 120.0,
            "median_turnover_20d": 10_000_000.0,
            "ma20": 110.0,
            "ma60": 100.0,
            "ma20_slope_10d": 0.04,
            "rs20": 0.10,
            "rs60": 0.15,
            "rsi14": 65.0,
            "macd_line": 3.0,
            "macd_signal": 2.0,
            "macd_histogram": 1.0,
            "macd_histogram_previous_1": 0.5,
            "macd_histogram_previous_2": 0.25,
            "atr14": 4.8,
            "atr14_close": 0.04,
            "volatility_20d": 0.50,
            "max_drawdown_60d": 0.10,
            "return_5d": 0.01,
            "volume_ratio_5_20": 1.2,
        }
        values.update(overrides)
        return QuantIndicators(**values)

    def test_exact_boundaries_and_weighted_total(self) -> None:
        score = score_quantitative(self._indicators())
        self.assertEqual(score.score_version, SCORE_VERSION)
        self.assertEqual(score.turnover_score, 50.0)
        self.assertEqual(score.liquidity, 50.0)
        self.assertEqual(score.trend, 100.0)
        self.assertEqual(score.relative_strength, 100.0)
        self.assertEqual(score.momentum, 100.0)
        self.assertEqual(score.risk, 100.0)
        self.assertEqual(score.total, 90.0)

    def test_thresholds_use_unrounded_values_and_hard_fail(self) -> None:
        with self.assertRaises(QuantInputError) as turnover_error:
            score_quantitative(
                self._indicators(median_turnover_20d=9_999_999.999999),
            )
        self.assertEqual(turnover_error.exception.reason, "h5_min_turnover")

    def test_macd_rising_requires_three_strictly_increasing_values(self) -> None:
        tied = self._indicators(
            macd_histogram=1.0,
            macd_histogram_previous_1=0.5,
            macd_histogram_previous_2=0.5,
        )
        self.assertEqual(
            score_quantitative(tied).macd_histogram_score,
            70.0,
        )

    def test_nonfinite_or_impossible_indicators_are_rejected(self) -> None:
        cases = (
            self._indicators(rs20=float("nan")),
            self._indicators(volatility_20d=-0.01),
            self._indicators(max_drawdown_60d=1.01),
            self._indicators(rsi14=101.0),
        )
        for indicators in cases:
            with self.subTest(indicators=indicators):
                with self.assertRaises(QuantInputError) as raised:
                    score_quantitative(indicators)
                self.assertEqual(raised.exception.reason, "invalid_indicator")


class MarketBarSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = duckdb.connect(":memory:")
        self.addCleanup(self.connection.close)
        _run_migrations(self.connection)
        self.factory = lambda: _ConnectionContext(self.connection)
        self.store = MarketBarSnapshotStore(
            connection_factory=self.factory,
            clock=lambda: datetime(2026, 7, 25, 1, tzinfo=timezone.utc),
        )

    @staticmethod
    def _rows(multiplier: float = 1.0) -> list[dict]:
        return [
            {
                "ts": f"2026-07-{day:02d}T00:00:00Z",
                "open": (100.0 + day) * multiplier,
                "high": (102.0 + day) * multiplier,
                "low": (99.0 + day) * multiplier,
                "close": (101.0 + day) * multiplier,
                "volume": 1_000_000.0 + day,
                "turnover": (101.0 + day) * multiplier * (1_000_000.0 + day),
            }
            for day in (22, 23, 24)
        ]

    def test_migration_is_idempotent_and_keeps_adjustment_dimension(self) -> None:
        _run_migrations(self.connection)
        snapshot_columns = {
            row[1]
            for row in self.connection.execute(
                "PRAGMA table_info('market_bar_snapshots')"
            ).fetchall()
        }
        row_columns = {
            row[1]
            for row in self.connection.execute(
                "PRAGMA table_info('market_bar_snapshot_rows')"
            ).fetchall()
        }
        self.assertEqual(snapshot_columns, {
            "snapshot_id", "symbol", "period", "adjust_type", "source",
            "captured_at", "data_as_of", "snapshot_version", "payload_hash",
            "row_count",
        })
        self.assertEqual(row_columns, {
            "snapshot_id", "ts", "open", "high", "low", "close",
            "volume", "turnover",
        })

    def test_forward_and_raw_snapshots_coexist_and_are_idempotent(self) -> None:
        forward_rows = self._rows(0.5)
        raw_rows = self._rows(1.0)
        forward = self.store.capture(
            symbol=" aaa.us ",
            period="DAY",
            adjust_type="forward_adjust",
            source="longbridge",
            data_as_of="2026-07-24",
            rows=list(reversed(forward_rows)),
        )
        raw = self.store.capture(
            symbol="AAA.US",
            period="day",
            adjust_type="no_adjust",
            source="longbridge",
            data_as_of="2026-07-24",
            rows=raw_rows,
        )
        repeated = self.store.capture(
            symbol="AAA.US",
            period="day",
            adjust_type="forward_adjust",
            source="longbridge",
            data_as_of="2026-07-24",
            rows=forward_rows,
        )

        self.assertNotEqual(forward["snapshot_id"], raw["snapshot_id"])
        self.assertEqual(forward["snapshot_id"], repeated["snapshot_id"])
        self.assertTrue(forward["persisted"])
        self.assertTrue(raw["persisted"])
        self.assertFalse(repeated["persisted"])
        self.assertEqual(forward["symbol"], "AAA.US")
        self.assertEqual(forward["snapshot_version"], MARKET_BAR_SNAPSHOT_VERSION)
        self.assertEqual(forward["row_count"], 3)
        self.assertTrue(forward["integrity_valid"])
        self.assertNotIn("snapshot_id", forward["reference"])
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM market_bar_snapshots"
            ).fetchone()[0],
            2,
        )

    def test_content_change_creates_new_snapshot_and_tampering_is_detected(self) -> None:
        first = self.store.capture(
            symbol="AAA.US",
            period="day",
            adjust_type="no_adjust",
            source="longbridge",
            data_as_of="2026-07-24",
            rows=self._rows(),
        )
        changed_rows = self._rows()
        changed_rows[-1] = dict(changed_rows[-1])
        changed_rows[-1]["close"] += 0.5
        changed_rows[-1]["high"] += 0.5
        second = self.store.capture(
            symbol="AAA.US",
            period="day",
            adjust_type="no_adjust",
            source="longbridge",
            data_as_of="2026-07-24",
            rows=changed_rows,
        )
        self.assertNotEqual(first["snapshot_id"], second["snapshot_id"])

        self.connection.execute(
            "UPDATE market_bar_snapshot_rows SET close = close + 1 "
            "WHERE snapshot_id = ? AND ts = ("
            "SELECT MAX(ts) FROM market_bar_snapshot_rows "
            "WHERE snapshot_id = ?)",
            [first["snapshot_id"], first["snapshot_id"]],
        )
        with self.assertRaises(MarketBarSnapshotIntegrityError):
            self.store.get(first["snapshot_id"])

    def test_invalid_snapshot_inputs_fail_before_persistence(self) -> None:
        cases = (
            {"adjust_type": "backward_adjust"},
            {"data_as_of": "2026-07-23"},
            {"rows": self._rows() + [self._rows()[-1]]},
        )
        base = {
            "symbol": "AAA.US",
            "period": "day",
            "adjust_type": "no_adjust",
            "source": "longbridge",
            "data_as_of": "2026-07-24",
            "rows": self._rows(),
        }
        for override in cases:
            with self.subTest(override=override):
                with self.assertRaises(MarketBarSnapshotError):
                    self.store.capture(**{**base, **override})
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM market_bar_snapshots"
            ).fetchone()[0],
            0,
        )


if __name__ == "__main__":
    unittest.main()
