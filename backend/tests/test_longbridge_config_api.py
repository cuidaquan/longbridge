from __future__ import annotations

import ast
import unittest
from pathlib import Path

from app.services import _build_longbridge_config


BACKEND_ROOT = Path(__file__).resolve().parents[1]
CONFIG_CALL_FILES = (
    BACKEND_ROOT / "app" / "services.py",
    BACKEND_ROOT / "app" / "trading_api.py",
    BACKEND_ROOT / "app" / "streaming.py",
)


class LongbridgeConfigApiTest(unittest.TestCase):
    def test_build_longbridge_config_uses_supported_sdk_factory(self) -> None:
        config = _build_longbridge_config(
            {
                "LONGPORT_APP_KEY": "test-app-key",
                "LONGPORT_APP_SECRET": "test-app-secret",
                "LONGPORT_ACCESS_TOKEN": "test-access-token",
            }
        )

        self.assertEqual("Config", type(config).__name__)

    def test_all_config_calls_use_from_apikey(self) -> None:
        legacy_calls: list[str] = []
        factory_calls: list[tuple[str, ast.Call]] = []

        for path in CONFIG_CALL_FILES:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                if isinstance(node.func, ast.Name) and node.func.id == "Config":
                    legacy_calls.append(f"{path.name}:{node.lineno}")
                if (
                    isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "Config"
                    and node.func.attr == "from_apikey"
                ):
                    factory_calls.append((path.name, node))

        self.assertEqual([], legacy_calls)
        self.assertEqual(4, len(factory_calls))
        for filename, call in factory_calls:
            with self.subTest(filename=filename, lineno=call.lineno):
                self.assertEqual(3, len(call.args))
                self.assertEqual([], call.keywords)


if __name__ == "__main__":
    unittest.main()
