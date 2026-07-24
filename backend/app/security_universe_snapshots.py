from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from typing import Any, Callable, Dict, Iterable, List, Optional
from uuid import uuid4
from zoneinfo import ZoneInfo

from .db import get_connection
from .external_service_resilience import (
    ExternalServiceTimeoutError,
    run_external_call,
)
from .security_catalog import get_security_catalog_service
from .stock_candidate_data import (
    get_security_static_info,
    get_security_tradeability,
    is_market_trading_day,
)
from .stock_picker_ai_snapshots import sanitize_error


SNAPSHOT_VERSION = "security-universe-snapshot-v1"
SNAPSHOT_SOURCE = "longbridge-official-security-list"
SNAPSHOT_COVERAGE_VERSION = "security-universe-coverage-v1"
SNAPSHOT_COMPARISON_VERSION = "security-universe-comparison-v1"
CLASSIFICATION_VERSION = "security-universe-classification-v1"
CLASSIFICATION_SOURCE = "longbridge-security-static-info"
CLASSIFICATION_COVERAGE_VERSION = (
    "security-universe-classification-coverage-v1"
)
TRADEABILITY_VERSION = "security-universe-tradeability-v1"
TRADEABILITY_SOURCE = "longbridge-security-quote"
TRADEABILITY_COVERAGE_VERSION = (
    "security-universe-tradeability-coverage-v1"
)
SOURCE_PATH = "/v1/quote/get_security_list"
SOURCE_CATEGORY = "Overnight"
STATIC_INFO_BATCH_SIZE = 500
QUOTE_BATCH_SIZE = 500
SUPPORTED_MARKETS = ("US", "HK", "CN")
MARKET_TIMEZONES = {
    "US": "America/New_York",
    "HK": "Asia/Hong_Kong",
    "CN": "Asia/Shanghai",
}
MINIMUM_SECURITY_COUNTS = {
    "US": 1000,
    "HK": 1000,
    "CN": 1000,
}
AUTO_CAPTURE_MARKETS = ("US", "HK")
AUTO_CAPTURE_LOCAL_HOUR = 17
CAPTURE_CLAIM_LEASE_MINUTES = 30
COMPARISON_METADATA_FIELDS = ("name", "name_en", "name_hk")
BOARD_CLASSIFICATION = {
    "usmain": ("listed_equity_board", True, None),
    "hkequity": ("listed_equity_board", True, None),
    "shmainconnect": ("listed_equity_board", True, None),
    "shmainnonconnect": ("listed_equity_board", True, None),
    "shstar": ("listed_equity_board", True, None),
    "szmainconnect": ("listed_equity_board", True, None),
    "szmainnonconnect": ("listed_equity_board", True, None),
    "szgemconnect": ("listed_equity_board", True, None),
    "szgemnonconnect": ("listed_equity_board", True, None),
    "uspink": ("otc_equity_board", False, "otc_board"),
    "usoption": ("derivative_board", False, "derivative_board"),
    "usoptions": ("derivative_board", False, "derivative_board"),
    "hkwarrant": ("derivative_board", False, "derivative_board"),
    "usdji": ("index_board", False, "index_board"),
    "usnsdq": ("index_board", False, "index_board"),
    "hkhs": ("index_board", False, "index_board"),
    "cnix": ("index_board", False, "index_board"),
    "spxindex": ("index_board", False, "index_board"),
    "vixindex": ("index_board", False, "index_board"),
    "ussector": ("sector_board", False, "sector_board"),
    "hksector": ("sector_board", False, "sector_board"),
    "cnsector": ("sector_board", False, "sector_board"),
    "hkpreipo": ("pre_ipo_board", False, "pre_ipo_board"),
}
MARKET_BOARD_ALLOWLIST = {
    "US": {
        "usmain", "uspink", "usoption", "usoptions", "usdji",
        "usnsdq", "ussector", "spxindex", "vixindex",
    },
    "HK": {"hkequity", "hkpreipo", "hkwarrant", "hkhs", "hksector"},
    "CN": {
        "shmainconnect", "shmainnonconnect", "shstar", "cnix",
        "cnsector", "szmainconnect", "szmainnonconnect",
        "szgemconnect", "szgemnonconnect",
    },
}
KNOWN_TRADE_STATUSES = {
    "codemoved",
    "delisted",
    "expired",
    "fuse",
    "halted",
    "normal",
    "preparelist",
    "splitstockhalts",
    "suspend",
    "tobeopened",
    "warrantpreparelist",
}


class SecurityUniverseSnapshotNotFoundError(LookupError):
    def __init__(self, snapshot_id: str) -> None:
        super().__init__(f"证券目录快照不存在: {snapshot_id}")
        self.snapshot_id = snapshot_id


class SecurityUniverseSnapshotService:
    """Persist immutable point-in-time copies of official security lists."""

    def __init__(
        self,
        catalog_loader: Optional[Callable[[str], List[dict]]] = None,
        connection_factory: Callable = get_connection,
        clock: Callable[[], datetime] = (
            lambda: datetime.now(timezone.utc)
        ),
        minimum_security_counts: Optional[Dict[str, int]] = None,
        trading_day_loader: Callable = is_market_trading_day,
        static_info_loader: Callable = get_security_static_info,
        static_info_batch_size: int = STATIC_INFO_BATCH_SIZE,
        tradeability_loader: Callable = get_security_tradeability,
        quote_batch_size: int = QUOTE_BATCH_SIZE,
    ) -> None:
        self.catalog_loader = catalog_loader or self._refresh_catalog
        self.connection_factory = connection_factory
        self.clock = clock
        self.minimum_security_counts = (
            dict(minimum_security_counts)
            if minimum_security_counts is not None
            else dict(MINIMUM_SECURITY_COUNTS)
        )
        self.trading_day_loader = trading_day_loader
        self.static_info_loader = static_info_loader
        if int(static_info_batch_size) < 1:
            raise ValueError("static_info_batch_size 必须大于 0")
        self.static_info_batch_size = int(static_info_batch_size)
        self.tradeability_loader = tradeability_loader
        if int(quote_batch_size) < 1:
            raise ValueError("quote_batch_size 必须大于 0")
        self.quote_batch_size = int(quote_batch_size)
        self._trading_day_cache: Dict[tuple[str, date], bool] = {}

    def capture_market(
        self,
        market: str,
        *,
        persist: bool = True,
    ) -> Dict[str, Any]:
        normalized_market = self._normalize_market(market)
        captured_at = self._as_utc(self.clock())
        observation_date = captured_at.astimezone(
            ZoneInfo(MARKET_TIMEZONES[normalized_market])
        ).date()
        if persist:
            existing = self._find_existing_summary(
                normalized_market,
                observation_date,
            )
            if existing is not None:
                classification = self.capture_classification(
                    existing["snapshot_id"],
                    persist=True,
                )
                tradeability = self._capture_tradeability_if_ready(
                    existing["snapshot_id"], classification, persist=True
                )
                return {
                    **existing,
                    "status": "already_captured",
                    "persisted": False,
                    "classification": classification,
                    "tradeability": tradeability,
                }
        raw_items = self.catalog_loader(normalized_market)
        items = self._normalize_items(raw_items, normalized_market)
        security_count = len(items)
        minimum_count = self.minimum_security_counts.get(
            normalized_market,
            1,
        )
        if security_count < minimum_count:
            raise ValueError(
                f"{normalized_market} 证券目录只有 {security_count} 条，"
                f"低于最低完整性门槛 {minimum_count}"
            )

        payload = self._payload(
            normalized_market,
            captured_at,
            observation_date,
            items,
        )
        payload_hash = self._payload_hash(payload)
        snapshot_id = uuid4().hex
        status = "captured"
        if persist:
            persisted_id, inserted = self._save_snapshot({
                "snapshot_id": snapshot_id,
                "captured_at": captured_at,
                "observation_date": observation_date,
                "market": normalized_market,
                "security_count": security_count,
                "payload_hash": payload_hash,
                "payload": payload,
            })
            if not inserted:
                existing = self._find_existing_summary(
                    normalized_market,
                    observation_date,
                )
                if existing is None:
                    raise RuntimeError("证券目录快照写入状态不一致")
                classification = self.capture_classification(
                    existing["snapshot_id"],
                    persist=True,
                )
                tradeability = self._capture_tradeability_if_ready(
                    existing["snapshot_id"], classification, persist=True
                )
                return {
                    **existing,
                    "status": "already_captured",
                    "persisted": False,
                    "classification": classification,
                    "tradeability": tradeability,
                }
            snapshot_id = persisted_id

        result = {
            "snapshot_id": snapshot_id,
            "snapshot_version": SNAPSHOT_VERSION,
            "source": SNAPSHOT_SOURCE,
            "source_query": self._source_query(normalized_market),
            "captured_at": captured_at.isoformat(),
            "observation_date": observation_date.isoformat(),
            "market": normalized_market,
            "security_count": security_count,
            "payload_hash": payload_hash,
            "status": status,
            "persisted": persist and status == "captured",
        }
        if persist:
            classification = self.capture_classification(snapshot_id)
            tradeability = self._capture_tradeability_if_ready(
                snapshot_id, classification, persist=True
            )
        else:
            classification = self._capture_classification_payload(
                source_snapshot=result,
                source_items=items,
                persist=False,
            )
            tradeability = self._capture_tradeability_if_ready(
                result, classification, persist=False
            )
        return {
            **result,
            "classification": classification,
            "tradeability": tradeability,
        }

    def capture_due(self, *, persist: bool = True) -> Dict[str, Any]:
        now = self._as_utc(self.clock())
        captured = []
        skipped = []
        errors = []

        for market in AUTO_CAPTURE_MARKETS:
            local_now = now.astimezone(ZoneInfo(MARKET_TIMEZONES[market]))
            observation_date = local_now.date()
            scope = {
                "market": market,
                "observation_date": observation_date.isoformat(),
            }
            if local_now.hour < AUTO_CAPTURE_LOCAL_HOUR:
                skipped.append({**scope, "reason": "session_not_closed"})
                continue
            existing = self._find_existing_summary(
                market,
                observation_date,
            )
            existing_classification = (
                self._find_classification_summary(existing["snapshot_id"])
                if existing
                else None
            )
            existing_tradeability = (
                self._find_tradeability_summary(
                    existing_classification["classification_snapshot_id"]
                )
                if existing_classification
                else None
            )
            if existing and existing_classification and existing_tradeability:
                if persist:
                    self._reconcile_existing_run(existing, now)
                skipped.append({**scope, "reason": "already_captured"})
                continue
            if (
                existing
                and existing_classification
                and not existing_classification["ready_for_research_universe"]
            ):
                if persist:
                    self._reconcile_existing_run(existing, now)
                skipped.append({
                    **scope,
                    "reason": "classification_not_ready",
                })
                continue
            if existing is None:
                try:
                    if not self._is_market_trading_day(
                        market,
                        observation_date,
                    ):
                        skipped.append({**scope, "reason": "market_closed"})
                        continue
                except Exception as exc:
                    error = sanitize_error(exc)
                    errors.append({
                        **scope,
                        "reason": "trading_calendar_unavailable",
                        "error": error,
                    })
                    continue

            claim_id = None
            if persist:
                claim_id = self._claim_capture(
                    market,
                    observation_date,
                    now,
                )
                if claim_id is None:
                    skipped.append({
                        **scope,
                        "reason": "capture_claim_unavailable",
                    })
                    continue
            try:
                if existing is not None:
                    classification = self.capture_classification(
                        existing["snapshot_id"],
                        persist=persist,
                    )
                    tradeability = self._capture_tradeability_if_ready(
                        existing["snapshot_id"],
                        classification,
                        persist=persist,
                    )
                    result = {
                        **existing,
                        "status": (
                            "classification_captured"
                            if existing_classification is None
                            else "tradeability_captured"
                        ),
                        "persisted": False,
                        "classification": classification,
                        "tradeability": tradeability,
                    }
                else:
                    result = self.capture_market(market, persist=persist)
            except Exception as exc:
                error = sanitize_error(exc)
                if claim_id is not None:
                    self._finish_capture_claim(
                        market,
                        observation_date,
                        claim_id,
                        status="failed",
                        completed_at=self._as_utc(self.clock()),
                        error=error,
                    )
                errors.append({**scope, "error": error})
                continue

            if claim_id is not None:
                self._finish_capture_claim(
                    market,
                    observation_date,
                    claim_id,
                    status="completed",
                    completed_at=self._as_utc(self.clock()),
                    snapshot_id=result["snapshot_id"],
                    security_count=result["security_count"],
                )
            captured.append(result)

        return {
            "snapshot_version": SNAPSHOT_VERSION,
            "auto_capture_markets": list(AUTO_CAPTURE_MARKETS),
            "captured": captured,
            "skipped": skipped,
            "errors": errors,
            "security_count": sum(
                item["security_count"] for item in captured
            ),
        }

    def get_history(
        self,
        market: Optional[str] = None,
        limit: int = 20,
    ) -> Dict[str, Any]:
        normalized_market = (
            self._normalize_market(market) if market else None
        )
        if not 1 <= int(limit) <= 100:
            raise ValueError("limit 必须在 1～100 之间")
        where = "WHERE market = ?" if normalized_market else ""
        parameters: List[Any] = (
            [normalized_market, int(limit)]
            if normalized_market
            else [int(limit)]
        )
        run_where = "WHERE market = ?" if normalized_market else ""
        run_parameters: List[Any] = (
            [normalized_market, int(limit)]
            if normalized_market
            else [int(limit)]
        )
        with self.connection_factory() as connection:
            rows = connection.execute(
                f"""
                SELECT snapshot_id, captured_at, observation_date,
                       snapshot_version, market, source, source_query,
                       security_count, payload_hash
                FROM security_universe_snapshots
                {where}
                ORDER BY observation_date DESC, captured_at DESC
                LIMIT ?
                """,
                parameters,
            ).fetchall()
            run_rows = connection.execute(
                f"""
                SELECT market, observation_date, status, claim_id,
                       started_at, completed_at, snapshot_id,
                       security_count, error
                FROM security_universe_snapshot_runs
                {run_where}
                ORDER BY started_at DESC, market
                LIMIT ?
                """,
                run_parameters,
            ).fetchall()
        return {
            "snapshot_version": SNAPSHOT_VERSION,
            "classification_version": CLASSIFICATION_VERSION,
            "tradeability_version": TRADEABILITY_VERSION,
            "source": SNAPSHOT_SOURCE,
            "items": [self._summary(row) for row in rows],
            "capture_runs": [self._run_summary(row) for row in run_rows],
        }

    def get_coverage(
        self,
        market: Optional[str] = None,
    ) -> Dict[str, Any]:
        normalized_market = (
            self._normalize_market(market) if market else None
        )
        where = "AND market = ?" if normalized_market else ""
        parameters: List[Any] = [SNAPSHOT_VERSION]
        if normalized_market:
            parameters.append(normalized_market)
        with self.connection_factory() as connection:
            rows = connection.execute(
                f"""
                SELECT snapshot_id, captured_at, observation_date,
                       snapshot_version, market, source, source_query,
                       security_count, payload_hash
                FROM security_universe_snapshots
                WHERE snapshot_version = ?
                {where}
                ORDER BY market, observation_date, captured_at
                """,
                parameters,
            ).fetchall()

        markets = (
            [normalized_market]
            if normalized_market
            else list(SUPPORTED_MARKETS)
        )
        grouped: Dict[str, List[Dict[str, Any]]] = {
            item_market: [] for item_market in markets
        }
        for row in rows:
            summary = self._summary(row)
            grouped[summary["market"]].append(summary)

        coverage = []
        for item_market in markets:
            snapshots = grouped[item_market]
            observation_dates = [
                date.fromisoformat(item["observation_date"])
                for item in snapshots
            ]
            intervals = [
                (current - previous).days
                for previous, current in zip(
                    observation_dates,
                    observation_dates[1:],
                )
            ]
            latest = snapshots[-1] if snapshots else None
            first_date = observation_dates[0] if observation_dates else None
            latest_date = observation_dates[-1] if observation_dates else None
            coverage.append({
                "market": item_market,
                "snapshot_count": len(snapshots),
                "observation_dates": len(set(observation_dates)),
                "first_observation_date": (
                    first_date.isoformat() if first_date else None
                ),
                "latest_observation_date": (
                    latest_date.isoformat() if latest_date else None
                ),
                "calendar_span_days": (
                    (latest_date - first_date).days + 1
                    if first_date and latest_date
                    else 0
                ),
                "comparable_transitions": max(0, len(snapshots) - 1),
                "latest_interval_calendar_days": (
                    intervals[-1] if intervals else None
                ),
                "maximum_interval_calendar_days": (
                    max(intervals) if intervals else None
                ),
                "latest_snapshot_id": (
                    latest["snapshot_id"] if latest else None
                ),
                "latest_security_count": (
                    latest["security_count"] if latest else 0
                ),
                "latest_payload_hash": (
                    latest["payload_hash"] if latest else None
                ),
                "payload_integrity_checked": False,
            })
        return {
            "coverage_version": SNAPSHOT_COVERAGE_VERSION,
            "snapshot_version": SNAPSHOT_VERSION,
            "source": SNAPSHOT_SOURCE,
            "markets": coverage,
        }

    def compare_snapshots(
        self,
        base_snapshot_id: str,
        target_snapshot_id: str,
        detail_limit: int = 100,
    ) -> Dict[str, Any]:
        if not 1 <= int(detail_limit) <= 1000:
            raise ValueError("detail_limit 必须在 1～1000 之间")
        base = self.get_snapshot(base_snapshot_id)
        if base is None:
            raise SecurityUniverseSnapshotNotFoundError(
                base_snapshot_id.strip()
            )
        target = (
            base
            if target_snapshot_id.strip() == base_snapshot_id.strip()
            else self.get_snapshot(target_snapshot_id)
        )
        if target is None:
            raise SecurityUniverseSnapshotNotFoundError(
                target_snapshot_id.strip()
            )

        reasons = []
        if not base["integrity_valid"]:
            reasons.append("base_snapshot_integrity_invalid")
        if not target["integrity_valid"]:
            reasons.append("target_snapshot_integrity_invalid")
        if base["market"] != target["market"]:
            reasons.append("market_mismatch")
        if base["snapshot_version"] != target["snapshot_version"]:
            reasons.append("snapshot_version_mismatch")

        result = {
            "comparison_version": SNAPSHOT_COMPARISON_VERSION,
            "ready": not reasons,
            "reasons": reasons,
            "base": self._comparison_snapshot_summary(base),
            "target": self._comparison_snapshot_summary(target),
            "detail_limit": int(detail_limit),
            "added_count": None,
            "removed_count": None,
            "metadata_changed_count": None,
            "added": [],
            "removed": [],
            "metadata_changed": [],
            "added_truncated": False,
            "removed_truncated": False,
            "metadata_changed_truncated": False,
        }
        if reasons:
            return result

        base_items = {
            item["symbol"]: item for item in base["payload"]["items"]
        }
        target_items = {
            item["symbol"]: item for item in target["payload"]["items"]
        }
        added_symbols = sorted(target_items.keys() - base_items.keys())
        removed_symbols = sorted(base_items.keys() - target_items.keys())
        common_symbols = sorted(base_items.keys() & target_items.keys())
        metadata_changed = []
        for symbol in common_symbols:
            before = base_items[symbol]
            after = target_items[symbol]
            changes = {
                field: {
                    "before": before[field],
                    "after": after[field],
                }
                for field in COMPARISON_METADATA_FIELDS
                if before[field] != after[field]
            }
            if changes:
                metadata_changed.append({
                    "symbol": symbol,
                    "changes": changes,
                })

        limit = int(detail_limit)
        result.update({
            "added_count": len(added_symbols),
            "removed_count": len(removed_symbols),
            "metadata_changed_count": len(metadata_changed),
            "added": [target_items[symbol] for symbol in added_symbols[:limit]],
            "removed": [base_items[symbol] for symbol in removed_symbols[:limit]],
            "metadata_changed": metadata_changed[:limit],
            "added_truncated": len(added_symbols) > limit,
            "removed_truncated": len(removed_symbols) > limit,
            "metadata_changed_truncated": len(metadata_changed) > limit,
        })
        return result

    def capture_classification(
        self,
        source_snapshot_id: str,
        *,
        persist: bool = True,
    ) -> Dict[str, Any]:
        normalized_id = source_snapshot_id.strip()
        if not normalized_id:
            raise ValueError("source_snapshot_id 不能为空")
        if persist:
            existing = self._find_classification_summary(normalized_id)
            if existing is not None:
                return {
                    **existing,
                    "status": "already_classified",
                    "persisted": False,
                }

        source_snapshot = self.get_snapshot(normalized_id)
        if source_snapshot is None:
            raise SecurityUniverseSnapshotNotFoundError(normalized_id)
        if not source_snapshot["integrity_valid"]:
            raise ValueError("源证券目录快照完整性校验失败")
        return self._capture_classification_payload(
            source_snapshot=source_snapshot,
            source_items=source_snapshot["payload"]["items"],
            persist=persist,
        )

    def get_classification(
        self,
        source_snapshot_id: str,
    ) -> Optional[Dict[str, Any]]:
        normalized_id = source_snapshot_id.strip()
        if not normalized_id:
            raise ValueError("source_snapshot_id 不能为空")
        with self.connection_factory() as connection:
            row = connection.execute(
                """
                SELECT classification_snapshot_id, source_snapshot_id,
                       captured_at, observation_date,
                       classification_version, market,
                       source_snapshot_version, security_count,
                       classified_count, resolved_board_count,
                       eligible_count, ready_for_research_universe,
                       payload_hash, payload
                FROM security_universe_classification_snapshots
                WHERE source_snapshot_id = ?
                  AND classification_version = ?
                """,
                [normalized_id, CLASSIFICATION_VERSION],
            ).fetchone()
        if row is None:
            return None

        summary = self._classification_summary(row[:13])
        try:
            payload = json.loads(row[13])
        except (json.JSONDecodeError, TypeError):
            return {
                **summary,
                "computed_payload_hash": None,
                "integrity_valid": False,
                "integrity_errors": ["invalid_payload_json"],
                "payload": None,
            }
        computed_hash = self._payload_hash(payload)
        if not isinstance(payload, dict):
            return {
                **summary,
                "computed_payload_hash": computed_hash,
                "integrity_valid": False,
                "integrity_errors": ["payload_not_object"],
                "payload": payload,
            }

        integrity_errors = []
        if computed_hash != row[12]:
            integrity_errors.append("payload_hash_mismatch")
        if payload.get("classification_version") != row[4]:
            integrity_errors.append("payload_version_mismatch")
        if payload.get("source") != CLASSIFICATION_SOURCE:
            integrity_errors.append("payload_source_mismatch")
        if payload.get("market") != row[5]:
            integrity_errors.append("payload_market_mismatch")
        if payload.get("observation_date") != row[3].isoformat():
            integrity_errors.append("payload_date_mismatch")
        captured_at = row[2].replace(tzinfo=timezone.utc).isoformat()
        if payload.get("captured_at") != captured_at:
            integrity_errors.append("payload_captured_at_mismatch")
        source_request = payload.get("source_request")
        if (
            not isinstance(source_request, dict)
            or source_request.get("method") != "SDK"
            or source_request.get("operation") != "QuoteContext.static_info"
            or not isinstance(source_request.get("batch_size"), int)
            or source_request.get("batch_size") < 1
        ):
            integrity_errors.append("source_request_mismatch")
        if payload.get("policy") != self._classification_policy(row[5]):
            integrity_errors.append("classification_policy_mismatch")

        source_reference = payload.get("source_snapshot")
        if (
            not isinstance(source_reference, dict)
            or source_reference.get("snapshot_id") != row[1]
            or source_reference.get("snapshot_version") != row[6]
            or source_reference.get("security_count") != row[7]
        ):
            integrity_errors.append("source_snapshot_reference_mismatch")
        source_snapshot = self.get_snapshot(row[1])
        if source_snapshot is None:
            integrity_errors.append("source_snapshot_missing")
        else:
            if not source_snapshot["integrity_valid"]:
                integrity_errors.append("source_snapshot_integrity_invalid")
            if (
                source_snapshot["snapshot_version"] != row[6]
                or source_snapshot["market"] != row[5]
                or source_snapshot["observation_date"]
                != row[3].isoformat()
                or source_snapshot["security_count"] != row[7]
            ):
                integrity_errors.append("source_snapshot_metadata_mismatch")
            if (
                not isinstance(source_reference, dict)
                or source_reference.get("payload_hash")
                != source_snapshot["payload_hash"]
            ):
                integrity_errors.append("source_snapshot_hash_mismatch")

        items = payload.get("items")
        if (
            not isinstance(items, list)
            or len(items) != row[7]
            or not self._classification_items_are_canonical(
                items,
                row[5],
            )
        ):
            integrity_errors.append("non_canonical_classification_items")
        else:
            counts = self._classification_counts(items)
            expected_counts = {
                "security_count": len(items),
                "classified_count": counts["classified_count"],
                "resolved_board_count": counts["resolved_board_count"],
                "eligible_count": counts["eligible_count"],
                "excluded_count": len(items) - counts["eligible_count"],
                "missing_static_info_count": counts[
                    "missing_static_info_count"
                ],
                "unknown_board_count": counts["unknown_board_count"],
                "board_market_mismatch_count": counts[
                    "board_market_mismatch_count"
                ],
            }
            if payload.get("counts") != expected_counts:
                integrity_errors.append("classification_count_mismatch")
            if (
                row[8] != counts["classified_count"]
                or row[9] != counts["resolved_board_count"]
                or row[10] != counts["eligible_count"]
            ):
                integrity_errors.append(
                    "classification_metadata_count_mismatch"
                )
            ready, reasons = self._classification_readiness(counts)
            if (
                payload.get("ready_for_research_universe") != ready
                or payload.get("readiness_reasons") != reasons
                or bool(row[11]) != ready
            ):
                integrity_errors.append("classification_readiness_mismatch")
            if payload.get("board_counts") != counts["board_counts"]:
                integrity_errors.append("board_counts_mismatch")
            if payload.get("category_counts") != counts["category_counts"]:
                integrity_errors.append("category_counts_mismatch")
            if payload.get("exclusion_counts") != counts["exclusion_counts"]:
                integrity_errors.append("exclusion_counts_mismatch")

        return {
            **summary,
            "computed_payload_hash": computed_hash,
            "integrity_valid": not integrity_errors,
            "integrity_errors": integrity_errors,
            "payload": payload,
        }

    def get_classification_coverage(
        self,
        market: Optional[str] = None,
    ) -> Dict[str, Any]:
        normalized_market = (
            self._normalize_market(market) if market else None
        )
        raw_where = "AND market = ?" if normalized_market else ""
        classification_where = "AND market = ?" if normalized_market else ""
        raw_parameters: List[Any] = [SNAPSHOT_VERSION]
        classification_parameters: List[Any] = [CLASSIFICATION_VERSION]
        if normalized_market:
            raw_parameters.append(normalized_market)
            classification_parameters.append(normalized_market)
        with self.connection_factory() as connection:
            raw_rows = connection.execute(
                f"""
                SELECT snapshot_id, market, observation_date
                FROM security_universe_snapshots
                WHERE snapshot_version = ?
                {raw_where}
                ORDER BY market, observation_date, captured_at
                """,
                raw_parameters,
            ).fetchall()
            classification_rows = connection.execute(
                f"""
                SELECT classification_snapshot_id, source_snapshot_id,
                       captured_at, observation_date,
                       classification_version, market,
                       source_snapshot_version, security_count,
                       classified_count, resolved_board_count,
                       eligible_count, ready_for_research_universe,
                       payload_hash
                FROM security_universe_classification_snapshots
                WHERE classification_version = ?
                {classification_where}
                ORDER BY market, observation_date, captured_at
                """,
                classification_parameters,
            ).fetchall()

        markets = (
            [normalized_market]
            if normalized_market
            else list(SUPPORTED_MARKETS)
        )
        result = []
        for item_market in markets:
            market_raw = [row for row in raw_rows if row[1] == item_market]
            market_classifications = [
                self._classification_summary(row)
                for row in classification_rows
                if row[5] == item_market
            ]
            classified_source_ids = {
                item["source_snapshot_id"]
                for item in market_classifications
            }
            latest = (
                market_classifications[-1]
                if market_classifications
                else None
            )
            result.append({
                "market": item_market,
                "source_snapshot_count": len(market_raw),
                "classification_snapshot_count": len(
                    market_classifications
                ),
                "unclassified_snapshot_count": sum(
                    1 for row in market_raw
                    if row[0] not in classified_source_ids
                ),
                "observation_dates": len({
                    item["observation_date"]
                    for item in market_classifications
                }),
                "ready_observation_dates": len({
                    item["observation_date"]
                    for item in market_classifications
                    if item["ready_for_research_universe"]
                }),
                "latest": latest,
                "payload_integrity_checked": False,
            })
        return {
            "coverage_version": CLASSIFICATION_COVERAGE_VERSION,
            "classification_version": CLASSIFICATION_VERSION,
            "source_snapshot_version": SNAPSHOT_VERSION,
            "source": CLASSIFICATION_SOURCE,
            "markets": result,
        }

    def capture_tradeability(
        self,
        source_snapshot_id: str,
        *,
        persist: bool = True,
    ) -> Dict[str, Any]:
        normalized_id = source_snapshot_id.strip()
        if not normalized_id:
            raise ValueError("source_snapshot_id 不能为空")
        classification = self.get_classification(normalized_id)
        if classification is None:
            raise ValueError("证券目录分类快照不存在")
        if persist:
            existing = self._find_tradeability_summary(
                classification["classification_snapshot_id"]
            )
            if existing is not None:
                return {
                    **existing,
                    "status": "already_captured",
                    "persisted": False,
                }
        if not classification["integrity_valid"]:
            raise ValueError("证券目录分类快照完整性校验失败")
        if not classification["ready_for_research_universe"]:
            raise ValueError("证券目录分类研究候选门禁未通过")
        source_snapshot = self.get_snapshot(normalized_id)
        if source_snapshot is None:
            raise SecurityUniverseSnapshotNotFoundError(normalized_id)
        return self._capture_tradeability_payload(
            source_snapshot=source_snapshot,
            classification=classification,
            persist=persist,
        )

    def _capture_tradeability_if_ready(
        self,
        source_snapshot: Any,
        classification: Dict[str, Any],
        *,
        persist: bool,
    ) -> Dict[str, Any]:
        if not classification.get("ready_for_research_universe"):
            return {
                "status": "blocked",
                "persisted": False,
                "reason": "classification_not_ready",
            }
        if isinstance(source_snapshot, str):
            return self.capture_tradeability(
                source_snapshot,
                persist=persist,
            )
        return self._capture_tradeability_payload(
            source_snapshot=source_snapshot,
            classification=classification,
            persist=persist,
        )

    def get_tradeability(
        self,
        source_snapshot_id: str,
    ) -> Optional[Dict[str, Any]]:
        normalized_id = source_snapshot_id.strip()
        if not normalized_id:
            raise ValueError("source_snapshot_id 不能为空")
        with self.connection_factory() as connection:
            row = connection.execute(
                """
                SELECT tradeability_snapshot_id, source_snapshot_id,
                       classification_snapshot_id, captured_at,
                       observation_date, tradeability_version, market,
                       source_snapshot_version, classification_version,
                       eligible_count, observed_count, tradable_count,
                       excluded_count, ready_for_point_in_time_universe,
                       payload_hash, payload
                FROM security_universe_tradeability_snapshots
                WHERE source_snapshot_id = ?
                  AND tradeability_version = ?
                """,
                [normalized_id, TRADEABILITY_VERSION],
            ).fetchone()
        if row is None:
            return None

        summary = self._tradeability_summary(row[:15])
        try:
            payload = json.loads(row[15])
        except (json.JSONDecodeError, TypeError):
            return {
                **summary,
                "computed_payload_hash": None,
                "integrity_valid": False,
                "integrity_errors": ["invalid_payload_json"],
                "payload": None,
            }
        computed_hash = self._payload_hash(payload)
        if not isinstance(payload, dict):
            return {
                **summary,
                "computed_payload_hash": computed_hash,
                "integrity_valid": False,
                "integrity_errors": ["payload_not_object"],
                "payload": payload,
            }

        errors = []
        if computed_hash != row[14]:
            errors.append("payload_hash_mismatch")
        if payload.get("tradeability_version") != row[5]:
            errors.append("payload_version_mismatch")
        if payload.get("source") != TRADEABILITY_SOURCE:
            errors.append("payload_source_mismatch")
        if payload.get("market") != row[6]:
            errors.append("payload_market_mismatch")
        if payload.get("observation_date") != row[4].isoformat():
            errors.append("payload_date_mismatch")
        captured_at = row[3].replace(tzinfo=timezone.utc).isoformat()
        if payload.get("captured_at") != captured_at:
            errors.append("payload_captured_at_mismatch")
        if payload.get("source_request") != self._tradeability_request():
            errors.append("source_request_mismatch")
        if payload.get("policy") != self._tradeability_policy():
            errors.append("tradeability_policy_mismatch")

        classification_reference = payload.get("classification_snapshot")
        if (
            not isinstance(classification_reference, dict)
            or classification_reference.get("classification_snapshot_id")
            != row[2]
            or classification_reference.get("classification_version")
            != row[8]
            or classification_reference.get("eligible_count") != row[9]
        ):
            errors.append("classification_snapshot_reference_mismatch")
        classification = self.get_classification(row[1])
        if classification is None:
            errors.append("classification_snapshot_missing")
        else:
            if not classification["integrity_valid"]:
                errors.append("classification_snapshot_integrity_invalid")
            if (
                classification["classification_snapshot_id"] != row[2]
                or classification["classification_version"] != row[8]
                or classification["source_snapshot_version"] != row[7]
                or classification["market"] != row[6]
                or classification["observation_date"]
                != row[4].isoformat()
                or classification["eligible_count"] != row[9]
            ):
                errors.append("classification_snapshot_metadata_mismatch")
            if (
                not isinstance(classification_reference, dict)
                or classification_reference.get("payload_hash")
                != classification["payload_hash"]
            ):
                errors.append("classification_snapshot_hash_mismatch")

        source_reference = payload.get("source_snapshot")
        source_snapshot = self.get_snapshot(row[1])
        if (
            not isinstance(source_reference, dict)
            or source_reference.get("snapshot_id") != row[1]
            or source_reference.get("snapshot_version") != row[7]
        ):
            errors.append("source_snapshot_reference_mismatch")
        if source_snapshot is None:
            errors.append("source_snapshot_missing")
        else:
            if not source_snapshot["integrity_valid"]:
                errors.append("source_snapshot_integrity_invalid")
            if (
                not isinstance(source_reference, dict)
                or source_reference.get("payload_hash")
                != source_snapshot["payload_hash"]
            ):
                errors.append("source_snapshot_hash_mismatch")

        items = payload.get("items")
        if (
            not isinstance(items, list)
            or len(items) != row[9]
            or not self._tradeability_items_are_canonical(items)
        ):
            errors.append("non_canonical_tradeability_items")
        else:
            counts = self._tradeability_counts(items)
            expected_counts = {
                "eligible_count": len(items),
                **counts,
            }
            if payload.get("counts") != expected_counts:
                errors.append("tradeability_count_mismatch")
            if (
                row[10] != counts["observed_count"]
                or row[11] != counts["tradable_count"]
                or row[12] != counts["excluded_count"]
            ):
                errors.append("tradeability_metadata_count_mismatch")
            ready, reasons = self._tradeability_readiness(counts)
            if (
                payload.get("ready_for_point_in_time_universe") != ready
                or payload.get("readiness_reasons") != reasons
                or bool(row[13]) != ready
            ):
                errors.append("tradeability_readiness_mismatch")
            if payload.get("trade_status_counts") != counts["trade_status_counts"]:
                errors.append("trade_status_counts_mismatch")
            if payload.get("exclusion_counts") != counts["exclusion_counts"]:
                errors.append("exclusion_counts_mismatch")

        return {
            **summary,
            "computed_payload_hash": computed_hash,
            "integrity_valid": not errors,
            "integrity_errors": errors,
            "payload": payload,
        }

    def get_tradeability_coverage(
        self,
        market: Optional[str] = None,
    ) -> Dict[str, Any]:
        normalized_market = (
            self._normalize_market(market) if market else None
        )
        scope = "AND market = ?" if normalized_market else ""
        classification_parameters: List[Any] = [CLASSIFICATION_VERSION]
        tradeability_parameters: List[Any] = [TRADEABILITY_VERSION]
        if normalized_market:
            classification_parameters.append(normalized_market)
            tradeability_parameters.append(normalized_market)
        with self.connection_factory() as connection:
            classification_rows = connection.execute(
                f"""
                SELECT classification_snapshot_id, market, observation_date
                FROM security_universe_classification_snapshots
                WHERE classification_version = ? {scope}
                ORDER BY market, observation_date, captured_at
                """,
                classification_parameters,
            ).fetchall()
            tradeability_rows = connection.execute(
                f"""
                SELECT tradeability_snapshot_id, source_snapshot_id,
                       classification_snapshot_id, captured_at,
                       observation_date, tradeability_version, market,
                       source_snapshot_version, classification_version,
                       eligible_count, observed_count, tradable_count,
                       excluded_count, ready_for_point_in_time_universe,
                       payload_hash
                FROM security_universe_tradeability_snapshots
                WHERE tradeability_version = ? {scope}
                ORDER BY market, observation_date, captured_at
                """,
                tradeability_parameters,
            ).fetchall()

        markets = (
            [normalized_market]
            if normalized_market
            else list(SUPPORTED_MARKETS)
        )
        result = []
        for item_market in markets:
            classifications = [
                row for row in classification_rows if row[1] == item_market
            ]
            snapshots = [
                self._tradeability_summary(row)
                for row in tradeability_rows
                if row[6] == item_market
            ]
            captured_classification_ids = {
                item["classification_snapshot_id"] for item in snapshots
            }
            result.append({
                "market": item_market,
                "classification_snapshot_count": len(classifications),
                "tradeability_snapshot_count": len(snapshots),
                "missing_tradeability_snapshot_count": sum(
                    1 for row in classifications
                    if row[0] not in captured_classification_ids
                ),
                "observation_dates": len({
                    item["observation_date"] for item in snapshots
                }),
                "ready_observation_dates": len({
                    item["observation_date"] for item in snapshots
                    if item["ready_for_point_in_time_universe"]
                }),
                "latest": snapshots[-1] if snapshots else None,
                "payload_integrity_checked": False,
            })
        return {
            "coverage_version": TRADEABILITY_COVERAGE_VERSION,
            "tradeability_version": TRADEABILITY_VERSION,
            "classification_version": CLASSIFICATION_VERSION,
            "source_snapshot_version": SNAPSHOT_VERSION,
            "source": TRADEABILITY_SOURCE,
            "markets": result,
        }

    def get_snapshot(self, snapshot_id: str) -> Optional[Dict[str, Any]]:
        normalized_id = snapshot_id.strip()
        if not normalized_id:
            raise ValueError("snapshot_id 不能为空")
        with self.connection_factory() as connection:
            row = connection.execute(
                """
                SELECT snapshot_id, captured_at, observation_date,
                       snapshot_version, market, source, source_query,
                       security_count, payload_hash, payload
                FROM security_universe_snapshots
                WHERE snapshot_id = ?
                """,
                [normalized_id],
            ).fetchone()
        if row is None:
            return None
        summary = self._summary(row[:9])
        try:
            payload = json.loads(row[9])
        except (json.JSONDecodeError, TypeError):
            return {
                **summary,
                "computed_payload_hash": None,
                "integrity_valid": False,
                "integrity_errors": ["invalid_payload_json"],
                "payload": None,
            }
        computed_hash = self._payload_hash(payload)
        if not isinstance(payload, dict):
            return {
                **summary,
                "computed_payload_hash": computed_hash,
                "integrity_valid": False,
                "integrity_errors": ["payload_not_object"],
                "payload": payload,
            }
        items = payload.get("items")
        integrity_errors = []
        if row[3] != SNAPSHOT_VERSION:
            integrity_errors.append("unsupported_snapshot_version")
        if row[5] != SNAPSHOT_SOURCE:
            integrity_errors.append("unexpected_source")
        if computed_hash != row[8]:
            integrity_errors.append("payload_hash_mismatch")
        if payload.get("snapshot_version") != row[3]:
            integrity_errors.append("payload_version_mismatch")
        if payload.get("source") != row[5]:
            integrity_errors.append("payload_source_mismatch")
        if payload.get("market") != row[4]:
            integrity_errors.append("payload_market_mismatch")
        if payload.get("observation_date") != row[2].isoformat():
            integrity_errors.append("payload_date_mismatch")
        row_captured_at = row[1].replace(tzinfo=timezone.utc).isoformat()
        if payload.get("captured_at") != row_captured_at:
            integrity_errors.append("payload_captured_at_mismatch")
        source_request = payload.get("source_request")
        if (
            not isinstance(source_request, dict)
            or source_request.get("method") != "GET"
            or source_request.get("path") != SOURCE_PATH
            or source_request.get("query") != row[6]
        ):
            integrity_errors.append("source_request_mismatch")
        if not isinstance(items, list) or len(items) != row[7]:
            integrity_errors.append("security_count_mismatch")
        elif not self._items_are_canonical(items, row[4]):
            integrity_errors.append("non_canonical_items")
        return {
            **summary,
            "computed_payload_hash": computed_hash,
            "integrity_valid": not integrity_errors,
            "integrity_errors": integrity_errors,
            "payload": payload,
        }

    def _capture_classification_payload(
        self,
        source_snapshot: Dict[str, Any],
        source_items: List[dict],
        *,
        persist: bool,
    ) -> Dict[str, Any]:
        market = self._normalize_market(source_snapshot["market"])
        static_items = self._load_static_info(
            [item["symbol"] for item in source_items]
        )
        items = self._normalize_classification_items(
            source_items,
            static_items,
            market,
        )
        counts = self._classification_counts(items)
        ready, readiness_reasons = self._classification_readiness(counts)
        captured_at = self._as_utc(self.clock())
        payload = {
            "classification_version": CLASSIFICATION_VERSION,
            "source": CLASSIFICATION_SOURCE,
            "source_request": self._classification_request(),
            "source_snapshot": {
                "snapshot_id": source_snapshot["snapshot_id"],
                "snapshot_version": source_snapshot["snapshot_version"],
                "payload_hash": source_snapshot["payload_hash"],
                "security_count": source_snapshot["security_count"],
            },
            "market": market,
            "captured_at": captured_at.isoformat(),
            "observation_date": source_snapshot["observation_date"],
            "policy": self._classification_policy(market),
            "counts": {
                "security_count": len(items),
                "classified_count": counts["classified_count"],
                "resolved_board_count": counts["resolved_board_count"],
                "eligible_count": counts["eligible_count"],
                "excluded_count": len(items) - counts["eligible_count"],
                "missing_static_info_count": counts[
                    "missing_static_info_count"
                ],
                "unknown_board_count": counts["unknown_board_count"],
                "board_market_mismatch_count": counts[
                    "board_market_mismatch_count"
                ],
            },
            "board_counts": counts["board_counts"],
            "category_counts": counts["category_counts"],
            "exclusion_counts": counts["exclusion_counts"],
            "ready_for_research_universe": ready,
            "readiness_reasons": readiness_reasons,
            "items": items,
        }
        payload_hash = self._payload_hash(payload)
        summary = {
            "classification_snapshot_id": uuid4().hex,
            "source_snapshot_id": source_snapshot["snapshot_id"],
            "captured_at": captured_at.isoformat(),
            "observation_date": source_snapshot["observation_date"],
            "classification_version": CLASSIFICATION_VERSION,
            "market": market,
            "source_snapshot_version": source_snapshot["snapshot_version"],
            "security_count": len(items),
            "classified_count": counts["classified_count"],
            "resolved_board_count": counts["resolved_board_count"],
            "eligible_count": counts["eligible_count"],
            "ready_for_research_universe": ready,
            "payload_hash": payload_hash,
        }
        if persist:
            classification_id, inserted = self._save_classification({
                **summary,
                "captured_at": captured_at,
                "payload": payload,
            })
            if not inserted:
                existing = self._find_classification_summary(
                    source_snapshot["snapshot_id"]
                )
                if existing is None:
                    raise RuntimeError("证券目录分类快照写入状态不一致")
                return {
                    **existing,
                    "status": "already_classified",
                    "persisted": False,
                }
            summary["classification_snapshot_id"] = classification_id
        return {
            **summary,
            "status": "classified",
            "persisted": persist,
            "payload": payload,
        }

    def _capture_tradeability_payload(
        self,
        source_snapshot: Dict[str, Any],
        classification: Dict[str, Any],
        *,
        persist: bool,
    ) -> Dict[str, Any]:
        classification_payload = classification.get("payload")
        if not isinstance(classification_payload, dict):
            persisted = self.get_classification(source_snapshot["snapshot_id"])
            classification_payload = (
                persisted.get("payload") if persisted is not None else None
            )
        if not isinstance(classification_payload, dict):
            raise ValueError("证券目录分类载荷不可用")
        symbols = [
            item["symbol"]
            for item in classification_payload.get("items", [])
            if item.get("research_eligible") is True
        ]
        observations = self._load_tradeability(symbols)
        items = self._normalize_tradeability_items(symbols, observations)
        counts = self._tradeability_counts(items)
        ready, readiness_reasons = self._tradeability_readiness(counts)
        captured_at = self._as_utc(self.clock())
        payload = {
            "tradeability_version": TRADEABILITY_VERSION,
            "source": TRADEABILITY_SOURCE,
            "source_request": self._tradeability_request(),
            "source_snapshot": {
                "snapshot_id": source_snapshot["snapshot_id"],
                "snapshot_version": source_snapshot["snapshot_version"],
                "payload_hash": source_snapshot["payload_hash"],
            },
            "classification_snapshot": {
                "classification_snapshot_id": classification[
                    "classification_snapshot_id"
                ],
                "classification_version": classification[
                    "classification_version"
                ],
                "payload_hash": classification["payload_hash"],
                "eligible_count": classification["eligible_count"],
            },
            "market": source_snapshot["market"],
            "captured_at": captured_at.isoformat(),
            "observation_date": source_snapshot["observation_date"],
            "policy": self._tradeability_policy(),
            "counts": {
                "eligible_count": len(items),
                **counts,
            },
            "trade_status_counts": counts["trade_status_counts"],
            "exclusion_counts": counts["exclusion_counts"],
            "ready_for_point_in_time_universe": ready,
            "readiness_reasons": readiness_reasons,
            "items": items,
        }
        payload_hash = self._payload_hash(payload)
        summary = {
            "tradeability_snapshot_id": uuid4().hex,
            "source_snapshot_id": source_snapshot["snapshot_id"],
            "classification_snapshot_id": classification[
                "classification_snapshot_id"
            ],
            "captured_at": captured_at.isoformat(),
            "observation_date": source_snapshot["observation_date"],
            "tradeability_version": TRADEABILITY_VERSION,
            "market": source_snapshot["market"],
            "source_snapshot_version": source_snapshot["snapshot_version"],
            "classification_version": classification[
                "classification_version"
            ],
            "eligible_count": len(items),
            "observed_count": counts["observed_count"],
            "tradable_count": counts["tradable_count"],
            "excluded_count": counts["excluded_count"],
            "ready_for_point_in_time_universe": ready,
            "payload_hash": payload_hash,
        }
        if persist:
            snapshot_id, inserted = self._save_tradeability({
                **summary,
                "captured_at": captured_at,
                "payload": payload,
            })
            if not inserted:
                existing = self._find_tradeability_summary(
                    classification["classification_snapshot_id"]
                )
                if existing is None:
                    raise RuntimeError("证券目录交易状态快照写入状态不一致")
                return {
                    **existing,
                    "status": "already_captured",
                    "persisted": False,
                }
            summary["tradeability_snapshot_id"] = snapshot_id
        return {
            **summary,
            "status": "captured",
            "persisted": persist,
            "payload": payload,
        }

    def _load_tradeability(
        self,
        symbols: List[str],
    ) -> Dict[str, Dict[str, Any]]:
        result: Dict[str, Dict[str, Any]] = {}
        for index in range(0, len(symbols), self.quote_batch_size):
            batch = symbols[index:index + self.quote_batch_size]
            loaded = run_external_call(
                "quote",
                "security_universe_tradeability",
                self.tradeability_loader,
                batch,
                include_depth=False,
                retry_if=lambda error: not isinstance(
                    error,
                    ExternalServiceTimeoutError,
                ),
            )
            if not isinstance(loaded, dict):
                raise ValueError("证券交易状态响应必须是对象")
            for symbol, item in loaded.items():
                normalized_symbol = str(symbol or "").strip().upper()
                if normalized_symbol in result:
                    raise ValueError(
                        f"证券交易状态响应存在重复 symbol: {normalized_symbol}"
                    )
                result[normalized_symbol] = item
        return result

    @classmethod
    def _normalize_tradeability_items(
        cls,
        symbols: List[str],
        observations: Dict[str, Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        items = []
        for symbol in sorted(symbols):
            raw = observations.get(symbol)
            if not isinstance(raw, dict) or raw.get("status") != "available":
                items.append({
                    "symbol": symbol,
                    "status": "no_data",
                    "error": (
                        str(raw.get("error") or "").strip() or None
                        if isinstance(raw, dict)
                        else None
                    ),
                    "trade_status": None,
                    "is_tradable": None,
                    "last_done": None,
                    "volume": None,
                    "turnover": None,
                    "data_as_of": None,
                    "point_in_time_eligible": False,
                    "exclusion_reason": "quote_no_data",
                })
                continue
            trade_status = cls._normalize_board(raw.get("trade_status"))
            known = trade_status in KNOWN_TRADE_STATUSES
            is_tradable = trade_status == "normal" if known else False
            exclusion_reason = None
            if not known:
                exclusion_reason = "unknown_trade_status"
            elif not is_tradable:
                exclusion_reason = f"trade_status_{trade_status}"
            items.append({
                "symbol": symbol,
                "status": "available",
                "error": None,
                "trade_status": trade_status,
                "is_tradable": is_tradable,
                "last_done": cls._normalize_optional_number(
                    raw.get("last_done")
                ),
                "volume": cls._normalize_optional_number(raw.get("volume")),
                "turnover": cls._normalize_optional_number(
                    raw.get("turnover")
                ),
                "data_as_of": str(raw.get("data_as_of") or "").strip() or None,
                "point_in_time_eligible": is_tradable,
                "exclusion_reason": exclusion_reason,
            })
        return items

    @classmethod
    def _tradeability_items_are_canonical(cls, items: List[dict]) -> bool:
        symbols = []
        observations = {}
        for item in items:
            if not isinstance(item, dict):
                return False
            symbol = str(item.get("symbol") or "").strip().upper()
            if not symbol or symbol in observations:
                return False
            symbols.append(symbol)
            observations[symbol] = item
        try:
            return cls._normalize_tradeability_items(
                symbols,
                observations,
            ) == items
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _tradeability_counts(items: List[dict]) -> Dict[str, Any]:
        trade_status_counts: Dict[str, int] = {}
        exclusion_counts: Dict[str, int] = {}
        for item in items:
            trade_status = item.get("trade_status") or "no_data"
            trade_status_counts[trade_status] = (
                trade_status_counts.get(trade_status, 0) + 1
            )
            reason = item.get("exclusion_reason")
            if reason:
                exclusion_counts[reason] = exclusion_counts.get(reason, 0) + 1
        return {
            "observed_count": sum(
                item.get("status") == "available" for item in items
            ),
            "tradable_count": sum(
                item.get("point_in_time_eligible") is True for item in items
            ),
            "excluded_count": sum(
                item.get("status") == "available"
                and item.get("trade_status") in KNOWN_TRADE_STATUSES
                and item.get("point_in_time_eligible") is False
                for item in items
            ),
            "missing_quote_count": sum(
                item.get("status") != "available" for item in items
            ),
            "unknown_trade_status_count": sum(
                item.get("status") == "available"
                and item.get("trade_status") not in KNOWN_TRADE_STATUSES
                for item in items
            ),
            "trade_status_counts": dict(sorted(trade_status_counts.items())),
            "exclusion_counts": dict(sorted(exclusion_counts.items())),
        }

    @staticmethod
    def _tradeability_readiness(
        counts: Dict[str, Any],
    ) -> tuple[bool, List[str]]:
        reasons = []
        if counts["missing_quote_count"]:
            reasons.append("incomplete_quote_coverage")
        if counts["unknown_trade_status_count"]:
            reasons.append("unknown_trade_status")
        return not reasons, reasons

    def _tradeability_request(self) -> Dict[str, Any]:
        return {
            "method": "SDK",
            "operation": "QuoteContext.quote",
            "batch_size": self.quote_batch_size,
            "include_depth": False,
        }

    @staticmethod
    def _tradeability_policy() -> Dict[str, Any]:
        return {
            "policy_version": TRADEABILITY_VERSION,
            "eligible_trade_status": "normal",
            "known_trade_statuses": sorted(KNOWN_TRADE_STATUSES),
            "requires_complete_quote_coverage": True,
            "requires_known_trade_status": True,
            "captures_depth": False,
            "data_as_of_semantics": "upstream_quote_timestamp",
            "captured_at_semantics": "request_observation_time",
            "does_not_prove": [
                "ordinary_stock_or_etf_type",
                "liquidity",
                "account_permission",
                "borrow_availability",
                "future_execution",
            ],
        }

    @staticmethod
    def _normalize_optional_number(value: Any) -> Optional[float]:
        if value is None or value == "":
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if number != number or number in (float("inf"), float("-inf")):
            return None
        return number

    def _load_static_info(self, symbols: List[str]) -> List[dict]:
        items = []
        for index in range(0, len(symbols), self.static_info_batch_size):
            batch = symbols[index:index + self.static_info_batch_size]
            loaded = run_external_call(
                "quote",
                "security_universe_static_info",
                self.static_info_loader,
                batch,
                retry_if=lambda error: not isinstance(
                    error,
                    ExternalServiceTimeoutError,
                ),
            )
            if not isinstance(loaded, list):
                raise ValueError("证券静态分类响应必须是列表")
            items.extend(loaded)
        return items

    @classmethod
    def _normalize_classification_items(
        cls,
        source_items: List[dict],
        static_items: Iterable[dict],
        market: str,
    ) -> List[Dict[str, Any]]:
        source_symbols = {item["symbol"] for item in source_items}
        static_by_symbol = {}
        for item in static_items:
            if not isinstance(item, dict):
                raise ValueError("证券静态分类条目必须是对象")
            symbol = str(item.get("symbol") or "").strip().upper()
            if not symbol:
                raise ValueError("证券静态分类存在空 symbol")
            if symbol not in source_symbols:
                raise ValueError(f"证券静态分类返回目录外 symbol: {symbol}")
            if symbol in static_by_symbol:
                raise ValueError(f"证券静态分类存在重复 symbol: {symbol}")
            static_by_symbol[symbol] = item

        normalized = []
        for source_item in source_items:
            symbol = source_item["symbol"]
            static_item = static_by_symbol.get(symbol)
            if static_item is None:
                normalized.append({
                    "symbol": symbol,
                    "static_info_status": "missing",
                    "board": None,
                    "board_raw": None,
                    "exchange": "",
                    "currency": "",
                    "lot_size": None,
                    "board_category": "unknown",
                    "research_eligible": False,
                    "exclusion_reason": "missing_static_info",
                })
                continue

            board_raw_value = static_item.get("board_raw")
            board_value = static_item.get("board")
            board = cls._normalize_board(board_value)
            board_raw = (
                str(board_raw_value).strip()
                if board_raw_value is not None
                else (
                    str(board_value).strip()
                    if board_value is not None
                    else None
                )
            )
            if board_raw == "":
                board_raw = None
            category, eligible, exclusion_reason = cls._classify_board(
                board,
                market,
            )
            lot_size_value = static_item.get("lot_size")
            try:
                lot_size = (
                    int(lot_size_value)
                    if lot_size_value is not None
                    else None
                )
            except (TypeError, ValueError):
                lot_size = None
            if lot_size is not None and lot_size <= 0:
                lot_size = None
            normalized.append({
                "symbol": symbol,
                "static_info_status": "available",
                "board": board,
                "board_raw": board_raw,
                "exchange": str(
                    static_item.get("exchange") or ""
                ).strip(),
                "currency": str(
                    static_item.get("currency") or ""
                ).strip().upper(),
                "lot_size": lot_size,
                "board_category": category,
                "research_eligible": eligible,
                "exclusion_reason": exclusion_reason,
            })
        normalized.sort(key=lambda item: item["symbol"])
        return normalized

    @classmethod
    def _classification_items_are_canonical(
        cls,
        items: List[dict],
        market: str,
    ) -> bool:
        if not all(isinstance(item, dict) for item in items):
            return False
        source_items = [{"symbol": item.get("symbol")} for item in items]
        static_items = []
        for item in items:
            if item.get("static_info_status") == "available":
                static_items.append({
                    "symbol": item.get("symbol"),
                    "board": item.get("board"),
                    "board_raw": item.get("board_raw"),
                    "exchange": item.get("exchange"),
                    "currency": item.get("currency"),
                    "lot_size": item.get("lot_size"),
                })
            elif item.get("static_info_status") != "missing":
                return False
        try:
            return cls._normalize_classification_items(
                source_items,
                static_items,
                market,
            ) == items
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _classification_counts(items: List[dict]) -> Dict[str, Any]:
        board_counts: Dict[str, int] = {}
        category_counts: Dict[str, int] = {}
        exclusion_counts: Dict[str, int] = {}
        for item in items:
            if item["board"]:
                board_counts[item["board"]] = (
                    board_counts.get(item["board"], 0) + 1
                )
            category = item["board_category"]
            category_counts[category] = category_counts.get(category, 0) + 1
            reason = item["exclusion_reason"]
            if reason:
                exclusion_counts[reason] = exclusion_counts.get(reason, 0) + 1
        return {
            "classified_count": sum(
                item["static_info_status"] == "available" for item in items
            ),
            "resolved_board_count": sum(
                item["board_category"] != "unknown" for item in items
            ),
            "eligible_count": sum(
                bool(item["research_eligible"]) for item in items
            ),
            "missing_static_info_count": exclusion_counts.get(
                "missing_static_info", 0
            ),
            "unknown_board_count": exclusion_counts.get("unknown_board", 0),
            "board_market_mismatch_count": exclusion_counts.get(
                "board_market_mismatch", 0
            ),
            "board_counts": dict(sorted(board_counts.items())),
            "category_counts": dict(sorted(category_counts.items())),
            "exclusion_counts": dict(sorted(exclusion_counts.items())),
        }

    @staticmethod
    def _classification_readiness(
        counts: Dict[str, Any],
    ) -> tuple[bool, List[str]]:
        reasons = []
        if counts["missing_static_info_count"]:
            reasons.append("incomplete_static_info")
        if counts["unknown_board_count"]:
            reasons.append("unknown_board")
        if counts["board_market_mismatch_count"]:
            reasons.append("board_market_mismatch")
        return not reasons, reasons

    @staticmethod
    def _normalize_board(value: Any) -> Optional[str]:
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return None
        return text.split(".")[-1].replace("'>", "").strip().lower() or None

    @staticmethod
    def _classify_board(
        board: Optional[str],
        market: str,
    ) -> tuple[str, bool, Optional[str]]:
        if board is None or board not in BOARD_CLASSIFICATION:
            return "unknown", False, "unknown_board"
        if board not in MARKET_BOARD_ALLOWLIST[market]:
            return "unknown", False, "board_market_mismatch"
        return BOARD_CLASSIFICATION[board]

    def _classification_request(self) -> Dict[str, Any]:
        return {
            "method": "SDK",
            "operation": "QuoteContext.static_info",
            "batch_size": self.static_info_batch_size,
        }

    @staticmethod
    def _classification_policy(market: str) -> Dict[str, Any]:
        eligible_boards = sorted(
            board
            for board in MARKET_BOARD_ALLOWLIST[market]
            if BOARD_CLASSIFICATION[board][1]
        )
        return {
            "policy_version": CLASSIFICATION_VERSION,
            "eligible_boards": eligible_boards,
            "eligible_semantics": "listed_equity_board_including_funds",
            "requires_complete_static_info": True,
            "requires_known_board": True,
            "does_not_prove": [
                "ordinary_stock_or_etf_type",
                "current_trade_status",
                "account_permission",
                "liquidity",
            ],
        }

    def _is_market_trading_day(
        self,
        market: str,
        observation_date: date,
    ) -> bool:
        key = (market, observation_date)
        cached = self._trading_day_cache.get(key)
        if cached is not None:
            return cached
        result = bool(run_external_call(
            "quote",
            "security_universe_trading_calendar",
            self.trading_day_loader,
            market,
            observation_date,
            retry_if=lambda error: not isinstance(
                error,
                ExternalServiceTimeoutError,
            ),
        ))
        self._trading_day_cache[key] = result
        return result

    def _claim_capture(
        self,
        market: str,
        observation_date: date,
        started_at: datetime,
    ) -> Optional[str]:
        claim_id = uuid4().hex
        stale_before = started_at - timedelta(
            minutes=CAPTURE_CLAIM_LEASE_MINUTES,
        )
        with self.connection_factory() as connection:
            row = connection.execute(
                """
                INSERT INTO security_universe_snapshot_runs (
                    market, observation_date, status, claim_id, started_at
                ) VALUES (?, ?, 'running', ?, ?)
                ON CONFLICT (market, observation_date) DO UPDATE SET
                    status = 'running',
                    claim_id = excluded.claim_id,
                    started_at = excluded.started_at,
                    completed_at = NULL,
                    snapshot_id = NULL,
                    security_count = 0,
                    error = NULL
                WHERE (
                    security_universe_snapshot_runs.status IN (
                        'failed', 'completed'
                    )
                    OR (
                        security_universe_snapshot_runs.status = 'running'
                        AND security_universe_snapshot_runs.started_at < ?
                    )
                )
                RETURNING claim_id
                """,
                [
                    market,
                    observation_date,
                    claim_id,
                    started_at.replace(tzinfo=None),
                    stale_before.replace(tzinfo=None),
                ],
            ).fetchone()
        return claim_id if row and row[0] == claim_id else None

    def _finish_capture_claim(
        self,
        market: str,
        observation_date: date,
        claim_id: str,
        *,
        status: str,
        completed_at: datetime,
        snapshot_id: Optional[str] = None,
        security_count: int = 0,
        error: Optional[str] = None,
    ) -> None:
        if status not in {"completed", "failed"}:
            raise ValueError("status 必须是 completed 或 failed")
        with self.connection_factory() as connection:
            connection.execute(
                """
                UPDATE security_universe_snapshot_runs
                SET status = ?,
                    completed_at = ?,
                    snapshot_id = ?,
                    security_count = ?,
                    error = ?
                WHERE market = ?
                  AND observation_date = ?
                  AND claim_id = ?
                """,
                [
                    status,
                    completed_at.replace(tzinfo=None),
                    snapshot_id,
                    int(security_count),
                    error,
                    market,
                    observation_date,
                    claim_id,
                ],
            )

    def _reconcile_existing_run(
        self,
        snapshot: Dict[str, Any],
        completed_at: datetime,
    ) -> None:
        with self.connection_factory() as connection:
            connection.execute(
                """
                UPDATE security_universe_snapshot_runs
                SET status = 'completed',
                    completed_at = COALESCE(completed_at, ?),
                    snapshot_id = ?,
                    security_count = ?,
                    error = NULL
                WHERE market = ?
                  AND observation_date = ?
                  AND status != 'completed'
                """,
                [
                    completed_at.replace(tzinfo=None),
                    snapshot["snapshot_id"],
                    int(snapshot["security_count"]),
                    snapshot["market"],
                    date.fromisoformat(snapshot["observation_date"]),
                ],
            )

    def _save_snapshot(
        self,
        snapshot: Dict[str, Any],
    ) -> tuple[str, bool]:
        payload_text = self._canonical_json(snapshot["payload"])
        with self.connection_factory() as connection:
            row = connection.execute(
                """
                INSERT INTO security_universe_snapshots (
                    snapshot_id, captured_at, observation_date,
                    snapshot_version, market, source, source_query,
                    security_count, payload_hash, payload
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (market, observation_date, snapshot_version)
                DO NOTHING
                RETURNING snapshot_id
                """,
                [
                    snapshot["snapshot_id"],
                    snapshot["captured_at"].replace(tzinfo=None),
                    snapshot["observation_date"],
                    SNAPSHOT_VERSION,
                    snapshot["market"],
                    SNAPSHOT_SOURCE,
                    self._source_query(snapshot["market"]),
                    snapshot["security_count"],
                    snapshot["payload_hash"],
                    payload_text,
                ],
            ).fetchone()
            if row is not None:
                return row[0], True
            existing = connection.execute(
                """
                SELECT snapshot_id
                FROM security_universe_snapshots
                WHERE market = ?
                  AND observation_date = ?
                  AND snapshot_version = ?
                """,
                [
                    snapshot["market"],
                    snapshot["observation_date"],
                    SNAPSHOT_VERSION,
                ],
            ).fetchone()
        if existing is None:
            raise RuntimeError("证券目录快照写入失败")
        return existing[0], False

    def _save_classification(
        self,
        classification: Dict[str, Any],
    ) -> tuple[str, bool]:
        payload_text = self._canonical_json(classification["payload"])
        with self.connection_factory() as connection:
            row = connection.execute(
                """
                INSERT INTO security_universe_classification_snapshots (
                    classification_snapshot_id, source_snapshot_id,
                    captured_at, observation_date,
                    classification_version, market,
                    source_snapshot_version, security_count,
                    classified_count, resolved_board_count,
                    eligible_count, ready_for_research_universe,
                    payload_hash, payload
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (source_snapshot_id, classification_version)
                DO NOTHING
                RETURNING classification_snapshot_id
                """,
                [
                    classification["classification_snapshot_id"],
                    classification["source_snapshot_id"],
                    classification["captured_at"].replace(tzinfo=None),
                    classification["observation_date"],
                    CLASSIFICATION_VERSION,
                    classification["market"],
                    classification["source_snapshot_version"],
                    classification["security_count"],
                    classification["classified_count"],
                    classification["resolved_board_count"],
                    classification["eligible_count"],
                    classification["ready_for_research_universe"],
                    classification["payload_hash"],
                    payload_text,
                ],
            ).fetchone()
            if row is not None:
                return row[0], True
            existing = connection.execute(
                """
                SELECT classification_snapshot_id
                FROM security_universe_classification_snapshots
                WHERE source_snapshot_id = ?
                  AND classification_version = ?
                """,
                [
                    classification["source_snapshot_id"],
                    CLASSIFICATION_VERSION,
                ],
            ).fetchone()
        if existing is None:
            raise RuntimeError("证券目录分类快照写入失败")
        return existing[0], False

    def _save_tradeability(
        self,
        snapshot: Dict[str, Any],
    ) -> tuple[str, bool]:
        payload_text = self._canonical_json(snapshot["payload"])
        with self.connection_factory() as connection:
            row = connection.execute(
                """
                INSERT INTO security_universe_tradeability_snapshots (
                    tradeability_snapshot_id, source_snapshot_id,
                    classification_snapshot_id, captured_at,
                    observation_date, tradeability_version, market,
                    source_snapshot_version, classification_version,
                    eligible_count, observed_count, tradable_count,
                    excluded_count, ready_for_point_in_time_universe,
                    payload_hash, payload
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (classification_snapshot_id, tradeability_version)
                DO NOTHING
                RETURNING tradeability_snapshot_id
                """,
                [
                    snapshot["tradeability_snapshot_id"],
                    snapshot["source_snapshot_id"],
                    snapshot["classification_snapshot_id"],
                    snapshot["captured_at"].replace(tzinfo=None),
                    snapshot["observation_date"],
                    TRADEABILITY_VERSION,
                    snapshot["market"],
                    snapshot["source_snapshot_version"],
                    snapshot["classification_version"],
                    snapshot["eligible_count"],
                    snapshot["observed_count"],
                    snapshot["tradable_count"],
                    snapshot["excluded_count"],
                    snapshot["ready_for_point_in_time_universe"],
                    snapshot["payload_hash"],
                    payload_text,
                ],
            ).fetchone()
            if row is not None:
                return row[0], True
            existing = connection.execute(
                """
                SELECT tradeability_snapshot_id
                FROM security_universe_tradeability_snapshots
                WHERE classification_snapshot_id = ?
                  AND tradeability_version = ?
                """,
                [
                    snapshot["classification_snapshot_id"],
                    TRADEABILITY_VERSION,
                ],
            ).fetchone()
        if existing is None:
            raise RuntimeError("证券目录交易状态快照写入失败")
        return existing[0], False

    def _find_classification_summary(
        self,
        source_snapshot_id: str,
    ) -> Optional[Dict[str, Any]]:
        with self.connection_factory() as connection:
            row = connection.execute(
                """
                SELECT classification_snapshot_id, source_snapshot_id,
                       captured_at, observation_date,
                       classification_version, market,
                       source_snapshot_version, security_count,
                       classified_count, resolved_board_count,
                       eligible_count, ready_for_research_universe,
                       payload_hash
                FROM security_universe_classification_snapshots
                WHERE source_snapshot_id = ?
                  AND classification_version = ?
                """,
                [source_snapshot_id, CLASSIFICATION_VERSION],
            ).fetchone()
        return (
            self._classification_summary(row)
            if row is not None
            else None
        )

    def _find_tradeability_summary(
        self,
        classification_snapshot_id: str,
    ) -> Optional[Dict[str, Any]]:
        with self.connection_factory() as connection:
            row = connection.execute(
                """
                SELECT tradeability_snapshot_id, source_snapshot_id,
                       classification_snapshot_id, captured_at,
                       observation_date, tradeability_version, market,
                       source_snapshot_version, classification_version,
                       eligible_count, observed_count, tradable_count,
                       excluded_count, ready_for_point_in_time_universe,
                       payload_hash
                FROM security_universe_tradeability_snapshots
                WHERE classification_snapshot_id = ?
                  AND tradeability_version = ?
                """,
                [classification_snapshot_id, TRADEABILITY_VERSION],
            ).fetchone()
        return self._tradeability_summary(row) if row is not None else None

    def _find_existing_summary(
        self,
        market: str,
        observation_date: date,
    ) -> Optional[Dict[str, Any]]:
        with self.connection_factory() as connection:
            row = connection.execute(
                """
                SELECT snapshot_id, captured_at, observation_date,
                       snapshot_version, market, source, source_query,
                       security_count, payload_hash
                FROM security_universe_snapshots
                WHERE market = ?
                  AND observation_date = ?
                  AND snapshot_version = ?
                """,
                [market, observation_date, SNAPSHOT_VERSION],
            ).fetchone()
        return self._summary(row) if row is not None else None

    @staticmethod
    def _normalize_items(
        items: Iterable[dict],
        market: str,
    ) -> List[Dict[str, str]]:
        normalized: List[Dict[str, str]] = []
        symbols = set()
        for item in items:
            if not isinstance(item, dict):
                raise ValueError("证券目录条目必须是对象")
            symbol = str(item.get("symbol") or "").strip().upper()
            item_market = str(item.get("market") or market).strip().upper()
            if not symbol:
                raise ValueError("证券目录存在空 symbol")
            if item_market != market:
                raise ValueError(
                    f"证券 {symbol} 的市场 {item_market} 与 {market} 不一致"
                )
            if symbol in symbols:
                raise ValueError(f"证券目录存在重复 symbol: {symbol}")
            symbols.add(symbol)
            name = str(item.get("name") or "").strip() or symbol
            normalized.append({
                "symbol": symbol,
                "name": name,
                "name_en": str(item.get("name_en") or "").strip(),
                "name_hk": str(item.get("name_hk") or "").strip(),
                "market": market,
            })
        normalized.sort(key=lambda item: item["symbol"])
        return normalized

    @classmethod
    def _items_are_canonical(
        cls,
        items: List[dict],
        market: str,
    ) -> bool:
        try:
            return cls._normalize_items(items, market) == items
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _payload(
        market: str,
        captured_at: datetime,
        observation_date: date,
        items: List[Dict[str, str]],
    ) -> Dict[str, Any]:
        return {
            "snapshot_version": SNAPSHOT_VERSION,
            "source": SNAPSHOT_SOURCE,
            "source_request": {
                "method": "GET",
                "path": SOURCE_PATH,
                "query": SecurityUniverseSnapshotService._source_query(
                    market
                ),
            },
            "market": market,
            "captured_at": captured_at.isoformat(),
            "observation_date": observation_date.isoformat(),
            "items": items,
        }

    @staticmethod
    def _payload_hash(payload: Any) -> str:
        return hashlib.sha256(
            SecurityUniverseSnapshotService._canonical_json(payload).encode(
                "utf-8"
            )
        ).hexdigest()

    @staticmethod
    def _canonical_json(value: Any) -> str:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @staticmethod
    def _source_query(market: str) -> str:
        return f"market={market}&category={SOURCE_CATEGORY}"

    @staticmethod
    def _summary(row: tuple) -> Dict[str, Any]:
        return {
            "snapshot_id": row[0],
            "captured_at": row[1].replace(
                tzinfo=timezone.utc
            ).isoformat(),
            "observation_date": row[2].isoformat(),
            "snapshot_version": row[3],
            "market": row[4],
            "source": row[5],
            "source_query": row[6],
            "security_count": row[7],
            "payload_hash": row[8],
        }

    @staticmethod
    def _run_summary(row: tuple) -> Dict[str, Any]:
        return {
            "market": row[0],
            "observation_date": row[1].isoformat(),
            "status": row[2],
            "claim_id": row[3],
            "started_at": row[4].replace(
                tzinfo=timezone.utc
            ).isoformat(),
            "completed_at": (
                row[5].replace(tzinfo=timezone.utc).isoformat()
                if row[5] is not None
                else None
            ),
            "snapshot_id": row[6],
            "security_count": row[7],
            "error": row[8],
        }

    @staticmethod
    def _classification_summary(row: tuple) -> Dict[str, Any]:
        return {
            "classification_snapshot_id": row[0],
            "source_snapshot_id": row[1],
            "captured_at": row[2].replace(
                tzinfo=timezone.utc
            ).isoformat(),
            "observation_date": row[3].isoformat(),
            "classification_version": row[4],
            "market": row[5],
            "source_snapshot_version": row[6],
            "security_count": row[7],
            "classified_count": row[8],
            "resolved_board_count": row[9],
            "eligible_count": row[10],
            "ready_for_research_universe": bool(row[11]),
            "payload_hash": row[12],
        }

    @staticmethod
    def _tradeability_summary(row: tuple) -> Dict[str, Any]:
        return {
            "tradeability_snapshot_id": row[0],
            "source_snapshot_id": row[1],
            "classification_snapshot_id": row[2],
            "captured_at": row[3].replace(
                tzinfo=timezone.utc
            ).isoformat(),
            "observation_date": row[4].isoformat(),
            "tradeability_version": row[5],
            "market": row[6],
            "source_snapshot_version": row[7],
            "classification_version": row[8],
            "eligible_count": row[9],
            "observed_count": row[10],
            "tradable_count": row[11],
            "excluded_count": row[12],
            "ready_for_point_in_time_universe": bool(row[13]),
            "payload_hash": row[14],
        }

    @staticmethod
    def _comparison_snapshot_summary(
        snapshot: Dict[str, Any],
    ) -> Dict[str, Any]:
        return {
            key: snapshot[key]
            for key in (
                "snapshot_id",
                "captured_at",
                "observation_date",
                "snapshot_version",
                "market",
                "security_count",
                "payload_hash",
                "computed_payload_hash",
                "integrity_valid",
                "integrity_errors",
            )
        }

    @staticmethod
    def _normalize_market(market: str) -> str:
        normalized = market.strip().upper()
        if normalized not in SUPPORTED_MARKETS:
            raise ValueError("market 必须是 US、HK 或 CN")
        return normalized

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    @staticmethod
    def _refresh_catalog(market: str) -> List[dict]:
        return get_security_catalog_service().refresh(market)


_security_universe_snapshot_service: Optional[
    SecurityUniverseSnapshotService
] = None


def get_security_universe_snapshot_service(
) -> SecurityUniverseSnapshotService:
    global _security_universe_snapshot_service
    if _security_universe_snapshot_service is None:
        _security_universe_snapshot_service = SecurityUniverseSnapshotService()
    return _security_universe_snapshot_service


def main() -> None:
    parser = argparse.ArgumentParser(
        description="采集 Longbridge 官方可搜索证券目录点时快照",
    )
    parser.add_argument(
        "--market",
        required=True,
        choices=list(SUPPORTED_MARKETS),
    )
    parser.add_argument(
        "--no-persist",
        action="store_true",
        help="刷新并校验目录，但不写入快照表",
    )
    args = parser.parse_args()
    result = get_security_universe_snapshot_service().capture_market(
        args.market,
        persist=not args.no_persist,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
