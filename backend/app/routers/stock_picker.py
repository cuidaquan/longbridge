"""
选股系统 API 路由
"""
from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import List, Optional, AsyncGenerator
from datetime import datetime, timedelta, timezone
import logging
import json
import asyncio
from uuid import uuid4

from ..stock_picker import get_stock_picker_service
from ..exceptions import LongbridgeAPIError, LongbridgeDependencyMissing
from ..models import SecuritySearchResponse
from ..security_catalog import get_security_catalog_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/stock-picker", tags=["stock-picker"])

ANALYSIS_JOB_TTL = timedelta(hours=1)
MAX_ANALYSIS_JOBS = 100
analysis_jobs = {}


# ========== 请求/响应模型 ==========

class AddStockRequest(BaseModel):
    pool_type: str  # LONG 或 SHORT
    symbol: str
    name: Optional[str] = None
    added_reason: Optional[str] = None
    priority: Optional[int] = 0


class BatchAddStocksRequest(BaseModel):
    pool_type: str
    symbols: List[str]  # 股票代码列表


class AnalyzeRequest(BaseModel):
    pool_type: Optional[str] = None  # LONG/SHORT/None(全部)
    force_refresh: bool = False


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


@router.get("/pools")
async def get_pools(
    pool_type: Optional[str] = None,
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
    pool_type: Optional[str] = None,
    sort_by: str = 'recommendation',
    limit: int = 100
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
