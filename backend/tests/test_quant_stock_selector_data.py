from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

import duckdb

from app.db import _run_migrations
from app.quant_stock_selector_ai import AICompletion, QuantAISelectionService
from app.quant_stock_selector_data import (
    JsonQuantRunInputProvider,
    QuantSourceBundleError,
    SOURCE_BUNDLE_SCHEMA_VERSION,
)
from app.quant_stock_selector_hashing import canonical_sha256
from app.quant_stock_selector_metadata import (
    AssetClass,
    ExposureDirection,
    ProductMetadata,
    ProductMetadataBatch,
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

    def close(self):
        self.connection.close()


def _record(
    symbol: str,
    asset: AssetClass,
    direction: ExposureDirection,
    leverage=None,
) -> ProductMetadata:
    return ProductMetadata(
        symbol=symbol,
        asset_class=asset,
        exchange="NASDAQ",
        exposure_direction=direction,
        leverage=leverage,
        effective_from="2020-01-01",
        effective_to=None,
        source="licensed-vendor",
        source_version="2026-07-24",
    )


def _metadata() -> tuple[ProductMetadataBatch, dict[str, list[str]]]:
    specifications = {
        "common_stock": (AssetClass.COMMON_STOCK, ExposureDirection.NOT_APPLICABLE, None),
        "broad_equity_etf": (AssetClass.EQUITY_ETF, ExposureDirection.LONG, 1.0),
        "sector_equity_etf": (AssetClass.EQUITY_ETF, ExposureDirection.LONG, 1.0),
        "leveraged_long_etf": (AssetClass.EQUITY_ETF, ExposureDirection.LONG, 2.0),
        "inverse_etf": (AssetClass.EQUITY_ETF, ExposureDirection.INVERSE, 1.0),
        "leveraged_inverse_etf": (AssetClass.EQUITY_ETF, ExposureDirection.INVERSE, 2.0),
        "bond_etf": (AssetClass.BOND_ETF, ExposureDirection.LONG, 1.0),
        "commodity_etf": (AssetClass.COMMODITY_ETF, ExposureDirection.LONG, 1.0),
        "warrant": (AssetClass.WARRANT, ExposureDirection.NOT_APPLICABLE, None),
        "unit": (AssetClass.UNIT, ExposureDirection.NOT_APPLICABLE, None),
    }
    records = {
        "AAA.US": _record(
            "AAA.US",
            AssetClass.COMMON_STOCK,
            ExposureDirection.NOT_APPLICABLE,
        ),
        "SPY.US": _record(
            "SPY.US",
            AssetClass.EQUITY_ETF,
            ExposureDirection.LONG,
            1.0,
        ),
    }
    samples = {}
    prefixes = {
        "common_stock": "CS",
        "broad_equity_etf": "BRD",
        "sector_equity_etf": "SEC",
        "leveraged_long_etf": "LNG",
        "inverse_etf": "INV",
        "leveraged_inverse_etf": "LINV",
        "bond_etf": "BND",
        "commodity_etf": "CMD",
        "warrant": "WAR",
        "unit": "UNT",
    }
    for category, (asset, direction, leverage) in specifications.items():
        prefix = prefixes[category]
        symbols = []
        for index in range(5):
            if category == "common_stock" and index == 0:
                symbol = "AAA.US"
            elif category == "broad_equity_etf" and index == 0:
                symbol = "SPY.US"
            else:
                symbol = f"{prefix}{index}.US"
            symbols.append(symbol)
            records[symbol] = _record(symbol, asset, direction, leverage)
        samples[category] = symbols
    payload_hash = canonical_sha256({
        "records": [records[symbol].to_dict() for symbol in sorted(records)]
    })
    return ProductMetadataBatch(
        data_as_of=DATA_AS_OF.isoformat(),
        source="licensed-vendor",
        source_version="2026-07-24",
        license="internal-research-license",
        refresh_cadence="daily",
        historical_semantics="point_in_time",
        payload_hash=payload_hash,
        records=records,
        missing_symbols=(),
    ), samples


class _MetadataProvider:
    def __init__(self, batch):
        self.batch = batch
        self.requested = None

    def load(self, symbols, *, data_as_of):
        self.requested = set(symbols)
        return self.batch


def _trading_dates(count=90):
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


def _bundle(samples, *, include_quote=True, quote_timestamp="2026-07-24T19:59:00Z"):
    aaa_bars = _bars(daily_step=0.6)
    spy_bars = _bars(daily_step=0.1)
    quotes = {}
    if include_quote:
        quotes["AAA.US"] = {
            "status": "available",
            "bid": aaa_bars[-1]["close"] - 0.01,
            "ask": aaa_bars[-1]["close"] + 0.01,
            "quote_timestamp": quote_timestamp,
            "session": "regular",
        }
    return {
        "schema_version": SOURCE_BUNDLE_SCHEMA_VERSION,
        "captured_at": CAPTURED_AT.isoformat(),
        "data_as_of": DATA_AS_OF.isoformat(),
        "official_close": "2026-07-24T20:00:00Z",
        "catalog_source": "longbridge-usmain",
        "tradeability_source": "longbridge-quote",
        "bar_source": "longbridge-history",
        "catalog": [
            {
                "symbol": "AAA.US",
                "name": "AAA",
                "market": "US",
                "exchange": "NASDAQ",
            },
            {
                "symbol": "SPY.US",
                "name": "SPY",
                "market": "US",
                "exchange": "NASDAQ",
            },
        ],
        "metadata_validation_samples": samples,
        "market_data": {
            "AAA.US": {
                "trade_status": "normal",
                "directly_buyable": True,
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
                "directly_buyable": True,
                "last_price": spy_bars[-1]["close"],
                "price_data_as_of": DATA_AS_OF.isoformat(),
                "bar_data_as_of": DATA_AS_OF.isoformat(),
                "forward_adjusted_bars": spy_bars,
                "unadjusted_bars": spy_bars,
            },
        },
        "nbbo": {
            "source": "licensed-batch-nbbo",
            "source_version": "2026-07-24",
            "license": "internal-research-license",
            "authorization_constraints": "research-only",
            "historical_semantics": "point_in_time",
            "max_batch_size": 50,
            "qps_limit": 10,
            "quotes": quotes,
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
        self.batch, self.samples = _metadata()
        self.metadata_provider = _MetadataProvider(self.batch)

    def _provider(self, bundle):
        self.path.write_text(json.dumps(bundle), encoding="utf-8")
        return JsonQuantRunInputProvider(
            self.path,
            self.path,
            metadata_provider=self.metadata_provider,
            market_bar_store=MarketBarSnapshotStore(
                connection_factory=self.factory,
                clock=lambda: CAPTURED_AT,
            ),
        )

    def test_bundle_computes_quant_locally_and_builds_ai_context(self):
        captured = self._provider(_bundle(self.samples)).capture()

        self.assertEqual(captured.quant_selection["status"], "completed")
        self.assertEqual(captured.quant_selection["ai_candidate_symbols"], ["AAA.US"])
        candidates = {
            item["symbol"]: item
            for item in captured.quant_selection["candidates"]
        }
        self.assertGreaterEqual(candidates["AAA.US"]["quant_score"]["total"], 65)
        self.assertEqual(
            candidates["SPY.US"]["hard_filters"]["H11"]["reason"],
            "benchmark_symbol",
        )
        self.assertIn("AAA.US", captured.ai_contexts)
        self.assertEqual(len(captured.ai_contexts["AAA.US"].daily_bars), 90)
        bar_refs = [
            item for item in captured.input_snapshots
            if item.snapshot_kind == "market_bar_reference"
        ]
        self.assertEqual(len(bar_refs), 4)
        self.assertIn("AAA.US", self.metadata_provider.requested)
        self.assertIn(self.samples["unit"][0], self.metadata_provider.requested)

    def test_missing_batch_quote_is_partial_not_a_false_complete(self):
        captured = self._provider(
            _bundle(self.samples, include_quote=False)
        ).capture()
        self.assertEqual(captured.quant_selection["status"], "partial")
        self.assertFalse(captured.required_inputs_complete)
        self.assertEqual(captured.quant_selection["ai_candidate_symbols"], [])
        self.assertEqual(captured.quant_selection["unresolved_symbols"], ["AAA.US"])
        aaa = next(
            item for item in captured.quant_selection["candidates"]
            if item["symbol"] == "AAA.US"
        )
        self.assertEqual(aaa["hard_filters"]["H8"]["status"], "unresolved")
        self.assertIn("provider_missing_symbol", aaa["exclusion_reasons"])

    def test_news_is_point_in_time_filtered_deduplicated_and_limited(self):
        bundle = _bundle(self.samples)
        news_items = [
            {
                "title": f"News {index}",
                "source": "Wire",
                "published_at": (
                    CAPTURED_AT - timedelta(hours=index + 1)
                ).isoformat(),
                "summary": f"Summary {index}",
            }
            for index in range(12)
        ]
        news_items.extend(
            [
                {
                    "title": "  NEWS 0 ",
                    "source": "wire",
                    "published_at": (
                        CAPTURED_AT - timedelta(minutes=30)
                    ).isoformat(),
                    "summary": "Newer duplicate",
                },
                {
                    "title": "Future",
                    "source": "Wire",
                    "published_at": (
                        CAPTURED_AT + timedelta(minutes=1)
                    ).isoformat(),
                    "summary": "Must not leak",
                },
                {
                    "title": "Old",
                    "source": "Wire",
                    "published_at": (
                        CAPTURED_AT - timedelta(days=8)
                    ).isoformat(),
                    "summary": "Outside window",
                },
            ]
        )
        bundle["market_data"]["AAA.US"]["news"]["news_items"] = news_items

        captured = self._provider(bundle).capture()
        items = captured.ai_contexts["AAA.US"].news_snapshot["news_items"]

        self.assertEqual(len(items), 10)
        self.assertEqual(items[0]["summary"], "Newer duplicate")
        self.assertNotIn("Future", {item["title"] for item in items})
        self.assertNotIn("Old", {item["title"] for item in items})

    def test_nbbo_governance_fields_fail_closed(self):
        for field in (
            "source_version",
            "license",
            "authorization_constraints",
            "historical_semantics",
            "max_batch_size",
            "qps_limit",
        ):
            with self.subTest(field=field):
                bundle = _bundle(self.samples)
                bundle["nbbo"].pop(field)
                with self.assertRaises(QuantSourceBundleError):
                    self._provider(bundle).capture()

    def test_provider_batch_limit_is_respected(self):
        bundle = _bundle(self.samples)
        aaa = bundle["market_data"]["AAA.US"]
        bundle["catalog"].append(
            {
                "symbol": "BBB.US",
                "name": "BBB",
                "market": "US",
                "exchange": "NASDAQ",
            }
        )
        bundle["market_data"]["BBB.US"] = json.loads(json.dumps(aaa))
        bundle["nbbo"]["quotes"]["BBB.US"] = {
            **bundle["nbbo"]["quotes"]["AAA.US"],
        }
        bundle["nbbo"]["max_batch_size"] = 1
        records = dict(self.batch.records)
        records["BBB.US"] = _record(
            "BBB.US",
            AssetClass.COMMON_STOCK,
            ExposureDirection.NOT_APPLICABLE,
        )
        self.metadata_provider.batch = ProductMetadataBatch(
            **{**self.batch.__dict__, "records": records}
        )

        captured = self._provider(bundle).capture()

        self.assertEqual(captured.quant_selection["nbbo_batch_calls"], 2)
        self.assertEqual(captured.quant_selection["status"], "completed")

    def test_low_metadata_coverage_makes_capture_partial(self):
        bundle = _bundle(self.samples)
        bundle["catalog"].append(
            {
                "symbol": "MISSING.US",
                "name": "Missing",
                "market": "US",
                "exchange": "NASDAQ",
            }
        )
        captured = self._provider(bundle).capture()

        self.assertFalse(captured.required_inputs_complete)
        self.assertIn(
            "product_metadata_catalog_coverage_below_slo",
            captured.errors,
        )
        metadata_snapshot = next(
            item for item in captured.input_snapshots
            if item.snapshot_kind == "product_metadata"
        )
        self.assertLess(metadata_snapshot.payload["catalog_coverage"], 0.95)

    def test_completely_missing_catalog_metadata_fails_capture(self):
        bundle = _bundle(self.samples)
        bundle["catalog"] = [
            {
                "symbol": "MISSING.US",
                "name": "Missing",
                "market": "US",
                "exchange": "NASDAQ",
            }
        ]
        with self.assertRaisesRegex(
            QuantSourceBundleError,
            "metadata is unavailable",
        ):
            self._provider(bundle).capture()

    def test_stale_quote_is_deterministic_hard_filter_failure(self):
        captured = self._provider(_bundle(
            self.samples,
            quote_timestamp="2026-07-24T18:00:00Z",
        )).capture()
        self.assertEqual(captured.quant_selection["status"], "completed")
        aaa = next(
            item for item in captured.quant_selection["candidates"]
            if item["symbol"] == "AAA.US"
        )
        self.assertEqual(aaa["hard_filters"]["H10"]["status"], "fail")
        self.assertEqual(captured.quant_selection["ai_candidate_symbols"], [])

    def test_stage_zero_gate_failure_stops_before_scoring(self):
        broken_samples = dict(self.samples)
        broken_samples["unit"] = broken_samples["unit"][:4]
        provider = self._provider(_bundle(broken_samples))
        with self.assertRaisesRegex(QuantSourceBundleError, "stage-0 gate failed"):
            provider.capture()

    def test_invalid_candidate_bars_are_preserved_as_exclusion_evidence(self):
        bundle = _bundle(self.samples)
        bundle["market_data"]["AAA.US"]["unadjusted_bars"] = []
        captured = self._provider(bundle).capture()
        aaa = next(
            item for item in captured.quant_selection["candidates"]
            if item["symbol"] == "AAA.US"
        )
        self.assertEqual(aaa["hard_filters"]["H9"]["status"], "fail")
        evidence = next(
            item for item in captured.input_snapshots
            if item.snapshot_kind == "invalid_market_bar_inputs"
        )
        self.assertEqual(
            evidence.payload["AAA.US"]["reason"],
            "market_bars_missing",
        )

    def test_market_bar_reference_is_revalidated_before_cache_reuse(self):
        input_provider = self._provider(_bundle(self.samples))
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
