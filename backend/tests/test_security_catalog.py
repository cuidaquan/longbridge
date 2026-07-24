from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app.exceptions import LongbridgeAPIError
from app.main import app
from app.security_catalog import SecurityCatalogService


class SecurityCatalogServiceTest(unittest.TestCase):
    def test_fetch_uses_official_full_market_list(self) -> None:
        response = MagicMock()
        response.json.return_value = {
            "code": 0,
            "data": {
                "list": [
                    {
                        "symbol": "AAPL.US",
                        "name_cn": "苹果",
                        "name_en": "Apple Inc.",
                        "name_hk": "蘋果",
                    }
                ]
            },
        }
        client = MagicMock()
        client.get.return_value = response
        client_context = MagicMock()
        client_context.__enter__.return_value = client
        credentials = {
            "LONGPORT_APP_KEY": "key",
            "LONGPORT_APP_SECRET": "secret",
            "LONGPORT_ACCESS_TOKEN": "token",
        }

        with (
            patch("app.security_catalog.load_credentials", return_value=credentials),
            patch("app.security_catalog.httpx.Client", return_value=client_context),
            patch.dict("os.environ", {"LONGBRIDGE_REGION": "CN"}, clear=False),
        ):
            items = SecurityCatalogService()._fetch_market_securities("US")

        request_url = client.get.call_args.args[0]
        request_headers = client.get.call_args.kwargs["headers"]
        self.assertEqual(
            request_url,
            "https://openapi.longbridge.cn"
            "/v1/quote/get_security_list?market=US&category=Overnight",
        )
        self.assertTrue(
            request_headers["X-Api-Signature"].startswith(
                "HMAC-SHA256 SignedHeaders="
            )
        )
        response.raise_for_status.assert_called_once_with()
        self.assertEqual(items[0]["symbol"], "AAPL.US")
        self.assertEqual(items[0]["name"], "苹果")
        self.assertIn("apple inc.", items[0]["_search_text"])

    def test_search_matches_code_and_names_and_reuses_cache(self) -> None:
        clock_value = [100.0]
        service = SecurityCatalogService(
            cache_ttl_seconds=60,
            clock=lambda: clock_value[0],
        )
        securities = [
            {
                "symbol": "AAPL.US",
                "name": "苹果",
                "name_en": "Apple Inc.",
                "name_hk": "蘋果",
                "market": "US",
                "_search_text": "aapl.us 苹果 apple inc. 蘋果",
            },
            {
                "symbol": "APP.US",
                "name": "AppLovin",
                "name_en": "AppLovin",
                "name_hk": "AppLovin",
                "market": "US",
                "_search_text": "app.us applovin",
            },
        ]

        with patch.object(
            service,
            "_fetch_market_securities",
            return_value=securities,
        ) as fetch:
            by_code = service.search("US", "AAPL", 20)
            by_name = service.search("US", "苹果", 20)
            clock_value[0] = 130.0
            service.search("US", "Apple", 20)

        self.assertEqual(fetch.call_count, 1)
        self.assertEqual([item["symbol"] for item in by_code], ["AAPL.US"])
        self.assertEqual([item["symbol"] for item in by_name], ["AAPL.US"])
        self.assertNotIn("_search_text", by_code[0])

    def test_search_prioritizes_underlying_over_longer_derivative_names(self) -> None:
        service = SecurityCatalogService()
        securities = [
            {
                "symbol": "13005.HK",
                "name": "腾讯法兴七三购A",
                "name_en": "SGTENCT@EC2703A",
                "name_hk": "騰訊法興七三購A",
                "market": "HK",
                "_search_text": "13005.hk 腾讯法兴七三购a sgtenct@ec2703a 騰訊法興七三購a",
            },
            {
                "symbol": "700.HK",
                "name": "腾讯控股",
                "name_en": "Tencent",
                "name_hk": "騰訊控股",
                "market": "HK",
                "_search_text": "700.hk 腾讯控股 tencent 騰訊控股",
            },
        ]

        with patch.object(
            service,
            "_fetch_market_securities",
            return_value=securities,
        ):
            results = service.search("HK", "腾讯")

        self.assertEqual([item["symbol"] for item in results], ["700.HK", "13005.HK"])

    def test_empty_query_does_not_fetch_catalog(self) -> None:
        service = SecurityCatalogService()
        with patch.object(service, "_fetch_market_securities") as fetch:
            self.assertEqual(service.search("HK", "  "), [])
        fetch.assert_not_called()

    def test_rejects_unsupported_market(self) -> None:
        with self.assertRaisesRegex(ValueError, "US、HK 或 CN"):
            SecurityCatalogService().search("SG", "D05")

    def test_retries_transient_official_api_failure(self) -> None:
        service = SecurityCatalogService(fetch_attempts=3)
        securities = [
            {
                "symbol": "AAPL.US",
                "name": "苹果",
                "name_en": "Apple Inc.",
                "name_hk": "Apple",
                "market": "US",
                "_search_text": "aapl.us 苹果 apple inc. apple",
            }
        ]
        with patch.object(
            service,
            "_fetch_market_securities",
            side_effect=[
                LongbridgeAPIError("request timeout"),
                securities,
            ],
        ) as fetch:
            results = service.search("US", "AAPL")

        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(results[0]["symbol"], "AAPL.US")

    def test_refresh_forces_live_fetch_and_does_not_use_stale_cache(
        self,
    ) -> None:
        service = SecurityCatalogService(fetch_attempts=2)
        service._cache["US"] = (
            float("inf"),
            [{
                "symbol": "STALE.US",
                "name": "Stale",
                "name_en": "Stale",
                "name_hk": "",
                "market": "US",
                "_search_text": "stale.us stale",
            }],
        )
        fresh = [{
            "symbol": "AAPL.US",
            "name": "苹果",
            "name_en": "Apple Inc.",
            "name_hk": "Apple",
            "market": "US",
            "_search_text": "aapl.us 苹果 apple inc. apple",
        }]
        with patch.object(
            service,
            "_fetch_market_securities",
            side_effect=[LongbridgeAPIError("timeout"), fresh],
        ) as fetch:
            result = service.refresh("us")

        self.assertEqual(fetch.call_count, 2)
        self.assertEqual([item["symbol"] for item in result], ["AAPL.US"])
        self.assertNotIn("_search_text", result[0])
        self.assertEqual(service.search("US", "Apple")[0]["symbol"], "AAPL.US")

    def test_refresh_failure_never_falls_back_to_cached_catalog(self) -> None:
        service = SecurityCatalogService(fetch_attempts=1)
        service._cache["HK"] = (
            float("inf"),
            [{
                "symbol": "700.HK",
                "name": "腾讯控股",
                "name_en": "Tencent",
                "name_hk": "騰訊控股",
                "market": "HK",
                "_search_text": "700.hk 腾讯控股 tencent 騰訊控股",
            }],
        )
        with patch.object(
            service,
            "_fetch_market_securities",
            side_effect=LongbridgeAPIError("live unavailable"),
        ):
            with self.assertRaisesRegex(
                LongbridgeAPIError,
                "live unavailable",
            ):
                service.refresh("HK")

    def test_reuses_persisted_catalog_across_service_instances(self) -> None:
        securities = [
            {
                "symbol": "700.HK",
                "name": "腾讯控股",
                "name_en": "Tencent",
                "name_hk": "騰訊控股",
                "market": "HK",
                "_search_text": "700.hk 腾讯控股 tencent 騰訊控股",
            }
        ]
        with TemporaryDirectory() as temporary_dir:
            cache_dir = Path(temporary_dir)
            writer = SecurityCatalogService(cache_dir=cache_dir)
            with patch.object(
                writer,
                "_fetch_market_securities",
                return_value=securities,
            ):
                writer.search("HK", "腾讯")

            reader = SecurityCatalogService(cache_dir=cache_dir)
            with patch.object(reader, "_fetch_market_securities") as fetch:
                results = reader.search("HK", "Tencent")

        fetch.assert_not_called()
        self.assertEqual(results[0]["symbol"], "700.HK")

    def test_search_endpoint_returns_catalog_results(self) -> None:
        service = MagicMock()
        service.search.return_value = [
            {
                "symbol": "700.HK",
                "name": "腾讯控股",
                "name_en": "Tencent",
                "name_hk": "騰訊控股",
                "market": "HK",
            }
        ]

        with patch(
            "app.routers.stock_picker.get_security_catalog_service",
            return_value=service,
        ):
            response = TestClient(app).get(
                "/api/stock-picker/securities",
                params={"market": "HK", "q": "腾讯", "limit": 10},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["source"], "longbridge")
        self.assertEqual(response.json()["items"][0]["symbol"], "700.HK")
        service.search.assert_called_once_with("HK", "腾讯", 10)


if __name__ == "__main__":
    unittest.main()
