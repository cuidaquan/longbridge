from __future__ import annotations

from datetime import date, datetime, timezone
from contextlib import contextmanager
from decimal import Decimal
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app.exceptions import LongbridgeAPIError
from app.quant_stock_selector_longbridge import (
    LongbridgeQuantSourceBundleCollector,
    load_longbridge_daily_bars,
    load_longbridge_latest_completed_us_session,
)


CAPTURED_AT = datetime(2026, 7, 24, 20, 5, tzinfo=timezone.utc)
DATA_AS_OF = date(2026, 7, 24)


def _calendar(*, now):
    return {
        "source": "Longbridge QuoteContext.trading_days",
        "source_version": "test-calendar-v1",
        "license": "test",
        "historical_semantics": "point_in_time",
        "market_calendar": "XNYS",
        "session_date": DATA_AS_OF.isoformat(),
        "official_open": "2026-07-24T13:30:00Z",
        "official_close": "2026-07-24T20:00:00Z",
        "session_status": "completed",
        "is_latest_completed_session": True,
        "half_trade_day": False,
        "captured_at": now.isoformat().replace("+00:00", "Z"),
    }


def _bars(symbols, *, data_as_of):
    assert data_as_of == DATA_AS_OF
    row = {
        "ts": "2026-07-24T00:00:00Z",
        "open": 100.0,
        "high": 101.0,
        "low": 99.0,
        "close": 100.0,
        "volume": 1_000_000,
        "turnover": 100_000_000,
    }
    return {
        symbol: {
            "forward_adjusted_bars": [dict(row)],
            "unadjusted_bars": [dict(row)],
            "error": None,
        }
        for symbol in symbols
    }


def _calc_indexes(symbols):
    return {
        symbol: {
            "turnover": 100_000_000.0,
            "total_market_value": 10_000_000_000.0,
            "volume_ratio": 1.2,
            "ten_day_change_rate": 0.05 if symbol == "SPY.US" else 0.10,
        }
        for symbol in symbols
    }
class LongbridgeQuantSourceBundleCollectorTests(unittest.TestCase):
    def _collector(
        self,
        *,
        catalog=None,
        static_info_loader=None,
        tradeability_loader=None,
        calc_index_loader=_calc_indexes,
        bar_loader=_bars,
        batch_size=500,
    ):
        catalog = catalog or [
            {"symbol": "AAA.US", "name": "Ordinary stock"},
            {"symbol": "BND.US", "name": "Bond ETF"},
            {"symbol": "INV.US", "name": "Inverse leveraged ETF -3x"},
            {"symbol": "OTC.US", "name": "OTC security"},
        ]

        if static_info_loader is None:
            def static_info_loader(symbols):
                return [
                    {
                        "symbol": symbol,
                        "board": "USPink" if symbol == "OTC.US" else "USMain",
                        "exchange": "NASD",
                    }
                    for symbol in symbols
                ]

        if tradeability_loader is None:
            def tradeability_loader(symbols):
                return {
                    symbol: {
                        "trade_status": "normal",
                        "last_done": 100.0,
                    }
                    for symbol in symbols
                }

        return LongbridgeQuantSourceBundleCollector(
            catalog_loader=lambda: catalog,
            static_info_loader=static_info_loader,
            tradeability_loader=tradeability_loader,
            calc_index_loader=calc_index_loader,
            calendar_loader=_calendar,
            bar_loader=bar_loader,
            clock=lambda: CAPTURED_AT,
            batch_size=batch_size,
        )

    def test_collects_all_usmain_product_names_without_classification(self):
        requested = []

        def bar_loader(symbols, *, data_as_of):
            requested.extend(symbols)
            return _bars(symbols, data_as_of=data_as_of)

        bundle = self._collector(bar_loader=bar_loader).capture()

        self.assertEqual(
            requested,
            ["SPY.US", "AAA.US", "BND.US", "INV.US"],
        )
        self.assertNotIn("OTC.US", bundle["market_data"])
        self.assertEqual(bundle["source_capture"]["status"], "complete")
        self.assertNotIn("product_metadata", bundle)
        self.assertNotIn("nbbo", bundle)
        self.assertNotIn("depth", bundle)
        self.assertEqual(
            {item["exchange"] for item in bundle["catalog"]},
            {"NASDAQ"},
        )

    def test_static_and_quote_calls_are_batched(self):
        catalog = [
            {"symbol": f"S{index:03d}.US", "name": str(index)}
            for index in range(5)
        ]
        static_batches = []
        quote_batches = []

        def static_info_loader(symbols):
            static_batches.append(list(symbols))
            return [
                {"symbol": symbol, "board": "USMain", "exchange": "NASD"}
                for symbol in symbols
            ]

        def tradeability_loader(symbols):
            quote_batches.append(list(symbols))
            return {
                symbol: {"trade_status": "normal", "last_done": 100}
                for symbol in symbols
            }

        self._collector(
            catalog=catalog,
            static_info_loader=static_info_loader,
            tradeability_loader=tradeability_loader,
            batch_size=2,
        ).capture()

        self.assertEqual([len(item) for item in static_batches], [2, 2, 2])
        self.assertEqual([len(item) for item in quote_batches], [2, 2, 2])
        self.assertTrue(all(len(item) <= 2 for item in static_batches))
        self.assertTrue(all(len(item) <= 2 for item in quote_batches))

    def test_live_capture_reuses_one_quote_context_across_all_batches(self):
        catalog = [
            {"symbol": f"S{index:03d}.US", "name": str(index)}
            for index in range(5)
        ]
        quote_context_instance = SimpleNamespace()
        context_entries = []
        static_calls = []
        quote_calls = []
        calc_calls = []
        bar_calls = []

        @contextmanager
        def quote_context(credentials):
            context_entries.append(credentials)
            yield quote_context_instance

        def calendar_loader(active_context, *, captured_at):
            self.assertIs(active_context, quote_context_instance)
            return _calendar(now=captured_at)

        def static_loader(symbols, *, context):
            self.assertIs(context, quote_context_instance)
            static_calls.append(list(symbols))
            return [
                {"symbol": symbol, "board": "USMain", "exchange": "NASD"}
                for symbol in symbols
            ]

        def quote_loader(symbols, *, context):
            self.assertIs(context, quote_context_instance)
            quote_calls.append(list(symbols))
            return {
                symbol: {"trade_status": "normal", "last_done": 100.0}
                for symbol in symbols
            }

        def bar_loader(active_context, symbols, *, data_as_of):
            self.assertIs(active_context, quote_context_instance)
            bar_calls.append(list(symbols))
            return _bars(symbols, data_as_of=data_as_of)

        def calc_loader(active_context, symbols):
            self.assertIs(active_context, quote_context_instance)
            calc_calls.append(list(symbols))
            return _calc_indexes(symbols)

        collector = LongbridgeQuantSourceBundleCollector(
            catalog_loader=lambda: catalog,
            clock=lambda: CAPTURED_AT,
            batch_size=2,
            isolate_live_capture=False,
        )
        with (
            patch(
                "app.quant_stock_selector_longbridge._quote_context",
                quote_context,
            ),
            patch(
                "app.quant_stock_selector_longbridge._credentials",
                return_value={"configured": "yes"},
            ),
            patch(
                "app.quant_stock_selector_longbridge."
                "_load_longbridge_latest_completed_us_session_from_context",
                side_effect=calendar_loader,
            ),
            patch(
                "app.quant_stock_selector_longbridge.get_security_static_info",
                side_effect=static_loader,
            ),
            patch(
                "app.quant_stock_selector_longbridge.get_security_tradeability",
                side_effect=quote_loader,
            ),
            patch(
                "app.quant_stock_selector_longbridge."
                "_load_longbridge_quant_prefilter_indexes_from_context",
                side_effect=calc_loader,
            ),
            patch(
                "app.quant_stock_selector_longbridge."
                "_load_longbridge_daily_bars_from_context",
                side_effect=bar_loader,
            ),
        ):
            bundle = collector.capture()

        self.assertEqual(context_entries, [{"configured": "yes"}])
        self.assertEqual([len(batch) for batch in static_calls], [2, 2, 2])
        self.assertEqual([len(batch) for batch in quote_calls], [2, 2, 2])
        self.assertEqual([len(batch) for batch in calc_calls], [2, 2, 2])
        self.assertEqual(bar_calls, [[
            "SPY.US",
            "S000.US",
            "S001.US",
            "S002.US",
            "S003.US",
            "S004.US",
        ]])
        self.assertEqual(bundle["source_capture"]["history_symbol_count"], 6)

    def test_bar_error_marks_capture_partial(self):
        def bar_loader(symbols, *, data_as_of):
            result = dict(_bars(symbols, data_as_of=data_as_of))
            result["AAA.US"] = {
                "forward_adjusted_bars": [],
                "unadjusted_bars": [],
                "error": "quota exceeded",
            }
            return result

        bundle = self._collector(bar_loader=bar_loader).capture()
        self.assertEqual(bundle["source_capture"]["status"], "partial")
        self.assertEqual(
            bundle["source_capture"]["errors"],
            ["bars:AAA.US:quota exceeded"],
        )

    def test_h1_to_h4_prefilter_limits_history_requests(self):
        requested = []

        def tradeability_loader(symbols):
            return {
                symbol: {
                    "trade_status": "halted" if symbol == "BND.US" else "normal",
                    "last_done": 4.99 if symbol == "INV.US" else 100.0,
                }
                for symbol in symbols
            }

        def bar_loader(symbols, *, data_as_of):
            requested.extend(symbols)
            return _bars(symbols, data_as_of=data_as_of)

        bundle = self._collector(
            tradeability_loader=tradeability_loader,
            bar_loader=bar_loader,
        ).capture()

        self.assertEqual(requested, ["SPY.US", "AAA.US"])
        self.assertEqual(bundle["source_capture"]["history_symbol_count"], 2)
        self.assertEqual(bundle["market_data"]["BND.US"]["unadjusted_bars"], [])
        self.assertEqual(bundle["market_data"]["INV.US"]["unadjusted_bars"], [])

    def test_strict_prefilter_sorts_every_match_without_truncation(self):
        catalog = [
            {"symbol": symbol, "name": symbol}
            for symbol in ("AAA.US", "BBB.US", "CCC.US", "DDD.US")
        ]
        requested = []

        def calc_loader(symbols):
            values = {
                "SPY.US": (100_000_000, 10_000_000_000, 1.2, 0.02),
                "AAA.US": (20_000_000, 2_000_000_000, 1.0, 0.03),
                "BBB.US": (50_000_000, 2_000_000_000, 1.0, 0.02),
                "CCC.US": (50_000_000, 2_000_000_000, 1.5, 0.04),
                "DDD.US": (9_999_999, 2_000_000_000, 2.0, 0.10),
            }
            return {
                symbol: {
                    "turnover": values[symbol][0],
                    "total_market_value": values[symbol][1],
                    "volume_ratio": values[symbol][2],
                    "ten_day_change_rate": values[symbol][3],
                }
                for symbol in symbols
            }

        def bar_loader(symbols, *, data_as_of):
            requested.extend(symbols)
            return _bars(symbols, data_as_of=data_as_of)

        bundle = self._collector(
            catalog=catalog,
            calc_index_loader=calc_loader,
            bar_loader=bar_loader,
        ).capture()

        self.assertEqual(
            requested,
            ["SPY.US", "CCC.US", "BBB.US", "AAA.US"],
        )
        self.assertEqual(bundle["source_capture"]["prefiltered_stock_count"], 3)
        self.assertIsNone(
            bundle["source_capture"]["prefilter_policy"]["history_symbol_limit"]
        )
        self.assertEqual(
            bundle["market_data"]["DDD.US"]["prefilter"]["status"],
            "excluded",
        )

    def test_monthly_quota_skip_is_preserved_without_aborting_capture(self):
        def bar_loader(symbols, *, data_as_of):
            result = _bars(symbols, data_as_of=data_as_of)
            result["AAA.US"] = {
                "forward_adjusted_bars": [],
                "unadjusted_bars": [],
                "error": "301607 Permission limit",
                "error_category": "monthly_history_symbol_quota",
            }
            return result

        bundle = self._collector(bar_loader=bar_loader).capture()

        self.assertEqual(bundle["source_capture"]["status"], "complete")
        self.assertEqual(bundle["source_capture"]["history_quota_skipped_count"], 1)
        self.assertEqual(
            bundle["source_capture"]["history_quota_skipped_symbols"],
            ["AAA.US"],
        )
        self.assertEqual(
            bundle["market_data"]["AAA.US"]["bar_error_category"],
            "monthly_history_symbol_quota",
        )

    def test_spy_monthly_quota_continues_all_sorted_history_requests(self):
        requested = []

        def bar_loader(symbols, *, data_as_of):
            requested.extend(symbols)
            result = _bars(symbols, data_as_of=data_as_of)
            result["SPY.US"] = {
                "forward_adjusted_bars": [],
                "unadjusted_bars": [],
                "error": "301607 Permission limit",
                "error_category": "monthly_history_symbol_quota",
            }
            return result

        bundle = self._collector(bar_loader=bar_loader).capture()

        self.assertEqual(requested, ["SPY.US", "AAA.US", "BND.US", "INV.US"])
        self.assertEqual(bundle["source_capture"]["status"], "partial")
        self.assertEqual(
            bundle["source_capture"]["errors"],
            ["benchmark:SPY.US:monthly_history_symbol_quota"],
        )
        self.assertEqual(
            bundle["source_capture"]["history_quota_skipped_symbols"],
            ["SPY.US"],
        )
        self.assertTrue(bundle["market_data"]["AAA.US"]["unadjusted_bars"])

    def test_history_requests_have_no_local_symbol_limit(self):
        catalog = [
            {"symbol": f"S{index:03d}.US", "name": str(index)}
            for index in range(101)
        ]
        requested = []

        def bar_loader(symbols, *, data_as_of):
            requested.extend(symbols)
            return _bars(symbols, data_as_of=data_as_of)

        bundle = self._collector(
            catalog=catalog,
            bar_loader=bar_loader,
        ).capture()

        self.assertEqual(len(requested), 102)
        self.assertEqual(bundle["source_capture"]["history_symbol_count"], 102)
        self.assertNotIn("history_symbol_limit", bundle["source_capture"])

    def test_missing_history_without_provider_error_is_an_exclusion_not_partial(self):
        def bar_loader(symbols, *, data_as_of):
            result = dict(_bars(symbols, data_as_of=data_as_of))
            result["AAA.US"] = {
                "forward_adjusted_bars": [],
                "unadjusted_bars": [],
                "error": None,
            }
            return result

        bundle = self._collector(bar_loader=bar_loader).capture()
        self.assertEqual(bundle["source_capture"]["status"], "complete")
        self.assertEqual(bundle["market_data"]["AAA.US"]["unadjusted_bars"], [])

    def test_spy_is_added_when_catalog_omits_it(self):
        bundle = self._collector().capture()
        spy = next(item for item in bundle["catalog"] if item["symbol"] == "SPY.US")
        self.assertEqual(spy["board"], "USMain")
        self.assertIn("SPY.US", bundle["market_data"])

    def test_duplicate_or_unexpected_provider_symbols_fail_closed(self):
        with self.subTest("duplicate catalog"):
            collector = self._collector(catalog=[
                {"symbol": "AAA.US", "name": "one"},
                {"symbol": "aaa.us", "name": "two"},
            ])
            with self.assertRaisesRegex(ValueError, "duplicate catalog"):
                collector.capture()

        with self.subTest("duplicate static"):
            def duplicate_static(symbols):
                return [
                    {"symbol": symbols[0], "board": "USMain", "exchange": "NYSE"},
                    {"symbol": symbols[0], "board": "USMain", "exchange": "NYSE"},
                ]

            with self.assertRaisesRegex(ValueError, "duplicate static"):
                self._collector(static_info_loader=duplicate_static).capture()

        with self.subTest("unexpected quote"):
            def unexpected_quote(_symbols):
                return {"UNKNOWN.US": {"trade_status": "normal"}}

            with self.assertRaisesRegex(ValueError, "unexpected quote"):
                self._collector(tradeability_loader=unexpected_quote).capture()


class LongbridgeQuantLoaderTests(unittest.TestCase):
    def test_calendar_resolves_verified_half_day_close(self):
        context = SimpleNamespace(
            trading_days=lambda *_args: SimpleNamespace(
                trading_days=[date(2026, 7, 2)],
                half_trading_days=[date(2026, 7, 3)],
            )
        )

        @contextmanager
        def quote_context(_credentials):
            yield context

        with (
            patch(
                "app.quant_stock_selector_longbridge._quote_context",
                quote_context,
            ),
            patch(
                "app.quant_stock_selector_longbridge._credentials",
                return_value={"configured": "yes"},
            ),
        ):
            session = load_longbridge_latest_completed_us_session(
                now=datetime(2026, 7, 3, 18, 0, tzinfo=timezone.utc),
            )

        self.assertEqual(session["session_date"], "2026-07-03")
        self.assertEqual(session["official_close"], "2026-07-03T17:00:00Z")
        self.assertTrue(session["half_trade_day"])

    def test_daily_bars_fetches_adjusted_and_raw_series_independently(self):
        calls = []
        bar = SimpleNamespace(
            timestamp=datetime(2026, 7, 24, tzinfo=timezone.utc),
            open=Decimal("99"),
            high=Decimal("101"),
            low=Decimal("98"),
            close=Decimal("100"),
            volume=1_000_000,
            turnover=Decimal("100000000"),
        )

        def history(symbol, period, adjust_type, start, end):
            calls.append((symbol, period, adjust_type, start, end))
            return [bar]

        context = SimpleNamespace(history_candlesticks_by_date=history)

        @contextmanager
        def quote_context(_credentials):
            yield context

        with (
            patch(
                "app.quant_stock_selector_longbridge._quote_context",
                quote_context,
            ),
            patch(
                "app.quant_stock_selector_longbridge._credentials",
                return_value={"configured": "yes"},
            ),
        ):
            result = load_longbridge_daily_bars(
                ["AAA.US"],
                data_as_of=DATA_AS_OF,
            )

        self.assertEqual(len(calls), 2)
        self.assertNotEqual(calls[0][2], calls[1][2])
        self.assertEqual(
            result["AAA.US"]["forward_adjusted_bars"][0]["close"],
            100.0,
        )
        self.assertEqual(
            result["AAA.US"]["unadjusted_bars"][0]["turnover"],
            100_000_000.0,
        )

    def test_daily_bars_skips_monthly_symbol_quota_and_continues(self):
        calls = []

        def history(symbol, *_args):
            calls.append(symbol)
            raise RuntimeError(
                "OpenApiException: code=301607 Permission limit"
            )

        context = SimpleNamespace(history_candlesticks_by_date=history)

        @contextmanager
        def quote_context(_credentials):
            yield context

        with (
            patch(
                "app.quant_stock_selector_longbridge._quote_context",
                quote_context,
            ),
            patch(
                "app.quant_stock_selector_longbridge._credentials",
                return_value={"configured": "yes"},
            ),
        ):
            result = load_longbridge_daily_bars(
                ["AAA.US", "BBB.US"],
                data_as_of=DATA_AS_OF,
            )

        self.assertEqual(calls, ["AAA.US", "BBB.US"])
        self.assertEqual(
            result["AAA.US"]["error_category"],
            "monthly_history_symbol_quota",
        )
        self.assertEqual(
            result["BBB.US"]["error_category"],
            "monthly_history_symbol_quota",
        )


if __name__ == "__main__":
    unittest.main()
