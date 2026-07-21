from __future__ import annotations

import ast
import importlib
import unittest
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_ROOT.parent
LONGPORT_CALL_FILES = (
    BACKEND_ROOT / "app" / "services.py",
    BACKEND_ROOT / "app" / "trading_api.py",
    BACKEND_ROOT / "app" / "streaming.py",
)


class LongportRuntimeCompatTest(unittest.TestCase):
    def _load_close_helper(self):
        try:
            module = importlib.import_module("app.longport_compat")
        except ModuleNotFoundError as exc:
            self.fail(f"Longport compatibility module is missing: {exc}")
        return module.close_longport_context

    def test_close_helper_calls_supported_close_method(self) -> None:
        close_longport_context = self._load_close_helper()

        class ClosableContext:
            def __init__(self) -> None:
                self.close_count = 0

            def close(self) -> None:
                self.close_count += 1

        context = ClosableContext()
        close_longport_context(context)

        self.assertEqual(1, context.close_count)

    def test_close_helper_accepts_context_without_close_method(self) -> None:
        close_longport_context = self._load_close_helper()

        close_longport_context(object())

    def test_longport_calls_do_not_use_removed_sdk_apis(self) -> None:
        first_push_calls: list[str] = []
        direct_close_calls: list[str] = []

        for path in LONGPORT_CALL_FILES:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                if any(keyword.arg == "is_first_push" for keyword in node.keywords):
                    first_push_calls.append(f"{path.name}:{node.lineno}")
                if (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr == "close"
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "ctx"
                ):
                    direct_close_calls.append(f"{path.name}:{node.lineno}")

        self.assertEqual([], first_push_calls)
        self.assertEqual([], direct_close_calls)

    def test_start_scripts_conditionally_load_local_endpoint_file(self) -> None:
        windows_script = (PROJECT_ROOT / "start.bat").read_text(encoding="utf-8")
        linux_script = (PROJECT_ROOT / "start.sh").read_text(encoding="utf-8")

        for name, script in (("start.bat", windows_script), ("start.sh", linux_script)):
            with self.subTest(script=name):
                self.assertIn(".longport.env", script)
                self.assertIn("--env-file", script)

    def test_endpoint_example_contains_no_credentials(self) -> None:
        example_path = BACKEND_ROOT / "longport.env.example"
        self.assertTrue(example_path.exists())
        content = example_path.read_text(encoding="utf-8")

        self.assertIn("LONGPORT_HTTP_URL=https://openapi.longbridge.com", content)
        self.assertIn("LONGPORT_QUOTE_WS_URL=wss://openapi-quote.longbridge.com/v2", content)
        self.assertIn("LONGPORT_TRADE_WS_URL=wss://openapi-trade.longbridge.com/v2", content)
        self.assertNotIn("LONGPORT_APP_KEY", content)
        self.assertNotIn("LONGPORT_APP_SECRET", content)
        self.assertNotIn("LONGPORT_ACCESS_TOKEN", content)


if __name__ == "__main__":
    unittest.main()
