from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient
from starlette.routing import Match

from app.main import app, _stream_websocket_queue
from app import services
from app.models import GlobalMonitoringSettings, MonitoringStatus
from app.position_monitor import PositionMonitor


class _StoppedEngine:
    def is_running(self) -> bool:
        return False


class _FakeConnection:
    def __init__(self, real_trading_enabled: bool = False) -> None:
        self.real_trading_enabled = real_trading_enabled
        self.statements: list[tuple[str, object]] = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None

    def execute(self, statement: str, parameters=None):
        self.statements.append((statement, parameters))
        return self

    def fetchone(self):
        return (1, self.real_trading_enabled)


class _DisconnectingWebSocket:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def receive(self):
        return {"type": "websocket.disconnect", "code": 1000}

    async def send_text(self, value: str) -> None:
        self.sent.append(value)


class SecurityContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)

    def test_health_rejects_untrusted_browser_origin(self) -> None:
        blocked = self.client.get(
            "/health", headers={"Origin": "https://malicious.example"}
        )
        allowed = self.client.get(
            "/health", headers={"Origin": "http://localhost:5173"}
        )

        self.assertEqual(403, blocked.status_code)
        self.assertEqual(200, allowed.status_code)

    def test_websocket_queue_stream_detects_idle_client_disconnect(self) -> None:
        websocket = _DisconnectingWebSocket()

        async def run_stream() -> None:
            with self.assertRaises(WebSocketDisconnect):
                await _stream_websocket_queue(
                    websocket,
                    asyncio.Queue(),
                    str,
                )

        asyncio.run(run_stream())
        self.assertEqual([], websocket.sent)

    def test_credential_reads_never_return_saved_secrets(self) -> None:
        longport = {
            "LONGPORT_APP_KEY": "app-key-secret",
            "LONGPORT_APP_SECRET": "app-secret-secret",
            "LONGPORT_ACCESS_TOKEN": "access-token-secret",
        }
        ai = {
            "DEEPSEEK_API_KEY": "deepseek-secret",
            "TAVILY_API_KEY": "tavily-secret",
            "EODHD_API_KEY": "eodhd-secret",
        }

        with patch("app.routers.settings.load_credentials", return_value=longport):
            response = self.client.get("/settings/credentials")
        with patch("app.routers.settings.load_ai_credentials", return_value=ai):
            ai_response = self.client.get("/settings/ai-credentials")

        self.assertEqual(200, response.status_code)
        self.assertEqual({"********"}, set(response.json().values()))
        self.assertEqual(200, ai_response.status_code)
        self.assertEqual({"********"}, set(ai_response.json().values()))
        for secret in (*longport.values(), *ai.values()):
            self.assertNotIn(secret, response.text + ai_response.text)

    def test_masked_credentials_preserve_saved_values(self) -> None:
        existing = {
            "LONGPORT_APP_KEY": "saved-key",
            "LONGPORT_APP_SECRET": "saved-secret",
            "LONGPORT_ACCESS_TOKEN": "saved-token",
        }
        save_credentials = Mock()

        with (
            patch("app.routers.settings.load_credentials", return_value=existing),
            patch("app.routers.settings.save_credentials", save_credentials),
            patch("app.routers.settings.quote_stream_manager.request_restart"),
        ):
            response = self.client.put(
                "/settings/credentials",
                json={key: "********" for key in existing},
            )

        self.assertEqual(204, response.status_code)
        save_credentials.assert_called_once_with(existing)

    def test_real_trading_requires_explicit_confirmation(self) -> None:
        saved_config = Mock()
        with (
            patch("app.routers.ai_trading.get_ai_trading_config", return_value={}),
            patch("app.routers.ai_trading.update_ai_trading_config", saved_config),
            patch(
                "app.routers.ai_trading.get_ai_trading_engine",
                return_value=_StoppedEngine(),
            ),
        ):
            denied = self.client.put(
                "/ai-trading/config", json={"enable_real_trading": True}
            )
            accepted = self.client.put(
                "/ai-trading/config",
                json={
                    "enable_real_trading": True,
                    "real_trading_confirmation": "CONFIRM_REAL_TRADING",
                },
            )

        self.assertEqual(400, denied.status_code)
        self.assertEqual(200, accepted.status_code)
        saved_config.assert_called_once_with({"enable_real_trading": True})

    def test_ai_key_mask_does_not_overwrite_saved_secret(self) -> None:
        saved_config = Mock()
        existing = {"ai_api_key": "secret-api-key", "enabled": False}
        with (
            patch(
                "app.routers.ai_trading.get_ai_trading_config",
                return_value=existing,
            ),
            patch(
                "app.routers.ai_trading.update_ai_trading_config", saved_config
            ),
            patch(
                "app.routers.ai_trading.get_ai_trading_engine",
                return_value=_StoppedEngine(),
            ),
            patch("app.routers.ai_trading.get_daily_trades_count", return_value=0),
            patch("app.routers.ai_trading.get_daily_pnl", return_value=0),
            patch("app.routers.ai_trading.get_ai_positions", return_value=[]),
        ):
            read_response = self.client.get("/ai-trading/config")
            status_response = self.client.get("/ai-trading/engine/status")
            write_response = self.client.put(
                "/ai-trading/config",
                json={"ai_api_key": "********", "enabled": True},
            )

        self.assertEqual(200, read_response.status_code)
        self.assertEqual(200, status_response.status_code)
        self.assertEqual("********", read_response.json()["ai_api_key"])
        self.assertEqual(
            "********", status_response.json()["config"]["ai_api_key"]
        )
        self.assertNotIn(
            "secret-api-key", read_response.text + status_response.text
        )
        self.assertEqual(200, write_response.status_code)
        saved_config.assert_called_once_with(
            {"ai_api_key": "secret-api-key", "enabled": True}
        )

    def test_auto_position_config_rejects_unknown_fields(self) -> None:
        response = self.client.put(
            "/position-manager/auto/config",
            json={"enable_real_trading": False, "unsafe_sql_field": True},
        )
        self.assertEqual(422, response.status_code)

    def test_auto_position_real_trading_requires_confirmation(self) -> None:
        connection = _FakeConnection()
        manager = Mock()
        manager.is_running.return_value = False

        with (
            patch("app.db.get_connection", return_value=connection),
            patch(
                "app.routers.position_manager.get_auto_position_manager",
                return_value=manager,
            ),
        ):
            denied = self.client.put(
                "/position-manager/auto/config",
                json={"enable_real_trading": True},
            )
            accepted = self.client.put(
                "/position-manager/auto/config",
                json={
                    "enable_real_trading": True,
                    "real_trading_confirmation": "CONFIRM_REAL_TRADING",
                },
            )

        self.assertEqual(400, denied.status_code)
        self.assertEqual(200, accepted.status_code)
        update_statements = [
            statement
            for statement, _ in connection.statements
            if statement.lstrip().upper().startswith("UPDATE")
        ]
        self.assertEqual(1, len(update_statements))
        self.assertIn("enable_real_trading = ?", update_statements[0])

    def test_static_routes_are_not_shadowed(self) -> None:
        expected = {
            "/strategies/signals": "/strategies/signals",
            "/signals/analyze/batch": "/signals/analyze/batch",
        }

        for request_path, expected_route in expected.items():
            scope = {
                "type": "http",
                "method": "GET",
                "path": request_path,
                "root_path": "",
                "headers": [],
            }
            matched_path = None
            for route in app.routes:
                match, _ = route.matches(scope)
                if match is Match.FULL:
                    matched_path = getattr(route, "path", None)
                    break
            self.assertEqual(expected_route, matched_path)

    def test_destructive_admin_reset_route_is_not_exposed(self) -> None:
        paths = {getattr(route, "path", None) for route in app.routes}
        self.assertNotIn("/admin/reset-ai-table", paths)

    def test_portfolio_requests_share_short_lived_snapshot(self) -> None:
        services._portfolio_cache_value = None
        services._portfolio_cache_expires_at = 0.0
        snapshot = {
            "positions": [{"symbol": "TEST.US"}],
            "totals": {"cost": 1.0},
        }

        with patch(
            "app.services._fetch_portfolio_overview", return_value=snapshot
        ) as fetch_snapshot:
            first = services.get_portfolio_overview()
            first["positions"].append({"symbol": "MUTATED.US"})
            second = services.get_portfolio_overview()

        fetch_snapshot.assert_called_once_with()
        self.assertEqual([{"symbol": "TEST.US"}], second["positions"])

    def test_monitoring_pnl_uses_decimal_ratio(self) -> None:
        portfolio = {
            "positions": [
                {
                    "symbol": "TEST.US",
                    "qty": 10,
                    "avg_price": 100,
                    "last_price": 88,
                    "market_value": 880,
                    "pnl": -120,
                    "pnl_percent": -12.0,
                }
            ],
            "totals": {},
        }
        with (
            patch(
                "app.routers.monitoring.get_portfolio_overview",
                return_value=portfolio,
            ),
            patch("app.routers.monitoring._load_config_map", return_value={}),
            patch(
                "app.routers.monitoring._load_global_settings_model",
                return_value=GlobalMonitoringSettings(),
            ),
        ):
            response = self.client.get("/monitoring/positions")

        self.assertEqual(200, response.status_code)
        self.assertEqual(-0.12, response.json()["positions"][0]["pnl_ratio"])

    def test_monitoring_update_uses_current_config_contract(self) -> None:
        monitor = Mock()
        monitor.update_position_config = AsyncMock()
        save_config = Mock()
        with (
            patch(
                "app.routers.monitoring.get_position_monitoring_config",
                return_value=None,
            ),
            patch(
                "app.routers.monitoring.save_position_monitoring_config",
                save_config,
            ),
            patch(
                "app.routers.monitoring.get_position_monitor", return_value=monitor
            ),
        ):
            accepted = self.client.put(
                "/monitoring/position/TEST.US",
                json={
                    "monitoring_status": "enabled",
                    "stop_loss_ratio": 0.08,
                },
            )
            legacy = self.client.put(
                "/monitoring/position/TEST.US",
                json={"custom_stop_loss": 0.08},
            )

        self.assertEqual(200, accepted.status_code)
        self.assertEqual(422, legacy.status_code)
        saved = save_config.call_args.args[0]
        self.assertEqual(MonitoringStatus.ENABLED, saved["monitoring_status"])
        self.assertEqual(0.08, saved["stop_loss_ratio"])
        monitor.update_position_config.assert_awaited_once()

    def test_position_monitor_initializes_configs_by_symbol(self) -> None:
        positions = [
            {"symbol": "EXIST.US", "qty": 1, "avg_price": 10},
            {"symbol": "NEW.US", "qty": 2, "avg_price": 20},
        ]
        configs = [
            {
                "symbol": "EXIST.US",
                "monitoring_status": "disabled",
                "strategy_mode": "alert_only",
            }
        ]
        strategy_engine = Mock()
        strategy_engine.kline_buffers = {}
        with patch(
            "app.position_monitor.get_strategy_engine", return_value=strategy_engine
        ):
            monitor = PositionMonitor()

        with (
            patch(
                "app.position_monitor.get_global_monitoring_settings",
                return_value=GlobalMonitoringSettings().model_dump(),
            ),
            patch(
                "app.position_monitor.get_all_monitoring_configs",
                return_value=configs,
            ),
            patch.object(
                monitor, "get_current_positions", AsyncMock(return_value=positions)
            ),
        ):
            asyncio.run(monitor.initialize())

        self.assertEqual(
            MonitoringStatus.DISABLED,
            monitor.monitored_positions["EXIST.US"].monitoring_config.monitoring_status,
        )
        self.assertEqual(
            MonitoringStatus.ENABLED,
            monitor.monitored_positions["NEW.US"].monitoring_config.monitoring_status,
        )

    def test_market_hours_use_exchange_timezones_and_weekdays(self) -> None:
        strategy_engine = Mock()
        strategy_engine.kline_buffers = {}
        with patch(
            "app.position_monitor.get_strategy_engine", return_value=strategy_engine
        ):
            monitor = PositionMonitor()

        monday_us_open = datetime(2026, 7, 20, 13, 30, tzinfo=timezone.utc)
        sunday_us_open = datetime(2026, 7, 19, 13, 30, tzinfo=timezone.utc)
        hk_lunch_break = datetime(2026, 7, 20, 4, 30, tzinfo=timezone.utc)

        self.assertTrue(monitor.is_us_market_hours(monday_us_open))
        self.assertFalse(monitor.is_us_market_hours(sunday_us_open))
        self.assertFalse(monitor.is_hk_market_hours(hk_lunch_break))

    def test_position_monitor_only_uses_declared_status_values(self) -> None:
        source = (
            Path(__file__).resolve().parents[1] / "app" / "position_monitor.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("MonitoringStatus.ACTIVE", source)
        self.assertNotIn("MonitoringStatus.EXCLUDED", source)
        for obsolete_field in (
            "max_daily_loss",
            "max_position_size",
            "pause_on_high_volatility",
            "custom_stop_loss",
            "custom_take_profit",
            "custom_position_limit",
            "auto_monitor_new_positions",
            "default_strategy_mode",
            "default_enabled_strategies",
            "monitoring_start_time",
            "monitoring_end_time",
        ):
            self.assertNotIn(obsolete_field, source)


if __name__ == "__main__":
    unittest.main()
