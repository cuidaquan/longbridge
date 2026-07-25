from __future__ import annotations

from datetime import date, datetime, timedelta
import json
from pathlib import Path
import tempfile
import unittest
from zoneinfo import ZoneInfo

from app.quant_stock_selector import QuantIndicators
from app.quant_stock_selector_metadata import (
    AssetClass,
    ExposureDirection,
    JsonProductMetadataProvider,
    ProductMetadata,
    ProductMetadataError,
    PRODUCT_METADATA_SCHEMA_VERSION,
    REQUIRED_VALIDATION_CATEGORIES,
    evaluate_product_metadata_gate,
)
from app.quant_stock_selector_universe import (
    FILTER_VERSION,
    CandidateMarketData,
    NbboQuote,
    QuantUniverseCandidate,
    QuantUniverseSelector,
    SelectionPolicy,
    UniverseSelectionError,
    build_unified_candidate_pool,
    evaluate_pre_nbbo_filters,
)


DATA_AS_OF = date(2026, 7, 24)
_DEFAULT_METADATA = object()
OFFICIAL_CLOSE = datetime(
    2026,
    7,
    24,
    16,
    tzinfo=ZoneInfo("America/New_York"),
)


def _high_indicators(*, turnover: float = 100_000_000.0) -> QuantIndicators:
    return QuantIndicators(
        data_as_of=DATA_AS_OF.isoformat(),
        close=120.0,
        median_turnover_20d=turnover,
        ma20=110.0,
        ma60=100.0,
        ma20_slope_10d=0.05,
        rs20=0.12,
        rs60=0.18,
        rsi14=60.0,
        macd_line=3.0,
        macd_signal=2.0,
        macd_histogram=1.0,
        macd_histogram_previous_1=0.5,
        macd_histogram_previous_2=0.25,
        atr14=3.0,
        atr14_close=0.025,
        volatility_20d=0.35,
        max_drawdown_60d=0.08,
        return_5d=0.03,
        volume_ratio_5_20=1.3,
    )


def _low_indicators() -> QuantIndicators:
    return QuantIndicators(
        data_as_of=DATA_AS_OF.isoformat(),
        close=80.0,
        median_turnover_20d=10_000_000.0,
        ma20=100.0,
        ma60=110.0,
        ma20_slope_10d=-0.03,
        rs20=-0.10,
        rs60=-0.12,
        rsi14=20.0,
        macd_line=-2.0,
        macd_signal=-1.0,
        macd_histogram=-1.0,
        macd_histogram_previous_1=-0.5,
        macd_histogram_previous_2=-0.25,
        atr14=8.0,
        atr14_close=0.10,
        volatility_20d=0.80,
        max_drawdown_60d=0.40,
        return_5d=-0.03,
        volume_ratio_5_20=1.0,
    )


def _metadata(
    symbol: str,
    *,
    asset_class: AssetClass = AssetClass.COMMON_STOCK,
    direction: ExposureDirection | None = None,
    leverage: float | None = None,
    exchange: str = "NASDAQ",
) -> ProductMetadata:
    if direction is None:
        direction = (
            ExposureDirection.NOT_APPLICABLE
            if asset_class == AssetClass.COMMON_STOCK
            else ExposureDirection.LONG
        )
    return ProductMetadata(
        symbol=symbol,
        asset_class=asset_class,
        exchange=exchange,
        exposure_direction=direction,
        leverage=leverage,
        effective_from="2020-01-01",
        effective_to=None,
        source="licensed-vendor",
        source_version="2026-07-24",
    )


def _candidate(
    symbol: str,
    *,
    metadata=_DEFAULT_METADATA,
    indicators: QuantIndicators | None = None,
    **overrides,
) -> QuantUniverseCandidate:
    values = {
        "symbol": symbol,
        "name": symbol,
        "market": "US",
        "exchange": "NASDAQ",
        "metadata": (
            _metadata(symbol)
            if metadata is _DEFAULT_METADATA
            else metadata
        ),
        "trade_status": "normal",
        "directly_buyable": True,
        "last_price": 120.0,
        "price_data_as_of": DATA_AS_OF,
        "bar_data_as_of": DATA_AS_OF,
        "valid_daily_bars": 90,
        "indicators": indicators or _high_indicators(),
        "indicator_error": None,
    }
    values.update(overrides)
    return QuantUniverseCandidate(**values)


def _quote(
    symbol: str,
    *,
    status: str = "available",
    bid: float | None = 100.0,
    ask: float | None = 100.0,
    timestamp: datetime | None = None,
    session: str | None = "regular",
    error_code: str | None = None,
) -> NbboQuote:
    return NbboQuote(
        symbol=symbol,
        status=status,
        bid=bid,
        ask=ask,
        quote_timestamp=timestamp or OFFICIAL_CLOSE - timedelta(minutes=1),
        session=session,
        source="test-nbbo",
        error_code=error_code,
    )


class _FakeNbboProvider:
    source = "test-nbbo"

    def __init__(self, responses=None) -> None:
        self.responses = responses or {}
        self.calls = []
        self.attempts = {}

    def fetch(self, symbols, *, data_as_of, official_close):
        self.calls.append(list(symbols))
        result = {}
        for symbol in symbols:
            attempt = self.attempts.get(symbol, 0)
            self.attempts[symbol] = attempt + 1
            configured = self.responses.get(symbol)
            if isinstance(configured, list):
                value = configured[min(attempt, len(configured) - 1)]
            else:
                value = configured
            if value is None:
                value = _quote(symbol)
            if isinstance(value, Exception):
                raise value
            result[symbol] = value
        return result


class ProductMetadataProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "products.json"

    def _write(self, **overrides) -> None:
        payload = {
            "schema_version": PRODUCT_METADATA_SCHEMA_VERSION,
            "source": "licensed-vendor",
            "source_version": "2026-07-24",
            "license": "internal-research-license",
            "refresh_cadence": "daily",
            "historical_semantics": "point_in_time",
            "items": [
                {
                    "symbol": "AAA.US",
                    "asset_class": "common_stock",
                    "exchange": "NYSE",
                    "exposure_direction": "not_applicable",
                    "leverage": None,
                    "effective_from": "2020-01-01",
                    "effective_to": "2025-12-31",
                },
                {
                    "symbol": "AAA.US",
                    "asset_class": "common_stock",
                    "exchange": "NASDAQ",
                    "exposure_direction": "not_applicable",
                    "leverage": None,
                    "effective_from": "2026-01-01",
                    "effective_to": None,
                },
                {
                    "symbol": "SQQQ.US",
                    "asset_class": "equity_etf",
                    "exchange": "NASDAQ",
                    "exposure_direction": "inverse",
                    "leverage": 3,
                    "effective_from": "2010-01-01",
                    "effective_to": None,
                },
                {
                    "symbol": "BND.US",
                    "asset_class": "bond_etf",
                    "exchange": "NASDAQ",
                    "exposure_direction": "long",
                    "leverage": 1,
                    "effective_from": "2010-01-01",
                    "effective_to": None,
                },
            ],
        }
        payload.update(overrides)
        self.path.write_text(json.dumps(payload), encoding="utf-8")

    def test_load_selects_point_in_time_records_and_preserves_inverse_context(self) -> None:
        self._write()
        provider = JsonProductMetadataProvider(self.path)
        batch = provider.load(
            ["sqqq.us", "AAA.US", "MISSING.US", "AAA.US"],
            data_as_of=DATA_AS_OF,
        )

        self.assertEqual(batch.records["AAA.US"].exchange, "NASDAQ")
        inverse = batch.records["SQQQ.US"]
        self.assertEqual(inverse.asset_class, AssetClass.EQUITY_ETF)
        self.assertEqual(inverse.exposure_direction, ExposureDirection.INVERSE)
        self.assertEqual(inverse.leverage, 3.0)
        self.assertTrue(inverse.eligible)
        self.assertEqual(batch.missing_symbols, ("MISSING.US",))
        self.assertEqual(len(batch.payload_hash), 64)
        self.assertEqual(
            batch.to_manifest()["historical_semantics"],
            "point_in_time",
        )

    def test_requested_order_does_not_change_metadata_hash(self) -> None:
        self._write()
        provider = JsonProductMetadataProvider(self.path)
        first = provider.load(
            ["AAA.US", "SQQQ.US"],
            data_as_of=DATA_AS_OF,
        )
        second = provider.load(
            ["SQQQ.US", "AAA.US"],
            data_as_of=DATA_AS_OF,
        )
        self.assertEqual(first.payload_hash, second.payload_hash)

    def test_missing_governance_and_overlapping_records_fail_closed(self) -> None:
        cases = (
            {"license": ""},
            {"historical_semantics": "latest_only"},
            {
                "items": [
                    {
                        "symbol": "AAA.US",
                        "asset_class": "common_stock",
                        "exchange": "NYSE",
                        "exposure_direction": "not_applicable",
                        "leverage": None,
                        "effective_from": "2020-01-01",
                        "effective_to": None,
                    },
                    {
                        "symbol": "AAA.US",
                        "asset_class": "common_stock",
                        "exchange": "NASDAQ",
                        "exposure_direction": "not_applicable",
                        "leverage": None,
                        "effective_from": "2021-01-01",
                        "effective_to": None,
                    },
                ]
            },
        )
        for override in cases:
            with self.subTest(override=override):
                self._write(**override)
                with self.assertRaises(ProductMetadataError):
                    JsonProductMetadataProvider(self.path).load(
                        ["AAA.US"],
                        data_as_of=DATA_AS_OF,
                    )

    def test_common_stock_cannot_be_disguised_as_leveraged_product(self) -> None:
        self._write(items=[{
            "symbol": "AAA.US",
            "asset_class": "common_stock",
            "exchange": "NASDAQ",
            "exposure_direction": "long",
            "leverage": 2,
            "effective_from": "2020-01-01",
            "effective_to": None,
        }])
        with self.assertRaises(ProductMetadataError):
            JsonProductMetadataProvider(self.path).load(
                ["AAA.US"],
                data_as_of=DATA_AS_OF,
            )

    def test_ten_category_gate_requires_five_correct_samples_each(self) -> None:
        category_config = {
            "common_stock": ("common_stock", "not_applicable", None),
            "broad_equity_etf": ("equity_etf", "long", 1),
            "sector_equity_etf": ("equity_etf", "long", 1),
            "leveraged_long_etf": ("equity_etf", "long", 2),
            "inverse_etf": ("equity_etf", "inverse", 1),
            "leveraged_inverse_etf": ("equity_etf", "inverse", 3),
            "bond_etf": ("bond_etf", "long", 1),
            "commodity_etf": ("commodity_etf", "long", 1),
            "warrant": ("warrant", "not_applicable", None),
            "unit": ("unit", "not_applicable", None),
        }
        items = []
        samples = {}
        for category_index, category in enumerate(REQUIRED_VALIDATION_CATEGORIES):
            asset_class, direction, leverage = category_config[category]
            samples[category] = []
            for index in range(5):
                symbol = f"T{category_index:02d}{index}.US"
                samples[category].append(symbol)
                items.append({
                    "symbol": symbol,
                    "asset_class": asset_class,
                    "exchange": "NASDAQ",
                    "exposure_direction": direction,
                    "leverage": leverage,
                    "effective_from": "2020-01-01",
                    "effective_to": None,
                })
        self._write(items=items)
        symbols = [symbol for values in samples.values() for symbol in values]
        batch = JsonProductMetadataProvider(self.path).load(
            symbols,
            data_as_of=DATA_AS_OF,
        )
        ready = evaluate_product_metadata_gate(batch, samples)
        self.assertTrue(ready["ready"])
        self.assertEqual(ready["failures"], [])

        broken_samples = dict(samples)
        broken_samples["common_stock"] = samples["common_stock"][:4]
        blocked = evaluate_product_metadata_gate(batch, broken_samples)
        self.assertFalse(blocked["ready"])
        self.assertIn(
            "common_stock:insufficient_samples",
            blocked["failures"],
        )


class HardFilterTests(unittest.TestCase):
    def test_candidate_pool_preserves_full_catalog_and_missing_evidence(self) -> None:
        batch = type("Batch", (), {
            "data_as_of": DATA_AS_OF.isoformat(),
            "records": {"AAA.US": _metadata("AAA.US")},
        })()
        facts = CandidateMarketData(
            trade_status="normal",
            directly_buyable=True,
            last_price=120.0,
            price_data_as_of=DATA_AS_OF,
            bar_data_as_of=DATA_AS_OF,
            valid_daily_bars=90,
            indicators=_high_indicators(),
        )
        candidates = build_unified_candidate_pool(
            [
                {"symbol": "BBB.US", "name": "Beta", "market": "US"},
                {"symbol": "AAA.US", "name": "Alpha", "market": "US"},
            ],
            metadata_batch=batch,
            market_data={"AAA.US": facts},
            data_as_of=DATA_AS_OF,
        )
        self.assertEqual([item.symbol for item in candidates], ["BBB.US", "AAA.US"])
        self.assertIsNone(candidates[0].metadata)
        self.assertEqual(candidates[0].indicator_error, "market_data_missing")
        filters, reasons = evaluate_pre_nbbo_filters(
            candidates[0],
            data_as_of=DATA_AS_OF,
        )
        self.assertEqual(filters["H2"]["reason"], "asset_class_unknown")
        self.assertIn("market_data_missing", reasons)

    def test_stocks_and_inverse_etfs_share_path_while_non_equity_is_excluded(self) -> None:
        stock = _candidate("AAA.US")
        inverse = _candidate(
            "SQQQ.US",
            metadata=_metadata(
                "SQQQ.US",
                asset_class=AssetClass.EQUITY_ETF,
                direction=ExposureDirection.INVERSE,
                leverage=3.0,
            ),
        )
        bond = _candidate(
            "BND.US",
            metadata=_metadata(
                "BND.US",
                asset_class=AssetClass.BOND_ETF,
                leverage=1.0,
            ),
        )
        for candidate in (stock, inverse):
            filters, reasons = evaluate_pre_nbbo_filters(
                candidate,
                data_as_of=DATA_AS_OF,
            )
            self.assertEqual(reasons, [])
            self.assertTrue(all(
                value["status"] in {"pass", "pending"}
                for value in filters.values()
            ))
        bond_filters, bond_reasons = evaluate_pre_nbbo_filters(
            bond,
            data_as_of=DATA_AS_OF,
        )
        self.assertEqual(bond_filters["H2"]["status"], "fail")
        self.assertIn("ineligible_asset_class", bond_reasons)

    def test_spy_is_explicit_h11_failure(self) -> None:
        filters, reasons = evaluate_pre_nbbo_filters(
            _candidate("SPY.US"),
            data_as_of=DATA_AS_OF,
        )
        self.assertEqual(filters["H11"], {
            "status": "fail",
            "reason": "benchmark_symbol",
        })
        self.assertIn("benchmark_symbol", reasons)

    def test_each_static_contract_fails_with_stable_reason(self) -> None:
        cases = (
            ({"market": "HK"}, "H1", "market_not_us"),
            ({"metadata": None}, "H2", "asset_class_unknown"),
            ({"metadata": _metadata("AAA.US", exchange="OTC")}, "H3", "ineligible_exchange"),
            ({"trade_status": "halted"}, "H4", "trade_status_not_normal"),
            ({"last_price": 4.99}, "H5", "price_below_minimum_or_missing"),
            ({"directly_buyable": False}, "H6", "not_directly_buyable"),
            ({"indicators": _high_indicators(turnover=9_999_999)}, "H7", "median_turnover_below_minimum"),
            ({"valid_daily_bars": 84}, "H9", "daily_bar_history_incomplete"),
            ({"bar_data_as_of": date(2026, 7, 23)}, "H10", "data_as_of_mismatch"),
        )
        for override, code, reason in cases:
            with self.subTest(code=code):
                filters, reasons = evaluate_pre_nbbo_filters(
                    _candidate("AAA.US", **override),
                    data_as_of=DATA_AS_OF,
                )
                self.assertEqual(filters[code]["status"], "fail")
                self.assertIn(reason, reasons)


class QuantUniverseSelectionTests(unittest.TestCase):
    def test_stable_tie_break_selects_first_30_without_expanding_ties(self) -> None:
        candidates = [
            _candidate(f"S{index:03d}.US") for index in range(32)
        ]
        provider = _FakeNbboProvider()
        result = QuantUniverseSelector(
            provider,
            policy=SelectionPolicy(batch_size=10),
        ).select(
            candidates,
            data_as_of=DATA_AS_OF,
            official_close=OFFICIAL_CLOSE,
        )

        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["boundary_proven"])
        self.assertEqual(result["filter_version"], FILTER_VERSION)
        self.assertEqual(result["nbbo_request_units"], 30)
        self.assertEqual(result["ai_candidate_symbols"], [
            f"S{index:03d}.US" for index in range(30)
        ])
        self.assertEqual(len(result["selection_manifest_hash"]), 64)
        manifest = result["selection_manifest"]
        manifest_records = {
            item["symbol"]: item for item in manifest["candidates"]
        }
        selected = manifest_records["S000.US"]
        self.assertEqual(selected["final_rank"], 1)
        self.assertEqual(selected["final_q"], result["quant_ranking"][0]["q"])
        self.assertEqual(selected["stable_sort_key"], {
            "median_turnover_20d_desc": 100_000_000.0,
            "q_desc": result["quant_ranking"][0]["q"],
            "symbol_asc": "S000.US",
        })
        self.assertTrue(selected["nbbo_queried"])
        self.assertIsNone(selected["prune_reason"])
        proof = manifest["boundary_proof"]
        self.assertTrue(proof["proven"])
        self.assertEqual(proof["frontier"]["kind"], "top_n_stable_sort_key")
        self.assertEqual(proof["frontier"]["symbol_asc"], "S029.US")
        self.assertEqual(
            [item["symbol_asc"] for item in proof["obstacles"]],
            ["S030.US", "S031.US"],
        )
        pruned = {
            item["symbol"]: item for item in result["candidates"]
        }["S030.US"]
        self.assertEqual(
            pruned["selection_status"],
            "quant_upper_bound_below_frontier",
        )
        self.assertEqual(pruned["hard_filters"]["H8"]["status"], "not_evaluated")
        manifest_pruned = manifest_records["S030.US"]
        self.assertFalse(manifest_pruned["nbbo_queried"])
        self.assertIsNone(manifest_pruned["final_q"])
        self.assertEqual(
            manifest_pruned["prune_reason"],
            "quant_upper_bound_below_frontier",
        )

    def test_stock_and_inverse_etf_receive_same_score_and_ranking_contract(self) -> None:
        candidates = [
            _candidate("AAA.US"),
            _candidate(
                "SQQQ.US",
                metadata=_metadata(
                    "SQQQ.US",
                    asset_class=AssetClass.EQUITY_ETF,
                    direction=ExposureDirection.INVERSE,
                    leverage=3.0,
                ),
            ),
        ]
        result = QuantUniverseSelector(_FakeNbboProvider()).select(
            candidates,
            data_as_of=DATA_AS_OF,
            official_close=OFFICIAL_CLOSE,
        )
        records = {item["symbol"]: item for item in result["candidates"]}
        self.assertEqual(
            records["AAA.US"]["quant_score"]["total"],
            records["SQQQ.US"]["quant_score"]["total"],
        )
        self.assertEqual(
            records["SQQQ.US"]["metadata"]["exposure_direction"],
            "inverse",
        )
        self.assertEqual(result["ai_candidate_symbols"], ["AAA.US", "SQQQ.US"])

    def test_stale_and_extended_hours_quotes_fail_deterministically(self) -> None:
        responses = {
            "STALE.US": _quote(
                "STALE.US",
                timestamp=OFFICIAL_CLOSE - timedelta(minutes=6),
            ),
            "EXT.US": _quote("EXT.US", session="extended"),
        }
        result = QuantUniverseSelector(_FakeNbboProvider(responses)).select(
            [_candidate("STALE.US"), _candidate("EXT.US")],
            data_as_of=DATA_AS_OF,
            official_close=OFFICIAL_CLOSE,
        )
        records = {item["symbol"]: item for item in result["candidates"]}
        self.assertEqual(result["status"], "completed")
        self.assertEqual(
            records["STALE.US"]["hard_filters"]["H10"]["reason"],
            "nbbo_outside_close_window",
        )
        self.assertEqual(
            records["EXT.US"]["hard_filters"]["H8"]["reason"],
            "nbbo_not_regular_session",
        )
        self.assertEqual(result["ai_candidate_symbols"], [])

    def test_high_upper_bound_provider_failure_makes_result_partial(self) -> None:
        provider = _FakeNbboProvider({
            "AAA.US": _quote(
                "AAA.US",
                status="error",
                bid=None,
                ask=None,
                error_code="rate_limited",
            ),
        })
        result = QuantUniverseSelector(provider).select(
            [_candidate("AAA.US")],
            data_as_of=DATA_AS_OF,
            official_close=OFFICIAL_CLOSE,
        )
        self.assertEqual(result["status"], "partial")
        self.assertFalse(result["boundary_proven"])
        self.assertEqual(result["unresolved_symbols"], ["AAA.US"])
        self.assertEqual(result["ai_candidate_symbols"], [])
        self.assertEqual(result["nbbo_request_units"], 2)
        candidate = result["candidates"][0]
        self.assertEqual(candidate["hard_filters"]["H8"]["status"], "unresolved")
        self.assertIn("rate_limited", candidate["exclusion_reasons"])

    def test_retry_can_resolve_transient_provider_failure(self) -> None:
        provider = _FakeNbboProvider({
            "AAA.US": [
                _quote(
                    "AAA.US",
                    status="error",
                    bid=None,
                    ask=None,
                    error_code="timeout",
                ),
                _quote("AAA.US"),
            ]
        })
        result = QuantUniverseSelector(provider).select(
            [_candidate("AAA.US")],
            data_as_of=DATA_AS_OF,
            official_close=OFFICIAL_CLOSE,
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["ai_candidate_symbols"], ["AAA.US"])
        self.assertEqual(result["nbbo_request_units"], 2)

    def test_overfetched_low_upper_error_is_safely_pruned(self) -> None:
        candidates = [
            _candidate(f"S{index:03d}.US") for index in range(30)
        ] + [_candidate("ZZZ.US", indicators=_low_indicators())]
        provider = _FakeNbboProvider({
            "ZZZ.US": _quote(
                "ZZZ.US",
                status="error",
                bid=None,
                ask=None,
                error_code="timeout",
            )
        })
        result = QuantUniverseSelector(
            provider,
            policy=SelectionPolicy(batch_size=31),
        ).select(
            candidates,
            data_as_of=DATA_AS_OF,
            official_close=OFFICIAL_CLOSE,
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["unresolved_symbols"], [])
        low = {item["symbol"]: item for item in result["candidates"]}["ZZZ.US"]
        self.assertEqual(
            low["selection_status"],
            "quant_upper_bound_below_frontier",
        )
        self.assertEqual(low["nbbo_error"], "timeout")
        manifest_low = {
            item["symbol"]: item
            for item in result["selection_manifest"]["candidates"]
        }["ZZZ.US"]
        self.assertTrue(manifest_low["nbbo_queried"])
        self.assertIsNone(manifest_low["final_q"])
        self.assertEqual(
            manifest_low["prune_reason"],
            "quant_upper_bound_below_frontier",
        )

    def test_budget_exhaustion_cannot_publish_unproven_top_30(self) -> None:
        candidates = [
            _candidate(f"S{index:03d}.US") for index in range(31)
        ]
        result = QuantUniverseSelector(
            _FakeNbboProvider(),
            policy=SelectionPolicy(
                batch_size=10,
                max_nbbo_request_units=20,
            ),
        ).select(
            candidates,
            data_as_of=DATA_AS_OF,
            official_close=OFFICIAL_CLOSE,
        )
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["nbbo_request_units"], 20)
        self.assertEqual(result["ai_candidate_symbols"], [])
        self.assertFalse(result["boundary_proven"])
        pending = [
            item for item in result["candidates"]
            if item["selection_status"] == "nbbo_not_evaluated"
        ]
        self.assertEqual(len(pending), 11)
        self.assertTrue(all(
            "nbbo_budget_exhausted" in item["exclusion_reasons"]
            for item in pending
        ))
        manifest = result["selection_manifest"]
        self.assertFalse(manifest["boundary_proof"]["proven"])
        self.assertEqual(len(manifest["boundary_proof"]["obstacles"]), 11)
        pending_manifest = [
            item for item in manifest["candidates"]
            if item["selection_status"] == "nbbo_not_evaluated"
        ]
        self.assertTrue(all(
            not item["nbbo_queried"] and item["prune_reason"] is None
            for item in pending_manifest
        ))

    def test_low_upper_bound_candidates_need_no_nbbo_call(self) -> None:
        provider = _FakeNbboProvider()
        result = QuantUniverseSelector(provider).select(
            [_candidate("LOW.US", indicators=_low_indicators())],
            data_as_of=DATA_AS_OF,
            official_close=OFFICIAL_CLOSE,
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["nbbo_request_units"], 0)
        self.assertEqual(provider.calls, [])
        record = result["candidates"][0]
        self.assertEqual(
            record["selection_status"],
            "quant_upper_bound_below_threshold",
        )
        manifest_record = result["selection_manifest"]["candidates"][0]
        self.assertFalse(manifest_record["nbbo_queried"])
        self.assertIsNone(manifest_record["final_q"])
        self.assertEqual(
            manifest_record["prune_reason"],
            "quant_upper_bound_below_threshold",
        )
        self.assertEqual(
            result["selection_manifest"]["boundary_proof"]["frontier"],
            {"kind": "q_threshold", "q_minimum": 65.0},
        )

    def test_input_order_does_not_change_selection_manifest(self) -> None:
        candidates = [_candidate("BBB.US"), _candidate("AAA.US")]
        first = QuantUniverseSelector(_FakeNbboProvider()).select(
            candidates,
            data_as_of=DATA_AS_OF,
            official_close=OFFICIAL_CLOSE,
        )
        second = QuantUniverseSelector(_FakeNbboProvider()).select(
            list(reversed(candidates)),
            data_as_of=DATA_AS_OF,
            official_close=OFFICIAL_CLOSE,
        )
        self.assertEqual(
            first["selection_manifest_hash"],
            second["selection_manifest_hash"],
        )
        self.assertEqual(
            [item["candidate_quant_input_hash"] for item in first["candidates"]],
            [item["candidate_quant_input_hash"] for item in second["candidates"]],
        )

    def test_official_close_must_be_timezone_aware_and_match_date(self) -> None:
        selector = QuantUniverseSelector(_FakeNbboProvider())
        with self.assertRaises(UniverseSelectionError):
            selector.select(
                [],
                data_as_of=DATA_AS_OF,
                official_close=datetime(2026, 7, 24, 16),
            )
        with self.assertRaises(UniverseSelectionError):
            selector.select(
                [],
                data_as_of=DATA_AS_OF,
                official_close=OFFICIAL_CLOSE + timedelta(days=1),
            )


if __name__ == "__main__":
    unittest.main()
