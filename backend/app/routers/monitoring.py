"""
API endpoints for position monitoring configuration
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Query
from typing import Dict, Any, Optional

from ..models import (
    PositionMonitoringConfig,
    GlobalMonitoringSettings,
    MonitoringStatus,
    UpdateMonitoringConfigRequest,
    BatchMonitoringUpdateRequest,
    GlobalMonitoringUpdateRequest,
)
from ..repositories import (
    get_position_monitoring_config,
    save_position_monitoring_config,
    get_all_monitoring_configs,
    get_global_monitoring_settings,
    save_global_monitoring_settings,
    get_monitoring_events,
    get_monitoring_events_count
)
from ..position_monitor import get_position_monitor
from ..services import get_portfolio_overview

import logging

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/monitoring", tags=["monitoring"])


def _load_config_map() -> Dict[str, PositionMonitoringConfig]:
    """Return monitoring configs keyed by symbol as Pydantic models."""
    raw_configs = get_all_monitoring_configs()
    configs: Dict[str, PositionMonitoringConfig] = {}
    for item in raw_configs:
        if isinstance(item, PositionMonitoringConfig):
            configs[item.symbol] = item
        elif isinstance(item, dict) and item.get('symbol'):
            try:
                configs[item['symbol']] = PositionMonitoringConfig(**item)
            except Exception:
                logger.warning("Skipping invalid monitoring config for %s", item.get('symbol'))
    return configs


def _load_global_settings_model() -> GlobalMonitoringSettings:
    settings_data = get_global_monitoring_settings()
    if isinstance(settings_data, GlobalMonitoringSettings):
        return settings_data
    return GlobalMonitoringSettings(**settings_data)

def _merge_position_config(
    symbol: str,
    existing: PositionMonitoringConfig | Dict[str, Any] | None,
    update: UpdateMonitoringConfigRequest,
) -> PositionMonitoringConfig:
    """Merge a validated partial update into a validated monitoring config."""
    if isinstance(existing, PositionMonitoringConfig):
        values = existing.model_dump()
    elif isinstance(existing, dict):
        values = PositionMonitoringConfig(**existing).model_dump()
    else:
        values = PositionMonitoringConfig(symbol=symbol).model_dump()

    values.update(update.model_dump(exclude_unset=True))
    values["symbol"] = symbol
    return PositionMonitoringConfig(**values)

@router.get("/positions")
async def get_monitored_positions() -> Dict[str, Any]:
    """Get all positions with their monitoring configuration"""
    try:
        # Get current positions
        portfolio = await asyncio.to_thread(get_portfolio_overview)
        positions = portfolio.get('positions', [])

        # Get monitoring configs and global settings
        configs = await asyncio.to_thread(_load_config_map)
        global_settings = await asyncio.to_thread(_load_global_settings_model)

        # Combine position and monitoring data
        monitored_positions = []
        for position in positions:
            symbol = position['symbol']

            # Get or create config
            if symbol in configs:
                config = configs[symbol]
            else:
                config = PositionMonitoringConfig(
                    symbol=symbol,
                    monitoring_status=(
                        MonitoringStatus.DISABLED
                        if symbol in (global_settings.excluded_symbols or [])
                        else (
                            MonitoringStatus.ENABLED
                            if global_settings.global_enabled
                            else MonitoringStatus.PAUSED
                        )
                    ),
                )

            monitored_positions.append({
                'symbol': symbol,
                'name': position.get('symbol_name', symbol),
                'quantity': position.get('qty', 0),
                'avg_cost': position.get('avg_price', 0),
                'current_price': position.get('last_price', 0),
                'market_value': position.get('market_value', 0),
                'pnl': position.get('pnl', 0),
                # Portfolio service exposes percentage points; monitoring UI uses a ratio.
                'pnl_ratio': float(position.get('pnl_percent', 0) or 0) / 100,
                'monitoring_status': config.monitoring_status,
                'strategy_mode': config.strategy_mode,
                'enabled_strategies': config.enabled_strategies,
                'stop_loss_ratio': config.stop_loss_ratio,
                'take_profit_ratio': config.take_profit_ratio,
                'max_position_ratio': config.max_position_ratio,
                'cooldown_minutes': config.cooldown_minutes,
                'notes': config.notes
            })

        return {
            'positions': monitored_positions,
            'total_positions': len(positions),
            'active_monitoring': len([p for p in monitored_positions
                                    if p['monitoring_status'] == MonitoringStatus.ENABLED]),
            'global_settings': global_settings.model_dump()
        }

    except Exception as e:
        logger.error(f"Error getting monitored positions: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/position/{symbol}")
async def get_position_monitoring(symbol: str) -> PositionMonitoringConfig:
    """Get monitoring configuration for a specific position"""
    try:
        config = await asyncio.to_thread(get_position_monitoring_config, symbol)
        if config:
            return config
        else:
            # Return default config
            return PositionMonitoringConfig(symbol=symbol)

    except Exception as e:
        logger.error(f"Error getting monitoring config for {symbol}: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.put("/position/{symbol}")
async def update_position_monitoring(
    symbol: str,
    update: UpdateMonitoringConfigRequest,
) -> Dict[str, str]:
    """Update monitoring configuration for a position"""
    try:
        existing = await asyncio.to_thread(get_position_monitoring_config, symbol)
        config = _merge_position_config(symbol, existing, update)

        # Save to database
        await asyncio.to_thread(save_position_monitoring_config, config.model_dump())

        # Update in position monitor
        monitor = get_position_monitor()
        await monitor.update_position_config(symbol, config)

        return {"message": f"Monitoring configuration updated for {symbol}"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating monitoring config for {symbol}: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/batch-update")
async def batch_update_monitoring(update: BatchMonitoringUpdateRequest) -> Dict[str, str]:
    """Batch update monitoring settings for multiple positions"""
    try:
        updated = 0
        monitor = get_position_monitor()

        for symbol in update.symbols:
            existing = await asyncio.to_thread(get_position_monitoring_config, symbol)
            config = _merge_position_config(symbol, existing, update.config)
            await asyncio.to_thread(save_position_monitoring_config, config.model_dump())
            await monitor.update_position_config(symbol, config)
            updated += 1

        return {"message": f"Updated {updated} positions"}

    except Exception as e:
        logger.error(f"Error batch updating monitoring: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/global-settings")
async def get_global_settings() -> GlobalMonitoringSettings:
    """Get global monitoring settings"""
    try:
        return await asyncio.to_thread(_load_global_settings_model)

    except Exception as e:
        logger.error(f"Error getting global settings: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.put("/global-settings")
async def update_global_settings(settings: GlobalMonitoringUpdateRequest) -> Dict[str, str]:
    """Update global monitoring settings"""
    try:
        current = await asyncio.to_thread(_load_global_settings_model)
        values = current.model_dump()
        values.update(settings.model_dump(exclude_unset=True))
        merged = GlobalMonitoringSettings(**values)
        await asyncio.to_thread(save_global_monitoring_settings, merged.model_dump())

        # Update in position monitor
        monitor = get_position_monitor()
        monitor.global_settings = merged

        return {"message": "Global settings updated successfully"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating global settings: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/enable-all")
async def enable_all_monitoring() -> Dict[str, str]:
    """Enable monitoring for all positions"""
    try:
        configs = await asyncio.to_thread(_load_config_map)
        portfolio = await asyncio.to_thread(get_portfolio_overview)
        global_settings = await asyncio.to_thread(_load_global_settings_model)
        monitor = get_position_monitor()

        symbols = set(configs)
        symbols.update(
            position["symbol"]
            for position in portfolio.get("positions", [])
            if position.get("symbol")
        )
        symbols.difference_update(global_settings.excluded_symbols or [])
        for symbol in symbols:
            config = configs.get(symbol, PositionMonitoringConfig(symbol=symbol))
            config.monitoring_status = MonitoringStatus.ENABLED
            await asyncio.to_thread(
                save_position_monitoring_config, config.model_dump()
            )
            await monitor.update_position_config(symbol, config)

        return {"message": f"Enabled monitoring for {len(symbols)} positions"}

    except Exception as e:
        logger.error(f"Error enabling all monitoring: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/disable-all")
async def disable_all_monitoring() -> Dict[str, str]:
    """Disable monitoring for all positions"""
    try:
        configs = await asyncio.to_thread(_load_config_map)
        portfolio = await asyncio.to_thread(get_portfolio_overview)
        monitor = get_position_monitor()

        symbols = set(configs)
        symbols.update(
            position["symbol"]
            for position in portfolio.get("positions", [])
            if position.get("symbol")
        )
        for symbol in symbols:
            config = configs.get(symbol, PositionMonitoringConfig(symbol=symbol))
            config.monitoring_status = MonitoringStatus.PAUSED
            await asyncio.to_thread(
                save_position_monitoring_config, config.model_dump()
            )
            await monitor.update_position_config(symbol, config)

        return {"message": f"Disabled monitoring for {len(symbols)} positions"}

    except Exception as e:
        logger.error(f"Error disabling all monitoring: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/status")
async def get_monitoring_status() -> Dict[str, Any]:
    """Get current monitoring system status"""
    try:
        monitor = get_position_monitor()
        status = await monitor.get_monitoring_status()

        return status

    except Exception as e:
        logger.error(f"Error getting monitoring status: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/exclude/{symbol}")
async def exclude_from_monitoring(symbol: str) -> Dict[str, str]:
    """Exclude a position from monitoring permanently"""
    try:
        existing = await asyncio.to_thread(get_position_monitoring_config, symbol)
        if isinstance(existing, PositionMonitoringConfig):
            config = existing
        elif isinstance(existing, dict):
            config = PositionMonitoringConfig(**existing)
        else:
            config = PositionMonitoringConfig(symbol=symbol)
        config.monitoring_status = MonitoringStatus.DISABLED

        await asyncio.to_thread(save_position_monitoring_config, config.model_dump())

        # Also add to global excluded list
        global_settings = await asyncio.to_thread(_load_global_settings_model)
        if symbol not in (global_settings.excluded_symbols or []):
            global_settings.excluded_symbols.append(symbol)
            await asyncio.to_thread(
                save_global_monitoring_settings, global_settings.model_dump()
            )

        # Update in position monitor
        monitor = get_position_monitor()
        await monitor.update_position_config(symbol, config)

        return {"message": f"{symbol} excluded from monitoring"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error excluding {symbol} from monitoring: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/include/{symbol}")
async def include_in_monitoring(symbol: str) -> Dict[str, str]:
    """Include a previously excluded position back to monitoring"""
    try:
        existing = await asyncio.to_thread(get_position_monitoring_config, symbol)
        if isinstance(existing, PositionMonitoringConfig):
            config = existing
        elif isinstance(existing, dict):
            config = PositionMonitoringConfig(**existing)
        else:
            config = PositionMonitoringConfig(symbol=symbol)
        config.monitoring_status = MonitoringStatus.ENABLED

        await asyncio.to_thread(save_position_monitoring_config, config.model_dump())

        # Remove from global excluded list
        global_settings = await asyncio.to_thread(_load_global_settings_model)
        if symbol in (global_settings.excluded_symbols or []):
            global_settings.excluded_symbols.remove(symbol)
            await asyncio.to_thread(
                save_global_monitoring_settings, global_settings.model_dump()
            )

        # Update in position monitor
        monitor = get_position_monitor()
        await monitor.update_position_config(symbol, config)

        return {"message": f"{symbol} included in monitoring"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error including {symbol} in monitoring: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/events")
async def get_events(
    symbol: Optional[str] = None,
    event_type: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
) -> Dict[str, Any]:
    """Get monitoring event history"""
    try:
        offset = (page - 1) * page_size
        events = await asyncio.to_thread(
            get_monitoring_events,
            symbol=symbol,
            event_type=event_type,
            limit=page_size,
            offset=offset,
        )
        total = await asyncio.to_thread(
            get_monitoring_events_count,
            symbol=symbol,
            event_type=event_type,
        )
        
        return {
            "events": events,
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": (total + page_size - 1) // page_size
        }
    
    except Exception as e:
        logger.error(f"Error getting monitoring events: {e}")
        raise HTTPException(status_code=500, detail=str(e))
