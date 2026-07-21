# Longbridge 4.x Config Factory Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate `cannot create 'builtins.Config' instances` by migrating every backend Longbridge configuration call to the supported 4.x factory API.

**Architecture:** Keep the existing call-site structure and replace only the obsolete constructor calls. A behavioral unit test covers the configuration returned by `services._build_longport_config`, while an AST-based regression check guarantees that all four backend call sites use `Config.from_apikey` with three positional credential arguments.

**Tech Stack:** Python 3.13, built-in `unittest`, Python `ast`, Longbridge Python SDK 4.x, FastAPI.

---

## File map

- Create `backend/tests/test_longport_config_api.py`: behavioral and migration regression tests.
- Modify `backend/app/services.py`: migrate quote verification and position configuration.
- Modify `backend/app/trading_api.py`: migrate trade context configuration.
- Modify `backend/app/streaming.py`: migrate realtime quote configuration.

### Task 1: Add a failing Longbridge 4.x regression test

**Files:**
- Create: `backend/tests/test_longport_config_api.py`

- [ ] **Step 1: Write the behavioral and source migration tests**

```python
from __future__ import annotations

import ast
import unittest
from pathlib import Path

from app.services import _build_longport_config


BACKEND_ROOT = Path(__file__).resolve().parents[1]
CONFIG_CALL_FILES = (
    BACKEND_ROOT / "app" / "services.py",
    BACKEND_ROOT / "app" / "trading_api.py",
    BACKEND_ROOT / "app" / "streaming.py",
)


class LongportConfigApiTest(unittest.TestCase):
    def test_build_longport_config_uses_supported_sdk_factory(self) -> None:
        config = _build_longport_config(
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
```

- [ ] **Step 2: Run the test and verify the current code fails for the reported reason**

Run:

```powershell
cd backend
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_longport_config_api.py" -v
```

Expected: `test_build_longport_config_uses_supported_sdk_factory` errors with `TypeError: cannot create 'builtins.Config' instances`, and the migration test reports the four legacy `Config(...)` locations.

### Task 2: Migrate all configuration call sites

**Files:**
- Modify: `backend/app/services.py:58-62`
- Modify: `backend/app/services.py:226-230`
- Modify: `backend/app/trading_api.py:119-123`
- Modify: `backend/app/streaming.py:221-225`
- Test: `backend/tests/test_longport_config_api.py`

- [ ] **Step 1: Replace the quote verification constructor in `services.py`**

```python
    config = Config.from_apikey(
        creds.get("LONGPORT_APP_KEY", ""),
        creds.get("LONGPORT_APP_SECRET", ""),
        creds.get("LONGPORT_ACCESS_TOKEN", ""),
    )
```

- [ ] **Step 2: Replace the position constructor in `services.py`**

```python
    return Config.from_apikey(
        creds["LONGPORT_APP_KEY"],
        creds["LONGPORT_APP_SECRET"],
        creds["LONGPORT_ACCESS_TOKEN"],
    )
```

- [ ] **Step 3: Replace the trade constructor in `trading_api.py`**

```python
        config = Config.from_apikey(
            self.credentials["LONGPORT_APP_KEY"],
            self.credentials["LONGPORT_APP_SECRET"],
            self.credentials["LONGPORT_ACCESS_TOKEN"],
        )
```

- [ ] **Step 4: Replace the streaming constructor in `streaming.py`**

```python
            config = Config.from_apikey(
                creds.get("LONGPORT_APP_KEY", ""),
                creds.get("LONGPORT_APP_SECRET", ""),
                creds.get("LONGPORT_ACCESS_TOKEN", ""),
            )
```

- [ ] **Step 5: Run the focused regression test**

Run:

```powershell
cd backend
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_longport_config_api.py" -v
```

Expected: both tests pass and output ends with `OK`.

- [ ] **Step 6: Confirm no obsolete constructor remains**

Run:

```powershell
rg -n "(^|[^A-Za-z0-9_])Config\(" app -g "*.py"
```

Expected: no matches and `rg` exits with code 1.

- [ ] **Step 7: Commit the tested migration**

```powershell
git add backend/tests/test_longport_config_api.py backend/app/services.py backend/app/trading_api.py backend/app/streaming.py docs/superpowers/plans/2026-07-21-longport-config-factory.md
git commit -m "fix: migrate Longbridge Config creation to 4.x API key factory"
```

### Task 3: Restart and verify the running application

**Files:**
- No source changes.

- [ ] **Step 1: Restart the backend using the existing virtual environment**

Stop only the process listening on port 8000, then run:

```powershell
cd backend
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Keep the process hidden/backgrounded in the desktop environment.

- [ ] **Step 2: Verify backend readiness**

Run:

```powershell
Invoke-RestMethod -TimeoutSec 10 -Uri "http://127.0.0.1:8000/health"
```

Expected: response contains `status = ok`.

- [ ] **Step 3: Verify the saved-credential path without exposing credentials**

Run:

```powershell
Invoke-WebRequest -UseBasicParsing -TimeoutSec 30 -Method Post -ContentType "application/json" -Body '{"symbols":["700.HK"]}' -Uri "http://127.0.0.1:8000/settings/verify"
```

Expected: the response must not contain `cannot create 'builtins.Config' instances`. With valid stored credentials it returns HTTP 200; an upstream authentication or network error is reported separately and does not invalidate the factory migration.

- [ ] **Step 4: Recheck repository scope**

Run:

```powershell
git status --short
```

Expected: only pre-existing untracked local artifacts remain; the migration files are committed.
