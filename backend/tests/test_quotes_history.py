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


if __name__ == "__main__":
    unittest.main()
