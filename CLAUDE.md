# Repository Guidance

## Communication

- User-facing text and responses default to Chinese unless the task requires another language.
- Keep `README.md` aligned with the current application. Do not add phase summaries, duplicate quick-start guides, generated API dumps, or one-off fix reports.

## Commands

Backend setup and tests:

```bash
python3 -m venv backend/.venv
backend/.venv/bin/python -m pip install -e backend
backend/.venv/bin/python -m unittest discover -s backend/tests -p 'test_*.py' -v
```

Frontend setup and checks:

```bash
cd frontend
npm ci
npm run check
```

Run the complete local application from the repository root with `./start.sh`; stop it with `./stop.sh`.

## Architecture

- `backend/app/main.py` registers FastAPI routers, CORS/origin protection, WebSockets, and service lifecycle hooks.
- `backend/app/routers/` owns HTTP endpoints. Put shared request and response models in `backend/app/models.py`.
- `backend/app/services.py` and focused service modules own business logic. Database access belongs in `backend/app/repositories.py` or the existing domain-specific storage layer.
- `backend/app/db.py` owns DuckDB initialization and schema migration.
- `frontend/src/api/client.ts` is the shared HTTP client. Focused API modules belong in `frontend/src/api/`.
- `frontend/src/App.tsx` exposes exactly eight active pages: AI trading, smart position, stock picker, sector rotation, strategy watch, position monitoring, position K lines, and settings.
- `frontend/src/components/ui/index.tsx` contains shared UI primitives. Reuse them before adding another component library or duplicate primitive.

## Safety

- Never commit credentials, `.env` files, encryption keys, DuckDB files, logs, editor state, build output, or Python package metadata.
- Real trading must remain opt-in. Preserve the server-side `CONFIRM_REAL_TRADING` check when changing AI trading or automatic position configuration.
- Keep local servers bound to loopback unless the user explicitly requests a deployment change with corresponding authentication and origin controls.
- Consult current official Longbridge documentation when changing OpenAPI calls; do not commit generated copies of external documentation.

## Change Discipline

- Preserve existing router/service/repository boundaries and error mapping.
- Use `get_connection()` context management for DuckDB access and the existing Longbridge context helpers for SDK resources.
- Add focused tests under `backend/tests/` for backend behavior changes.
- Run `npm run check` after frontend changes and the backend test suite after Python changes.
- Do not keep standalone migration, diagnosis, or manual test scripts after their purpose has been absorbed by application code or automated tests.
