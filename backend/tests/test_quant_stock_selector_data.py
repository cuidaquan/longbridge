from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

import duckdb

from app.exceptions import LongbridgeAPIError
from app.db import _run_migrations
from app.quant_stock_selector_ai import AICompletion, QuantAISelectionService
from app.quant_stock_selector_data import (
    JsonQuantRunInputProvider,
    QuantSourceBundleError,
    SOURCE_BUNDLE_SCHEMA_VERSION,
)
from app.quant_stock_selector_service import (
    QuantSelectionRunRepository,
    QuantSelectionService,
)
from app.quant_stock_selector_snapshots import MarketBarSnapshotStore


DATA_AS_OF = date(2026, 7, 24)
CAPTURED_AT = datetime(2026, 7, 24, 20, 5, tzinfo=timezone.utc)


class _ConnectionFactory:
    def __init__(self) -> None:
        self.connection = duckdb.connect(":memory:")
        _run_migrations(self.connection)

    @contextmanager
    def __call__(self):
        yield self.connection

    def close(self) -> None:
        self.connection.close()


def _trading_dates(count: int = 90) -> list[date]:
    current = DATA_AS_OF
    values = []
    while len(values) < count:
        if current.weekday() < 5:
            values.append(current)
        current -= timedelta(days=1)
    return list(reversed(values))


def _bars(*, daily_step: float) -> list[dict]:
    result = []
    for index, trading_date in enumerate(_trading_dates()):
        close = 100.0 + index * daily_step
        result.append({
            "ts": datetime.combine(
                trading_date,
                datetime.min.time(),
                tzinfo=timezone.utc,
            ).isoformat(),
            "open": close - 0.25,
            "high": close + 0.5,
            "low": close - 0.5,
            "close": close,
            "volume": 1_000_000 + index * 10_000,
            "turnover": 120_000_000 + index * 100_000,
        })
    return result


def _bundle() -> dict:
    aaa_bars = _bars(daily_step=0.6)
    spy_bars = _bars(daily_step=0.1)
    return {
        "schema_version": SOURCE_BUNDLE_SCHEMA_VERSION,
        "captured_at": CAPTURED_AT.isoformat(),
        "source_capture": {
            "status": "complete",
            "source": "test-bundle",
            "errors": [],
        },
        "data_as_of": DATA_AS_OF.isoformat(),
        "official_close": "2026-07-24T20:00:00Z",
        "exchange_calendar": {
            "source": "licensed-nyse-calendar",
            "source_version": "2026-07-24",
            "license": "internal-research-license",
            "historical_semantics": "point_in_time",
            "market_calendar": "XNYS",
            "session_date": DATA_AS_OF.isoformat(),
            "official_open": "2026-07-24T13:30:00Z",
            "official_close": "2026-07-24T20:00:00Z",
            "session_status": "completed",
            "is_latest_completed_session": True,
            "captured_at": "2026-07-24T20:01:00Z",
        },
        "catalog_source": "longbridge-usmain",
        "catalog_source_version": "2026-07-24",
        "catalog_captured_at": "2026-07-24T20:02:00Z",
        "tradeability_source": "longbridge-quote",
        "bar_source": "longbridge-history",
        "catalog": [
            {
                "symbol": "AAA.US",
                "name": "AAA",
                "market": "US",
                "board": "USMain",
                "exchange": "NASDAQ",
            },
            {
                "symbol": "SPY.US",
                "name": "SPY",
                "market": "US",
                "board": "USMain",
                "exchange": "NASDAQ",
            },
        ],
        "market_data": {
            "AAA.US": {
                "trade_status": "normal",
                "last_price": aaa_bars[-1]["close"],
                "price_data_as_of": DATA_AS_OF.isoformat(),
                "bar_data_as_of": DATA_AS_OF.isoformat(),
                "forward_adjusted_bars": aaa_bars,
                "unadjusted_bars": aaa_bars,
                "news": {
                    "status": "available",
                    "source": "tavily",
                    "news_items": [],
                },
                "events": {
                    "status": "available",
                    "source": "longbridge-fundamental",
                    "events": [],
                },
            },
            "SPY.US": {
                "trade_status": "normal",
                "last_price": spy_bars[-1]["close"],
                "price_data_as_of": DATA_AS_OF.isoformat(),
                "bar_data_as_of": DATA_AS_OF.isoformat(),
                "forward_adjusted_bars": spy_bars,
                "unadjusted_bars": spy_bars,
            },
        },
    }


class _AIProvider:
    model_alias = "deepseek-v4-flash"
    temperature = 0.1

    def resolve_model_id(self):
        return "deepseek-v4-flash-20260701"

    def complete(self, **_kwargs):
        return AICompletion(
            raw_text=json.dumps({
                "decision": "SELECT",
                "confidence": 0.9,
                "suitability_score": 90,
                "risk_level": "MEDIUM",
                "time_horizon_days": 10,
                "reasons": ["trend", "liquidity"],
                "risks": ["volatility"],
                "entry_condition": "close above MA20",
                "invalidation_condition": "close below MA20",
                "data_conflicts": [],
            }),
            resolved_model_id="deepseek-v4-flash-20260701",
        )


class QuantSourceBundleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "bundle.json"
        self.factory = _ConnectionFactory()
        self.addCleanup(self.factory.close)

    def _provider(self, bundle):
        self.path.write_text(json.dumps(bundle), encoding="utf-8")
        return JsonQuantRunInputProvider(
            self.path,
            market_bar_store=MarketBarSnapshotStore(
                connection_factory=self.factory,
                clock=lambda: CAPTURED_AT,
            ),
        )

    def test_provider_requires_exactly_one_input_source(self):
        with self.assertRaisesRegex(QuantSourceBundleError, "exactly one"):
            JsonQuantRunInputProvider()
        with self.assertRaisesRegex(QuantSourceBundleError, "exactly one"):
            JsonQuantRunInputProvider(
                self.path,
                bundle_loader=_bundle,
            )

    def test_live_provider_preserves_actionable_longbridge_error(self):
        def failed_capture():
            raise LongbridgeAPIError(
                "Longbridge 历史 K 线月度唯一证券额度已用尽"
            )

        provider = JsonQuantRunInputProvider(
            bundle_loader=failed_capture,
            market_bar_store=MarketBarSnapshotStore(
                connection_factory=self.factory,
                clock=lambda: CAPTURED_AT,
            ),
        )
        with self.assertRaisesRegex(
            QuantSourceBundleError,
            "量化优选数据采集失败.*月度唯一证券额度已用尽",
        ):
            provider.capture()

    def test_bundle_loader_uses_same_validated_contract(self):
        provider = JsonQuantRunInputProvider(
            bundle_loader=_bundle,
            market_bar_store=MarketBarSnapshotStore(
                connection_factory=self.factory,
                clock=lambda: CAPTURED_AT,
            ),
        )
        captured = provider.capture()
        self.assertTrue(captured.required_inputs_complete)
        self.assertEqual(
            captured.input_snapshots[0].snapshot_kind,
            "source_capture",
        )

    def test_partial_source_capture_marks_run_incomplete(self):
        bundle = _bundle()
        bundle["source_capture"] = {
            "status": "partial",
            "source": "longbridge",
            "errors": ["bars:AAA.US:quota exceeded"],
        }
        captured = self._provider(bundle).capture()
        self.assertFalse(captured.required_inputs_complete)
        self.assertEqual(captured.errors, ["bars:AAA.US:quota exceeded"])

    def test_complete_source_capture_rejects_errors(self):
        bundle = _bundle()
        bundle["source_capture"]["errors"] = ["unexpected"]
        with self.assertRaisesRegex(
            QuantSourceBundleError,
            "complete source_capture",
        ):
            self._provider(bundle).capture()

    def test_bundle_computes_quant_without_product_metadata_or_nbbo(self):
        bundle = _bundle()
        self.assertNotIn("product_metadata", bundle)
        self.assertNotIn("nbbo", bundle)
        captured = self._provider(bundle).capture()

        self.assertEqual(captured.quant_selection["status"], "completed")
        self.assertTrue(captured.required_inputs_complete)
        self.assertEqual(captured.quant_selection["ai_candidate_symbols"], ["AAA.US"])
        candidates = {
            item["symbol"]: item for item in captured.quant_selection["candidates"]
        }
        self.assertGreaterEqual(candidates["AAA.US"]["quant_score"]["total"], 65)
        self.assertEqual(
            candidates["SPY.US"]["hard_filters"]["H8"]["reason"],
            "benchmark_symbol",
        )
        self.assertEqual(
            captured.ai_contexts["AAA.US"].security_context["board"],
            "USMAIN",
        )
        snapshot_kinds = {item.snapshot_kind for item in captured.input_snapshots}
        self.assertNotIn("product_metadata", snapshot_kinds)
        self.assertNotIn("nbbo", snapshot_kinds)
        self.assertIn("security_catalog", snapshot_kinds)

    def test_catalog_evidence_is_required_and_cannot_look_ahead(self):
        for field in ("catalog_source", "catalog_source_version", "catalog_captured_at"):
            with self.subTest(field=field):
                bundle = _bundle()
                bundle.pop(field)
                with self.assertRaises(QuantSourceBundleError):
                    self._provider(bundle).capture()
        future = _bundle()
        future["catalog_captured_at"] = "2026-07-24T20:06:00Z"
        with self.assertRaisesRegex(QuantSourceBundleError, "catalog_captured_at"):
            self._provider(future).capture()

    def test_unclassified_catalog_security_uses_same_path(self):
        bundle = _bundle()
        aaa = json.loads(json.dumps(bundle["market_data"]["AAA.US"]))
        bundle["catalog"].append({
            "symbol": "BND.US",
            "name": "Bond ETF 3x inverse warrant-like name",
            "market": "US",
            "board": "USMain",
            "exchange": "NASDAQ",
        })
        bundle["market_data"]["BND.US"] = aaa
        captured = self._provider(bundle).capture()
        records = {
            item["symbol"]: item for item in captured.quant_selection["candidates"]
        }
        self.assertEqual(records["BND.US"]["selection_status"], "quant_eligible")
        self.assertEqual(
            records["BND.US"]["quant_score"],
            records["AAA.US"]["quant_score"],
        )

    def test_news_is_point_in_time_filtered_deduplicated_and_limited(self):
        bundle = _bundle()
        news_items = [
            {
                "title": f"News {index}",
                "source": "Wire",
                "published_at": (CAPTURED_AT - timedelta(hours=index + 1)).isoformat(),
                "summary": f"Summary {index}",
            }
            for index in range(12)
        ]
        news_items.extend([
            {
                "title": "  NEWS 0 ",
                "source": "wire",
                "published_at": (CAPTURED_AT - timedelta(minutes=30)).isoformat(),
                "summary": "Newer duplicate",
            },
            {
                "title": "Future",
                "source": "Wire",
                "published_at": (CAPTURED_AT + timedelta(minutes=1)).isoformat(),
                "summary": "Must not leak",
            },
            {
                "title": "Old",
                "source": "Wire",
                "published_at": (CAPTURED_AT - timedelta(days=8)).isoformat(),
                "summary": "Outside window",
            },
        ])
        bundle["market_data"]["AAA.US"]["news"]["news_items"] = news_items
        captured = self._provider(bundle).capture()
        items = captured.ai_contexts["AAA.US"].news_snapshot["news_items"]
        self.assertEqual(len(items), 10)
        self.assertEqual(items[0]["summary"], "Newer duplicate")
        self.assertNotIn("Future", {item["title"] for item in items})
        self.assertNotIn("Old", {item["title"] for item in items})

    def test_exchange_calendar_governance_and_close_are_verified(self):
        cases = (
            ("source", None),
            ("source_version", None),
            ("license", None),
            ("historical_semantics", "latest_only"),
            ("market_calendar", "NASDAQ"),
            ("session_status", "scheduled"),
            ("is_latest_completed_session", False),
            ("official_close", "2026-07-24T17:00:00Z"),
            ("captured_at", "2026-07-24T19:59:00Z"),
        )
        for field, value in cases:
            with self.subTest(field=field):
                bundle = _bundle()
                if value is None:
                    bundle["exchange_calendar"].pop(field)
                else:
                    bundle["exchange_calendar"][field] = value
                with self.assertRaises(QuantSourceBundleError):
                    self._provider(bundle).capture()

    def test_verified_early_close_remains_valid_without_quote_window(self):
        bundle = _bundle()
        bundle["official_close"] = "2026-07-24T17:00:00Z"
        bundle["exchange_calendar"].update({
            "official_close": "2026-07-24T17:00:00Z",
            "captured_at": "2026-07-24T17:01:00Z",
        })
        captured = self._provider(bundle).capture()
        self.assertEqual(captured.quant_selection["status"], "completed")
        self.assertEqual(
            captured.quant_selection["official_close"],
            "2026-07-24T17:00:00.000Z",
        )

    def test_invalid_candidate_bars_are_preserved_as_exclusion_evidence(self):
        bundle = _bundle()
        bundle["market_data"]["AAA.US"]["unadjusted_bars"] = []
        captured = self._provider(bundle).capture()
        aaa = next(
            item for item in captured.quant_selection["candidates"]
            if item["symbol"] == "AAA.US"
        )
        self.assertEqual(aaa["hard_filters"]["H6"]["status"], "fail")
        evidence = next(
            item for item in captured.input_snapshots
            if item.snapshot_kind == "invalid_market_bar_inputs"
        )
        self.assertEqual(evidence.payload["AAA.US"]["reason"], "market_bars_missing")
        self.assertTrue(captured.required_inputs_complete)

    def test_old_schema_is_rejected(self):
        bundle = _bundle()
        bundle["schema_version"] = "quant-selector-source-bundle-v2"
        with self.assertRaisesRegex(QuantSourceBundleError, "schema_version"):
            self._provider(bundle).capture()

    def test_market_bar_reference_is_revalidated_before_cache_reuse(self):
        input_provider = self._provider(_bundle())
        repository = QuantSelectionRunRepository(
            connection_factory=self.factory,
            clock=lambda: CAPTURED_AT,
        )
        service = QuantSelectionService(
            input_provider,
            QuantAISelectionService(_AIProvider(), max_workers=1),
            repository=repository,
            runtime_id="runtime-test",
            clock=lambda: CAPTURED_AT,
        )
        completed = service.execute_run(service.create_run()["run_id"])
        self.assertTrue(repository.verify_integrity(completed["run_id"]))
        snapshot_id = self.factory.connection.execute(
            """
            SELECT snapshot_id FROM market_bar_snapshots
            WHERE symbol = 'AAA.US' AND adjust_type = 'forward_adjust'
            """
        ).fetchone()[0]
        self.factory.connection.execute(
            "DELETE FROM market_bar_snapshot_rows WHERE snapshot_id = ?",
            [snapshot_id],
        )
        self.assertFalse(repository.verify_integrity(completed["run_id"]))


if __name__ == "__main__":
    unittest.main()
