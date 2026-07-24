"""
选股系统 API 路由
"""
from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from typing import List, Literal, Optional, AsyncGenerator
from datetime import datetime, timedelta, timezone
import logging
import json
import asyncio
from uuid import uuid4

from ..stock_picker import get_stock_picker_service
from ..exceptions import LongbridgeAPIError, LongbridgeDependencyMissing
from ..models import SecuritySearchResponse
from ..security_catalog import get_security_catalog_service
from ..stock_picker_backtest import get_stock_picker_backtest_service
from ..stock_picker_factor_snapshots import (
    get_stock_picker_factor_snapshot_service,
)
from ..stock_picker_reliability import (
    get_stock_picker_reliability_service,
)
from ..stock_screener import get_stock_screener_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/stock-picker", tags=["stock-picker"])

ANALYSIS_JOB_TTL = timedelta(hours=1)
MAX_ANALYSIS_JOBS = 100
analysis_jobs = {}


# ========== 请求/响应模型 ==========

class AddStockRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pool_type: Literal["LONG", "SHORT"]
    symbol: str = Field(min_length=1, max_length=32)
    name: Optional[str] = None
    added_reason: Optional[str] = None
    priority: Optional[int] = 0


class BatchAddStocksRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pool_type: Literal["LONG", "SHORT"]
    symbols: List[str] = Field(min_length=1, max_length=500)


class AnalyzeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pool_type: Optional[Literal["LONG", "SHORT"]] = None
    force_refresh: bool = False


class StockPickerConfigUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    auto_refresh_enabled: Optional[bool] = None
    auto_refresh_interval: Optional[int] = Field(None, ge=60, le=86400)
    max_pool_size: Optional[int] = Field(None, ge=1, le=500)
    cache_duration: Optional[int] = Field(None, ge=0, le=86400)
    min_score_to_recommend: Optional[int] = Field(None, ge=0, le=100)
    analysis_lookback: Optional[int] = Field(None, ge=60, le=1000)
    ai_top_n_per_pool: Optional[int] = Field(None, ge=0, le=100)
    history_retention_days: Optional[int] = Field(None, ge=1, le=3650)
    max_history_per_stock: Optional[int] = Field(None, ge=1, le=1000)
    factor_snapshot_enabled: Optional[bool] = None
    factor_snapshot_poll_interval: Optional[int] = Field(
        None,
        ge=300,
        le=3600,
    )


class ScreenerIndexFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min_turnover: Optional[float] = Field(default=None, ge=0)
    min_market_value: Optional[float] = Field(default=None, ge=0)
    min_turnover_rate: Optional[float] = Field(default=None, ge=0)
    min_pe_ttm: Optional[float] = Field(default=None, ge=0)
    max_pe_ttm: Optional[float] = Field(default=None, gt=0)
    max_pb: Optional[float] = Field(default=None, gt=0)
    min_capital_flow: Optional[float] = None
    min_volume_ratio: Optional[float] = Field(default=None, ge=0)
    min_market_rs_10d: Optional[float] = None
    min_market_rs_half_year: Optional[float] = None
    min_industry_rs_10d: Optional[float] = None
    min_industry_rs_half_year: Optional[float] = None
    max_days_to_cover: Optional[float] = Field(default=None, ge=0)
    max_short_ratio: Optional[float] = Field(default=None, ge=0)
    max_short_ratio_change: Optional[float] = None
    max_spread_bps: Optional[float] = Field(default=None, ge=0)
    min_top_of_book_notional: Optional[float] = Field(
        default=None,
        ge=0,
    )
    min_revenue_yoy: Optional[float] = None
    max_revenue_yoy: Optional[float] = None
    min_net_profit_yoy: Optional[float] = None
    max_net_profit_yoy: Optional[float] = None
    min_operating_cash_flow_yoy: Optional[float] = None
    min_analyst_alignment: Optional[float] = Field(
        default=None,
        ge=-1,
        le=1,
    )
    min_eps_revision_alignment: Optional[float] = Field(
        default=None,
        ge=-1,
        le=1,
    )
    min_days_to_financial_event: Optional[float] = Field(
        default=None,
        ge=0,
        le=365,
    )
    min_days_to_corporate_action: Optional[float] = Field(
        default=None,
        ge=0,
        le=365,
    )
    max_initial_margin_ratio: Optional[float] = Field(
        default=None,
        ge=0,
    )


class ScreenerSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    market: Literal["US", "HK", "CN", "SG"]
    strategy_id: int = Field(gt=0)
    page: int = Field(default=0, ge=0, le=10000)
    size: int = Field(default=20, ge=1, le=100)
    include_indexes: bool = True
    filters: Optional[ScreenerIndexFilters] = None
    target_direction: Literal["LONG", "SHORT"] = "LONG"
    benchmark_symbol: Optional[str] = Field(
        default=None,
        min_length=1,
        max_length=32,
    )
    include_short_risk: bool = True
    include_tradeability: bool = True
    require_normal_trade_status: bool = True
    include_fundamentals: bool = False
    include_margin_requirements: bool = False
    fundamental_event_window_days: int = Field(
        default=30,
        ge=1,
        le=365,
    )
    include_corporate_actions: bool = False


class ScreenerImportItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(min_length=1, max_length=32)
    name: Optional[str] = Field(default=None, max_length=200)


class ScreenerImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pool_type: Literal["LONG", "SHORT"]
    strategy_id: int = Field(gt=0)
    strategy_name: str = Field(min_length=1, max_length=200)
    items: List[ScreenerImportItem] = Field(min_length=1, max_length=100)


class StockPickerBacktestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pool_type: Literal["LONG", "SHORT"]
    symbols: Optional[List[str]] = Field(default=None, max_length=100)
    horizons: List[int] = Field(
        default_factory=lambda: [5, 10, 20],
        min_length=1,
        max_length=10,
    )
    lookback: int = Field(default=250, ge=30, le=5000)
    max_bars: int = Field(default=1000, ge=30, le=5000)
    min_history: int = Field(default=60, ge=30, le=5000)
    step: int = Field(default=5, ge=1, le=60)
    top_n: int = Field(default=5, ge=1, le=100)
    train_ratio: float = Field(default=0.7, ge=0.5, le=0.9)
    walk_forward_folds: int = Field(default=3, ge=1, le=10)
    transaction_cost_bps: float = Field(default=10, ge=0, le=1000)
    order_notional: Optional[float] = Field(default=None, gt=0)
    max_participation_rate: float = Field(default=0.1, gt=0, le=1)
    impact_coefficient: float = Field(default=0.5, ge=0, le=10)
    impact_volatility_lookback: int = Field(default=20, ge=2, le=252)


class StockPickerFactorSnapshotCaptureRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    market: Literal["US", "HK"]
    target_direction: Literal["LONG", "SHORT"]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _prune_analysis_jobs() -> None:
    """Remove expired terminal jobs and cap retained job history."""
    cutoff = _utc_now() - ANALYSIS_JOB_TTL
    expired = [
        job_id
        for job_id, job in analysis_jobs.items()
        if job['updated_at'] < cutoff
    ]
    for job_id in expired:
        del analysis_jobs[job_id]

    if len(analysis_jobs) < MAX_ANALYSIS_JOBS:
        return
    terminal_jobs = sorted(
        (
            (job['updated_at'], job_id)
            for job_id, job in analysis_jobs.items()
            if job['status'] in {'completed', 'error'}
        ),
    )
    for _, job_id in terminal_jobs[:len(analysis_jobs) - MAX_ANALYSIS_JOBS + 1]:
        del analysis_jobs[job_id]


def _create_analysis_job(pool_type: Optional[str], force_refresh: bool) -> str:
    _prune_analysis_jobs()
    if len(analysis_jobs) >= MAX_ANALYSIS_JOBS:
        raise HTTPException(
            status_code=429,
            detail="当前分析任务过多，请稍后重试",
        )
    job_id = uuid4().hex
    now = _utc_now()
    analysis_jobs[job_id] = {
        'job_id': job_id,
        'pool_type': pool_type,
        'force_refresh': force_refresh,
        'current': None,
        'total': 0,
        'completed': 0,
        'status': 'queued',
        'logs': [],
        'result': None,
        'error': None,
        'created_at': now,
        'updated_at': now,
    }
    return job_id


def _append_job_log(job: dict, message: str) -> None:
    job['logs'].append({
        'time': _utc_now().isoformat(),
        'message': message,
    })
    if len(job['logs']) > 50:
        job['logs'] = job['logs'][-50:]
    job['updated_at'] = _utc_now()


async def _run_analysis_job(
    job_id: str,
    pool_type: Optional[str],
    force_refresh: bool,
) -> None:
    job = analysis_jobs.get(job_id)
    if job is None:
        return
    service = get_stock_picker_service()

    def update_progress(data: dict) -> None:
        current_job = analysis_jobs.get(job_id)
        if current_job is None:
            return
        for field in ('status', 'total', 'completed', 'current'):
            if field in data:
                if field == 'status' and data[field] in {'completed', 'error'}:
                    # The runner publishes terminal status only after result/error
                    # has been attached, so SSE cannot close on a partial payload.
                    continue
                current_job[field] = data[field]
        if 'log' in data:
            _append_job_log(current_job, data['log'])
        else:
            current_job['updated_at'] = _utc_now()

    try:
        job['status'] = 'running'
        job['updated_at'] = _utc_now()
        result = await service.analyze_pool(
            pool_type=pool_type,
            force_refresh=force_refresh,
            progress_callback=update_progress,
            job_id=job_id,
        )
        job['result'] = result
        job['status'] = 'completed'
        job['updated_at'] = _utc_now()
    except Exception as exc:
        job['status'] = 'error'
        job['error'] = str(exc)
        _append_job_log(job, f'❌ 错误: {exc}')
        logger.error("批量分析任务失败: job_id=%s", job_id, exc_info=True)


# ========== API 端点 ==========


@router.get("/securities", response_model=SecuritySearchResponse)
async def search_securities(
    market: str = Query(default="US", pattern="^(US|HK|CN)$"),
    q: str = Query(default="", max_length=80),
    limit: int = Query(default=20, ge=1, le=50),
):
    """按代码、中英文名称搜索 Longbridge 官方证券列表。"""
    try:
        service = get_security_catalog_service()
        items = await asyncio.to_thread(service.search, market, q, limit)
        return {
            "market": market,
            "query": q,
            "source": "longbridge",
            "items": items,
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except LongbridgeDependencyMissing as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except LongbridgeAPIError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/screener/strategies")
async def get_screener_strategies(
    market: Literal["US", "HK", "CN", "SG"] = Query(default="US"),
    include_user: bool = True,
):
    try:
        return await asyncio.to_thread(
            get_stock_screener_service().list_strategies,
            market,
            include_user,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except LongbridgeDependencyMissing as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except LongbridgeAPIError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/screener/search")
async def search_screener_candidates(request: ScreenerSearchRequest):
    try:
        return await asyncio.to_thread(
            get_stock_screener_service().search,
            market=request.market,
            strategy_id=request.strategy_id,
            page=request.page,
            size=request.size,
            filters=(
                request.filters.model_dump(exclude_none=True)
                if request.filters
                else None
            ),
            include_indexes=request.include_indexes,
            target_direction=request.target_direction,
            benchmark_symbol=request.benchmark_symbol,
            include_short_risk=request.include_short_risk,
            include_tradeability=request.include_tradeability,
            require_normal_trade_status=(
                request.require_normal_trade_status
            ),
            include_fundamentals=request.include_fundamentals,
            include_margin_requirements=(
                request.include_margin_requirements
            ),
            fundamental_event_window_days=(
                request.fundamental_event_window_days
            ),
            include_corporate_actions=(
                request.include_corporate_actions
            ),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except LongbridgeDependencyMissing as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except LongbridgeAPIError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/screener/import")
async def import_screener_candidates(request: ScreenerImportRequest):
    stock_picker = get_stock_picker_service()
    reason = (
        f"Longbridge Screener：{request.strategy_name} "
        f"(strategy_id={request.strategy_id})"
    )
    success = []
    failed = []
    for item in request.items:
        try:
            stock_picker.add_stock(
                request.pool_type,
                item.symbol,
                name=item.name,
                added_reason=reason,
            )
            success.append(item.symbol.strip().upper())
        except ValueError as exc:
            failed.append({
                "symbol": item.symbol,
                "error": str(exc),
            })
        except Exception as exc:
            logger.error("导入 Screener 候选失败: %s", item.symbol, exc_info=True)
            failed.append({
                "symbol": item.symbol,
                "error": "导入失败",
            })
    return {
        "success": success,
        "failed": failed,
        "total": len(request.items),
        "success_count": len(success),
    }


@router.post("/backtest")
async def run_stock_picker_backtest(request: StockPickerBacktestRequest):
    try:
        return await asyncio.to_thread(
            get_stock_picker_backtest_service().run,
            pool_type=request.pool_type,
            symbols=request.symbols,
            horizons=request.horizons,
            lookback=request.lookback,
            max_bars=request.max_bars,
            min_history=request.min_history,
            step=request.step,
            top_n=request.top_n,
            train_ratio=request.train_ratio,
            walk_forward_folds=request.walk_forward_folds,
            transaction_cost_bps=request.transaction_cost_bps,
            order_notional=request.order_notional,
            max_participation_rate=request.max_participation_rate,
            impact_coefficient=request.impact_coefficient,
            impact_volatility_lookback=(
                request.impact_volatility_lookback
            ),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.error("运行智能选股回测失败: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/backtests")
async def get_stock_picker_backtests(
    limit: int = Query(default=20, ge=1, le=100),
):
    try:
        return {
            "items": await asyncio.to_thread(
                get_stock_picker_backtest_service().get_history,
                limit,
            )
        }
    except Exception as exc:
        logger.error("获取智能选股回测历史失败: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/factor-snapshots/capture")
async def capture_stock_picker_factor_snapshots(
    request: StockPickerFactorSnapshotCaptureRequest,
):
    try:
        return await asyncio.to_thread(
            get_stock_picker_factor_snapshot_service().capture_baseline,
            market=request.market,
            target_direction=request.target_direction,
            persist=True,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.error(
            "采集智能选股因子点时快照失败: %s",
            exc,
            exc_info=True,
        )
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/factor-snapshots/coverage")
async def get_stock_picker_factor_snapshot_coverage(
    days: int = Query(default=365, ge=1, le=3650),
):
    try:
        return await asyncio.to_thread(
            get_stock_picker_factor_snapshot_service().get_coverage,
            days,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.error(
            "获取智能选股因子快照覆盖率失败: %s",
            exc,
            exc_info=True,
        )
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/reliability")
def get_stock_picker_reliability():
    try:
        return get_stock_picker_reliability_service().get_current()
    except Exception as exc:
        logger.error(
            "获取智能选股可靠性状态失败: %s",
            exc,
            exc_info=True,
        )
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/reliability/health")
def get_stock_picker_reliability_health():
    try:
        health = (
            get_stock_picker_reliability_service()
            .get_worker_health()
        )
        if not health["healthy"]:
            return JSONResponse(status_code=503, content=health)
        return health
    except Exception as exc:
        logger.error(
            "获取智能选股可靠性 worker 健康状态失败: %s",
            exc,
            exc_info=True,
        )
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/reliability/history")
def get_stock_picker_reliability_history(
    hours: int = Query(default=24, ge=1, le=720),
    limit: int = Query(default=100, ge=1, le=1000),
):
    try:
        return get_stock_picker_reliability_service().get_history(
            hours=hours,
            limit=limit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.error(
            "获取智能选股可靠性历史失败: %s",
            exc,
            exc_info=True,
        )
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/reliability/deliveries")
def get_stock_picker_reliability_deliveries(
    limit: int = Query(default=100, ge=1, le=1000),
):
    try:
        return get_stock_picker_reliability_service().get_deliveries(
            limit=limit
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.error(
            "获取智能选股可靠性告警投递失败: %s",
            exc,
            exc_info=True,
        )
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/pools")
async def get_pools(
    pool_type: Optional[Literal["LONG", "SHORT"]] = None,
    include_inactive: bool = True,
):
    """
    获取股票池
    
    Args:
        pool_type: LONG | SHORT | None(全部)
    """
    try:
        service = get_stock_picker_service()
        pools = service.get_pools(pool_type, include_inactive=include_inactive)
        return pools
    except Exception as e:
        logger.error(f"获取股票池失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/config")
async def get_stock_picker_config():
    try:
        return get_stock_picker_service().get_config()
    except Exception as exc:
        logger.error("获取选股配置失败: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.put("/config")
async def update_stock_picker_config(request: StockPickerConfigUpdate):
    try:
        updates = request.model_dump(exclude_none=True)
        return get_stock_picker_service().update_config(updates)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.error("更新选股配置失败: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/pools")
async def add_stock(request: AddStockRequest):
    """
    添加股票到池
    """
    try:
        service = get_stock_picker_service()
        stock_id = service.add_stock(
            pool_type=request.pool_type,
            symbol=request.symbol,
            name=request.name,
            added_reason=request.added_reason,
            priority=request.priority
        )
        return {
            "success": True,
            "id": stock_id,
            "message": f"成功添加 {request.symbol} 到 {request.pool_type} 池"
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.error(f"添加股票失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/pools/batch")
async def batch_add_stocks(request: BatchAddStocksRequest):
    """
    批量添加股票
    """
    try:
        service = get_stock_picker_service()
        result = service.batch_add_stocks(
            pool_type=request.pool_type,
            symbols=request.symbols
        )
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.error(f"批量添加失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/pools/{pool_id}")
async def remove_stock(pool_id: int):
    """
    从池中移除股票
    """
    try:
        service = get_stock_picker_service()
        service.remove_stock(pool_id)
        return {"success": True, "message": "移除成功"}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.error(f"移除股票失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/pools/clear/{pool_type}")
async def clear_pool(pool_type: str):
    """
    清空指定类型的股票池
    
    Args:
        pool_type: LONG | SHORT
    """
    try:
        if pool_type not in ['LONG', 'SHORT']:
            raise HTTPException(status_code=400, detail="pool_type 必须是 LONG 或 SHORT")
        
        service = get_stock_picker_service()
        count = service.clear_pool(pool_type)
        return {"success": True, "message": f"已清空{count}只股票", "count": count}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"清空股票池失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.patch("/pools/{pool_id}/toggle")
async def toggle_stock(pool_id: int):
    """
    切换股票激活状态
    """
    try:
        service = get_stock_picker_service()
        service.toggle_active(pool_id)
        return {"success": True, "message": "切换成功"}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.error(f"切换状态失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/analyze", status_code=202)
async def analyze_pools(
    request: AnalyzeRequest,
    background_tasks: BackgroundTasks,
):
    """
    触发批量分析
    
    Args:
        pool_type: LONG | SHORT | None(全部)
        force_refresh: 是否强制刷新缓存
    """
    job_id = _create_analysis_job(request.pool_type, request.force_refresh)
    logger.info(
        "创建分析任务: job_id=%s, pool_type=%s, force=%s",
        job_id,
        request.pool_type,
        request.force_refresh,
    )
    background_tasks.add_task(
        _run_analysis_job,
        job_id,
        request.pool_type,
        request.force_refresh,
    )
    return {
        "success": True,
        "job_id": job_id,
        "status": "queued",
        "message": "分析任务已创建",
    }


@router.get("/analysis/progress/{job_id}")
async def get_analysis_progress(job_id: str):
    """
    获取实时分析进度 (SSE)
    """
    _prune_analysis_jobs()
    if job_id not in analysis_jobs:
        raise HTTPException(status_code=404, detail="分析任务不存在或已过期")

    async def event_generator() -> AsyncGenerator[str, None]:
        """生成SSE事件"""
        try:
            while True:
                job = analysis_jobs.get(job_id)
                if job is None:
                    yield (
                        "data: "
                        + json.dumps({
                            'job_id': job_id,
                            'status': 'error',
                            'error': '分析任务已过期',
                            'logs': [],
                        }, ensure_ascii=False)
                        + "\n\n"
                    )
                    break
                progress_data = {
                    'job_id': job_id,
                    'current': job['current'],
                    'total': job['total'],
                    'completed': job['completed'],
                    'status': job['status'],
                    'logs': job['logs'][-10:],
                    'result': job['result'],
                    'error': job['error'],
                }
                
                yield f"data: {json.dumps(progress_data, ensure_ascii=False)}\n\n"
                
                if job['status'] in {'completed', 'error'}:
                    break
                
                await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            pass
    
    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )


@router.get("/analysis")
async def get_analysis_results(
    pool_type: Optional[Literal["LONG", "SHORT"]] = None,
    sort_by: Literal["recommendation", "score", "confidence"] = 'recommendation',
    limit: int = Query(default=100, ge=1, le=500),
):
    """
    获取分析结果（排序）
    
    Args:
        pool_type: LONG | SHORT | None(全部)
        sort_by: recommendation | score | confidence
        limit: 返回数量
    """
    try:
        service = get_stock_picker_service()
        results = service.get_analysis_results(
            pool_type=pool_type,
            sort_by=sort_by,
            limit=limit
        )
        return results
    except Exception as e:
        logger.error(f"获取分析结果失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/analysis/{symbol}/history")
async def get_symbol_analysis_history(
    symbol: str,
    pool_type: Optional[Literal["LONG", "SHORT"]] = None,
    limit: int = Query(default=30, ge=1, le=500),
):
    try:
        return {
            "symbol": symbol.strip().upper(),
            "items": get_stock_picker_service().get_analysis_history(
                symbol,
                pool_type=pool_type,
                limit=limit,
            ),
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.error("获取分析历史失败: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/analysis/{symbol}")
async def get_symbol_analysis(symbol: str):
    """
    获取单个股票的详细分析
    """
    try:
        service = get_stock_picker_service()
        results = service.get_analysis_results()
        
        # 查找该股票
        for item in results['long_analysis'] + results['short_analysis']:
            if item['symbol'] == symbol:
                return item
        
        raise HTTPException(status_code=404, detail=f"未找到 {symbol} 的分析结果")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"获取股票详情失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/stats")
async def get_statistics():
    """
    获取统计信息
    """
    try:
        service = get_stock_picker_service()
        pools = service.get_pools()
        results = service.get_analysis_results()
        
        return {
            "pools": {
                "long_count": len(pools['long_pool']),
                "short_count": len(pools['short_pool'])
            },
            "analysis": results['stats']
        }
    except Exception as e:
        logger.error(f"获取统计信息失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))
