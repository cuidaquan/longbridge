from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime
import unittest
from zoneinfo import ZoneInfo

from app.quant_stock_selector import QuantIndicators
from app.quant_stock_selector_universe import (
    FILTER_VERSION,
    CandidateMarketData,
    QuantUniverseCandidate,
    QuantUniverseSelector,
    SelectionPolicy,
    UniverseSelectionError,
    build_unified_candidate_pool,
    evaluate_hard_filters,
)


DATA_AS_OF = date(2026, 7, 24)
OFFICIAL_CLOSE = datetime(
    2026,
    7,
    24,
    16,
    tzinfo=ZoneInfo("America/New_York"),
)


def _indicators(*, turnover: float = 100_000_000.0) -> QuantIndicators:
    return QuantIndicators(
        data_as_of=DATA_AS_OF.isoformat(),
        close=120.0,
        median_turnover_20d=turnover,
        ma20=110.0,
        ma60=100.0,
        ma20_slope_10d=0.05,
        rs20=0.12,
        rs60=0.18,
        rsi14=60.0,
        macd_line=3.0,
        macd_signal=2.0,
        macd_histogram=1.0,
        macd_histogram_previous_1=0.5,
        macd_histogram_previous_2=0.25,
        atr14=3.0,
        atr14_close=0.025,
        volatility_20d=0.35,
        max_drawdown_60d=0.08,
        return_5d=0.03,
        volume_ratio_5_20=1.3,
    )


def _candidate(symbol: str, **overrides) -> QuantUniverseCandidate:
    values = {
        "symbol": symbol,
        "name": symbol,
        "market": "US",
        "board": "USMain",
        "exchange": "NASDAQ",
        "catalog_source": "longbridge",
        "catalog_version": "2026-07-24",
        "catalog_captured_at": "2026-07-25T00:05:00Z",
        "trade_status": "normal",
        "last_price": 120.0,
        "price_data_as_of": DATA_AS_OF,
        "bar_data_as_of": DATA_AS_OF,
        "valid_daily_bars": 90,
        "indicators": _indicators(),
        "indicator_error": None,
        "current_turnover": 100_000_000.0,
        "total_market_value": 10_000_000_000.0,
        "volume_ratio": 1.2,
        "ten_day_change_rate": 0.10,
        "ten_day_relative_strength": 0.05,
    }
    values.update(overrides)
    return QuantUniverseCandidate(**values)


class CandidatePoolTests(unittest.TestCase):
    def test_pool_preserves_catalog_and_freezes_catalog_evidence(self) -> None:
        facts = CandidateMarketData(
            trade_status="normal",
            last_price=120.0,
            price_data_as_of=DATA_AS_OF,
            bar_data_as_of=DATA_AS_OF,
            valid_daily_bars=90,
            indicators=_indicators(),
        )
        candidates = build_unified_candidate_pool(
            [
                {
                    "symbol": "BND.US",
                    "name": "Bond ETF",
                    "market": "US",
                    "board": "usmain",
                    "exchange": "NASDAQ",
                },
                {
                    "symbol": "AAA.US",
                    "name": "Alpha",
                    "market": "US",
                    "board": "usmain",
                    "exchange": "NYSE",
                },
            ],
            market_data={"BND.US": facts},
            catalog_source="longbridge",
            catalog_version="catalog-20260724",
            catalog_captured_at="2026-07-25T00:05:00Z",
        )
        self.assertEqual([item.symbol for item in candidates], ["BND.US", "AAA.US"])
        self.assertEqual(candidates[0].catalog_evidence()["board"], "USMAIN")
        self.assertEqual(candidates[0].catalog_evidence()["source"], "longbridge")
        self.assertEqual(candidates[1].indicator_error, "market_data_missing")

    def test_pool_rejects_duplicate_symbols_and_missing_evidence_version(self) -> None:
        duplicate = [
            {"symbol": "AAA.US"},
            {"symbol": "aaa.us"},
        ]
        with self.assertRaisesRegex(UniverseSelectionError, "duplicate"):
            build_unified_candidate_pool(
                duplicate,
                market_data={},
                catalog_source="longbridge",
                catalog_version="v1",
                catalog_captured_at="2026-07-25T00:05:00Z",
            )
        with self.assertRaisesRegex(UniverseSelectionError, "catalog_version"):
            build_unified_candidate_pool(
                [{"symbol": "AAA.US"}],
                market_data={},
                catalog_source="longbridge",
                catalog_version="",
                catalog_captured_at="2026-07-25T00:05:00Z",
            )


class HardFilterTests(unittest.TestCase):
    def test_longbridge_nasd_exchange_alias_is_eligible(self) -> None:
        candidate = _candidate("AAA.US", exchange="NASD")
        filters, reasons = evaluate_hard_filters(
            candidate,
            data_as_of=DATA_AS_OF,
        )
        self.assertEqual(filters["H2"], {"status": "pass", "reason": None})
        self.assertEqual(candidate.catalog_evidence()["exchange"], "NASDAQ")
        self.assertEqual(reasons, [])

    def test_product_names_and_structures_do_not_change_eligibility(self) -> None:
        symbols = ("COMMON.US", "BOND.US", "SQQQ.US", "LEV3X.US", "WARRANT.US")
        for symbol in symbols:
            with self.subTest(symbol=symbol):
                filters, reasons = evaluate_hard_filters(
                    _candidate(symbol),
                    data_as_of=DATA_AS_OF,
                )
                self.assertEqual(reasons, [])
                self.assertTrue(all(item["status"] == "pass" for item in filters.values()))

    def test_each_hard_filter_has_a_stable_reason(self) -> None:
        cases = (
            ({"market": "HK"}, "H1", "market_or_catalog_not_usmain"),
            ({"board": "USOTC"}, "H1", "market_or_catalog_not_usmain"),
            ({"exchange": "OTC"}, "H2", "ineligible_exchange"),
            ({"trade_status": "halted"}, "H3", "trade_status_not_normal"),
            ({"last_price": 4.99}, "H4", "price_outside_5_to_500_or_missing"),
            ({"last_price": 500.01}, "H4", "price_outside_5_to_500_or_missing"),
            (
                {"indicators": _indicators(turnover=9_999_999)},
                "H5",
                "median_turnover_below_minimum",
            ),
            ({"valid_daily_bars": 84}, "H6", "daily_bar_history_incomplete"),
            ({"bar_data_as_of": date(2026, 7, 23)}, "H7", "data_as_of_mismatch"),
            ({"symbol": "SPY.US"}, "H8", "benchmark_symbol"),
            (
                {"current_turnover": 9_999_999},
                "H9",
                "current_turnover_below_minimum_or_missing",
            ),
            (
                {"total_market_value": 999_999_999},
                "H10",
                "market_value_below_minimum_or_missing",
            ),
            (
                {"volume_ratio": 0.79},
                "H11",
                "volume_ratio_below_minimum_or_missing",
            ),
            (
                {"ten_day_relative_strength": -0.01},
                "H12",
                "ten_day_relative_strength_below_spy_or_missing",
            ),
        )
        for override, code, reason in cases:
            with self.subTest(code=code, override=override):
                symbol = override.pop("symbol", "AAA.US") if "symbol" in override else "AAA.US"
                filters, reasons = evaluate_hard_filters(
                    _candidate(symbol, **override),
                    data_as_of=DATA_AS_OF,
                )
                self.assertEqual(filters[code], {"status": "fail", "reason": reason})
                self.assertIn(reason, reasons)


class QuantUniverseSelectionTests(unittest.TestCase):
    def test_all_candidates_are_scored_and_stable_top_30_does_not_expand_ties(self) -> None:
        candidates = [_candidate(f"S{index:03d}.US") for index in range(32)]
        result = QuantUniverseSelector().select(
            candidates,
            data_as_of=DATA_AS_OF,
            official_close=OFFICIAL_CLOSE,
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["filter_version"], FILTER_VERSION)
        self.assertEqual(result["ai_candidate_symbols"], [
            f"S{index:03d}.US" for index in range(30)
        ])
        self.assertEqual(len(result["quant_ranking"]), 32)
        records = {item["symbol"]: item for item in result["candidates"]}
        self.assertIsNotNone(records["S031.US"]["quant_score"])
        self.assertFalse(records["S031.US"]["selected_for_ai"])
        self.assertNotIn("nbbo", records["S000.US"])
        self.assertNotIn("q_upper_bound", records["S000.US"])
        manifest = result["selection_manifest"]
        self.assertEqual(
            manifest["candidate_set_method"],
            "deterministic-full-score-v1.3",
        )
        self.assertEqual(len(result["selection_manifest_hash"]), 64)

    def test_below_threshold_is_not_padded_into_ai_set(self) -> None:
        low = _candidate(
            "LOW.US",
            indicators=replace(
                _indicators(turnover=10_000_000),
                close=80.0,
                ma20=100.0,
                ma60=110.0,
                ma20_slope_10d=-0.03,
                rs20=-0.10,
                rs60=-0.12,
                rsi14=20.0,
                macd_histogram=-1.0,
                macd_histogram_previous_1=-0.5,
                macd_histogram_previous_2=-0.25,
                atr14_close=0.10,
                volatility_20d=0.80,
                max_drawdown_60d=0.40,
                return_5d=-0.03,
                volume_ratio_5_20=1.0,
            ),
        )
        result = QuantUniverseSelector().select(
            [_candidate("AAA.US"), low],
            data_as_of=DATA_AS_OF,
            official_close=OFFICIAL_CLOSE,
        )
        self.assertEqual(result["ai_candidate_symbols"], ["AAA.US"])
        record = {item["symbol"]: item for item in result["candidates"]}["LOW.US"]
        self.assertEqual(record["selection_status"], "quant_score_below_threshold")

    def test_catalog_evidence_changes_candidate_hash(self) -> None:
        first = QuantUniverseSelector().select(
            [_candidate("AAA.US")],
            data_as_of=DATA_AS_OF,
            official_close=OFFICIAL_CLOSE,
        )["candidates"][0]
        second = QuantUniverseSelector().select(
            [_candidate("AAA.US", catalog_version="next")],
            data_as_of=DATA_AS_OF,
            official_close=OFFICIAL_CLOSE,
        )["candidates"][0]
        self.assertNotEqual(
            first["candidate_quant_input_hash"],
            second["candidate_quant_input_hash"],
        )

    def test_policy_and_run_time_contracts_are_validated(self) -> None:
        with self.assertRaises(UniverseSelectionError):
            SelectionPolicy(q_threshold=101)
        with self.assertRaises(UniverseSelectionError):
            SelectionPolicy(top_n=0)
        with self.assertRaisesRegex(UniverseSelectionError, "timezone-aware"):
            QuantUniverseSelector().select(
                [_candidate("AAA.US")],
                data_as_of=DATA_AS_OF,
                official_close=datetime(2026, 7, 24, 16),
            )
        with self.assertRaisesRegex(UniverseSelectionError, "candidate symbols"):
            QuantUniverseSelector().select(
                [_candidate("AAA.US"), _candidate("aaa.us")],
                data_as_of=DATA_AS_OF,
                official_close=OFFICIAL_CLOSE,
            )


if __name__ == "__main__":
    unittest.main()
