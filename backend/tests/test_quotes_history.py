from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta
import unittest
from unittest.mock import MagicMock, patch

from app import services


class QuoteHistoryPeriodTest(unittest.TestCase):
    def test_incremental_daily_sync_reuses_one_context_for_all_symbols(self) -> None:
        context = MagicMock()
        context.history_candlesticks_by_date.return_value = [object()]

        @contextmanager
        def quote_context(_credentials):
            yield context

        latest = datetime.now() - timedelta(days=1)
        with (
            patch.object(
                services,
                "load_credentials",
                return_value={
                    "LONGPORT_APP_KEY": "key",
                    "LONGPORT_APP_SECRET": "secret",
                    "LONGPORT_ACCESS_TOKEN": "token",
                },
            ),
            patch.object(services, "_quote_context", side_effect=quote_context) as open_context,
            patch.object(
                services,
                "fetch_latest_candlestick_timestamp",
                return_value=latest,
            ),
            patch.object(services, "store_candlesticks", return_value=1) as store,
        ):
            result = services.sync_history_candlesticks(
                symbols=["AAA.US", "BBB.US"],
                period="day",
                count=250,
                incremental=True,
            )

        self.assertEqual(result, {"AAA.US": 1, "BBB.US": 1})
        self.assertEqual(open_context.call_count, 1)
        self.assertEqual(context.history_candlesticks_by_date.call_count, 2)
        context.candlesticks.assert_not_called()
        self.assertEqual(store.call_count, 2)

    def test_stale_daily_data_refreshes_only_requested_window(self) -> None:
        context = MagicMock()
        context.candlesticks.return_value = [object()]

        @contextmanager
        def quote_context(_credentials):
            yield context

        stale = datetime.now() - timedelta(days=30)
        with (
            patch.object(
                services,
                "load_credentials",
                return_value={
                    "LONGPORT_APP_KEY": "key",
                    "LONGPORT_APP_SECRET": "secret",
                    "LONGPORT_ACCESS_TOKEN": "token",
                },
            ),
            patch.object(services, "_quote_context", side_effect=quote_context),
            patch.object(
                services,
                "fetch_latest_candlestick_timestamp",
                return_value=stale,
            ),
            patch.object(services, "store_candlesticks", return_value=1),
        ):
            services.sync_history_candlesticks(
                symbols=["AAA.US"],
                period="day",
                count=250,
                incremental=True,
            )

        context.candlesticks.assert_called_once()
        self.assertEqual(context.candlesticks.call_args.args[2], 250)
        context.history_candlesticks_by_date.assert_not_called()

    def test_empty_incremental_result_does_not_trigger_full_fallback(self) -> None:
        context = MagicMock()
        context.history_candlesticks_by_date.return_value = []

        @contextmanager
        def quote_context(_credentials):
            yield context

        latest = datetime.now() - timedelta(days=1)
        with (
            patch.object(
                services,
                "load_credentials",
                return_value={
                    "LONGPORT_APP_KEY": "key",
                    "LONGPORT_APP_SECRET": "secret",
                    "LONGPORT_ACCESS_TOKEN": "token",
                },
            ),
            patch.object(services, "_quote_context", side_effect=quote_context),
            patch.object(
                services,
                "fetch_latest_candlestick_timestamp",
                return_value=latest,
            ),
            patch.object(services, "store_candlesticks", return_value=0),
        ):
            result = services.sync_history_candlesticks(
                symbols=["AAA.US"],
                period="day",
                count=250,
                incremental=True,
            )

        self.assertEqual(result, {"AAA.US": 0})
        context.candlesticks.assert_not_called()
        context.history_candlesticks_by_offset.assert_not_called()

    def test_missing_non_minute_period_does_not_return_minute_tick_bars(self) -> None:
        tick_bars = [{"ts": "2026-07-22T00:00:00", "close": 10.0}]
        with (
            patch.object(services, "_repo_fetch_candlesticks", return_value=[]),
            patch.object(services, "fetch_bars_from_ticks", return_value=tick_bars) as fetch_ticks,
        ):
            result = services.get_cached_candlesticks("TEST.US", "week", 20)

        self.assertEqual(result, [])
        fetch_ticks.assert_not_called()

    def test_missing_minute_period_can_fall_back_to_tick_bars(self) -> None:
        tick_bars = [{"ts": "2026-07-22T00:00:00", "close": 10.0}]
        with (
            patch.object(services, "_repo_fetch_candlesticks", return_value=[]),
            patch.object(services, "fetch_bars_from_ticks", return_value=tick_bars),
        ):
            result = services.get_cached_candlesticks("TEST.US", "min1", 20)

        self.assertEqual(result, tick_bars)

    def test_minute_period_merges_cached_history_with_live_tick_bars(self) -> None:
        cached_bars = [
            {"ts": "2026-07-21T15:59:00", "close": 31.9},
            {"ts": "2026-07-21T16:00:00", "close": 32.0},
        ]
        tick_bars = [
            {"ts": "2026-07-22T10:59:00", "close": 31.6},
            {"ts": "2026-07-22T11:00:00", "close": 31.7},
        ]
        with (
            patch.object(services, "_repo_fetch_candlesticks", return_value=cached_bars),
            patch.object(services, "fetch_bars_from_ticks", return_value=tick_bars),
        ):
            result = services.get_cached_candlesticks("3288.HK", "min1", 3)

        self.assertEqual(
            [bar["ts"] for bar in result],
            ["2026-07-21T16:00:00", "2026-07-22T10:59:00", "2026-07-22T11:00:00"],
        )

    def test_live_tick_bar_replaces_overlapping_cached_minute(self) -> None:
        cached_bars = [{"ts": "2026-07-22T11:00:00", "close": 31.6}]
        tick_bars = [{"ts": "2026-07-22T11:00:00", "close": 31.7}]
        with (
            patch.object(services, "_repo_fetch_candlesticks", return_value=cached_bars),
            patch.object(services, "fetch_bars_from_ticks", return_value=tick_bars),
        ):
            result = services.get_cached_candlesticks("3288.HK", "min1", 10)

        self.assertEqual(result, tick_bars)


if __name__ == "__main__":
    unittest.main()
