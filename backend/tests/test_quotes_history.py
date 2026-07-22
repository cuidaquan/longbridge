from __future__ import annotations

import unittest
from unittest.mock import patch

from app import services


class QuoteHistoryPeriodTest(unittest.TestCase):
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
