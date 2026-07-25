"""HTTP and SSE API for persisted quantitative selection runs."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import AsyncGenerator

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict

from ..quant_stock_selector_service import (
    TERMINAL_STATUSES,
    QuantSelectionRunConflict,
    QuantSelectionRunNotFound,
    QuantSelectionService,
)


logger = logging.getLogger(__name__)
router = APIRouter(
    prefix="/api/quant-stock-selector",
    tags=["quant-stock-selector"],
)


class CreateQuantSelectionRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    force_refresh: bool = False


_service: QuantSelectionService | None = None


def configure_quant_selection_service(
    service: QuantSelectionService | None,
) -> None:
    """Install the runtime service after its gated data providers are ready."""
    global _service
    _service = service


def get_quant_selection_service() -> QuantSelectionService:
    if _service is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "量化优选数据源尚未配置：需要通过产品元数据阶段 0 门禁，"
                "并配置可用的批量 NBBO 提供方"
            ),
        )
    return _service


def _public_run(run: dict) -> dict:
    return {
        key: value
        for key, value in run.items()
        if key not in {"quant_manifest", "run_manifest"}
    }


def _run_or_404(service: QuantSelectionService, run_id: str) -> dict:
    try:
        return service.terminate_stale_run(run_id)
    except QuantSelectionRunNotFound as exc:
        raise HTTPException(status_code=404, detail="量化优选运行不存在") from exc


async def _execute_run(service: QuantSelectionService, run_id: str) -> None:
    try:
        await asyncio.to_thread(service.execute_run, run_id)
    except QuantSelectionRunConflict:
        logger.warning("quant selection run was not executable: %s", run_id)
    except Exception:
        logger.exception("quant selection background run failed: %s", run_id)


@router.post("/runs", status_code=202)
async def create_run(
    payload: CreateQuantSelectionRunRequest,
    background_tasks: BackgroundTasks,
):
    service = get_quant_selection_service()
    run = service.create_run(force_refresh=payload.force_refresh)
    background_tasks.add_task(_execute_run, service, run["run_id"])
    return _public_run(run)


@router.get("/runs")
def list_runs(limit: int = Query(default=20, ge=1, le=100)):
    service = get_quant_selection_service()
    return {
        "items": [
            _public_run(item)
            for item in service.repository.list_runs(limit=limit)
        ]
    }


@router.get("/latest")
def get_latest():
    service = get_quant_selection_service()
    run = service.repository.latest_completed()
    if run is None:
        raise HTTPException(status_code=404, detail="暂无完整量化优选结果")
    payload = service.repository.get_results(run["run_id"])
    payload["run"] = _public_run(payload["run"])
    return payload


@router.get("/runs/{run_id}")
def get_run(run_id: str):
    service = get_quant_selection_service()
    return _public_run(_run_or_404(service, run_id))


@router.get("/runs/{run_id}/results")
def get_results(run_id: str):
    service = get_quant_selection_service()
    _run_or_404(service, run_id)
    payload = service.repository.get_results(run_id)
    payload["run"] = _public_run(payload["run"])
    return payload


@router.get("/runs/{run_id}/events")
async def get_run_events(run_id: str, request: Request):
    service = get_quant_selection_service()
    _run_or_404(service, run_id)

    async def event_generator() -> AsyncGenerator[str, None]:
        previous_event_id = None
        while True:
            if await request.is_disconnected():
                break
            try:
                run = service.terminate_stale_run(run_id)
            except QuantSelectionRunNotFound:
                payload = {
                    "run_id": run_id,
                    "status": "failed",
                    "error_summary": ["run_not_found"],
                }
                yield "event: failed\ndata: " + json.dumps(payload) + "\n\n"
                break
            event_id = ":".join(str(run.get(key) or 0) for key in (
                "status",
                "candidate_count",
                "ai_completed_count",
                "final_count",
            ))
            if event_id != previous_event_id:
                public = _public_run(run)
                yield (
                    f"id: {event_id}\n"
                    f"event: {run['status']}\n"
                    f"data: {json.dumps(public, ensure_ascii=False)}\n\n"
                )
                previous_event_id = event_id
            if run["status"] in TERMINAL_STATUSES:
                break
            await asyncio.sleep(0.5)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
