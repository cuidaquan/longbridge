from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import threading
from typing import Any, Callable, Dict, Iterable, Optional
from urllib.error import HTTPError
from urllib.request import (
    HTTPRedirectHandler,
    Request,
    build_opener,
)
from uuid import uuid4

from .config import get_settings
from .db import get_connection
from .external_service_resilience import (
    get_stock_picker_reliability_snapshot,
)


RELIABILITY_CAPTURE_INTERVAL_SECONDS = 60
RELIABILITY_RETENTION_DAYS = 30
MIN_ALERT_REQUESTS = 5
FAILURE_RATE_WARNING = 0.5
TIMEOUT_RATE_WARNING = 0.3
REJECTED_RATE_WARNING = 0.2
AI_DEGRADATION_WARNING = 0.5
DELIVERY_MAX_ATTEMPTS = 3
DELIVERY_RETRY_SECONDS = 60
DELIVERY_BATCH_SIZE = 20


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(
        self,
        req,
        fp,
        code,
        msg,
        headers,
        newurl,
    ):
        return None


def _get_webhook_delivery_config() -> Dict[str, Any]:
    settings = get_settings()
    url = (settings.stock_picker_alert_webhook_url or "").strip()
    secret = (
        settings.stock_picker_alert_webhook_secret or ""
    ).strip()
    return {
        "enabled": settings.stock_picker_alert_webhook_enabled,
        "configured": bool(url),
        "signed": bool(secret),
        "url": url,
        "secret": secret,
        "timeout_seconds": (
            settings.stock_picker_alert_webhook_timeout_seconds
        ),
    }


def _send_webhook(
    config: Dict[str, Any],
    payload: Dict[str, Any],
) -> int:
    body = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "X-Longbridge-Event": payload["event"],
        "X-Longbridge-Delivery": str(payload["delivery_id"]),
    }
    secret = config.get("secret") or ""
    if secret:
        signature = hmac.new(
            secret.encode("utf-8"),
            body,
            hashlib.sha256,
        ).hexdigest()
        headers["X-Longbridge-Signature"] = f"sha256={signature}"
    request = Request(
        config["url"],
        data=body,
        headers=headers,
        method="POST",
    )
    try:
        opener = build_opener(_NoRedirectHandler())
        with opener.open(
            request,
            timeout=float(config["timeout_seconds"]),
        ) as response:
            return int(response.getcode())
    except HTTPError as exc:
        return int(exc.code)
    except Exception as exc:
        raise RuntimeError(
            "webhook request failed: "
            f"{type(exc).__name__}"
        ) from exc


class StockPickerReliabilityService:
    """Persist process metrics and maintain durable alert state."""

    def __init__(
        self,
        snapshot_provider: Callable[
            [], Dict[str, Any]
        ] = get_stock_picker_reliability_snapshot,
        connection_factory: Callable = get_connection,
        clock: Callable[[], datetime] = (
            lambda: datetime.now(timezone.utc)
        ),
        process_id: Optional[str] = None,
        delivery_config_provider: Callable[
            [], Dict[str, Any]
        ] = _get_webhook_delivery_config,
        webhook_sender: Callable[
            [Dict[str, Any], Dict[str, Any]],
            int,
        ] = _send_webhook,
    ) -> None:
        self.snapshot_provider = snapshot_provider
        self.connection_factory = connection_factory
        self.clock = clock
        self.process_id = process_id or uuid4().hex
        self.delivery_config_provider = delivery_config_provider
        self.webhook_sender = webhook_sender
        self._delivery_lock = threading.Lock()

    def capture(self) -> Dict[str, Any]:
        observed_at = self._as_utc(self.clock())
        current = self.snapshot_provider()
        with self.connection_factory() as conn:
            previous = self._latest_process_payload(conn)
            window = self._window_metrics(current, previous)
            alerts = self._evaluate_alerts(current, window)
            delivery_config = self.delivery_config_provider()
            observed_db = observed_at.replace(tzinfo=None)
            conn.execute("BEGIN TRANSACTION")
            try:
                row = conn.execute(
                    """
                    INSERT INTO stock_picker_reliability_snapshots (
                        observed_at,
                        process_id,
                        payload,
                        window_metrics,
                        alerts
                    )
                    VALUES (?, ?, ?, ?, ?)
                    RETURNING id
                    """,
                    [
                        observed_db,
                        self.process_id,
                        json.dumps(current, ensure_ascii=False),
                        json.dumps(window, ensure_ascii=False),
                        json.dumps(alerts, ensure_ascii=False),
                    ],
                ).fetchone()
                transitions = self._sync_alerts(
                    conn,
                    alerts,
                    observed_db,
                )
                delivery_ids = self._enqueue_deliveries(
                    conn,
                    transitions,
                    observed_db,
                    delivery_config,
                )
                conn.execute(
                    """
                    DELETE FROM stock_picker_reliability_snapshots
                    WHERE observed_at < ?
                    """,
                    [
                        (
                            observed_at
                            - timedelta(days=RELIABILITY_RETENTION_DAYS)
                        ).replace(tzinfo=None)
                    ],
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return {
            "id": int(row[0]),
            "observed_at": observed_at.isoformat(),
            "process_id": self.process_id,
            "window_metrics": window,
            "alerts": alerts,
            "alert_transitions": transitions,
            "delivery_ids": delivery_ids,
        }

    def get_current(self) -> Dict[str, Any]:
        current = self.snapshot_provider()
        delivery_config = self.delivery_config_provider()
        persistence_error = None
        try:
            with self.connection_factory() as conn:
                latest = self._latest_snapshot(conn)
                active_alerts = self._load_alerts(
                    conn,
                    statuses=("active",),
                    limit=100,
                )
                delivery_summary = self._delivery_summary(conn)
        except Exception as exc:
            latest = None
            active_alerts = []
            delivery_summary = self._empty_delivery_summary()
            persistence_error = str(exc)
        latest_age_seconds = None
        if latest is not None:
            latest_observed_at = datetime.fromisoformat(
                latest["observed_at"]
            )
            latest_age_seconds = round(
                max(
                    0.0,
                    (
                        self._as_utc(self.clock())
                        - self._as_utc(latest_observed_at)
                    ).total_seconds(),
                ),
                3,
            )
        limitations = [
            (
                "实时计数、熔断和并发额度仅在当前服务进程内共享；"
                "历史快照另存本地数据库"
            ),
            (
                "持久化快照和告警使用本地 DuckDB；多 worker 或"
                "多实例仍需要外部指标系统和共享协调"
            ),
        ]
        return {
            **current,
            "limitations": limitations,
            "persistence": {
                "enabled": True,
                "available": persistence_error is None,
                "error": persistence_error,
                "stale": (
                    latest_age_seconds is None
                    or latest_age_seconds
                    > RELIABILITY_CAPTURE_INTERVAL_SECONDS * 2
                ),
                "latest_snapshot_age_seconds": latest_age_seconds,
                "process_id": self.process_id,
                "capture_interval_seconds": (
                    RELIABILITY_CAPTURE_INTERVAL_SECONDS
                ),
                "retention_days": RELIABILITY_RETENTION_DAYS,
                "latest_snapshot": latest,
            },
            "alerts": {
                "active_count": len(active_alerts),
                "active": active_alerts,
            },
            "delivery": {
                "enabled": bool(delivery_config.get("enabled")),
                "configured": bool(
                    delivery_config.get("configured")
                ),
                "signed": bool(delivery_config.get("signed")),
                "max_attempts": DELIVERY_MAX_ATTEMPTS,
                **delivery_summary,
            },
        }

    def get_history(
        self,
        hours: int = 24,
        limit: int = 100,
    ) -> Dict[str, Any]:
        if hours < 1 or hours > 24 * RELIABILITY_RETENTION_DAYS:
            raise ValueError(
                "hours 必须在 1 到 "
                f"{24 * RELIABILITY_RETENTION_DAYS} 之间"
            )
        if limit < 1 or limit > 1000:
            raise ValueError("limit 必须在 1 到 1000 之间")
        cutoff = self._as_utc(self.clock()) - timedelta(hours=hours)
        with self.connection_factory() as conn:
            rows = conn.execute(
                """
                SELECT
                    id,
                    observed_at,
                    process_id,
                    payload,
                    window_metrics,
                    alerts
                FROM stock_picker_reliability_snapshots
                WHERE observed_at >= ?
                ORDER BY observed_at DESC, id DESC
                LIMIT ?
                """,
                [cutoff.replace(tzinfo=None), limit],
            ).fetchall()
            alerts = self._load_alerts(
                conn,
                statuses=("active", "resolved"),
                limit=200,
            )
            deliveries = self._load_deliveries(conn, limit=200)
        return {
            "hours": hours,
            "limit": limit,
            "items": [
                {
                    "id": int(row[0]),
                    "observed_at": self._isoformat(row[1]),
                    "process_id": row[2],
                    "payload": json.loads(row[3]),
                    "window_metrics": json.loads(row[4]),
                    "alerts": json.loads(row[5]),
                }
                for row in rows
            ],
            "alerts": alerts,
            "deliveries": deliveries,
        }

    def get_deliveries(self, limit: int = 100) -> Dict[str, Any]:
        if limit < 1 or limit > 1000:
            raise ValueError("limit 必须在 1 到 1000 之间")
        config = self.delivery_config_provider()
        with self.connection_factory() as conn:
            deliveries = self._load_deliveries(conn, limit=limit)
            summary = self._delivery_summary(conn)
        return {
            "enabled": bool(config.get("enabled")),
            "configured": bool(config.get("configured")),
            "signed": bool(config.get("signed")),
            "max_attempts": DELIVERY_MAX_ATTEMPTS,
            **summary,
            "items": deliveries,
        }

    def deliver_due(
        self,
        limit: int = DELIVERY_BATCH_SIZE,
    ) -> Dict[str, Any]:
        if limit < 1 or limit > 100:
            raise ValueError("limit 必须在 1 到 100 之间")
        config = self.delivery_config_provider()
        if not config.get("enabled"):
            return {
                "enabled": False,
                "configured": bool(config.get("configured")),
                "selected": 0,
                "delivered": 0,
                "failed": 0,
                "dead_letter": 0,
            }
        if not config.get("configured"):
            return {
                "enabled": True,
                "configured": False,
                "selected": 0,
                "delivered": 0,
                "failed": 0,
                "dead_letter": 0,
            }

        with self._delivery_lock:
            now = self._as_utc(self.clock())
            with self.connection_factory() as conn:
                rows = conn.execute(
                    """
                    SELECT id, attempt_count, payload
                    FROM stock_picker_reliability_deliveries
                    WHERE
                        status IN ('pending', 'failed')
                        AND (
                            next_attempt_at IS NULL
                            OR next_attempt_at <= ?
                        )
                    ORDER BY created_at, id
                    LIMIT ?
                    """,
                    [now.replace(tzinfo=None), limit],
                ).fetchall()

            result = {
                "enabled": True,
                "configured": True,
                "selected": len(rows),
                "delivered": 0,
                "failed": 0,
                "dead_letter": 0,
            }
            for delivery_id, previous_attempts, raw_payload in rows:
                payload = json.loads(raw_payload)
                payload["delivery_id"] = int(delivery_id)
                attempt = int(previous_attempts) + 1
                status_code = None
                error = None
                try:
                    status_code = self.webhook_sender(
                        config,
                        payload,
                    )
                    if status_code < 200 or status_code >= 300:
                        raise RuntimeError(
                            f"webhook returned HTTP {status_code}"
                        )
                except Exception as exc:
                    error = self._safe_delivery_error(exc)

                if error is None:
                    status = "delivered"
                    next_attempt_at = None
                    delivered_at = now
                    result["delivered"] += 1
                elif attempt >= DELIVERY_MAX_ATTEMPTS:
                    status = "dead_letter"
                    next_attempt_at = None
                    delivered_at = None
                    result["dead_letter"] += 1
                else:
                    status = "failed"
                    next_attempt_at = now + timedelta(
                        seconds=(
                            DELIVERY_RETRY_SECONDS
                            * (2 ** (attempt - 1))
                        )
                    )
                    delivered_at = None
                    result["failed"] += 1

                with self.connection_factory() as conn:
                    conn.execute(
                        """
                        UPDATE stock_picker_reliability_deliveries
                        SET
                            status = ?,
                            attempt_count = ?,
                            next_attempt_at = ?,
                            updated_at = ?,
                            delivered_at = ?,
                            http_status = ?,
                            error = ?
                        WHERE id = ?
                        """,
                        [
                            status,
                            attempt,
                            (
                                next_attempt_at.replace(tzinfo=None)
                                if next_attempt_at is not None
                                else None
                            ),
                            now.replace(tzinfo=None),
                            (
                                delivered_at.replace(tzinfo=None)
                                if delivered_at is not None
                                else None
                            ),
                            status_code,
                            error,
                            delivery_id,
                        ],
                    )
            return result

    def _latest_process_payload(self, conn) -> Optional[Dict[str, Any]]:
        row = conn.execute(
            """
            SELECT payload
            FROM stock_picker_reliability_snapshots
            WHERE process_id = ?
            ORDER BY observed_at DESC, id DESC
            LIMIT 1
            """,
            [self.process_id],
        ).fetchone()
        return json.loads(row[0]) if row else None

    def _latest_snapshot(self, conn) -> Optional[Dict[str, Any]]:
        row = conn.execute(
            """
            SELECT
                id,
                observed_at,
                process_id,
                window_metrics,
                alerts
            FROM stock_picker_reliability_snapshots
            ORDER BY observed_at DESC, id DESC
            LIMIT 1
            """
        ).fetchone()
        if row is None:
            return None
        return {
            "id": int(row[0]),
            "observed_at": self._isoformat(row[1]),
            "process_id": row[2],
            "window_metrics": json.loads(row[3]),
            "alerts": json.loads(row[4]),
        }

    def _window_metrics(
        self,
        current: Dict[str, Any],
        previous: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        previous = previous or {}
        previous_services = previous.get("services") or {}
        services = {}
        for name, metrics in (current.get("services") or {}).items():
            prior = previous_services.get(name) or {}
            counters = {
                key: self._counter_delta(metrics, prior, key)
                for key in (
                    "requests",
                    "attempts",
                    "successes",
                    "failures",
                    "timeouts",
                    "retries",
                    "rejected",
                )
            }
            latency_ms = self._counter_delta(
                metrics,
                prior,
                "total_latency_ms",
            )
            requests = counters["requests"]
            services[name] = {
                **counters,
                "total_latency_ms": round(latency_ms, 3),
                "avg_latency_ms": (
                    round(latency_ms / requests, 3)
                    if requests
                    else 0.0
                ),
                "failure_rate": self._rate(
                    counters["failures"],
                    requests,
                ),
                "timeout_rate": self._rate(
                    counters["timeouts"],
                    requests,
                ),
                "rejected_rate": self._rate(
                    counters["rejected"],
                    requests,
                ),
                "in_flight": metrics.get("in_flight", 0),
                "max_in_flight": metrics.get("max_in_flight", 0),
                "circuit_state": metrics.get(
                    "circuit_state",
                    "unknown",
                ),
                "consecutive_failures": metrics.get(
                    "consecutive_failures",
                    0,
                ),
                "last_error": metrics.get("last_error"),
                "policy": metrics.get("policy") or {},
            }

        current_picker = current.get("stock_picker") or {}
        previous_picker = previous.get("stock_picker") or {}
        current_cache = current_picker.get("cache") or {}
        previous_cache = previous_picker.get("cache") or {}
        cache = {
            key: self._counter_delta(
                current_cache,
                previous_cache,
                key,
            )
            for key in (
                "requests",
                "hits",
                "misses",
                "bypasses",
            )
        }
        cache["hit_rate"] = self._rate(
            cache["hits"],
            cache["requests"],
        )
        current_ai = current_picker.get("ai") or {}
        previous_ai = previous_picker.get("ai") or {}
        ai = {
            key: self._counter_delta(
                current_ai,
                previous_ai,
                key,
            )
            for key in (
                "attempts",
                "available",
                "degraded",
            )
        }
        ai["degradation_rate"] = self._rate(
            ai["degraded"],
            ai["attempts"],
        )
        return {
            "services": services,
            "stock_picker": {
                "cache": cache,
                "ai": ai,
            },
        }

    def _evaluate_alerts(
        self,
        current: Dict[str, Any],
        window: Dict[str, Any],
    ) -> list[Dict[str, Any]]:
        alerts = []
        current_services = current.get("services") or {}
        for name, metrics in window["services"].items():
            current_metrics = current_services.get(name) or {}
            circuit_state = metrics["circuit_state"]
            if circuit_state != "closed":
                severity = (
                    "critical"
                    if circuit_state == "open"
                    else "warning"
                )
                alerts.append(
                    self._alert(
                        f"service:{name}:circuit",
                        severity,
                        f"{name} 服务熔断状态为 {circuit_state}",
                        {
                            "service": name,
                            "circuit_state": circuit_state,
                            "consecutive_failures": metrics[
                                "consecutive_failures"
                            ],
                            "last_error": metrics["last_error"],
                        },
                    )
                )

            requests = metrics["requests"]
            if requests >= MIN_ALERT_REQUESTS:
                self._append_rate_alert(
                    alerts,
                    name,
                    "failure_rate",
                    metrics["failure_rate"],
                    FAILURE_RATE_WARNING,
                    requests,
                )
                self._append_rate_alert(
                    alerts,
                    name,
                    "timeout_rate",
                    metrics["timeout_rate"],
                    TIMEOUT_RATE_WARNING,
                    requests,
                )
                self._append_rate_alert(
                    alerts,
                    name,
                    "rejected_rate",
                    metrics["rejected_rate"],
                    REJECTED_RATE_WARNING,
                    requests,
                )

            max_concurrency = (
                (current_metrics.get("policy") or {}).get(
                    "max_concurrency",
                    0,
                )
            )
            if (
                max_concurrency
                and current_metrics.get("in_flight", 0)
                >= max_concurrency
            ):
                alerts.append(
                    self._alert(
                        f"service:{name}:capacity",
                        "warning",
                        f"{name} 服务并发额度已占满",
                        {
                            "service": name,
                            "in_flight": current_metrics.get(
                                "in_flight",
                                0,
                            ),
                            "max_concurrency": max_concurrency,
                        },
                    )
                )

        ai = window["stock_picker"]["ai"]
        if (
            ai["attempts"] >= MIN_ALERT_REQUESTS
            and ai["degradation_rate"] >= AI_DEGRADATION_WARNING
        ):
            alerts.append(
                self._alert(
                    "stock_picker:ai:degradation_rate",
                    (
                        "critical"
                        if ai["degradation_rate"] >= 0.8
                        else "warning"
                    ),
                    "智能选股 AI 降级率超过阈值",
                    {
                        "attempts": ai["attempts"],
                        "degraded": ai["degraded"],
                        "rate": ai["degradation_rate"],
                        "threshold": AI_DEGRADATION_WARNING,
                    },
                )
            )
        return alerts

    def _append_rate_alert(
        self,
        alerts: list[Dict[str, Any]],
        service: str,
        metric: str,
        value: float,
        threshold: float,
        requests: int,
    ) -> None:
        if value < threshold:
            return
        label = {
            "failure_rate": "失败率",
            "timeout_rate": "超时率",
            "rejected_rate": "拒绝率",
        }[metric]
        alerts.append(
            self._alert(
                f"service:{service}:{metric}",
                "critical" if value >= 0.8 else "warning",
                f"{service} 服务窗口{label}超过阈值",
                {
                    "service": service,
                    "metric": metric,
                    "requests": requests,
                    "rate": value,
                    "threshold": threshold,
                },
            )
        )

    @staticmethod
    def _alert(
        key: str,
        severity: str,
        message: str,
        details: Dict[str, Any],
    ) -> Dict[str, Any]:
        return {
            "key": key,
            "severity": severity,
            "message": message,
            "details": details,
        }

    def _sync_alerts(
        self,
        conn,
        alerts: Iterable[Dict[str, Any]],
        observed_at: datetime,
    ) -> list[Dict[str, Any]]:
        alerts = list(alerts)
        active_keys = {alert["key"] for alert in alerts}
        existing_rows = conn.execute(
            """
            SELECT
                alert_key,
                severity,
                status,
                message,
                details
            FROM stock_picker_reliability_alerts
            """
        ).fetchall()
        existing = {
            row[0]: {
                "key": row[0],
                "severity": row[1],
                "status": row[2],
                "message": row[3],
                "details": json.loads(row[4]),
            }
            for row in existing_rows
        }
        transitions = []
        for alert in alerts:
            previous = existing.get(alert["key"])
            if previous is None or previous["status"] != "active":
                transitions.append({
                    **alert,
                    "event_type": "triggered",
                    "status": "active",
                })
            conn.execute(
                """
                INSERT INTO stock_picker_reliability_alerts (
                    alert_key,
                    severity,
                    status,
                    first_seen_at,
                    last_seen_at,
                    resolved_at,
                    occurrence_count,
                    message,
                    details
                )
                VALUES (?, ?, 'active', ?, ?, NULL, 1, ?, ?)
                ON CONFLICT(alert_key) DO UPDATE SET
                    severity = excluded.severity,
                    status = 'active',
                    last_seen_at = excluded.last_seen_at,
                    resolved_at = NULL,
                    occurrence_count = (
                        stock_picker_reliability_alerts.occurrence_count
                        + 1
                    ),
                    message = excluded.message,
                    details = excluded.details
                """,
                [
                    alert["key"],
                    alert["severity"],
                    observed_at,
                    observed_at,
                    alert["message"],
                    json.dumps(
                        alert["details"],
                        ensure_ascii=False,
                    ),
                ],
            )
        for alert_key, previous in existing.items():
            if previous["status"] != "active":
                continue
            if alert_key in active_keys:
                continue
            conn.execute(
                """
                UPDATE stock_picker_reliability_alerts
                SET
                    status = 'resolved',
                    last_seen_at = ?,
                    resolved_at = ?
                WHERE alert_key = ?
                """,
                [observed_at, observed_at, alert_key],
            )
            transitions.append({
                "key": alert_key,
                "severity": previous["severity"],
                "message": previous["message"],
                "details": previous["details"],
                "event_type": "resolved",
                "status": "resolved",
            })
        return transitions

    def _enqueue_deliveries(
        self,
        conn,
        transitions: Iterable[Dict[str, Any]],
        observed_at: datetime,
        config: Dict[str, Any],
    ) -> list[int]:
        delivery_ids = []
        enabled = bool(config.get("enabled"))
        configured = bool(config.get("configured"))
        if enabled and configured:
            status = "pending"
            error = None
        elif not enabled:
            status = "skipped"
            error = "webhook_disabled"
        else:
            status = "skipped"
            error = "webhook_not_configured"

        for transition in transitions:
            event_type = transition["event_type"]
            payload = {
                "event": (
                    "stock_picker.reliability.alert."
                    f"{event_type}"
                ),
                "observed_at": self._isoformat(observed_at),
                "process_id": self.process_id,
                "alert": {
                    "key": transition["key"],
                    "severity": transition["severity"],
                    "status": transition["status"],
                    "message": transition["message"],
                    "details": transition["details"],
                },
            }
            row = conn.execute(
                """
                INSERT INTO stock_picker_reliability_deliveries (
                    alert_key,
                    event_type,
                    destination_type,
                    status,
                    attempt_count,
                    next_attempt_at,
                    created_at,
                    updated_at,
                    delivered_at,
                    http_status,
                    error,
                    payload
                )
                VALUES (
                    ?,
                    ?,
                    'webhook',
                    ?,
                    0,
                    ?,
                    ?,
                    ?,
                    NULL,
                    NULL,
                    ?,
                    ?
                )
                RETURNING id
                """,
                [
                    transition["key"],
                    event_type,
                    status,
                    observed_at if status == "pending" else None,
                    observed_at,
                    observed_at,
                    error,
                    json.dumps(payload, ensure_ascii=False),
                ],
            ).fetchone()
            delivery_ids.append(int(row[0]))
        return delivery_ids

    def _load_alerts(
        self,
        conn,
        statuses: tuple[str, ...],
        limit: int,
    ) -> list[Dict[str, Any]]:
        placeholders = ", ".join("?" for _ in statuses)
        rows = conn.execute(
            f"""
            SELECT
                alert_key,
                severity,
                status,
                first_seen_at,
                last_seen_at,
                resolved_at,
                occurrence_count,
                message,
                details
            FROM stock_picker_reliability_alerts
            WHERE status IN ({placeholders})
            ORDER BY
                CASE severity
                    WHEN 'critical' THEN 0
                    ELSE 1
                END,
                last_seen_at DESC
            LIMIT ?
            """,
            [*statuses, limit],
        ).fetchall()
        return [
            {
                "key": row[0],
                "severity": row[1],
                "status": row[2],
                "first_seen_at": self._isoformat(row[3]),
                "last_seen_at": self._isoformat(row[4]),
                "resolved_at": (
                    self._isoformat(row[5])
                    if row[5] is not None
                    else None
                ),
                "occurrence_count": int(row[6]),
                "message": row[7],
                "details": json.loads(row[8]),
            }
            for row in rows
        ]

    def _load_deliveries(
        self,
        conn,
        limit: int,
    ) -> list[Dict[str, Any]]:
        rows = conn.execute(
            """
            SELECT
                id,
                alert_key,
                event_type,
                destination_type,
                status,
                attempt_count,
                next_attempt_at,
                created_at,
                updated_at,
                delivered_at,
                http_status,
                error,
                payload
            FROM stock_picker_reliability_deliveries
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            [limit],
        ).fetchall()
        return [
            {
                "id": int(row[0]),
                "alert_key": row[1],
                "event_type": row[2],
                "destination_type": row[3],
                "status": row[4],
                "attempt_count": int(row[5]),
                "next_attempt_at": (
                    self._isoformat(row[6])
                    if row[6] is not None
                    else None
                ),
                "created_at": self._isoformat(row[7]),
                "updated_at": self._isoformat(row[8]),
                "delivered_at": (
                    self._isoformat(row[9])
                    if row[9] is not None
                    else None
                ),
                "http_status": row[10],
                "error": row[11],
                "payload": json.loads(row[12]),
            }
            for row in rows
        ]

    def _delivery_summary(self, conn) -> Dict[str, Any]:
        rows = conn.execute(
            """
            SELECT status, COUNT(*)
            FROM stock_picker_reliability_deliveries
            GROUP BY status
            """
        ).fetchall()
        status_counts = {
            status: int(count)
            for status, count in rows
        }
        last_row = conn.execute(
            """
            SELECT updated_at
            FROM stock_picker_reliability_deliveries
            ORDER BY updated_at DESC, id DESC
            LIMIT 1
            """
        ).fetchone()
        return {
            "status_counts": status_counts,
            "pending_count": (
                status_counts.get("pending", 0)
                + status_counts.get("failed", 0)
            ),
            "dead_letter_count": status_counts.get(
                "dead_letter",
                0,
            ),
            "last_updated_at": (
                self._isoformat(last_row[0])
                if last_row is not None
                else None
            ),
        }

    @staticmethod
    def _empty_delivery_summary() -> Dict[str, Any]:
        return {
            "status_counts": {},
            "pending_count": 0,
            "dead_letter_count": 0,
            "last_updated_at": None,
        }

    @staticmethod
    def _safe_delivery_error(exc: Exception) -> str:
        message = str(exc)
        if not (
            message.startswith("webhook returned HTTP ")
            or message.startswith("webhook request failed: ")
        ):
            message = (
                "webhook delivery failed: "
                f"{type(exc).__name__}"
            )
        return message[:500]

    @staticmethod
    def _counter_delta(
        current: Dict[str, Any],
        previous: Dict[str, Any],
        key: str,
    ) -> float | int:
        current_value = current.get(key, 0) or 0
        previous_value = previous.get(key, 0) or 0
        if current_value < previous_value:
            return current_value
        return current_value - previous_value

    @staticmethod
    def _rate(numerator: float, denominator: float) -> float:
        return (
            round(numerator / denominator, 6)
            if denominator
            else 0.0
        )

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    @classmethod
    def _isoformat(cls, value: datetime) -> str:
        return cls._as_utc(value).isoformat()


_reliability_service: Optional[StockPickerReliabilityService] = None


def get_stock_picker_reliability_service(
) -> StockPickerReliabilityService:
    global _reliability_service
    if _reliability_service is None:
        _reliability_service = StockPickerReliabilityService()
    return _reliability_service
