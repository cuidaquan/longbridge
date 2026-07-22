from __future__ import annotations

import unittest
from types import SimpleNamespace

from app.streaming import QuoteStreamManager


class QuoteStreamSymbolTest(unittest.TestCase):
    def test_hk_quote_keeps_subscription_symbol(self) -> None:
        manager = QuoteStreamManager()
        event = SimpleNamespace(
            timestamp=1784686313,
            sequence=1,
            last_done=365.0,
            prev_close=360.0,
        )

        payload = manager._normalize_quote("700.HK", event)

        self.assertEqual(payload["symbol"], "700.HK")


if __name__ == "__main__":
    unittest.main()
