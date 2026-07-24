from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

import httpx

from .config import get_settings
from .exceptions import LongbridgeAPIError
from .repositories import load_credentials


logger = logging.getLogger(__name__)

SUPPORTED_MARKETS = ("US", "HK", "CN")
_REQUIRED_CREDENTIALS = (
    "LONGPORT_APP_KEY",
    "LONGPORT_APP_SECRET",
    "LONGPORT_ACCESS_TOKEN",
)
_SECURITY_LIST_PATH = "/v1/quote/get_security_list"
_SIGNED_HEADERS = "authorization;x-api-key;x-timestamp"


class SecurityCatalogService:
    """Searchable, short-lived cache of Longbridge's official security lists."""

    def __init__(
        self,
        cache_ttl_seconds: int = 3600,
        clock: Callable[[], float] = time.monotonic,
        cache_dir: Optional[Path] = None,
        fetch_attempts: int = 3,
    ) -> None:
        self.cache_ttl_seconds = cache_ttl_seconds
        self._clock = clock
        self._cache_dir = cache_dir
        self._fetch_attempts = max(1, fetch_attempts)
        self._cache: Dict[str, tuple[float, List[dict]]] = {}
        self._lock = threading.Lock()

    def search(self, market: str, query: str, limit: int = 20) -> List[dict]:
        normalized_market = self._normalize_market(market)

        normalized_query = query.strip().casefold()
        if not normalized_query:
            return []

        securities = self._get_market_securities(normalized_market)
        matches = [
            item
            for item in securities
            if normalized_query in item["_search_text"]
        ]
        matches.sort(key=lambda item: self._match_rank(item, normalized_query))
        return [
            {key: value for key, value in item.items() if key != "_search_text"}
            for item in matches[:limit]
        ]

    def refresh(self, market: str) -> List[dict]:
        """Fetch a current official list without falling back to stale cache."""
        normalized_market = self._normalize_market(market)
        with self._lock:
            last_error: Optional[LongbridgeAPIError] = None
            for attempt in range(1, self._fetch_attempts + 1):
                try:
                    securities = self._fetch_market_securities(
                        normalized_market
                    )
                    break
                except LongbridgeAPIError as exc:
                    last_error = exc
                    logger.warning(
                        "Security catalog refresh failed for %s "
                        "(attempt %s/%s): %s",
                        normalized_market,
                        attempt,
                        self._fetch_attempts,
                        exc,
                    )
            else:
                assert last_error is not None
                raise last_error

            self._cache[normalized_market] = (
                self._clock() + self.cache_ttl_seconds,
                securities,
            )
            self._store_disk_cache(normalized_market, securities)
            return [
                {
                    key: value
                    for key, value in item.items()
                    if key != "_search_text"
                }
                for item in securities
            ]

    def _get_market_securities(self, market: str) -> List[dict]:
        now = self._clock()
        cached = self._cache.get(market)
        if cached and cached[0] > now:
            return cached[1]

        with self._lock:
            now = self._clock()
            cached = self._cache.get(market)
            if cached and cached[0] > now:
                return cached[1]

            disk_cache = self._load_disk_cache(market)
            if disk_cache and disk_cache[0] > time.time():
                expires_at, securities = disk_cache
                remaining_ttl = max(1, int(expires_at - time.time()))
                self._cache[market] = (
                    now + min(self.cache_ttl_seconds, remaining_ttl),
                    securities,
                )
                return securities

            stale_securities = disk_cache[1] if disk_cache else None
            last_error: Optional[LongbridgeAPIError] = None
            for attempt in range(1, self._fetch_attempts + 1):
                try:
                    securities = self._fetch_market_securities(market)
                    break
                except LongbridgeAPIError as exc:
                    last_error = exc
                    logger.warning(
                        "Security catalog fetch failed for %s (attempt %s/%s): %s",
                        market,
                        attempt,
                        self._fetch_attempts,
                        exc,
                    )
            else:
                if stale_securities is not None:
                    logger.warning(
                        "Using expired local security catalog for %s after refresh failed",
                        market,
                    )
                    securities = stale_securities
                else:
                    assert last_error is not None
                    raise last_error

            self._cache[market] = (
                now + self.cache_ttl_seconds,
                securities,
            )
            if last_error is None or securities is not stale_securities:
                self._store_disk_cache(market, securities)
            return securities

    @staticmethod
    def _normalize_market(market: str) -> str:
        normalized = market.strip().upper()
        if normalized not in SUPPORTED_MARKETS:
            raise ValueError("market 必须是 US、HK 或 CN")
        return normalized

    def _cache_path(self, market: str) -> Optional[Path]:
        if self._cache_dir is None:
            return None
        return self._cache_dir / f"security_catalog_{market.lower()}.json"

    def _load_disk_cache(
        self,
        market: str,
    ) -> Optional[tuple[float, List[dict]]]:
        path = self._cache_path(market)
        if path is None or not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            expires_at = float(payload["expires_at"])
            securities = payload["items"]
            if not isinstance(securities, list):
                raise ValueError("items must be a list")
            return expires_at, securities
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            logger.warning("Ignoring invalid security catalog cache %s: %s", path, exc)
            return None

    def _store_disk_cache(self, market: str, securities: List[dict]) -> None:
        path = self._cache_path(market)
        if path is None:
            return
        temporary_path = path.with_suffix(f"{path.suffix}.tmp")
        payload = {
            "market": market,
            "expires_at": time.time() + self.cache_ttl_seconds,
            "items": securities,
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary_path.write_text(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            temporary_path.replace(path)
        except OSError as exc:
            logger.warning("Failed to persist security catalog cache %s: %s", path, exc)

    def _fetch_market_securities(self, market: str) -> List[dict]:
        credentials = load_credentials()
        if not credentials or any(
            not credentials.get(key) for key in _REQUIRED_CREDENTIALS
        ):
            raise ValueError("请先在基础配置中保存完整的 Longbridge 凭据")

        app_key = credentials["LONGPORT_APP_KEY"]
        app_secret = credentials["LONGPORT_APP_SECRET"]
        access_token = credentials["LONGPORT_ACCESS_TOKEN"]
        query = f"market={market}&category=Overnight"
        timestamp = str(int(time.time()))
        headers = self._build_signed_headers(
            app_key,
            app_secret,
            access_token,
            timestamp,
            query,
        )
        url = f"{self._api_base_url()}{_SECURITY_LIST_PATH}?{query}"

        try:
            # The SDK hard-codes a 30-second response timeout. The official
            # full-market payload can exceed that, so this endpoint uses the
            # same official URL/signing algorithm with a wider read timeout.
            with httpx.Client(timeout=90) as client:
                response = client.get(url, headers=headers)
                response.raise_for_status()
                payload = response.json()
            if payload.get("code") != 0:
                raise LongbridgeAPIError(
                    "获取 Longbridge 官方证券列表失败: "
                    f"{payload.get('message') or '未知接口错误'}"
                )
            raw_securities = (payload.get("data") or {}).get("list") or []
        except LongbridgeAPIError:
            raise
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            raise LongbridgeAPIError(f"获取 Longbridge 官方证券列表失败: {exc}") from exc

        securities: List[dict] = []
        for security in raw_securities:
            symbol = str(security.get("symbol", "") or "").strip()
            if not symbol:
                continue

            name = str(security.get("name_cn", "") or "").strip()
            name_en = str(security.get("name_en", "") or "").strip()
            name_hk = str(security.get("name_hk", "") or "").strip()
            securities.append(
                {
                    "symbol": symbol,
                    "name": name or name_hk or name_en or symbol,
                    "name_en": name_en,
                    "name_hk": name_hk,
                    "market": market,
                    "_search_text": " ".join(
                        (symbol, name, name_en, name_hk)
                    ).casefold(),
                }
            )
        return securities

    @staticmethod
    def _api_base_url() -> str:
        configured_url = os.getenv("LONGBRIDGE_HTTP_URL") or os.getenv(
            "LONGPORT_HTTP_URL"
        )
        if configured_url:
            return configured_url.rstrip("/")

        region = (
            os.getenv("LONGBRIDGE_REGION")
            or os.getenv("LONGPORT_REGION")
            or "CN"
        )
        if region.upper() == "CN":
            return "https://openapi.longbridge.cn"
        return "https://openapi.longbridge.com"

    @staticmethod
    def _build_signed_headers(
        app_key: str,
        app_secret: str,
        access_token: str,
        timestamp: str,
        query: str,
    ) -> Dict[str, str]:
        signed_values = (
            f"authorization:{access_token}\n"
            f"x-api-key:{app_key}\n"
            f"x-timestamp:{timestamp}\n"
        )
        canonical_request = (
            f"GET|{_SECURITY_LIST_PATH}|{query}|"
            f"{signed_values}|{_SIGNED_HEADERS}|"
        )
        string_to_sign = "HMAC-SHA256|" + hashlib.sha1(
            canonical_request.encode("utf-8")
        ).hexdigest()
        signature = hmac.new(
            app_secret.encode("utf-8"),
            string_to_sign.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        dc_region = (
            "us"
            if any(
                value.startswith("us_")
                for value in (app_key, app_secret, access_token)
            )
            else "ap"
        )
        return {
            "Authorization": access_token,
            "X-Api-Key": app_key,
            "X-Timestamp": timestamp,
            "X-Api-Signature": (
                "HMAC-SHA256 "
                f"SignedHeaders={_SIGNED_HEADERS}, Signature={signature}"
            ),
            "x-dc-region": dc_region,
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "openapi-sdk",
        }

    @staticmethod
    def _match_rank(item: dict, query: str) -> tuple[int, int, int, str]:
        symbol = item["symbol"].casefold()
        names = (
            item["name"].casefold(),
            item["name_en"].casefold(),
            item["name_hk"].casefold(),
        )
        if symbol == query:
            rank = 0
            matched_name_length = 0
        elif any(name == query for name in names if name):
            rank = 1
            matched_name_length = 0
        elif symbol.startswith(query):
            rank = 2
            matched_name_length = 0
        elif any(name.startswith(query) for name in names if name):
            rank = 3
            matched_name_length = min(
                len(name) for name in names if name.startswith(query)
            )
        else:
            rank = 4
            matched_name_length = min(
                (len(name) for name in names if query in name),
                default=999,
            )
        return rank, matched_name_length, len(symbol), symbol


_security_catalog_service = SecurityCatalogService(
    cache_ttl_seconds=86400,
    cache_dir=get_settings().data_dir,
    fetch_attempts=1,
)


def get_security_catalog_service() -> SecurityCatalogService:
    return _security_catalog_service
