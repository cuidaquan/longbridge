"""Point-in-time product metadata contract for quant selection."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from enum import Enum
import json
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from .quant_stock_selector_hashing import canonical_sha256


PRODUCT_METADATA_SCHEMA_VERSION = "quant-selector-product-metadata-v2"
REQUIRED_VALIDATION_CATEGORIES = (
    "common_stock",
    "broad_equity_etf",
    "sector_equity_etf",
    "leveraged_long_etf",
    "inverse_etf",
    "leveraged_inverse_etf",
    "bond_etf",
    "commodity_etf",
    "warrant",
    "unit",
)
MINIMUM_VALIDATION_SAMPLES_PER_CATEGORY = 5


class ProductMetadataError(ValueError):
    pass


class AssetClass(str, Enum):
    COMMON_STOCK = "common_stock"
    EQUITY_ETF = "equity_etf"
    BOND_ETF = "bond_etf"
    COMMODITY_ETF = "commodity_etf"
    WARRANT = "warrant"
    RIGHT = "right"
    UNIT = "unit"
    INDEX = "index"
    UNKNOWN = "unknown"


class ExposureDirection(str, Enum):
    LONG = "long"
    INVERSE = "inverse"
    UNKNOWN = "unknown"
    NOT_APPLICABLE = "not_applicable"


ELIGIBLE_ASSET_CLASSES = frozenset({
    AssetClass.COMMON_STOCK,
    AssetClass.EQUITY_ETF,
})


@dataclass(frozen=True)
class ProductMetadata:
    symbol: str
    raw_asset_class: str
    asset_class: AssetClass
    exchange: str
    exposure_direction: ExposureDirection
    leverage: float | None
    effective_from: str
    effective_to: str | None
    source: str
    source_version: str
    captured_at: str
    mapping_version: str

    @property
    def eligible(self) -> bool:
        return self.asset_class in ELIGIBLE_ASSET_CLASSES

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["asset_class"] = self.asset_class.value
        payload["exposure_direction"] = self.exposure_direction.value
        return payload


@dataclass(frozen=True)
class ProductMetadataBatch:
    data_as_of: str
    source: str
    source_version: str
    captured_at: str
    mapping_version: str
    license: str
    refresh_cadence: str
    historical_semantics: str
    payload_hash: str
    records: Mapping[str, ProductMetadata]
    missing_symbols: tuple[str, ...]

    def to_manifest(self) -> dict[str, Any]:
        return {
            "data_as_of": self.data_as_of,
            "captured_at": self.captured_at,
            "historical_semantics": self.historical_semantics,
            "license": self.license,
            "mapping_version": self.mapping_version,
            "missing_symbols": list(self.missing_symbols),
            "payload_hash": self.payload_hash,
            "record_count": len(self.records),
            "refresh_cadence": self.refresh_cadence,
            "schema_version": PRODUCT_METADATA_SCHEMA_VERSION,
            "source": self.source,
            "source_version": self.source_version,
        }


class ProductMetadataProvider(Protocol):
    def load(
        self,
        symbols: Sequence[str],
        *,
        data_as_of: date,
    ) -> ProductMetadataBatch:
        ...


def evaluate_product_metadata_gate(
    batch: ProductMetadataBatch,
    validation_samples: Mapping[str, Sequence[str]],
) -> dict[str, Any]:
    """Validate the design's ten-category implementation gate."""
    failures = []
    category_results = {}

    try:
        normalized_captured_at = _as_timestamp(
            batch.captured_at,
            field="captured_at",
        )
    except ProductMetadataError:
        normalized_captured_at = None
        failures.append("batch_evidence:captured_at_invalid")
    if not str(batch.mapping_version or "").strip():
        failures.append("batch_evidence:mapping_version_missing")

    def has_complete_evidence(record: ProductMetadata) -> bool:
        return (
            bool(str(record.raw_asset_class or "").strip())
            and bool(str(record.source or "").strip())
            and bool(str(record.source_version or "").strip())
            and record.captured_at == normalized_captured_at
            and bool(str(record.mapping_version or "").strip())
            and record.mapping_version == batch.mapping_version
            and record.source == batch.source
            and record.source_version == batch.source_version
        )

    def matches(category: str, record: ProductMetadata) -> bool:
        if category == "common_stock":
            return (
                record.asset_class == AssetClass.COMMON_STOCK
                and record.exposure_direction == ExposureDirection.NOT_APPLICABLE
                and record.leverage is None
            )
        if category in {"broad_equity_etf", "sector_equity_etf"}:
            return (
                record.asset_class == AssetClass.EQUITY_ETF
                and record.exposure_direction == ExposureDirection.LONG
                and record.leverage == 1.0
            )
        if category == "leveraged_long_etf":
            return (
                record.asset_class == AssetClass.EQUITY_ETF
                and record.exposure_direction == ExposureDirection.LONG
                and record.leverage is not None
                and record.leverage > 1.0
            )
        if category == "inverse_etf":
            return (
                record.asset_class == AssetClass.EQUITY_ETF
                and record.exposure_direction == ExposureDirection.INVERSE
                and record.leverage == 1.0
            )
        if category == "leveraged_inverse_etf":
            return (
                record.asset_class == AssetClass.EQUITY_ETF
                and record.exposure_direction == ExposureDirection.INVERSE
                and record.leverage is not None
                and record.leverage > 1.0
            )
        expected_assets = {
            "bond_etf": AssetClass.BOND_ETF,
            "commodity_etf": AssetClass.COMMODITY_ETF,
            "warrant": AssetClass.WARRANT,
            "unit": AssetClass.UNIT,
        }
        return record.asset_class == expected_assets[category]

    unexpected = sorted(
        set(validation_samples) - set(REQUIRED_VALIDATION_CATEGORIES)
    )
    if unexpected:
        failures.append(f"unexpected_categories:{','.join(unexpected)}")
    for category in REQUIRED_VALIDATION_CATEGORIES:
        symbols = sorted({
            normalize_symbol(symbol)
            for symbol in validation_samples.get(category, [])
        })
        category_failures = []
        if len(symbols) < MINIMUM_VALIDATION_SAMPLES_PER_CATEGORY:
            category_failures.append("insufficient_samples")
        for symbol in symbols:
            record = batch.records.get(symbol)
            if record is None:
                category_failures.append(f"missing:{symbol}")
            elif not matches(category, record):
                category_failures.append(f"mismatch:{symbol}")
            elif not has_complete_evidence(record):
                category_failures.append(f"evidence_missing:{symbol}")
        category_results[category] = {
            "failures": category_failures,
            "passed": not category_failures,
            "sample_count": len(symbols),
            "symbols": symbols,
        }
        failures.extend(
            f"{category}:{failure}" for failure in category_failures
        )
    return {
        "category_results": category_results,
        "failures": failures,
        "minimum_samples_per_category": (
            MINIMUM_VALIDATION_SAMPLES_PER_CATEGORY
        ),
        "mapping_version": batch.mapping_version,
        "ready": not failures,
        "required_categories": list(REQUIRED_VALIDATION_CATEGORIES),
        "schema_version": PRODUCT_METADATA_SCHEMA_VERSION,
        "source": batch.source,
        "source_version": batch.source_version,
    }


def normalize_symbol(value: Any) -> str:
    normalized = str(value or "").strip().upper()
    if not normalized:
        raise ProductMetadataError("symbol is required")
    return normalized


def _as_date(value: Any, *, field: str) -> date:
    if isinstance(value, datetime):
        if value.tzinfo is not None and value.utcoffset() is not None:
            value = value.astimezone(timezone.utc)
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise ProductMetadataError(f"{field} must be an ISO date") from exc


def _as_timestamp(value: Any, *, field: str) -> str:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ProductMetadataError(
                f"{field} must be an ISO timezone-aware timestamp"
            ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProductMetadataError(
            f"{field} must be an ISO timezone-aware timestamp"
        )
    return parsed.astimezone(timezone.utc).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")


def _required_text(payload: Mapping[str, Any], field: str) -> str:
    value = str(payload.get(field) or "").strip()
    if not value:
        raise ProductMetadataError(f"{field} is required")
    return value


def _optional_leverage(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        leverage = float(value)
    except (TypeError, ValueError) as exc:
        raise ProductMetadataError("leverage must be numeric") from exc
    if leverage <= 0:
        raise ProductMetadataError("leverage must be positive")
    return leverage


class JsonProductMetadataProvider:
    """Read an authoritative point-in-time vendor export from JSON."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def _read_payload(self) -> Mapping[str, Any]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ProductMetadataError(
                f"product metadata file not found: {self.path}"
            ) from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise ProductMetadataError(
                f"product metadata file is invalid: {type(exc).__name__}"
            ) from exc
        if not isinstance(payload, Mapping):
            raise ProductMetadataError("product metadata root must be an object")
        return payload

    def load(
        self,
        symbols: Sequence[str],
        *,
        data_as_of: date,
    ) -> ProductMetadataBatch:
        requested = tuple(sorted({normalize_symbol(item) for item in symbols}))
        payload = self._read_payload()
        if payload.get("schema_version") != PRODUCT_METADATA_SCHEMA_VERSION:
            raise ProductMetadataError("unsupported product metadata schema_version")
        source = _required_text(payload, "source")
        source_version = _required_text(payload, "source_version")
        captured_at = _as_timestamp(payload.get("captured_at"), field="captured_at")
        mapping_version = _required_text(payload, "mapping_version")
        license_name = _required_text(payload, "license")
        refresh_cadence = _required_text(payload, "refresh_cadence")
        historical_semantics = _required_text(payload, "historical_semantics")
        if historical_semantics != "point_in_time":
            raise ProductMetadataError(
                "historical_semantics must be point_in_time"
            )
        raw_items = payload.get("items")
        if not isinstance(raw_items, list):
            raise ProductMetadataError("items must be an array")

        records_by_symbol: dict[str, list[ProductMetadata]] = {}
        for raw in raw_items:
            if not isinstance(raw, Mapping):
                raise ProductMetadataError("each metadata item must be an object")
            symbol = normalize_symbol(raw.get("symbol"))
            try:
                asset_class = AssetClass(str(raw.get("asset_class") or ""))
            except ValueError as exc:
                raise ProductMetadataError(
                    f"unsupported asset_class for {symbol}"
                ) from exc
            try:
                exposure = ExposureDirection(
                    str(raw.get("exposure_direction") or "")
                )
            except ValueError as exc:
                raise ProductMetadataError(
                    f"unsupported exposure_direction for {symbol}"
                ) from exc
            leverage = _optional_leverage(raw.get("leverage"))
            if asset_class == AssetClass.COMMON_STOCK:
                if exposure != ExposureDirection.NOT_APPLICABLE:
                    raise ProductMetadataError(
                        f"common stock {symbol} must use not_applicable direction"
                    )
                if leverage is not None:
                    raise ProductMetadataError(
                        f"common stock {symbol} must not define leverage"
                    )
            elif asset_class == AssetClass.EQUITY_ETF:
                if exposure == ExposureDirection.NOT_APPLICABLE:
                    raise ProductMetadataError(
                        f"equity ETF {symbol} must define ETF direction state"
                    )
            effective_from = _as_date(
                raw.get("effective_from"),
                field=f"{symbol}.effective_from",
            )
            effective_to_value = raw.get("effective_to")
            effective_to = (
                None
                if effective_to_value in (None, "")
                else _as_date(
                    effective_to_value,
                    field=f"{symbol}.effective_to",
                )
            )
            if effective_to is not None and effective_to < effective_from:
                raise ProductMetadataError(
                    f"effective_to precedes effective_from for {symbol}"
                )
            record = ProductMetadata(
                symbol=symbol,
                raw_asset_class=_required_text(raw, "raw_asset_class"),
                asset_class=asset_class,
                exchange=_required_text(raw, "exchange").upper(),
                exposure_direction=exposure,
                leverage=leverage,
                effective_from=effective_from.isoformat(),
                effective_to=(
                    effective_to.isoformat() if effective_to is not None else None
                ),
                source=source,
                source_version=source_version,
                captured_at=captured_at,
                mapping_version=mapping_version,
            )
            records_by_symbol.setdefault(symbol, []).append(record)

        selected: dict[str, ProductMetadata] = {}
        for symbol in requested:
            matching = []
            for record in records_by_symbol.get(symbol, []):
                effective_from = date.fromisoformat(record.effective_from)
                effective_to = (
                    date.fromisoformat(record.effective_to)
                    if record.effective_to is not None
                    else None
                )
                if effective_from <= data_as_of and (
                    effective_to is None or data_as_of <= effective_to
                ):
                    matching.append(record)
            if len(matching) > 1:
                raise ProductMetadataError(
                    f"overlapping point-in-time records for {symbol}"
                )
            if matching:
                selected[symbol] = matching[0]

        missing = tuple(symbol for symbol in requested if symbol not in selected)
        hash_payload = {
            "data_as_of": data_as_of,
            "captured_at": captured_at,
            "historical_semantics": historical_semantics,
            "license": license_name,
            "mapping_version": mapping_version,
            "missing_symbols": list(missing),
            "records": [selected[symbol].to_dict() for symbol in sorted(selected)],
            "refresh_cadence": refresh_cadence,
            "requested_symbols": list(requested),
            "schema_version": PRODUCT_METADATA_SCHEMA_VERSION,
            "source": source,
            "source_version": source_version,
        }
        return ProductMetadataBatch(
            data_as_of=data_as_of.isoformat(),
            source=source,
            source_version=source_version,
            captured_at=captured_at,
            mapping_version=mapping_version,
            license=license_name,
            refresh_cadence=refresh_cadence,
            historical_semantics=historical_semantics,
            payload_hash=canonical_sha256(hash_payload),
            records=selected,
            missing_symbols=missing,
        )
