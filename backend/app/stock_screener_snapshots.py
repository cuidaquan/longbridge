from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from typing import Any, Callable, Dict, List, Optional, Tuple
from uuid import uuid4
from zoneinfo import ZoneInfo

from .db import get_connection
from .stock_screener import MIN_INDUSTRY_PEERS, RELATIVE_STRENGTH_VERSION


LEGACY_SNAPSHOT_VERSION = "stock-screener-scan-snapshot-v1"
SNAPSHOT_VERSION = "stock-screener-scan-snapshot-v2"
SUPPORTED_SNAPSHOT_VERSIONS = {
    LEGACY_SNAPSHOT_VERSION,
    SNAPSHOT_VERSION,
}
FILTER_VERSION = "stock-screener-candidate-filter-v1"
LEGACY_RELATIVE_STRENGTH_VERSION = "directional-return-difference-v1"
SUPPORTED_RELATIVE_STRENGTH_VERSIONS = {
    LEGACY_RELATIVE_STRENGTH_VERSION,
    RELATIVE_STRENGTH_VERSION,
}
COVERAGE_VERSION = "stock-screener-scan-coverage-v2"
MIN_CAPTURE_DATES = 20
MIN_CALENDAR_SPAN_DAYS = 28
MIN_DISTINCT_UNIVERSE_SYMBOLS = 30
MIN_DISTINCT_SELECTED_SYMBOLS = 10
MIN_UNIVERSE_OBSERVATIONS = 200
MIN_SELECTED_OBSERVATIONS = 60
MIN_INTEGRITY_RATE = 1.0
MIN_MARKET_ENVIRONMENT_COVERAGE = 1.0

_MARKET_TIMEZONES = {
    "US": ZoneInfo("America/New_York"),
    "HK": ZoneInfo("Asia/Hong_Kong"),
    "CN": ZoneInfo("Asia/Shanghai"),
    "SG": ZoneInfo("Asia/Singapore"),
}


def _utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


class StockScreenerSnapshotService:
    def __init__(
        self,
        connection_factory: Callable = get_connection,
        clock: Callable[[], datetime] = (
            lambda: datetime.now(timezone.utc)
        ),
    ) -> None:
        self.connection_factory = connection_factory
        self.clock = clock

    def capture(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        request = payload["request"]
        scan = payload["scan"]
        snapshot_id = str(uuid4())
        captured_at = self.clock()
        capture = {
            "snapshot_id": snapshot_id,
            "captured_at": _utc_iso(captured_at),
            "snapshot_version": SNAPSHOT_VERSION,
            "filter_version": FILTER_VERSION,
            "relative_strength_version": RELATIVE_STRENGTH_VERSION,
        }
        stored_payload = {"capture": capture, **payload}
        payload_hash = sha256(
            _canonical_json(stored_payload).encode("utf-8")
        ).hexdigest()
        stored_payload["capture"]["payload_hash"] = payload_hash
        serialized = _canonical_json(stored_payload)

        with self.connection_factory() as connection:
            connection.execute(
                """
                INSERT INTO stock_screener_scan_snapshots (
                    snapshot_id,
                    captured_at,
                    snapshot_version,
                    market,
                    target_direction,
                    strategy_id,
                    strategy_name,
                    strategy_source,
                    scan_mode,
                    first_page,
                    last_page,
                    pages_scanned,
                    candidates_scanned,
                    candidates_unique,
                    candidates_returned,
                    duplicates_removed,
                    payload_hash,
                    payload
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    snapshot_id,
                    captured_at,
                    SNAPSHOT_VERSION,
                    request["market"],
                    request["target_direction"],
                    request["strategy"]["id"],
                    request["strategy"].get("name"),
                    request["strategy"].get("source"),
                    scan["mode"],
                    scan["first_page"],
                    scan["last_page"],
                    scan["pages_scanned"],
                    scan["candidates_scanned"],
                    len(payload["universe"]),
                    scan["candidates_returned"],
                    scan["duplicates_removed"],
                    payload_hash,
                    serialized,
                ],
            )

        return {
            "status": "captured",
            **capture,
            "payload_hash": payload_hash,
            "candidates_unique": len(payload["universe"]),
        }

    def get_history(
        self,
        *,
        market: Optional[str] = None,
        target_direction: Optional[str] = None,
        strategy_id: Optional[int] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> Dict[str, Any]:
        if not 1 <= limit <= 100:
            raise ValueError("limit 必须在 1～100 之间")
        if offset < 0:
            raise ValueError("offset 不能小于 0")
        normalized_market = market.strip().upper() if market else None
        if normalized_market and normalized_market not in {"US", "HK", "CN", "SG"}:
            raise ValueError("market 必须是 US、HK、CN 或 SG")
        direction = (
            target_direction.strip().upper()
            if target_direction
            else None
        )
        if direction and direction not in {"LONG", "SHORT"}:
            raise ValueError("target_direction 必须是 LONG 或 SHORT")
        if strategy_id is not None and strategy_id <= 0:
            raise ValueError("strategy_id 必须是正整数")

        clauses = []
        parameters = []
        if normalized_market:
            clauses.append("market = ?")
            parameters.append(normalized_market)
        if direction:
            clauses.append("target_direction = ?")
            parameters.append(direction)
        if strategy_id is not None:
            clauses.append("strategy_id = ?")
            parameters.append(strategy_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        with self.connection_factory() as connection:
            total = connection.execute(
                f"SELECT COUNT(*) FROM stock_screener_scan_snapshots {where}",
                parameters,
            ).fetchone()[0]
            rows = connection.execute(
                f"""
                SELECT
                    snapshot_id,
                    captured_at,
                    snapshot_version,
                    market,
                    target_direction,
                    strategy_id,
                    strategy_name,
                    strategy_source,
                    scan_mode,
                    first_page,
                    last_page,
                    pages_scanned,
                    candidates_scanned,
                    candidates_unique,
                    candidates_returned,
                    duplicates_removed,
                    payload_hash
                FROM stock_screener_scan_snapshots
                {where}
                ORDER BY captured_at DESC, snapshot_id DESC
                LIMIT ? OFFSET ?
                """,
                [*parameters, limit, offset],
            ).fetchall()

        return {
            "snapshot_version": SNAPSHOT_VERSION,
            "items": [self._summary(row) for row in rows],
            "pagination": {
                "total": int(total),
                "limit": limit,
                "offset": offset,
            },
        }

    def get_snapshot(self, snapshot_id: str) -> Dict[str, Any]:
        normalized_id = snapshot_id.strip()
        if not normalized_id or len(normalized_id) > 64:
            raise ValueError("snapshot_id 无效")
        with self.connection_factory() as connection:
            row = connection.execute(
                """
                SELECT
                    snapshot_id,
                    captured_at,
                    snapshot_version,
                    market,
                    target_direction,
                    strategy_id,
                    strategy_name,
                    strategy_source,
                    scan_mode,
                    first_page,
                    last_page,
                    pages_scanned,
                    candidates_scanned,
                    candidates_unique,
                    candidates_returned,
                    duplicates_removed,
                    payload_hash,
                    payload
                FROM stock_screener_scan_snapshots
                WHERE snapshot_id = ?
                """,
                [normalized_id],
            ).fetchone()
        if row is None:
            raise KeyError(normalized_id)
        return {**self._summary(row[:17]), "payload": json.loads(row[17])}

    def get_auto_capture_templates(self) -> List[Dict[str, Any]]:
        """Return the latest intact v2 request for each saved cohort."""
        with self.connection_factory() as connection:
            rows = connection.execute(
                """
                SELECT
                    snapshot_id,
                    captured_at,
                    snapshot_version,
                    market,
                    target_direction,
                    strategy_id,
                    strategy_name,
                    strategy_source,
                    payload_hash,
                    payload
                FROM stock_screener_scan_snapshots
                WHERE snapshot_version = ?
                ORDER BY captured_at DESC, snapshot_id DESC
                """,
                [SNAPSHOT_VERSION],
            ).fetchall()

        templates: List[Dict[str, Any]] = []
        seen: set[Tuple[str, str, int, str]] = set()
        for row in rows:
            coverage = self._coverage_row(row)
            if not coverage["integrity_valid"]:
                continue
            key = (
                coverage["market"],
                coverage["target_direction"],
                coverage["strategy_id"],
                coverage["policy_hash"],
            )
            if key in seen:
                continue
            seen.add(key)
            payload = json.loads(row[9])
            templates.append({
                "source_snapshot_id": coverage["snapshot_id"],
                "captured_at": _utc_iso(coverage["captured_at"]),
                "capture_date": coverage["capture_date"],
                "market": coverage["market"],
                "target_direction": coverage["target_direction"],
                "strategy_id": coverage["strategy_id"],
                "strategy_name": coverage["strategy_name"],
                "strategy_source": coverage["strategy_source"],
                "policy_hash": coverage["policy_hash"],
                "request": payload["request"],
            })
        return templates

    def get_coverage(
        self,
        *,
        days: int = 365,
        market: Optional[str] = None,
        target_direction: Optional[str] = None,
        strategy_id: Optional[int] = None,
        current_time: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        if not 1 <= int(days) <= 3650:
            raise ValueError("days 必须在 1～3650 之间")
        normalized_market, direction = self._normalize_scope(
            market,
            target_direction,
            strategy_id,
        )
        now = current_time or self.clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        now = now.astimezone(timezone.utc)
        cutoff = now - timedelta(days=int(days))

        clauses = ["captured_at >= ?"]
        parameters: List[Any] = [cutoff]
        if normalized_market:
            clauses.append("market = ?")
            parameters.append(normalized_market)
        if direction:
            clauses.append("target_direction = ?")
            parameters.append(direction)
        if strategy_id is not None:
            clauses.append("strategy_id = ?")
            parameters.append(strategy_id)

        with self.connection_factory() as connection:
            rows = connection.execute(
                f"""
                SELECT
                    snapshot_id,
                    captured_at,
                    snapshot_version,
                    market,
                    target_direction,
                    strategy_id,
                    strategy_name,
                    strategy_source,
                    payload_hash,
                    payload
                FROM stock_screener_scan_snapshots
                WHERE {' AND '.join(clauses)}
                ORDER BY captured_at, snapshot_id
                """,
                parameters,
            ).fetchall()

        parsed_rows = [self._coverage_row(row) for row in rows]
        daily_rows: Dict[Tuple[str, str, int, str, str], Dict[str, Any]] = {}
        for row in parsed_rows:
            key = (
                row["market"],
                row["target_direction"],
                row["strategy_id"],
                row["policy_hash"],
                row["capture_date"],
            )
            previous = daily_rows.get(key)
            if previous is None or row["captured_at"] > previous["captured_at"]:
                daily_rows[key] = row

        cohorts: Dict[Tuple[str, str, int, str], List[Dict[str, Any]]] = {}
        raw_counts = Counter()
        for row in parsed_rows:
            cohort_key = (
                row["market"],
                row["target_direction"],
                row["strategy_id"],
                row["policy_hash"],
            )
            raw_counts[cohort_key] += 1
        for row in daily_rows.values():
            cohort_key = (
                row["market"],
                row["target_direction"],
                row["strategy_id"],
                row["policy_hash"],
            )
            cohorts.setdefault(cohort_key, []).append(row)

        groups = [
            self._coverage_group(key, group_rows, raw_counts[key])
            for key, group_rows in cohorts.items()
        ]
        groups.sort(
            key=lambda item: (
                item["market"],
                item["target_direction"],
                item["strategy_id"],
                item["policy_hash"],
            )
        )
        ready_groups = [group for group in groups if group["ready"]]
        environment_ready_groups = [
            group
            for group in groups
            if group["market_environment"]["ready"]
        ]
        environment_available_count = sum(
            group["market_environment"]["available_snapshot_count"]
            for group in groups
        )
        environment_total_count = sum(
            group["market_environment"]["total_snapshot_count"]
            for group in groups
        )
        environment_missing_reasons = Counter()
        for group in groups:
            environment_missing_reasons.update(
                group["market_environment"]["missing_reasons"]
            )
        return {
            "coverage_version": COVERAGE_VERSION,
            "snapshot_version": SNAPSHOT_VERSION,
            "window_days": int(days),
            "raw_snapshot_count": len(parsed_rows),
            "daily_snapshot_count": len(daily_rows),
            "cohort_count": len(groups),
            "ready_cohort_count": len(ready_groups),
            "ready_for_scope_evaluation": bool(ready_groups),
            "minimums": {
                "capture_dates": MIN_CAPTURE_DATES,
                "calendar_span_days": MIN_CALENDAR_SPAN_DAYS,
                "distinct_universe_symbols": MIN_DISTINCT_UNIVERSE_SYMBOLS,
                "distinct_selected_symbols": MIN_DISTINCT_SELECTED_SYMBOLS,
                "universe_observations": MIN_UNIVERSE_OBSERVATIONS,
                "selected_observations": MIN_SELECTED_OBSERVATIONS,
                "integrity_rate": MIN_INTEGRITY_RATE,
            },
            "market_environment": {
                "ready": bool(environment_ready_groups),
                "ready_cohort_count": len(environment_ready_groups),
                "total_cohort_count": len(groups),
                "available_snapshot_count": environment_available_count,
                "total_snapshot_count": environment_total_count,
                "coverage": (
                    environment_available_count / environment_total_count
                    if environment_total_count
                    else 0.0
                ),
                "minimum_coverage": MIN_MARKET_ENVIRONMENT_COVERAGE,
                "missing_reasons": dict(environment_missing_reasons),
            },
            "groups": groups,
        }

    @staticmethod
    def _normalize_scope(
        market: Optional[str],
        target_direction: Optional[str],
        strategy_id: Optional[int],
    ) -> Tuple[Optional[str], Optional[str]]:
        normalized_market = market.strip().upper() if market else None
        if normalized_market and normalized_market not in _MARKET_TIMEZONES:
            raise ValueError("market 必须是 US、HK、CN 或 SG")
        direction = target_direction.strip().upper() if target_direction else None
        if direction and direction not in {"LONG", "SHORT"}:
            raise ValueError("target_direction 必须是 LONG 或 SHORT")
        if strategy_id is not None and strategy_id <= 0:
            raise ValueError("strategy_id 必须是正整数")
        return normalized_market, direction

    def _coverage_row(self, row: tuple) -> Dict[str, Any]:
        captured_at = row[1]
        if captured_at.tzinfo is None:
            captured_at = captured_at.replace(tzinfo=timezone.utc)
        captured_at = captured_at.astimezone(timezone.utc)
        market = row[3]
        integrity_reasons: List[str] = []
        try:
            payload = json.loads(row[9])
        except (TypeError, ValueError):
            payload = None
            integrity_reasons.append("invalid_payload_json")

        policy_hash = "invalid"
        universe_symbols: List[str] = []
        selected_symbols: List[str] = []
        market_environment_available = False
        market_environment_missing_reason = "invalid_payload"
        if isinstance(payload, dict):
            request = payload.get("request")
            capture = payload.get("capture")
            universe = payload.get("universe")
            selected = payload.get("selected_symbols")
            if not isinstance(request, dict):
                integrity_reasons.append("missing_request")
                request = {}
            if not isinstance(capture, dict):
                integrity_reasons.append("missing_capture")
                capture = {}
            if not isinstance(universe, list):
                integrity_reasons.append("missing_universe")
                universe = []
            if not isinstance(selected, list):
                integrity_reasons.append("missing_selected_symbols")
                selected = []

            strategy = request.get("strategy")
            if not isinstance(strategy, dict):
                integrity_reasons.append("invalid_strategy")
                strategy = {}
            policy = {
                key: value
                for key, value in request.items()
                if key not in {"market", "target_direction", "strategy"}
            }
            policy["strategy_source"] = strategy.get("source")
            policy["filter_version"] = capture.get("filter_version")
            policy["relative_strength_version"] = capture.get(
                "relative_strength_version"
            )
            policy_hash = sha256(
                _canonical_json({
                    "snapshot_version": row[2],
                    "policy": policy,
                }).encode("utf-8")
            ).hexdigest()
            selected_from_universe = []
            for item in universe:
                if not isinstance(item, dict):
                    integrity_reasons.append("invalid_universe_item")
                    continue
                candidate = item.get("candidate")
                if not isinstance(candidate, dict):
                    integrity_reasons.append("invalid_universe_candidate")
                    continue
                symbol = str(candidate.get("symbol") or "").strip().upper()
                if not symbol:
                    integrity_reasons.append("missing_universe_symbol")
                    continue
                universe_symbols.append(symbol)
                if item.get("selected") is True:
                    selected_from_universe.append(symbol)
            selected_symbols = [
                str(symbol).strip().upper()
                for symbol in selected
                if str(symbol).strip()
            ]
            expected_fields = {
                "snapshot_id": row[0],
                "captured_at": _utc_iso(captured_at),
                "snapshot_version": row[2],
            }
            if any(
                capture.get(key) != value
                for key, value in expected_fields.items()
            ):
                integrity_reasons.append("capture_metadata_mismatch")
            if row[2] not in SUPPORTED_SNAPSHOT_VERSIONS:
                integrity_reasons.append("unsupported_snapshot_version")
            if capture.get("filter_version") != FILTER_VERSION:
                integrity_reasons.append("unsupported_filter_version")
            if (
                capture.get("relative_strength_version")
                not in SUPPORTED_RELATIVE_STRENGTH_VERSIONS
            ):
                integrity_reasons.append(
                    "unsupported_relative_strength_version"
                )
            request_strategy = strategy
            if (
                request.get("market") != market
                or request.get("target_direction") != row[4]
                or request_strategy.get("id") != int(row[5])
            ):
                integrity_reasons.append("request_scope_mismatch")
            if selected_symbols != selected_from_universe:
                integrity_reasons.append("selection_mismatch")
            (
                market_environment_available,
                market_environment_missing_reason,
                benchmark_integrity_reasons,
            ) = self._market_environment_status(
                row[2],
                request,
                payload.get("metric_basis"),
                payload.get("pages"),
            )
            integrity_reasons.extend(benchmark_integrity_reasons)
            if (
                capture.get("relative_strength_version")
                == RELATIVE_STRENGTH_VERSION
                and (
                    not isinstance(payload.get("metric_basis"), dict)
                    or payload["metric_basis"].get("version")
                    != RELATIVE_STRENGTH_VERSION
                    or payload["metric_basis"].get("industry_basis")
                    not in {
                        "current_page_leave_one_out_industry_median",
                        "scan_range_leave_one_out_industry_median",
                    }
                    or payload["metric_basis"].get(
                        "industry_membership_source"
                    ) != "current_screener_scan_candidates"
                    or payload["metric_basis"].get(
                        "minimum_industry_peers"
                    ) != MIN_INDUSTRY_PEERS
                    or payload["metric_basis"].get(
                        "historical_industry_membership"
                    ) is not False
                )
            ):
                integrity_reasons.append(
                    "relative_strength_basis_mismatch"
                )
            if capture.get("payload_hash") != row[8]:
                integrity_reasons.append("payload_hash_mismatch")
            hash_payload = json.loads(json.dumps(payload))
            if isinstance(hash_payload.get("capture"), dict):
                hash_payload["capture"].pop("payload_hash", None)
            recalculated = sha256(
                _canonical_json(hash_payload).encode("utf-8")
            ).hexdigest()
            if recalculated != row[8]:
                integrity_reasons.append("payload_hash_invalid")

        capture_date = captured_at.astimezone(
            _MARKET_TIMEZONES.get(market, timezone.utc)
        ).date().isoformat()
        return {
            "snapshot_id": row[0],
            "captured_at": captured_at,
            "capture_date": capture_date,
            "snapshot_version": row[2],
            "market": market,
            "target_direction": row[4],
            "strategy_id": int(row[5]),
            "strategy_name": row[6],
            "strategy_source": row[7],
            "policy_hash": policy_hash,
            "universe_symbols": universe_symbols,
            "selected_symbols": selected_symbols,
            "market_environment_available": market_environment_available,
            "market_environment_missing_reason": (
                market_environment_missing_reason
            ),
            "integrity_valid": not integrity_reasons,
            "integrity_reasons": integrity_reasons,
        }

    @staticmethod
    def _coverage_group(
        key: Tuple[str, str, int, str],
        rows: List[Dict[str, Any]],
        raw_snapshot_count: int,
    ) -> Dict[str, Any]:
        rows.sort(key=lambda item: item["captured_at"])
        capture_dates = sorted({row["capture_date"] for row in rows})
        span_days = 0
        if capture_dates:
            first_date = datetime.fromisoformat(capture_dates[0])
            last_date = datetime.fromisoformat(capture_dates[-1])
            span_days = (last_date - first_date).days + 1
        universe_symbols = {
            symbol for row in rows for symbol in row["universe_symbols"]
        }
        selected_symbols = {
            symbol for row in rows for symbol in row["selected_symbols"]
        }
        universe_observations = sum(
            len(row["universe_symbols"])
            for row in rows
        )
        selected_observations = sum(
            len(row["selected_symbols"])
            for row in rows
        )
        valid_count = sum(row["integrity_valid"] for row in rows)
        integrity_rate = valid_count / len(rows) if rows else 0.0
        integrity_reasons = Counter(
            reason
            for row in rows
            for reason in row["integrity_reasons"]
        )
        not_ready_reasons = []
        if len(capture_dates) < MIN_CAPTURE_DATES:
            not_ready_reasons.append("insufficient_capture_dates")
        if span_days < MIN_CALENDAR_SPAN_DAYS:
            not_ready_reasons.append("insufficient_calendar_span")
        if len(universe_symbols) < MIN_DISTINCT_UNIVERSE_SYMBOLS:
            not_ready_reasons.append("insufficient_universe_symbols")
        if len(selected_symbols) < MIN_DISTINCT_SELECTED_SYMBOLS:
            not_ready_reasons.append("insufficient_selected_symbols")
        if universe_observations < MIN_UNIVERSE_OBSERVATIONS:
            not_ready_reasons.append("insufficient_universe_observations")
        if selected_observations < MIN_SELECTED_OBSERVATIONS:
            not_ready_reasons.append("insufficient_selected_observations")
        if integrity_rate < MIN_INTEGRITY_RATE:
            not_ready_reasons.append("snapshot_integrity_incomplete")
        environment_available_count = sum(
            row["market_environment_available"]
            for row in rows
        )
        environment_coverage = (
            environment_available_count / len(rows)
            if rows
            else 0.0
        )
        environment_missing_reasons = Counter(
            row["market_environment_missing_reason"]
            for row in rows
            if not row["market_environment_available"]
        )
        latest = rows[-1] if rows else None
        return {
            "market": key[0],
            "target_direction": key[1],
            "strategy_id": key[2],
            "strategy_name": latest["strategy_name"] if latest else None,
            "strategy_source": latest["strategy_source"] if latest else None,
            "snapshot_version": latest["snapshot_version"] if latest else None,
            "policy_hash": key[3],
            "raw_snapshot_count": int(raw_snapshot_count),
            "daily_snapshot_count": len(rows),
            "duplicate_same_day_count": int(raw_snapshot_count) - len(rows),
            "capture_dates": len(capture_dates),
            "first_capture_date": capture_dates[0] if capture_dates else None,
            "latest_capture_date": capture_dates[-1] if capture_dates else None,
            "calendar_span_days": span_days,
            "universe_observations": universe_observations,
            "selected_observations": selected_observations,
            "distinct_universe_symbols": len(universe_symbols),
            "distinct_selected_symbols": len(selected_symbols),
            "valid_snapshot_count": valid_count,
            "integrity_rate": integrity_rate,
            "integrity_reasons": dict(integrity_reasons),
            "ready": not not_ready_reasons,
            "not_ready_reasons": not_ready_reasons,
            "market_environment": {
                "available_snapshot_count": environment_available_count,
                "total_snapshot_count": len(rows),
                "coverage": environment_coverage,
                "minimum_coverage": MIN_MARKET_ENVIRONMENT_COVERAGE,
                "missing_reasons": dict(environment_missing_reasons),
                "ready": (
                    not not_ready_reasons
                    and environment_coverage
                    >= MIN_MARKET_ENVIRONMENT_COVERAGE
                ),
            },
        }

    @staticmethod
    def _market_environment_status(
        snapshot_version: str,
        request: Dict[str, Any],
        metric_basis: Any,
        pages: Any,
    ) -> Tuple[bool, Optional[str], List[str]]:
        if snapshot_version == LEGACY_SNAPSHOT_VERSION:
            return False, "legacy_snapshot_version", []
        if snapshot_version != SNAPSHOT_VERSION:
            return False, "unsupported_snapshot_version", []
        if not isinstance(metric_basis, dict):
            return False, "missing_metric_basis", ["missing_metric_basis"]
        integrity_reasons = []
        if (
            metric_basis.get("benchmark_symbol")
            != request.get("benchmark_symbol")
            or metric_basis.get("target_direction")
            != request.get("target_direction")
        ):
            integrity_reasons.append("benchmark_basis_mismatch")
        observations = metric_basis.get("benchmark_observations")
        if not isinstance(observations, list):
            return (
                False,
                "missing_benchmark_observations",
                [*integrity_reasons, "missing_benchmark_observations"],
            )
        if not observations:
            return (
                False,
                "missing_benchmark_observations",
                [*integrity_reasons, "missing_benchmark_observations"],
            )

        complete = True
        observation_pages = []
        for observation in observations:
            if not isinstance(observation, dict):
                integrity_reasons.append("invalid_benchmark_observation")
                complete = False
                continue
            if not isinstance(observation.get("page"), int):
                integrity_reasons.append("invalid_benchmark_observation")
            else:
                observation_pages.append(observation["page"])
            for key in ("ten_day_change_rate", "half_year_change_rate"):
                if key not in observation:
                    integrity_reasons.append("invalid_benchmark_observation")
                    complete = False
                    continue
                value = observation[key]
                if value is None:
                    complete = False
                    continue
                try:
                    number = float(value)
                except (TypeError, ValueError):
                    integrity_reasons.append("invalid_benchmark_observation")
                    complete = False
                    continue
                if not (-float("inf") < number < float("inf")):
                    integrity_reasons.append("invalid_benchmark_observation")
                    complete = False
        benchmark_returns = metric_basis.get("benchmark_returns")
        if not isinstance(benchmark_returns, dict):
            integrity_reasons.append("missing_benchmark_returns")
        else:
            summary_keys = (
                "ten_day_change_rate",
                "half_year_change_rate",
            )
            if any(key not in benchmark_returns for key in summary_keys):
                integrity_reasons.append("invalid_benchmark_returns")
            if isinstance(observations[0], dict) and any(
                benchmark_returns.get(key) != observations[0].get(key)
                for key in summary_keys
            ):
                integrity_reasons.append("benchmark_summary_mismatch")
        if not isinstance(pages, list):
            integrity_reasons.append("missing_pages")
        else:
            expected_pages = [
                page.get("page")
                for page in pages
                if isinstance(page, dict)
            ]
            if (
                len(expected_pages) != len(pages)
                or observation_pages != expected_pages
            ):
                integrity_reasons.append("benchmark_page_mismatch")
        return (
            complete and not integrity_reasons,
            None if complete and not integrity_reasons
            else "incomplete_benchmark_returns",
            list(dict.fromkeys(integrity_reasons)),
        )

    @staticmethod
    def _summary(row) -> Dict[str, Any]:
        return {
            "snapshot_id": row[0],
            "captured_at": _utc_iso(row[1]),
            "snapshot_version": row[2],
            "market": row[3],
            "target_direction": row[4],
            "strategy_id": int(row[5]),
            "strategy_name": row[6],
            "strategy_source": row[7],
            "scan_mode": row[8],
            "first_page": int(row[9]),
            "last_page": int(row[10]),
            "pages_scanned": int(row[11]),
            "candidates_scanned": int(row[12]),
            "candidates_unique": int(row[13]),
            "candidates_returned": int(row[14]),
            "duplicates_removed": int(row[15]),
            "payload_hash": row[16],
        }


_stock_screener_snapshot_service: Optional[StockScreenerSnapshotService] = None


def get_stock_screener_snapshot_service() -> StockScreenerSnapshotService:
    global _stock_screener_snapshot_service
    if _stock_screener_snapshot_service is None:
        _stock_screener_snapshot_service = StockScreenerSnapshotService()
    return _stock_screener_snapshot_service
