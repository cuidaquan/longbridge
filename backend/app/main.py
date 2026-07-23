from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .config import get_settings
from .routers import portfolio as portfolio_router
from .routers import quotes as quotes_router
from .routers import settings as settings_router
from .routers import strategies as strategies_router
from .routers import monitoring as monitoring_router
from .routers import notifications as notifications_router
from .routers import signal_analysis as signal_analysis_router
from .routers import strategies_advanced as strategies_advanced_router
from .routers import position_manager as position_manager_router
from .routers import ai_trading as ai_trading_router
from .routers import stock_picker as stock_picker_router
from .routers import ai_config as ai_config_router  # ⬆️ 新增AI配置路由
from .routers import sector_rotation as sector_rotation_router  # 板块轮动路由
from .streaming import quote_stream_manager
from .position_monitor import get_position_monitor
from .ai_trading_engine import get_ai_trading_engine
from .stock_picker_reliability import (
    RELIABILITY_CAPTURE_INTERVAL_SECONDS,
    get_stock_picker_reliability_service,
)


app = FastAPI(title="Longbridge Quant Backend", version="0.1.0")

settings = get_settings()

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins(),
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def reject_untrusted_browser_origins(request: Request, call_next):
    """Prevent cross-site browser requests from changing local trading state."""
    origin = request.headers.get("origin")
    if origin and origin not in settings.allowed_origins():
        return JSONResponse(status_code=403, content={"detail": "Origin not allowed"})
    return await call_next(request)

# 全局异常处理器，确保所有错误响应都包含 CORS 头
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"全局异常: {type(exc).__name__}: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"},
    )

app.include_router(settings_router.router)
app.include_router(quotes_router.router)
app.include_router(portfolio_router.router)
app.include_router(strategies_router.router)
app.include_router(strategies_advanced_router.router)
app.include_router(monitoring_router.router)
app.include_router(notifications_router.router)
app.include_router(signal_analysis_router.router)
app.include_router(position_manager_router.router)
app.include_router(ai_trading_router.router)
app.include_router(stock_picker_router.router)
app.include_router(ai_config_router.router)  # ⬆️ 注册AI配置路由
app.include_router(sector_rotation_router.router)  # 板块轮动路由

# 配置日志 - 确保所有模块的日志都能输出
logging.basicConfig(
    level=logging.INFO,
    format='%(levelname)s:%(name)s:%(message)s'
)

# 确保 stock_picker 模块的日志也输出到控制台
stock_picker_logger = logging.getLogger('app.stock_picker')
stock_picker_logger.setLevel(logging.INFO)
if not stock_picker_logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter('%(levelname)s:%(name)s:%(message)s'))
    stock_picker_logger.addHandler(handler)

logger = logging.getLogger(__name__)
_background_tasks: set[asyncio.Task] = set()


def _start_background_task(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


async def _auto_sync_position_data() -> None:
    """
    自动同步持仓股票的历史K线数据
    在后台异步执行，避免阻塞启动
    """
    from .services import get_portfolio_overview, sync_history_candlesticks
    from .repositories import load_symbols
    
    # 与持仓监控初始化错峰，避免启动时并发请求券商接口
    await asyncio.sleep(12)
    
    logger.info("auto-sync: starting position data sync")
    
    try:
        # 1. 获取持仓股票
        position_symbols = set()
        try:
            portfolio = await asyncio.to_thread(get_portfolio_overview)
            if portfolio and portfolio.get('positions'):
                position_symbols = {pos['symbol'] for pos in portfolio['positions']}
                logger.info(f"auto-sync: found {len(position_symbols)} position symbols")
        except Exception as e:
            logger.warning(f"auto-sync: failed to get positions: {e}")
        
        # 2. 获取手工配置的股票
        manual_symbols = set()
        try:
            manual_symbols = set(await asyncio.to_thread(load_symbols))
            logger.info(f"auto-sync: found {len(manual_symbols)} manual symbols")
        except Exception as e:
            logger.warning(f"auto-sync: failed to load symbols: {e}")
        
        # 3. 合并去重
        all_symbols = list(position_symbols | manual_symbols)
        
        if not all_symbols:
            logger.info("auto-sync: no symbols to sync")
            return
        
        logger.info(f"auto-sync: syncing {len(all_symbols)} symbols")
        
        # 4. 逐个同步（限制并发，避免API限流）
        success_count = 0
        fail_count = 0
        
        for symbol in all_symbols:
            try:
                result = await asyncio.to_thread(
                    sync_history_candlesticks,
                    [symbol],
                    "day",
                    "forward_adjust",
                    100,
                    False,
                )
                
                synced = result.get('synced_count', 0)
                if synced > 0:
                    logger.info(f"auto-sync: {symbol} synced {synced} bars")
                    success_count += 1
                else:
                    logger.debug(f"auto-sync: {symbol} no new data")
                    success_count += 1
                
                # 避免频繁请求，每次间隔0.5秒
                await asyncio.sleep(0.5)
                
            except Exception as e:
                logger.error(f"auto-sync: {symbol} failed: {e}")
                fail_count += 1
        
        logger.info(f"auto-sync: completed - success: {success_count}, failed: {fail_count}")
        
    except Exception as e:
        logger.error(f"auto-sync: fatal error: {e}")


async def _run_stock_picker_auto_refresh_once(config: dict) -> Optional[str]:
    if not config['auto_refresh_enabled']:
        return None
    job_id = stock_picker_router._create_analysis_job(None, False)
    logger.info("stock-picker auto-refresh: starting job %s", job_id)
    await stock_picker_router._run_analysis_job(job_id, None, False)
    return job_id


async def _auto_refresh_stock_picker() -> None:
    """Run configured stock-picker refreshes as isolated analysis jobs."""
    from .stock_picker import get_stock_picker_service

    await asyncio.sleep(30)
    service = get_stock_picker_service()
    last_run = 0.0
    while True:
        try:
            config = await asyncio.to_thread(service.get_config)
            interval = max(60, int(config['auto_refresh_interval']))
            now = asyncio.get_running_loop().time()
            if now - last_run >= interval:
                job_id = await _run_stock_picker_auto_refresh_once(config)
                if job_id is not None:
                    last_run = asyncio.get_running_loop().time()
            await asyncio.sleep(min(30, interval))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("stock-picker auto-refresh failed: %s", exc)
            await asyncio.sleep(30)


async def _run_stock_picker_factor_snapshot_once(
    config: dict,
) -> Optional[dict]:
    if not config["factor_snapshot_enabled"]:
        return None
    from .stock_picker_factor_snapshots import (
        get_stock_picker_factor_snapshot_service,
    )

    return await asyncio.to_thread(
        get_stock_picker_factor_snapshot_service().capture_due_baseline,
        persist=True,
    )


async def _auto_capture_stock_picker_factor_snapshots() -> None:
    """Poll for post-close market dates and capture each group once."""
    from .stock_picker import get_stock_picker_service

    await asyncio.sleep(60)
    config_service = get_stock_picker_service()
    while True:
        poll_interval = 900
        try:
            config = await asyncio.to_thread(
                config_service.get_config
            )
            poll_interval = max(
                300,
                min(
                    3600,
                    int(config["factor_snapshot_poll_interval"]),
                ),
            )
            result = await _run_stock_picker_factor_snapshot_once(
                config
            )
            if result is not None:
                logger.info(
                    "stock-picker factor snapshots: captured=%s "
                    "skipped=%s errors=%s rows=%s",
                    len(result["captured"]),
                    len(result["skipped"]),
                    len(result["errors"]),
                    result["row_count"],
                )
                for error in result["errors"]:
                    logger.warning(
                        "stock-picker factor snapshot group failed: %s",
                        error,
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "stock-picker factor snapshot capture failed: %s",
                exc,
            )
        await asyncio.sleep(poll_interval)


async def _persist_stock_picker_reliability() -> None:
    """Persist interval metrics and update durable alert state."""
    await asyncio.sleep(RELIABILITY_CAPTURE_INTERVAL_SECONDS)
    service = get_stock_picker_reliability_service()
    while True:
        try:
            result = await asyncio.to_thread(service.capture)
            if result["alerts"]:
                logger.warning(
                    "stock-picker reliability: %s active alerts",
                    len(result["alerts"]),
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "stock-picker reliability persistence failed: %s",
                exc,
            )
        await asyncio.sleep(RELIABILITY_CAPTURE_INTERVAL_SECONDS)


@app.on_event("startup")
async def on_startup() -> None:
    logger.info("startup: entering handler")
    loop = asyncio.get_running_loop()
    quote_stream_manager.attach_loop(loop)
    logger.info("startup: loop attached %s", loop)
    async def start_quote_stream_after_health_ready() -> None:
        await asyncio.sleep(3)
        quote_stream_manager.ensure_started()
        logger.info("startup: quote stream started")

    _start_background_task(start_quote_stream_after_health_ready())
    logger.info("startup: quote stream scheduled")

    # Initialize position monitor
    monitor = get_position_monitor()
    _start_background_task(monitor.start_monitoring())
    logger.info("startup: position monitor started")
    
    # Auto-sync position historical data
    _start_background_task(_auto_sync_position_data())
    logger.info("startup: auto-sync task scheduled")

    _start_background_task(_auto_refresh_stock_picker())
    logger.info("startup: stock-picker auto-refresh scheduled")

    _start_background_task(
        _auto_capture_stock_picker_factor_snapshots()
    )
    logger.info("startup: stock-picker factor snapshots scheduled")

    _start_background_task(_persist_stock_picker_reliability())
    logger.info("startup: stock-picker reliability persistence scheduled")
    
    # Initialize AI Trading Engine (if enabled)
    ai_engine = get_ai_trading_engine()
    # Note: 引擎会根据配置决定是否启动
    # 用户需要通过 API 或配置文件启用
    logger.info("startup: AI trading engine initialized")



@app.on_event("shutdown")
async def on_shutdown() -> None:
    monitor = get_position_monitor()
    await monitor.stop_monitoring()
    for task in list(_background_tasks):
        task.cancel()
    if _background_tasks:
        await asyncio.gather(*list(_background_tasks), return_exceptions=True)

    await quote_stream_manager.stop()
    
    # Stop AI trading engine
    ai_engine = get_ai_trading_engine()
    await ai_engine.stop()
    logger.info("shutdown: AI trading engine stopped")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


async def _stream_websocket_queue(
    websocket: WebSocket,
    queue: asyncio.Queue,
    json_serializer,
) -> None:
    """Forward queue messages while independently watching for client disconnects."""
    receive_task = asyncio.create_task(websocket.receive())
    queue_task = asyncio.create_task(queue.get())
    try:
        while True:
            done, _ = await asyncio.wait(
                {receive_task, queue_task},
                return_when=asyncio.FIRST_COMPLETED,
            )

            if receive_task in done:
                event = receive_task.result()
                if event["type"] == "websocket.disconnect":
                    raise WebSocketDisconnect(event.get("code", 1000))
                receive_task = asyncio.create_task(websocket.receive())

            if queue_task in done:
                payload = queue_task.result()
                await websocket.send_text(
                    json.dumps(payload, default=json_serializer)
                )
                queue_task = asyncio.create_task(queue.get())
    finally:
        for task in (receive_task, queue_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(receive_task, queue_task, return_exceptions=True)


@app.websocket("/ws/quotes")
async def quotes_websocket(websocket: WebSocket) -> None:
    from datetime import datetime

    def json_serializer(obj):
        """JSON serializer for objects not serializable by default json code"""
        if isinstance(obj, datetime):
            return obj.isoformat()
        # Handle enum-like objects and other non-serializable types
        if hasattr(obj, 'name'):
            return obj.name
        if hasattr(obj, 'value'):
            return obj.value
        # Convert to string as last resort
        return str(obj)

    await websocket.accept()
    queue = quote_stream_manager.add_listener()
    try:
        await _stream_websocket_queue(websocket, queue, json_serializer)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
    finally:
        quote_stream_manager.remove_listener(queue)


@app.websocket("/ws/ai-trading")
async def ai_trading_websocket(websocket: WebSocket) -> None:
    """AI 交易实时推送 WebSocket"""
    from datetime import datetime
    from .ai_trading_engine import get_ai_trading_engine

    def json_serializer(obj):
        """JSON serializer for objects not serializable by default json code"""
        if isinstance(obj, datetime):
            return obj.isoformat()
        if hasattr(obj, 'name'):
            return obj.name
        if hasattr(obj, 'value'):
            return obj.value
        return str(obj)

    await websocket.accept()
    engine = get_ai_trading_engine()
    queue = engine.add_listener()
    
    try:
        # 发送欢迎消息也必须在清理保护范围内，客户端若提前断开仍会移除监听器
        welcome_msg = {
            'type': 'connected',
            'message': 'Connected to AI Trading Engine',
            'running': engine.is_running(),
            'timestamp': datetime.now().isoformat()
        }
        await websocket.send_text(json.dumps(welcome_msg, default=json_serializer))

        await _stream_websocket_queue(websocket, queue, json_serializer)
    except WebSocketDisconnect:
        logger.info("📡 AI trading WebSocket disconnected")
    except Exception as e:
        logger.error(f"AI trading WebSocket error: {e}")
    finally:
        engine.remove_listener(queue)
