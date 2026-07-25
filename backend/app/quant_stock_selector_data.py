"""Gated point-in-time source bundle adapter for quantitative selection."""

from __future__ import annotations

from datetime import date, datetime, timezone
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from .quant_stock_selector import QuantInputError, calculate_quant_indicators
from .quant_stock_selector_ai import AICandidateContext
from .quant_stock_selector_hashing import canonical_json
from .quant_stock_selector_metadata import (
    JsonProductMetadataProvider,
    ProductMetadataProvider,
    evaluate_product_metadata_gate,
    normalize_symbol,
)
from .quant_stock_selector_service import (
    CapturedInputSnapshot,
    CapturedQuantRun,
)
from .quant_stock_selector_snapshots import (
    MarketBarSnapshotError,
    MarketBarSnapshotStore,
)
from .quant_stock_selector_universe import (
    CandidateMarketData,
    NbboQuote,
    QuantUniverseSelector,
    SelectionPolicy,
    build_unified_candidate_pool,
)


SOURCE_BUNDLE_SCHEMA_VERSION = "quant-selector-source-bundle-v1"


class QuantSourceBundleError(ValueError):
    pass


def _datetime(value: Any, *, field: str) -> datetime:
    text = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise QuantSourceBundleError(f"{field} must be an ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise QuantSourceBundleError(f"{field} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _date(value: Any, *, field: str) -> date:
    try:
        return date.fromisoformat(str(value or ""))
    except ValueError as exc:
        raise QuantSourceBundleError(f"{field} must be an ISO date") from exc


def _mapping(value: Any, *, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise QuantSourceBundleError(f"{field} must be an object")
    return value


def _sequence(value: Any, *, field: str) -> Sequence[Any]:
    if not isinstance(value, list):
        raise QuantSourceBundleError(f"{field} must be an array")
    return value


def _optional_float(value: Any, *, field: str) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise QuantSourceBundleError(f"{field} must be numeric") from exc


class BundleNbboProvider:
    def __init__(self, payload: Mapping[str, Any]) -> None:
        self.source = str(payload.get("source") or "").strip()
        if not self.source:
            raise QuantSourceBundleError("nbbo.source is required")
        raw_quotes = _mapping(payload.get("quotes"), field="nbbo.quotes")
        self.quotes = {
            normalize_symbol(symbol): self._quote(symbol, value)
            for symbol, value in raw_quotes.items()
        }

    def _quote(self, symbol: str, value: Any) -> NbboQuote:
        payload = _mapping(value, field=f"nbbo.quotes.{symbol}")
        timestamp = payload.get("quote_timestamp")
        return NbboQuote(
            symbol=normalize_symbol(symbol),
            status=str(payload.get("status") or "").strip().lower(),
            bid=_optional_float(payload.get("bid"), field=f"{symbol}.bid"),
            ask=_optional_float(payload.get("ask"), field=f"{symbol}.ask"),
            quote_timestamp=(
                _datetime(timestamp, field=f"{symbol}.quote_timestamp")
                if timestamp is not None else None
            ),
            session=(
                str(payload["session"]).strip().lower()
                if payload.get("session") is not None else None
            ),
            source=self.source,
            error_code=(
                str(payload["error_code"]).strip()
                if payload.get("error_code") is not None else None
            ),
        )

    def fetch(self, symbols, *, data_as_of, official_close):
        return {
            symbol: self.quotes[symbol]
            for value in symbols
            if (symbol := normalize_symbol(value)) in self.quotes
        }


class JsonQuantRunInputProvider:
    """Build a run from frozen vendor facts while computing Q locally."""

    def __init__(
        self,
        bundle_path: Path | str,
        product_metadata_path: Path | str,
        *,
        metadata_provider: ProductMetadataProvider | None = None,
        market_bar_store: MarketBarSnapshotStore | None = None,
        selection_policy: SelectionPolicy | None = None,
    ) -> None:
        self.bundle_path = Path(bundle_path)
        self.metadata_provider = metadata_provider or JsonProductMetadataProvider(
            product_metadata_path
        )
        self.market_bar_store = market_bar_store or MarketBarSnapshotStore()
        self.selection_policy = selection_policy or SelectionPolicy()

    def _read(self) -> Mapping[str, Any]:
        try:
            payload = json.loads(self.bundle_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise QuantSourceBundleError(
                f"quant source bundle not found: {self.bundle_path}"
            ) from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise QuantSourceBundleError(
                f"quant source bundle is invalid: {type(exc).__name__}"
            ) from exc
        payload = _mapping(payload, field="bundle")
        canonical_json(payload)
        if payload.get("schema_version") != SOURCE_BUNDLE_SCHEMA_VERSION:
            raise QuantSourceBundleError("unsupported source bundle schema_version")
        return payload

    def _capture_bars(
        self,
        *,
        symbol: str,
        data_as_of: date,
        captured_at: datetime,
        source: str,
        adjusted: Sequence[Mapping[str, Any]],
        raw: Sequence[Mapping[str, Any]],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        adjusted_snapshot = self.market_bar_store.capture(
            symbol=symbol,
            period="day",
            adjust_type="forward_adjust",
            source=source,
            data_as_of=data_as_of,
            rows=adjusted,
        )
        raw_snapshot = self.market_bar_store.capture(
            symbol=symbol,
            period="day",
            adjust_type="no_adjust",
            source=source,
            data_as_of=data_as_of,
            rows=raw,
        )
        return adjusted_snapshot, raw_snapshot

    @staticmethod
    def _reference_snapshot(
        detail: Mapping[str, Any],
        *,
        captured_at: datetime,
        data_as_of: datetime,
    ) -> CapturedInputSnapshot:
        return CapturedInputSnapshot(
            snapshot_kind="market_bar_reference",
            symbol=detail["symbol"],
            source=detail["source"],
            schema_version=detail["snapshot_version"],
            captured_at=captured_at,
            data_as_of=data_as_of,
            payload={
                "adjust_type": detail["adjust_type"],
                "snapshot_id": detail["snapshot_id"],
                "reference": detail["reference"],
            },
        )

    def capture(self) -> CapturedQuantRun:
        payload = self._read()
        captured_at = _datetime(payload.get("captured_at"), field="captured_at")
        data_as_of = _date(payload.get("data_as_of"), field="data_as_of")
        official_close = _datetime(
            payload.get("official_close"), field="official_close"
        )
        if official_close.date() != data_as_of:
            raise QuantSourceBundleError("official_close date must equal data_as_of")
        catalog = [
            dict(_mapping(item, field="catalog item"))
            for item in _sequence(payload.get("catalog"), field="catalog")
        ]
        if not catalog:
            raise QuantSourceBundleError("catalog must not be empty")
        catalog_symbols = [normalize_symbol(item.get("symbol")) for item in catalog]
        if len(catalog_symbols) != len(set(catalog_symbols)):
            raise QuantSourceBundleError("catalog symbols must be unique")

        validation_samples = _mapping(
            payload.get("metadata_validation_samples"),
            field="metadata_validation_samples",
        )
        validation_symbols = [
            normalize_symbol(symbol)
            for values in validation_samples.values()
            for symbol in _sequence(values, field="metadata validation category")
        ]
        metadata_batch = self.metadata_provider.load(
            [*catalog_symbols, *validation_symbols],
            data_as_of=data_as_of,
        )
        metadata_gate = evaluate_product_metadata_gate(
            metadata_batch,
            validation_samples,
        )
        if not metadata_gate["ready"]:
            raise QuantSourceBundleError(
                "product metadata stage-0 gate failed: "
                + ",".join(metadata_gate["failures"])
            )

        bar_source = str(payload.get("bar_source") or "").strip()
        if not bar_source:
            raise QuantSourceBundleError("bar_source is required")
        all_market_data = _mapping(payload.get("market_data"), field="market_data")
        normalized_market_data = {
            normalize_symbol(symbol): _mapping(value, field=f"market_data.{symbol}")
            for symbol, value in all_market_data.items()
        }
        spy_payload = normalized_market_data.get("SPY.US")
        if spy_payload is None:
            raise QuantSourceBundleError("SPY.US market data is required")
        spy_adjusted = list(_sequence(
            spy_payload.get("forward_adjusted_bars"),
            field="SPY.US.forward_adjusted_bars",
        ))
        spy_raw = list(_sequence(
            spy_payload.get("unadjusted_bars"),
            field="SPY.US.unadjusted_bars",
        ))
        spy_adjusted_snapshot, spy_raw_snapshot = self._capture_bars(
            symbol="SPY.US",
            data_as_of=data_as_of,
            captured_at=captured_at,
            source=bar_source,
            adjusted=spy_adjusted,
            raw=spy_raw,
        )

        facts_by_symbol = {}
        adjusted_by_symbol = {"SPY.US": spy_adjusted}
        bar_references = [spy_adjusted_snapshot, spy_raw_snapshot]
        tradeability_payload = {
            symbol: {"status": "market_data_missing"}
            for symbol in catalog_symbols
        }
        invalid_bar_inputs = {}
        for symbol in catalog_symbols:
            raw_facts = normalized_market_data.get(symbol)
            if raw_facts is None:
                continue
            tradeability_payload[symbol] = {
                key: raw_facts.get(key)
                for key in (
                    "trade_status",
                    "directly_buyable",
                    "last_price",
                    "price_data_as_of",
                    "bar_data_as_of",
                )
            }
            adjusted = list(raw_facts.get("forward_adjusted_bars") or [])
            raw = list(raw_facts.get("unadjusted_bars") or [])
            indicators = None
            indicator_error = None
            if not adjusted or not raw:
                indicator_error = "market_bars_missing"
                invalid_bar_inputs[symbol] = {
                    "forward_adjusted_bars": adjusted,
                    "unadjusted_bars": raw,
                    "reason": indicator_error,
                }
            else:
                try:
                    adjusted_snapshot, raw_snapshot = self._capture_bars(
                        symbol=symbol,
                        data_as_of=data_as_of,
                        captured_at=captured_at,
                        source=bar_source,
                        adjusted=adjusted,
                        raw=raw,
                    )
                    bar_references.extend([adjusted_snapshot, raw_snapshot])
                    indicators = calculate_quant_indicators(
                        adjusted,
                        raw,
                        spy_adjusted,
                    )
                    adjusted_by_symbol[symbol] = adjusted
                except QuantInputError as exc:
                    indicator_error = exc.reason
                    invalid_bar_inputs[symbol] = {
                        "forward_adjusted_bars": adjusted,
                        "unadjusted_bars": raw,
                        "reason": indicator_error,
                    }
                except MarketBarSnapshotError:
                    indicator_error = "invalid_market_bars"
                    invalid_bar_inputs[symbol] = {
                        "forward_adjusted_bars": adjusted,
                        "unadjusted_bars": raw,
                        "reason": indicator_error,
                    }
            facts_by_symbol[symbol] = CandidateMarketData(
                trade_status=(
                    str(raw_facts.get("trade_status") or "").strip().lower()
                    or None
                ),
                directly_buyable=raw_facts.get("directly_buyable") is True,
                last_price=_optional_float(
                    raw_facts.get("last_price"), field=f"{symbol}.last_price"
                ),
                price_data_as_of=(
                    _date(raw_facts["price_data_as_of"], field=f"{symbol}.price_data_as_of")
                    if raw_facts.get("price_data_as_of") else None
                ),
                bar_data_as_of=(
                    _date(raw_facts["bar_data_as_of"], field=f"{symbol}.bar_data_as_of")
                    if raw_facts.get("bar_data_as_of") else None
                ),
                valid_daily_bars=min(len(adjusted), len(raw)),
                indicators=indicators,
                indicator_error=indicator_error,
            )

        candidates = build_unified_candidate_pool(
            catalog,
            metadata_batch=metadata_batch,
            market_data=facts_by_symbol,
            data_as_of=data_as_of,
        )
        nbbo_payload = _mapping(payload.get("nbbo"), field="nbbo")
        selector = QuantUniverseSelector(
            BundleNbboProvider(nbbo_payload),
            policy=self.selection_policy,
        )
        quant_selection = selector.select(
            candidates,
            data_as_of=data_as_of,
            official_close=official_close,
        )

        input_snapshots = [
            CapturedInputSnapshot(
                snapshot_kind="security_catalog",
                source=str(payload.get("catalog_source") or "bundle"),
                schema_version=SOURCE_BUNDLE_SCHEMA_VERSION,
                captured_at=captured_at,
                data_as_of=official_close,
                payload=catalog,
            ),
            CapturedInputSnapshot(
                snapshot_kind="product_metadata",
                source=metadata_batch.source,
                schema_version=metadata_gate["schema_version"],
                captured_at=captured_at,
                data_as_of=official_close,
                payload={
                    "gate": metadata_gate,
                    "manifest": metadata_batch.to_manifest(),
                    "records": [
                        metadata_batch.records[symbol].to_dict()
                        for symbol in sorted(metadata_batch.records)
                    ],
                },
            ),
            CapturedInputSnapshot(
                snapshot_kind="tradeability",
                source=str(payload.get("tradeability_source") or "bundle"),
                schema_version=SOURCE_BUNDLE_SCHEMA_VERSION,
                captured_at=captured_at,
                data_as_of=official_close,
                payload=tradeability_payload,
            ),
            CapturedInputSnapshot(
                snapshot_kind="nbbo",
                source=str(nbbo_payload.get("source") or "bundle"),
                schema_version=SOURCE_BUNDLE_SCHEMA_VERSION,
                captured_at=captured_at,
                data_as_of=official_close,
                payload=nbbo_payload,
            ),
            *[
                self._reference_snapshot(
                    detail,
                    captured_at=captured_at,
                    data_as_of=official_close,
                )
                for detail in {
                    item["snapshot_id"]: item for item in bar_references
                }.values()
            ],
        ]
        if invalid_bar_inputs:
            input_snapshots.append(CapturedInputSnapshot(
                snapshot_kind="invalid_market_bar_inputs",
                source=bar_source,
                schema_version=SOURCE_BUNDLE_SCHEMA_VERSION,
                captured_at=captured_at,
                data_as_of=official_close,
                payload=invalid_bar_inputs,
            ))

        candidate_payloads = {
            item["symbol"]: item for item in quant_selection["candidates"]
        }
        ai_contexts = {}
        spy_closes = [float(item["close"]) for item in spy_adjusted]
        spy_state = {
            "data_as_of": data_as_of.isoformat(),
            "return_20d": spy_closes[-1] / spy_closes[-21] - 1.0,
            "return_60d": spy_closes[-1] / spy_closes[-61] - 1.0,
            "bar_snapshot_reference": spy_adjusted_snapshot["reference"],
        }
        for symbol in quant_selection["ai_candidate_symbols"]:
            candidate = candidate_payloads[symbol]
            source_facts = normalized_market_data[symbol]
            news = dict(source_facts.get("news") or {
                "status": "unavailable",
                "news_items": [],
            })
            events = dict(source_facts.get("events") or {
                "status": "unavailable",
                "events": [],
            })
            missing_fields = []
            if news.get("status") != "available":
                missing_fields.append("news")
            if events.get("status") not in {"available", "not_applicable"}:
                missing_fields.append("events")
            metadata = candidate["metadata"]
            if metadata.get("exposure_direction") == "unknown":
                missing_fields.append("exposure_direction")
            if metadata.get("asset_class") == "equity_etf" and metadata.get("leverage") is None:
                missing_fields.append("leverage")
            ai_contexts[symbol] = AICandidateContext(
                symbol=symbol,
                name=candidate["name"],
                data_as_of=data_as_of.isoformat(),
                product_metadata=metadata,
                quant_score=candidate["quant_score"],
                indicators=candidate["indicators"],
                daily_bars=adjusted_by_symbol[symbol],
                spy_state=spy_state,
                news_snapshot=news,
                event_snapshot=events,
                missing_fields=missing_fields,
            )
            input_snapshots.extend([
                CapturedInputSnapshot(
                    snapshot_kind="news",
                    symbol=symbol,
                    source=str(news.get("source") or "bundle"),
                    schema_version=SOURCE_BUNDLE_SCHEMA_VERSION,
                    captured_at=captured_at,
                    data_as_of=official_close,
                    payload=news,
                ),
                CapturedInputSnapshot(
                    snapshot_kind="events",
                    symbol=symbol,
                    source=str(events.get("source") or "bundle"),
                    schema_version=SOURCE_BUNDLE_SCHEMA_VERSION,
                    captured_at=captured_at,
                    data_as_of=official_close,
                    payload=events,
                ),
            ])

        return CapturedQuantRun(
            data_as_of=data_as_of,
            quant_selection=quant_selection,
            ai_contexts=ai_contexts,
            input_snapshots=input_snapshots,
            required_inputs_complete=quant_selection["status"] == "completed",
            errors=(
                [] if quant_selection["status"] == "completed"
                else ["quant_candidate_boundary_unproven"]
            ),
        )


def build_configured_quant_selection_service():
    """Build the runtime service only when every gated dependency is configured."""
    from .config import get_settings
    from .quant_stock_selector_ai import (
        DeepSeekQuantSelectorProvider,
        QuantAISelectionService,
    )
    from .quant_stock_selector_service import QuantSelectionService
    from .repositories import load_ai_credentials

    settings = get_settings()
    if settings.quant_selector_bundle_path is None:
        raise QuantSourceBundleError("QUANT_SELECTOR_BUNDLE_PATH is not configured")
    if settings.quant_selector_product_metadata_path is None:
        raise QuantSourceBundleError(
            "QUANT_SELECTOR_PRODUCT_METADATA_PATH is not configured"
        )
    credentials = load_ai_credentials()
    api_key = str(credentials.get("DEEPSEEK_API_KEY") or "").strip()
    if not api_key:
        raise QuantSourceBundleError("DEEPSEEK_API_KEY is not configured")
    ai_provider = DeepSeekQuantSelectorProvider(
        api_key,
        base_url=settings.deepseek_base_url,
    )
    return QuantSelectionService(
        JsonQuantRunInputProvider(
            settings.quant_selector_bundle_path,
            settings.quant_selector_product_metadata_path,
        ),
        QuantAISelectionService(ai_provider),
    )
