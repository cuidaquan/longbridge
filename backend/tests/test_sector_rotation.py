from __future__ import annotations

import asyncio
import json
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from app.sector_rotation_service import SectorRotationService


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _FakeConnection:
    def __init__(self, rows):
        self._rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, _query, _params=None):
        return _FakeResult(self._rows)


class _RecordingConnection(_FakeConnection):
    def __init__(self, rows):
        super().__init__(rows)
        self.calls = []

    def execute(self, query, params=None):
        self.calls.append((query, params))
        return super().execute(query, params)


class SectorRotationServiceTest(unittest.TestCase):
    def test_sync_uses_longport_when_eodhd_is_not_configured(self):
        service = SectorRotationService()
        start = datetime(2026, 1, 1)
        bars = [
            {
                "ts": start + timedelta(days=index),
                "open": 100 + index,
                "high": 101 + index,
                "low": 99 + index,
                "close": 100 + index,
                "volume": 1000 + index,
            }
            for index in range(61)
        ]
        saved = []

        with (
            patch.object(service, "_get_client", return_value=None),
            patch("app.sector_rotation_service.SECTOR_ETFS", {"XLK": {"type": "sector"}}),
            patch("app.sector_rotation_service.sync_history_candlesticks") as sync_history,
            patch("app.sector_rotation_service.get_cached_candlesticks", return_value=bars),
            patch.object(
                service,
                "_save_sector_performance",
                side_effect=lambda symbol, data, *_: saved.append((symbol, data)),
            ),
            patch.object(service, "_save_sector_etf_info"),
        ):
            loop = asyncio.new_event_loop()
            try:
                result = loop.run_until_complete(service.sync_sector_data(days=60, etf_type="sector"))
            finally:
                loop.close()

        self.assertEqual(result["source"], "longport")
        self.assertEqual(result["success"], ["XLK"])
        sync_history.assert_called_once_with(["XLK.US"], "day", "no_adjust", 61)
        self.assertEqual(saved[0][0], "XLK")
        self.assertAlmostEqual(saved[0][1][-1]["change_60d"], 60.0)

    def test_sector_strength_columns_are_not_shifted(self):
        service = SectorRotationService()
        rows = [("XLK", 250.0, 1.5, 4.0, 8.0, 12.0, "2026-07-20")]

        with (
            patch("app.sector_rotation_service.get_connection", return_value=_FakeConnection(rows)),
            patch(
                "app.sector_rotation_service.SECTOR_ETFS",
                {"XLK": {"name": "Technology", "name_cn": "科技", "color": "#000"}},
            ),
        ):
            result = service.calculate_sector_strength()

        self.assertEqual(result[0]["close"], 250.0)
        self.assertEqual(result[0]["change_1d"], 1.5)
        self.assertEqual(result[0]["change_5d"], 4.0)
        self.assertEqual(result[0]["change_20d"], 8.0)
        self.assertEqual(result[0]["change_60d"], 12.0)

    def test_date_filters_use_duckdb_compatible_parameters(self):
        service = SectorRotationService()
        connection = _RecordingConnection([])

        with patch("app.sector_rotation_service.get_connection", return_value=connection):
            service.get_rotation_trend(days=30)
            service.detect_factor_rotation(lookback_days=20)
            service._save_factor_rotation_signal("value", "neutral", {}, "观望")

        self.assertEqual(len(connection.calls), 3)
        self.assertNotIn("date('now'", connection.calls[0][0].lower())
        self.assertIsNotNone(connection.calls[0][1])
        self.assertNotIn("date('now'", connection.calls[1][0].lower())
        self.assertIsNotNone(connection.calls[1][1])
        self.assertIn("CURRENT_DATE", connection.calls[2][0])

    def test_factor_rotation_result_is_json_serializable(self):
        service = SectorRotationService()
        rows = [
            ("value", datetime(2026, 7, 1) + timedelta(days=index), index * 0.2, index * 0.3)
            for index in range(10)
        ]

        with (
            patch("app.sector_rotation_service.get_connection", return_value=_FakeConnection(rows)),
            patch.object(service, "_save_factor_rotation_signal"),
        ):
            result = service.detect_factor_rotation(lookback_days=20)

        json.dumps(result, ensure_ascii=False)
        momentum = result["factor_momentum"]["value"]
        self.assertIs(type(momentum["recent_avg"]), float)
        self.assertIs(type(momentum["is_strengthening"]), bool)


if __name__ == "__main__":
    unittest.main()
