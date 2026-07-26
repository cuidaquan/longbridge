"""HTTP and SSE API for persisted quantitative selection runs."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import AsyncGenerator

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict

from ..quant_stock_selector_evaluation import (
    JsonQuantOutcomeProvider,
    QuantOutcomeError,
    QuantSelectionEvaluationService,
)
from ..quant_stock_selector_service import (
    TERMINAL_STATUSES,
    QuantSelectionRunConflict,
    QuantSelectionRunNotFound,
    QuantSelectionService,
)
from ..quant_stock_selector_quality import QuantSelectionQualityService
from ..quant_stock_selector_shadow import (
    QuantShadowEvaluationError,
    QuantShadowEvaluationService,
    build_configured_quant_shadow_service,
)


logger = logging.getLogger(__name__)
router = APIRouter(
    prefix="/api/quant-stock-selector",
    tags=["quant-stock-selector"],
)


class CreateQuantSelectionRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    force_refresh: bool = False


class RunQuantSelectionEvaluationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RunQuantShadowEvaluationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_run_id: str


_service: QuantSelectionService | None = None
_evaluation_service: QuantSelectionEvaluationService | None = None
_quality_service: QuantSelectionQualityService | None = None
_shadow_service: QuantShadowEvaluationService | None = None


def configure_quant_selection_service(
    service: QuantSelectionService | None,
) -> None:
    """Install the runtime service after its gated data providers are ready."""
    global _service
    _service = service


def configure_quant_selection_evaluation_service(
    service: QuantSelectionEvaluationService | None,
) -> None:
    """Install the outcome evaluation service for tests or runtime overrides."""
    global _evaluation_service
    _evaluation_service = service


def configure_quant_selection_quality_service(
    service: QuantSelectionQualityService | None,
) -> None:
    global _quality_service
    _quality_service = service


def get_quant_selection_quality_service() -> QuantSelectionQualityService:
    global _quality_service
    if _quality_service is None:
        _quality_service = QuantSelectionQualityService()
    return _quality_service


def configure_quant_shadow_evaluation_service(
    service: QuantShadowEvaluationService | None,
) -> None:
    global _shadow_service
    _shadow_service = service


def get_quant_shadow_evaluation_service() -> QuantShadowEvaluationService:
    global _shadow_service
    if _shadow_service is None:
        try:
            _shadow_service = build_configured_quant_shadow_service()
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail=(
                    "Flash/Pro 影子评估未启用，或模型与成本预算尚未配置"
                ),
            ) from exc
    return _shadow_service


def get_quant_selection_service() -> QuantSelectionService:
    global _service
    if _service is None:
        try:
            from ..quant_stock_selector_data import (
                build_configured_quant_selection_service,
            )

            _service = build_configured_quant_selection_service()
        except Exception as exc:
            logger.warning("quant selection service is not ready: %s", exc)
            raise HTTPException(
                status_code=503,
                detail=(
                    "量化优选服务尚未就绪：请配置 DeepSeek API Key 与完整的 "
                    "Longbridge 凭据，并按账户额度设置 "
                    "QUANT_SELECTOR_HISTORY_SYMBOL_LIMIT；"
                    "QUANT_SELECTOR_BUNDLE_PATH 仅用于冻结数据包回放"
                ),
            ) from exc
    return _service


def get_quant_selection_evaluation_service(
    *, require_outcome: bool = True,
) -> QuantSelectionEvaluationService:
    global _evaluation_service
    if _evaluation_service is None:
        from ..config import get_settings

        settings = get_settings()
        _evaluation_service = QuantSelectionEvaluationService(
            (
                JsonQuantOutcomeProvider(settings.quant_selector_outcome_bundle_path)
                if settings.quant_selector_outcome_bundle_path is not None
                else None
            )
        )
    if (
        require_outcome
        and getattr(_evaluation_service, "outcome_provider", object()) is None
    ):
        raise HTTPException(
            status_code=503,
            detail=(
                "量化优选效果评估数据源尚未就绪：需要配置经验证的总回报价格、"
                "NYSE 交易日历及退市结算数据包"
            ),
        )
    return _evaluation_service


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


async def _execute_shadow(
    service: QuantShadowEvaluationService,
    shadow_id: str,
) -> None:
    try:
        await asyncio.to_thread(service.execute, shadow_id)
    except Exception:
        logger.exception("quant selection shadow evaluation failed: %s", shadow_id)


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


@router.get("/quality")
def get_quality(limit: int = Query(default=100, ge=1, le=1000)):
    try:
        return get_quant_selection_quality_service().report(limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("loading quant selection quality report failed")
        raise HTTPException(status_code=500, detail="获取量化优选运行质量失败") from exc


@router.post("/shadow-evaluations", status_code=202)
async def create_shadow_evaluation(
    payload: RunQuantShadowEvaluationRequest,
    background_tasks: BackgroundTasks,
):
    service = get_quant_shadow_evaluation_service()
    try:
        evaluation = service.start(payload.source_run_id)
    except QuantShadowEvaluationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    background_tasks.add_task(
        _execute_shadow,
        service,
        evaluation["shadow_id"],
    )
    return evaluation


@router.get("/shadow-evaluations")
def list_shadow_evaluations(
    limit: int = Query(default=20, ge=1, le=100),
):
    service = get_quant_shadow_evaluation_service()
    return {"items": service.repository.list(limit=limit)}


@router.get("/shadow-evaluations/{shadow_id}")
def get_shadow_evaluation(shadow_id: str):
    service = get_quant_shadow_evaluation_service()
    try:
        return service.repository.get(shadow_id)
    except QuantShadowEvaluationError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/evaluation")
async def run_evaluation(_payload: RunQuantSelectionEvaluationRequest):
    service = get_quant_selection_evaluation_service()
    try:
        return await asyncio.to_thread(service.run, persist=True)
    except QuantOutcomeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("quant selection evaluation failed")
        raise HTTPException(status_code=500, detail="量化优选效果评估失败") from exc


@router.get("/evaluations")
async def list_evaluations(limit: int = Query(default=20, ge=1, le=100)):
    service = get_quant_selection_evaluation_service(require_outcome=False)
    try:
        return {
            "items": await asyncio.to_thread(service.get_history, limit)
        }
    except Exception as exc:
        logger.exception("loading quant selection evaluations failed")
        raise HTTPException(status_code=500, detail="获取量化优选效果评估历史失败") from exc


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
