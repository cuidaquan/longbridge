"""
AI 自动交易 - API 路由
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from typing import List, Optional
import logging

from ..repositories import (
    get_ai_trading_config,
    update_ai_trading_config,
    get_ai_analysis_logs,
    get_ai_trades,
    get_ai_positions,
    get_daily_trades_count,
    get_daily_pnl,
)
from ..ai_trading_engine import get_ai_trading_engine
from ..services import get_cached_candlesticks

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ai-trading", tags=["ai_trading"])
AI_API_KEY_MASK = "********"
AI_CONFIG_PUBLIC_FIELDS = {
    "enabled",
    "symbols",
    "check_interval_minutes",
    "ai_model",
    "ai_api_key",
    "ai_temperature",
    "min_confidence",
    "max_position_per_stock",
    "max_daily_trades",
    "max_loss_per_day",
    "enable_stop_loss",
    "default_stop_loss_percent",
    "enable_real_trading",
    "position_sizing_method",
    "fixed_amount_per_trade",
}


def _public_config(config: Optional[dict]) -> dict:
    """Return a copy that is safe to serialize in API responses."""
    public = {
        key: value
        for key, value in (config or {}).items()
        if key in AI_CONFIG_PUBLIC_FIELDS
    }
    if public.get("ai_api_key"):
        public["ai_api_key"] = AI_API_KEY_MASK
    return public


# ============================================
# Pydantic 模型
# ============================================

class AiTradingConfigUpdate(BaseModel):
    """AI 交易配置更新模型"""
    model_config = ConfigDict(extra="forbid")

    enabled: Optional[bool] = None
    symbols: Optional[List[str]] = None
    check_interval_minutes: Optional[int] = Field(None, ge=1, le=1440)
    ai_model: Optional[str] = None
    ai_api_key: Optional[str] = None
    ai_temperature: Optional[float] = Field(None, ge=0, le=2)
    min_confidence: Optional[float] = Field(None, ge=0, le=1)
    max_position_per_stock: Optional[float] = Field(None, gt=0)
    max_daily_trades: Optional[int] = Field(None, ge=0, le=1000)
    max_loss_per_day: Optional[float] = Field(None, gt=0)
    enable_stop_loss: Optional[bool] = None
    default_stop_loss_percent: Optional[float] = Field(None, gt=0, le=100)
    enable_real_trading: Optional[bool] = None
    position_sizing_method: Optional[str] = None
    fixed_amount_per_trade: Optional[float] = Field(None, gt=0)
    real_trading_confirmation: Optional[str] = None


# ============================================
# 引擎控制接口
# ============================================

@router.post("/engine/start")
async def start_engine():
    """启动 AI 交易引擎"""
    try:
        engine = get_ai_trading_engine()
        
        if engine.is_running():
            return {
                "status": "already_running",
                "message": "AI Trading Engine is already running"
            }
        
        await engine.start()
        
        return {
            "status": "started",
            "message": "AI Trading Engine started successfully",
            "config": _public_config(engine.config),
        }
    except Exception as e:
        logger.error(f"Failed to start AI engine: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/engine/stop")
async def stop_engine():
    """停止 AI 交易引擎"""
    try:
        engine = get_ai_trading_engine()
        
        if not engine.is_running():
            return {
                "status": "already_stopped",
                "message": "AI Trading Engine is not running"
            }
        
        await engine.stop()
        
        return {
            "status": "stopped",
            "message": "AI Trading Engine stopped"
        }
    except Exception as e:
        logger.error(f"Failed to stop AI engine: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/engine/trigger")
async def trigger_immediate_analysis():
    """立即触发一次分析（不等待定时器）"""
    try:
        engine = get_ai_trading_engine()
        
        if not engine.is_running():
            raise HTTPException(
                status_code=400,
                detail="AI Trading Engine is not running. Please start it first."
            )
        
        result = await engine.trigger_immediate_analysis()
        
        return {
            "success": True,
            "result": result
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to trigger immediate analysis: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/engine/status")
async def get_engine_status():
    """获取引擎状态"""
    try:
        engine = get_ai_trading_engine()
        config = get_ai_trading_config()
        
        today_trades = get_daily_trades_count()
        today_pnl = get_daily_pnl()
        positions = get_ai_positions()
        
        return {
            "running": engine.is_running(),
            "enabled_in_config": config.get('enabled', False) if config else False,
            "symbols_monitoring": len(config.get('symbols', [])) if config else 0,
            "today_trades": today_trades,
            "today_pnl": today_pnl,
            "current_positions": len(positions),
            "config": _public_config(config),
        }
    except Exception as e:
        logger.error(f"Failed to get engine status: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ============================================
# 配置管理接口
# ============================================

@router.get("/config")
async def get_config():
    """获取 AI 交易配置"""
    try:
        config = get_ai_trading_config()
        
        if not config:
            # 返回默认配置
            return {
                "enabled": False,
                "symbols": [],
                "check_interval_minutes": 5,
                "ai_model": "deepseek-chat",
                "ai_api_key": "",
                "ai_temperature": 0.3,
                "min_confidence": 0.75,
                "max_position_per_stock": 50000,
                "max_daily_trades": 20,
                "max_loss_per_day": 5000,
                "enable_stop_loss": True,
                "default_stop_loss_percent": 5.0,
                "position_sizing_method": "fixed_amount",
                "fixed_amount_per_trade": 10000
            }
        
        # Always return a fixed mask so secret length/prefix/suffix are not exposed.
        return _public_config(config)
        
    except Exception as e:
        logger.error(f"Failed to get config: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/config")
async def update_config(config_update: AiTradingConfigUpdate):
    """更新 AI 交易配置"""
    try:
        # 获取现有配置
        current_config = get_ai_trading_config() or {}
        
        # 更新配置（只更新提供的字段）
        update_data = config_update.model_dump(exclude_unset=True)
        if update_data.get('ai_api_key') == AI_API_KEY_MASK:
            update_data.pop('ai_api_key')
        confirmation = update_data.pop('real_trading_confirmation', None)
        enabling_real_trading = (
            update_data.get('enable_real_trading') is True
            and not current_config.get('enable_real_trading', False)
        )
        if enabling_real_trading and confirmation != "CONFIRM_REAL_TRADING":
            raise HTTPException(
                status_code=400,
                detail="启用真实交易需要明确的二次确认",
            )
        current_config.update(update_data)
        
        # 保存配置
        update_ai_trading_config(current_config)
        
        # 检查是否需要重启引擎
        engine = get_ai_trading_engine()
        need_restart = engine.is_running()
        
        if need_restart:
            logger.info("配置已更新，停止引擎以应用新配置...")
            await engine.stop()
            if current_config.get("enabled", False):
                await engine.start()

        restarted = need_restart and bool(current_config.get("enabled", False))
        
        return {
            "status": "success",
            "message": "Configuration updated",
            "need_restart": need_restart,
            "restarted": restarted
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to update config: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ============================================
# 分析记录接口
# ============================================

@router.get("/analysis")
async def get_analysis(
    limit: int = 50,
    offset: int = 0,
    symbol: Optional[str] = None
):
    """获取 AI 分析记录"""
    try:
        logs = get_ai_analysis_logs(limit=limit, offset=offset, symbol=symbol)
        
        # 计算总数（简化版本，实际应该单独查询）
        total = len(logs)
        
        return {
            "total": total,
            "items": logs
        }
        
    except Exception as e:
        logger.error(f"Failed to get analysis logs: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ============================================
# 交易记录接口
# ============================================

@router.get("/trades")
async def get_trades(
    limit: int = 50,
    offset: int = 0,
    symbol: Optional[str] = None,
    status: Optional[str] = None
):
    """获取 AI 交易记录"""
    try:
        trades = get_ai_trades(
            limit=limit,
            offset=offset,
            symbol=symbol,
            status=status
        )
        
        return {
            "total": len(trades),
            "items": trades
        }
        
    except Exception as e:
        logger.error(f"Failed to get trades: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ============================================
# 持仓管理接口
# ============================================

@router.get("/positions")
async def get_positions_api():
    """获取当前 AI 持仓"""
    try:
        positions = get_ai_positions()
        
        # 计算汇总
        total_value = sum(p.get('market_value', 0) or 0 for p in positions.values())
        total_cost = sum(p.get('avg_cost', 0) * p.get('quantity', 0) for p in positions.values())
        total_pnl = sum(p.get('unrealized_pnl', 0) or 0 for p in positions.values())
        
        total_pnl_percent = 0
        if total_cost > 0:
            total_pnl_percent = (total_pnl / total_cost) * 100
        
        return {
            "total_value": total_value,
            "total_cost": total_cost,
            "total_pnl": total_pnl,
            "total_pnl_percent": total_pnl_percent,
            "positions": list(positions.values())
        }
        
    except Exception as e:
        logger.error(f"Failed to get positions: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/positions/{symbol}")
async def delete_position(symbol: str):
    """删除指定持仓（⚠️ 仅用于清理模拟持仓或错误数据）"""
    try:
        from ..repositories import delete_ai_position
        
        # 检查持仓是否存在
        positions = get_ai_positions()
        if symbol not in positions:
            raise HTTPException(status_code=404, detail=f"持仓不存在: {symbol}")
        
        # 删除持仓
        delete_ai_position(symbol)
        logger.info(f"✅ 手动删除持仓: {symbol}")
        
        return {
            "status": "success",
            "message": f"持仓已删除: {symbol}"
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to delete position: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/positions")
async def clear_all_positions():
    """清空所有 AI 持仓（⚠️ 危险操作，仅用于测试或清理模拟数据）"""
    try:
        from ..db import get_connection
        
        with get_connection() as conn:
            # 获取当前持仓数量
            count = conn.execute("SELECT COUNT(*) FROM ai_positions").fetchone()[0]
            
            if count == 0:
                return {
                    "status": "success",
                    "message": "没有持仓需要清理",
                    "deleted_count": 0
                }
            
            # 清空所有持仓
            conn.execute("DELETE FROM ai_positions")
            logger.warning(f"⚠️  手动清空所有持仓，共 {count} 条")
        
        return {
            "status": "success",
            "message": f"已清空所有持仓",
            "deleted_count": count
        }
        
    except Exception as e:
        logger.error(f"Failed to clear positions: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ============================================
# 统计分析接口
# ============================================

@router.get("/statistics/daily")
async def get_daily_statistics(days: int = 7):
    """获取每日统计"""
    try:
        from datetime import date, timedelta
        
        summary = []
        
        for i in range(days):
            trade_date = (date.today() - timedelta(days=i)).isoformat()
            trades_count = get_daily_trades_count(trade_date)
            pnl = get_daily_pnl(trade_date)
            
            summary.append({
                "trade_date": trade_date,
                "total_trades": trades_count,
                "total_pnl": pnl
            })
        
        return {
            "summary": summary
        }
        
    except Exception as e:
        logger.error(f"Failed to get daily statistics: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/klines/{symbol}")
async def get_ai_klines(
    symbol: str,
    limit: int = 100,
    period: str = "day"  # 新增：支持不同周期
):
    """获取用于AI分析的K线数据（支持多种周期）"""
    try:
        # 获取K线数据
        klines = get_cached_candlesticks(
            symbol=symbol,
            period=period,  # 传递周期参数
            limit=limit
        )
        
        if not klines:
            raise HTTPException(
                status_code=404,
                detail=f"没有找到 {symbol} 的K线数据（{period}），请先同步历史数据"
            )
        
        return {
            "symbol": symbol,
            "count": len(klines),
            "klines": klines
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get klines for {symbol}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/statistics/performance")
async def get_performance(days: int = 30):
    """获取整体绩效指标"""
    try:
        from datetime import date, timedelta
        
        # 计算日期范围
        end_date = date.today()
        start_date = end_date - timedelta(days=days)
        
        # 获取所有交易
        all_trades = get_ai_trades(limit=1000)
        
        # 筛选日期范围内的交易
        period_trades = [
            t for t in all_trades
            if t.get('order_time') and
            start_date.isoformat() <= t['order_time'][:10] <= end_date.isoformat()
        ]
        
        # 统计
        total_trades = len(period_trades)
        filled_trades = [t for t in period_trades if t.get('status') == 'FILLED']
        
        # 盈亏统计
        profitable_trades = [t for t in filled_trades if t.get('pnl', 0) > 0]
        losing_trades = [t for t in filled_trades if t.get('pnl', 0) < 0]
        
        win_count = len(profitable_trades)
        loss_count = len(losing_trades)
        win_rate = (win_count / len(filled_trades) * 100) if filled_trades else 0
        
        total_pnl = sum(t.get('pnl', 0) or 0 for t in filled_trades)
        avg_win = sum(t.get('pnl', 0) for t in profitable_trades) / win_count if win_count > 0 else 0
        avg_loss = sum(t.get('pnl', 0) for t in losing_trades) / loss_count if loss_count > 0 else 0
        
        return {
            "period": {
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "trading_days": days
            },
            "returns": {
                "total_pnl": total_pnl,
                "total_return_percent": 0,  # 需要初始资金才能计算
            },
            "trading": {
                "total_trades": total_trades,
                "win_count": win_count,
                "loss_count": loss_count,
                "win_rate": win_rate,
                "avg_win": avg_win,
                "avg_loss": avg_loss,
                "profit_factor": abs(avg_win / avg_loss) if avg_loss != 0 else 0
            },
            "ai_metrics": {
                "avg_confidence": sum(t.get('ai_confidence', 0) or 0 for t in filled_trades) / len(filled_trades) if filled_trades else 0
            }
        }
        
    except Exception as e:
        logger.error(f"Failed to get performance: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
