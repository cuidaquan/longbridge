from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from app.services import get_watchlist_quotes, get_watchlists


class WatchlistServiceTests(unittest.TestCase):
    def test_get_watchlists_normalizes_sdk_response(self) -> None:
        response = [
            SimpleNamespace(id=-6, name="holdings", securities=[]),
            SimpleNamespace(
                id=2630,
                name="美股",
                securities=[
                    SimpleNamespace(
                        symbol="AAPL.US",
                        name="Apple",
                        market=SimpleNamespace(name="US"),
                        is_pinned=True,
                        watched_price=123.45,
                        watched_at=datetime(
                            2026, 8, 1, 12, 0, tzinfo=timezone.utc
                        ),
                    )
                ],
            )
        ]

        @contextmanager
        def quote_context(_credentials):
            yield SimpleNamespace(watchlist=lambda: response)

        with (
            patch("app.services.load_credentials", return_value={
                "LONGPORT_APP_KEY": "key",
                "LONGPORT_APP_SECRET": "secret",
                "LONGPORT_ACCESS_TOKEN": "token",
            }),
            patch("app.services._quote_context", side_effect=quote_context),
            patch(
                "app.services.get_security_catalog_service",
                return_value=SimpleNamespace(lookup_symbols=lambda market, symbols: {}),
            ),
        ):
            result = get_watchlists()

        self.assertEqual(result, [{
            "id": 2630,
            "name": "美股",
            "securities": [{
                "symbol": "AAPL.US",
                "name": "Apple",
                "name_cn": "Apple",
                "name_en": "Apple",
                "market": "US",
                "is_pinned": True,
                "watched_price": 123.45,
                "watched_at": "2026-08-01T12:00:00+00:00",
            }],
        }])

    def test_get_watchlist_quotes_calculates_change_rate(self) -> None:
        response = [
            SimpleNamespace(symbol="AAPL.US", last_done=105, prev_close=100),
        ]

        @contextmanager
        def quote_context(_credentials):
            yield SimpleNamespace(quote=lambda symbols: response)

        with (
            patch("app.services.load_credentials", return_value={
                "LONGPORT_APP_KEY": "key",
                "LONGPORT_APP_SECRET": "secret",
                "LONGPORT_ACCESS_TOKEN": "token",
            }),
            patch("app.services._quote_context", side_effect=quote_context),
        ):
            result = get_watchlist_quotes(["AAPL.US"])

        self.assertEqual(result["AAPL.US"]["last_done"], 105.0)
        self.assertEqual(result["AAPL.US"]["change_rate"], 5.0)

    def test_get_watchlists_requires_longbridge_credentials(self) -> None:
        with patch("app.services.load_credentials", return_value={}):
            with self.assertRaisesRegex(Exception, "完整的 Longbridge 凭据"):
                get_watchlists()

    def test_remove_watchlist_security_updates_matching_groups(self) -> None:
        update_watchlist_group = Mock()
        response = [
            SimpleNamespace(
                id=100,
                securities=[SimpleNamespace(symbol="AAPL.US")],
            ),
            SimpleNamespace(
                id=101,
                securities=[SimpleNamespace(symbol="MSFT.US")],
            ),
        ]

        @contextmanager
        def quote_context(_credentials):
            yield SimpleNamespace(
                watchlist=lambda: response,
                update_watchlist_group=update_watchlist_group,
            )

        with (
            patch("app.services.load_credentials", return_value={
                "LONGPORT_APP_KEY": "key",
                "LONGPORT_APP_SECRET": "secret",
                "LONGPORT_ACCESS_TOKEN": "token",
            }),
            patch("app.services._quote_context", side_effect=quote_context),
        ):
            from app.services import remove_watchlist_security

            result = remove_watchlist_security("aapl.us")

        self.assertEqual(result, {"symbol": "AAPL.US", "removed_from_groups": [100]})
        update_watchlist_group.assert_called_once()
        self.assertEqual(update_watchlist_group.call_args.args[0], 100)
        self.assertEqual(update_watchlist_group.call_args.kwargs["securities"], ["AAPL.US"])

    def test_update_watchlist_pinned_uses_sdk_pin_mode(self) -> None:
        update_pinned = Mock()

        @contextmanager
        def quote_context(_credentials):
            yield SimpleNamespace(update_pinned=update_pinned)

        with (
            patch("app.services.load_credentials", return_value={
                "LONGPORT_APP_KEY": "key",
                "LONGPORT_APP_SECRET": "secret",
                "LONGPORT_ACCESS_TOKEN": "token",
            }),
            patch("app.services._quote_context", side_effect=quote_context),
        ):
            from app.services import update_watchlist_pinned

            result = update_watchlist_pinned("aapl.us", True)

        self.assertEqual(result, {"symbol": "AAPL.US", "is_pinned": True})
        update_pinned.assert_called_once()
        self.assertEqual(update_pinned.call_args.args[1], ["AAPL.US"])


if __name__ == "__main__":
    unittest.main()
