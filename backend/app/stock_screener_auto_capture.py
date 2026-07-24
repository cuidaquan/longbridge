from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional
from uuid import uuid4
from zoneinfo import ZoneInfo

from .db import get_connection
from .stock_candidate_data import is_market_trading_day
from .stock_picker_ai_snapshots import sanitize_error
from .stock_screener_snapshots import get_stock_screener_snapshot_service


AUTO_CAPTURE_VERSION = "stock-screener-auto-capture-v1"
AUTO_CAPTURE_LOCAL_HOUR = 17
RUN_STALE_MINUTES = 30
MAX_DAILY_ATTEMPTS = 3
_MARKET_TIMEZONES = {
    "US": ZoneInfo("America/New_York"),
    "HK": ZoneInfo("Asia/Hong_Kong"),
    "CN": ZoneInfo("Asia/Shanghai"),
    "SG": ZoneInfo("Asia/Singapore"),
}
_HEAVY_FILTER_KEYS = {
    "min_revenue_yoy",
    "max_revenue_yoy",
    "min_net_profit_yoy",
    "max_net_profit_yoy",
    "min_operating_cash_flow_yoy",
    "min_analyst_alignment",
    "min_eps_revision_alignment",
    "min_days_to_financial_event",
    "min_days_to_corporate_action",
    "max_initial_margin_ratio",
    "min_short_selling_quantity",
}


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _utc_iso(value: Optional[datetime]) -> Optional[str]:
    return _utc(value).isoformat() if value is not None else None


class StockScreenerAutoCaptureService:
    """Replay intact lightweight snapshot cohorts after the market close."""

    def __init__(
        self,
        connection_factory: Callable = get_connection,
        snapshot_service: Optional[Any] = None,
        searcher: Optional[Callable[..., Dict[str, Any]]] = None,
        trading_day_loader: Callable = is_market_trading_day,
        clock: Callable[[], datetime] = (
            lambda: datetime.now(timezone.utc)
        ),
    ) -> None:
        self.connection_factory = connection_factory
        self.snapshot_service = (
            snapshot_service or get_stock_screener_snapshot_service()
        )
        self.searcher = searcher or self._search
        self.trading_day_loader = trading_day_loader
        self.clock = clock

    def capture_due(self) -> Dict[str, Any]:
        now = _utc(self.clock())
        captured: List[Dict[str, Any]] = []
        skipped: List[Dict[str, Any]] = []
        errors: List[Dict[str, Any]] = []
        trading_days: Dict[tuple[str, str], bool] = {}

        for template in self._templates():
            scope = self._scope(template)
            reason = template.get("ineligible_reason")
            if reason:
                skipped.append({**scope, "reason": reason})
                continue

            local_now = now.astimezone(_MARKET_TIMEZONES[template["market"]])
            capture_date = local_now.date()
            scope["capture_date"] = capture_date.isoformat()
            if local_now.hour < AUTO_CAPTURE_LOCAL_HOUR:
                skipped.append({**scope, "reason": "session_not_closed"})
                continue
            if template["capture_date"] == capture_date.isoformat():
                skipped.append({**scope, "reason": "already_captured"})
                continue

            existing = self._existing_run(template, capture_date, now)
            if existing:
                skipped.append({**scope, "reason": existing})
                continue

            calendar_key = (template["market"], capture_date.isoformat())
            try:
                if calendar_key not in trading_days:
                    trading_days[calendar_key] = bool(
                        self.trading_day_loader(
                            template["market"],
                            capture_date,
                        )
                    )
            except Exception as exc:
                errors.append({
                    **scope,
                    "error": sanitize_error(exc),
                    "reason": "trading_calendar_unavailable",
                })
                continue
            if not trading_days[calendar_key]:
                skipped.append({**scope, "reason": "market_closed"})
                continue

            run_id = uuid4().hex
            self._start_run(run_id, template, capture_date, now)
            try:
                result = self.searcher(**self._search_arguments(template))
                snapshot = result.get("snapshot") or {}
                if snapshot.get("status") != "captured":
                    raise RuntimeError("Screener 自动采集未生成快照")
                snapshot_id = str(snapshot["snapshot_id"])
            except Exception as exc:
                error = sanitize_error(exc)
                self._finish_run(run_id, now, "failed", error=error)
                errors.append({**scope, "run_id": run_id, "error": error})
                continue

            self._finish_run(
                run_id,
                _utc(self.clock()),
                "succeeded",
                snapshot_id=snapshot_id,
            )
            captured.append({
                **scope,
                "run_id": run_id,
                "snapshot_id": snapshot_id,
                "candidates_unique": snapshot.get("candidates_unique", 0),
            })

        return {
            "auto_capture_version": AUTO_CAPTURE_VERSION,
            "max_daily_attempts": MAX_DAILY_ATTEMPTS,
            "captured": captured,
            "skipped": skipped,
            "errors": errors,
        }

    def get_status(self, limit: int = 20) -> Dict[str, Any]:
        if not 1 <= int(limit) <= 100:
            raise ValueError("limit 必须在 1～100 之间")
        templates = self._templates()
        with self.connection_factory() as connection:
            rows = connection.execute(
                """
                SELECT
                    run_id,
                    started_at,
                    completed_at,
                    capture_date,
                    market,
                    target_direction,
                    strategy_id,
                    policy_hash,
                    source_snapshot_id,
                    status,
                    snapshot_id,
                    error
                FROM stock_screener_auto_capture_runs
                ORDER BY started_at DESC, run_id DESC
                LIMIT ?
                """,
                [int(limit)],
            ).fetchall()
            status_rows = connection.execute(
                """
                SELECT status, COUNT(*)
                FROM stock_screener_auto_capture_runs
                GROUP BY status
                """
            ).fetchall()
        return {
            "auto_capture_version": AUTO_CAPTURE_VERSION,
            "max_daily_attempts": MAX_DAILY_ATTEMPTS,
            "template_count": len(templates),
            "eligible_template_count": sum(
                not template.get("ineligible_reason")
                for template in templates
            ),
            "status_counts": dict(status_rows),
            "templates": [
                {
                    **self._scope(template),
                    "latest_capture_date": template["capture_date"],
                    "eligible": not template.get("ineligible_reason"),
                    "ineligible_reason": template.get("ineligible_reason"),
                }
                for template in templates
            ],
            "runs": [self._run_summary(row) for row in rows],
        }

    def _templates(self) -> List[Dict[str, Any]]:
        templates = self.snapshot_service.get_auto_capture_templates()
        for template in templates:
            request = template["request"]
            filters = request.get("filters") or {}
            if (
                request.get("include_fundamentals")
                or request.get("include_margin_requirements")
                or request.get("include_short_capacity")
                or request.get("include_corporate_actions")
                or _HEAVY_FILTER_KEYS.intersection(filters)
            ):
                template["ineligible_reason"] = "heavy_enrichment_not_supported"
            else:
                template["ineligible_reason"] = None
        return templates

    @staticmethod
    def _scope(template: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "market": template["market"],
            "target_direction": template["target_direction"],
            "strategy_id": template["strategy_id"],
            "strategy_name": template.get("strategy_name"),
            "policy_hash": template["policy_hash"],
            "source_snapshot_id": template["source_snapshot_id"],
        }

    @staticmethod
    def _search_arguments(template: Dict[str, Any]) -> Dict[str, Any]:
        request = template["request"]
        strategy = request["strategy"]
        filters = dict(request.get("filters") or {})
        filters.pop("require_normal_trade_status", None)
        return {
            "market": request["market"],
            "strategy_id": int(strategy["id"]),
            "page": int(request.get("page", 0)),
            "size": int(request.get("size", 20)),
            "filters": filters,
            "include_indexes": bool(request.get("include_indexes", True)),
            "target_direction": request["target_direction"],
            "benchmark_symbol": request.get("benchmark_symbol"),
            "include_short_risk": bool(
                request.get("include_short_risk", True)
            ),
            "include_tradeability": bool(
                request.get("include_tradeability", True)
            ),
            "require_normal_trade_status": bool(
                request.get("require_normal_trade_status", True)
            ),
            "include_fundamentals": False,
            "include_margin_requirements": False,
            "include_short_capacity": False,
            "fundamental_event_window_days": int(
                request.get("fundamental_event_window_days", 30)
            ),
            "include_corporate_actions": False,
            "scan_pages": int(request.get("scan_pages", 1)),
            "capture_snapshot": True,
            "strategy_name": strategy.get("name"),
            "strategy_source": strategy.get("source"),
        }

    def _existing_run(
        self,
        template: Dict[str, Any],
        capture_date,
        now: datetime,
    ) -> Optional[str]:
        with self.connection_factory() as connection:
            row = connection.execute(
                """
                SELECT run_id, status, started_at
                FROM stock_screener_auto_capture_runs
                WHERE market = ?
                  AND target_direction = ?
                  AND strategy_id = ?
                  AND policy_hash = ?
                  AND capture_date = ?
                ORDER BY started_at DESC
                LIMIT 1
                """,
                [
                    template["market"],
                    template["target_direction"],
                    template["strategy_id"],
                    template["policy_hash"],
                    capture_date,
                ],
            ).fetchone()
            failed_count = connection.execute(
                """
                SELECT COUNT(*)
                FROM stock_screener_auto_capture_runs
                WHERE market = ?
                  AND target_direction = ?
                  AND strategy_id = ?
                  AND policy_hash = ?
                  AND capture_date = ?
                  AND status = 'failed'
                """,
                [
                    template["market"],
                    template["target_direction"],
                    template["strategy_id"],
                    template["policy_hash"],
                    capture_date,
                ],
            ).fetchone()[0]
        if row is None:
            return (
                "attempt_limit_reached"
                if failed_count >= MAX_DAILY_ATTEMPTS
                else None
            )
        if row[1] == "succeeded":
            return "already_captured"
        if row[1] == "running" and now - _utc(row[2]) < timedelta(
            minutes=RUN_STALE_MINUTES
        ):
            return "capture_running"
        if row[1] == "running":
            with self.connection_factory() as connection:
                connection.execute(
                    """
                    UPDATE stock_screener_auto_capture_runs
                    SET completed_at = ?, status = 'failed', error = ?
                    WHERE run_id = ? AND status = 'running'
                    """,
                    [now, "stale_run_replaced", row[0]],
                )
            failed_count += 1
        return (
            "attempt_limit_reached"
            if failed_count >= MAX_DAILY_ATTEMPTS
            else None
        )

    def _start_run(
        self,
        run_id: str,
        template: Dict[str, Any],
        capture_date,
        started_at: datetime,
    ) -> None:
        with self.connection_factory() as connection:
            connection.execute(
                """
                INSERT INTO stock_screener_auto_capture_runs (
                    run_id,
                    started_at,
                    capture_date,
                    market,
                    target_direction,
                    strategy_id,
                    policy_hash,
                    source_snapshot_id,
                    status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'running')
                """,
                [
                    run_id,
                    started_at,
                    capture_date,
                    template["market"],
                    template["target_direction"],
                    template["strategy_id"],
                    template["policy_hash"],
                    template["source_snapshot_id"],
                ],
            )

    def _finish_run(
        self,
        run_id: str,
        completed_at: datetime,
        status: str,
        *,
        snapshot_id: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        with self.connection_factory() as connection:
            connection.execute(
                """
                UPDATE stock_screener_auto_capture_runs
                SET completed_at = ?, status = ?, snapshot_id = ?, error = ?
                WHERE run_id = ? AND status = 'running'
                """,
                [completed_at, status, snapshot_id, error, run_id],
            )

    @staticmethod
    def _run_summary(row: tuple) -> Dict[str, Any]:
        return {
            "run_id": row[0],
            "started_at": _utc_iso(row[1]),
            "completed_at": _utc_iso(row[2]),
            "capture_date": row[3].isoformat(),
            "market": row[4],
            "target_direction": row[5],
            "strategy_id": int(row[6]),
            "policy_hash": row[7],
            "source_snapshot_id": row[8],
            "status": row[9],
            "snapshot_id": row[10],
            "error": row[11],
        }

    @staticmethod
    def _search(**kwargs) -> Dict[str, Any]:
        from .stock_screener import get_stock_screener_service

        return get_stock_screener_service().search(**kwargs)


_stock_screener_auto_capture_service: Optional[
    StockScreenerAutoCaptureService
] = None


def get_stock_screener_auto_capture_service(
) -> StockScreenerAutoCaptureService:
    global _stock_screener_auto_capture_service
    if _stock_screener_auto_capture_service is None:
        _stock_screener_auto_capture_service = (
            StockScreenerAutoCaptureService()
        )
    return _stock_screener_auto_capture_service
