"""
智能选股服务 - V2.0 优化版
主要优化：
1. 重新设计评分维度和权重（更科学的配比）
2. 添加趋势强度、支撑阻力分析
3. 添加相对强度(RS)分析  
4. 优化推荐度计算公式
5. 增加多周期分析
"""
from typing import Any, List, Dict, Optional, Tuple
from datetime import datetime, timedelta
import asyncio
import logging
import json
import re
import numpy as np

from .db import get_connection
from .external_service_resilience import (
    record_stock_picker_ai,
    record_stock_picker_cache,
    run_external_call,
)
from .services import get_cached_candlesticks
from .repositories import load_ai_credentials
from .stock_picker_ai_snapshots import (
    AI_INPUT_SNAPSHOT_VERSION,
    AI_OUTPUT_SNAPSHOT_VERSION,
    DATA_DEFINITION_VERSION,
    canonical_json,
    klines_hash,
    parse_snapshot,
    sanitize_error,
    sha256_json,
    snapshot_hash,
    utc_now_iso,
)

logger = logging.getLogger(__name__)


class StockPickerService:
    """智能选股服务"""

    ANALYSIS_LOOKBACK = 250
    AI_TOP_N_PER_POOL = 10
    SCORE_VERSION = "stock-picker-v2.1"
    PROMPT_VERSION = "stock-picker-v2"
    AI_MODEL = "deepseek-chat"
    DEFAULT_CONFIG = {
        'auto_refresh_enabled': False,
        'auto_refresh_interval': 300,
        'max_pool_size': 20,
        'cache_duration': 300,
        'min_score_to_recommend': 65,
        'analysis_lookback': 250,
        'ai_top_n_per_pool': 10,
        'history_retention_days': 90,
        'max_history_per_stock': 30,
        'factor_snapshot_enabled': False,
        'factor_snapshot_poll_interval': 900,
    }
    
    def __init__(self):
        self.cache: Dict[Tuple, Dict] = {}
        self.cache_duration = 300  # 5分钟

    def get_config(self) -> Dict:
        """Load the single persisted stock-picker configuration row."""
        with get_connection() as conn:
            row = conn.execute("""
                SELECT
                    auto_refresh_enabled,
                    auto_refresh_interval,
                    max_pool_size,
                    cache_duration,
                    min_score_to_recommend,
                    analysis_lookback,
                    ai_top_n_per_pool,
                    history_retention_days,
                    max_history_per_stock,
                    factor_snapshot_enabled,
                    factor_snapshot_poll_interval,
                    updated_at
                FROM stock_picker_config
                WHERE id = 1
            """).fetchone()
        if not row:
            return dict(self.DEFAULT_CONFIG)
        keys = list(self.DEFAULT_CONFIG)
        config = {
            key: row[index] if row[index] is not None else self.DEFAULT_CONFIG[key]
            for index, key in enumerate(keys)
        }
        config['updated_at'] = row[len(keys)]
        return config

    def update_config(self, updates: Dict) -> Dict:
        """Persist validated configuration fields."""
        allowed = set(self.DEFAULT_CONFIG)
        unknown = set(updates) - allowed
        if unknown:
            raise ValueError(f"不支持的配置项: {', '.join(sorted(unknown))}")
        if not updates:
            return self.get_config()
        ranges = {
            'auto_refresh_interval': (60, 86400),
            'max_pool_size': (1, 500),
            'cache_duration': (0, 86400),
            'min_score_to_recommend': (0, 100),
            'analysis_lookback': (60, 1000),
            'ai_top_n_per_pool': (0, 100),
            'history_retention_days': (1, 3650),
            'max_history_per_stock': (1, 1000),
            'factor_snapshot_poll_interval': (300, 3600),
        }
        for key, (minimum, maximum) in ranges.items():
            if key in updates and not minimum <= int(updates[key]) <= maximum:
                raise ValueError(f"{key} 必须在 {minimum}～{maximum} 之间")
        if (
            'auto_refresh_enabled' in updates
            and not isinstance(updates['auto_refresh_enabled'], bool)
        ):
            raise ValueError("auto_refresh_enabled 必须是布尔值")
        if (
            'factor_snapshot_enabled' in updates
            and not isinstance(
                updates['factor_snapshot_enabled'],
                bool,
            )
        ):
            raise ValueError("factor_snapshot_enabled 必须是布尔值")

        assignments = ", ".join(f"{key} = ?" for key in updates)
        values = list(updates.values())
        with get_connection() as conn:
            conn.execute(
                f"""
                UPDATE stock_picker_config
                SET {assignments}, updated_at = CURRENT_TIMESTAMP
                WHERE id = 1
                """,
                values,
            )
        self.cache.clear()
        return self.get_config()

    @staticmethod
    def _normalize_symbol(symbol: str) -> str:
        normalized = symbol.strip().upper()
        if not normalized or not re.fullmatch(r"[A-Z0-9][A-Z0-9._-]{0,31}", normalized):
            raise ValueError("股票代码格式无效")
        return normalized

    @staticmethod
    def _validate_pool_type(pool_type: str) -> str:
        normalized = pool_type.upper()
        if normalized not in {'LONG', 'SHORT'}:
            raise ValueError("pool_type 必须是 LONG 或 SHORT")
        return normalized
    
    # ========== 股票池管理 ==========
    
    def add_stock(self, pool_type: str, symbol: str, **kwargs) -> int:
        """添加股票到池"""
        pool_type = self._validate_pool_type(pool_type)
        symbol = self._normalize_symbol(symbol)
        config = self.get_config()
        with get_connection() as conn:
            existing = conn.execute(
                """
                SELECT is_active
                FROM stock_picker_pools
                WHERE pool_type = ? AND symbol = ?
                """,
                (pool_type, symbol),
            ).fetchone()
            if not existing or not existing[0]:
                active_count_row = conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM stock_picker_pools
                    WHERE pool_type = ? AND is_active = TRUE
                    """,
                    (pool_type,),
                ).fetchone()
                active_count = active_count_row[0] if active_count_row else 0
                if active_count >= config['max_pool_size']:
                    raise ValueError(
                        f"{pool_type} 股票池最多启用 {config['max_pool_size']} 只股票"
                    )
            result = conn.execute("""
                INSERT INTO stock_picker_pools 
                (pool_type, symbol, name, added_reason, priority)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (pool_type, symbol) DO UPDATE SET
                is_active = TRUE
                RETURNING id
            """, (
                pool_type,
                symbol,
                kwargs.get('name'),
                kwargs.get('added_reason'),
                kwargs.get('priority', 0)
            ))
            row = result.fetchone()
            return row[0] if row else None
    
    def batch_add_stocks(self, pool_type: str, symbols: List[str]) -> Dict:
        """批量添加股票"""
        pool_type = self._validate_pool_type(pool_type)
        success = []
        failed = []
        
        for symbol in symbols:
            try:
                normalized = self._normalize_symbol(symbol)
                stock_id = self.add_stock(pool_type, normalized)
                if stock_id:
                    success.append(normalized)
                    logger.info(f"✅ 添加成功: {normalized} (ID: {stock_id})")
            except Exception as e:
                failed.append({'symbol': symbol, 'error': str(e)})
                logger.error(f"❌ 添加失败: {symbol} - {e}")
        
        return {
            'success': success,
            'failed': failed,
            'total': len(symbols),
            'success_count': len(success)
        }
    
    def remove_stock(self, pool_id: int):
        """移除股票"""
        with get_connection() as conn:
            conn.execute(
                "DELETE FROM stock_picker_analysis WHERE pool_id = ?",
                (pool_id,),
            )
            conn.execute("DELETE FROM stock_picker_pools WHERE id = ?", (pool_id,))
        self._remove_cache_entries(lambda key: key[0] == pool_id)
    
    def clear_pool(self, pool_type: str) -> int:
        """清空指定类型的股票池（🔥 同时清理历史分析结果）"""
        with get_connection() as conn:
            # 获取要删除的数量
            count_result = conn.execute(
                "SELECT COUNT(*) as cnt FROM stock_picker_pools WHERE pool_type = ?",
                (pool_type,)
            ).fetchone()
            count = count_result[0] if count_result else 0
            
            # 🔥 新增：删除该股票池的所有历史分析结果
            analysis_result = conn.execute(
                "DELETE FROM stock_picker_analysis WHERE pool_type = ?",
                (pool_type,)
            )
            analysis_count = analysis_result.rowcount if hasattr(analysis_result, 'rowcount') else 0
            logger.info(f"🗑️  清理{pool_type}池历史分析结果: {analysis_count}条")
            
            # 删除股票池
            conn.execute("DELETE FROM stock_picker_pools WHERE pool_type = ?", (pool_type,))
            
            # 🧹 清理内存缓存
            cache_keys_to_remove = self._remove_cache_entries(
                lambda key: key[2] == pool_type
            )
            logger.info(f"🧹 清理缓存: {len(cache_keys_to_remove)}条")
            
            logger.info(f"✅ 清空股票池: {pool_type} - {count}只股票")
            return count

    def _remove_cache_entries(self, predicate) -> List[Tuple]:
        keys = [key for key in self.cache if predicate(key)]
        for key in keys:
            del self.cache[key]
        return keys
    
    def toggle_active(self, pool_id: int):
        """切换激活状态"""
        config = self.get_config()
        with get_connection() as conn:
            row = conn.execute(
                "SELECT pool_type, is_active FROM stock_picker_pools WHERE id = ?",
                (pool_id,),
            ).fetchone()
            if not row:
                raise ValueError("股票池记录不存在")
            pool_type, is_active = row
            if not is_active:
                count_row = conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM stock_picker_pools
                    WHERE pool_type = ? AND is_active = TRUE
                    """,
                    (pool_type,),
                ).fetchone()
                active_count = count_row[0] if count_row else 0
                if active_count >= config['max_pool_size']:
                    raise ValueError(
                        f"{pool_type} 股票池最多启用 {config['max_pool_size']} 只股票"
                    )
            conn.execute("""
                UPDATE stock_picker_pools 
                SET is_active = NOT is_active 
                WHERE id = ?
            """, (pool_id,))
    
    def get_pools(
        self,
        pool_type: Optional[str] = None,
        include_inactive: bool = False,
    ) -> Dict:
        """获取股票池"""
        with get_connection() as conn:
            active_filter = "" if include_inactive else " AND is_active = TRUE"
            if pool_type:
                query = (
                    "SELECT * FROM stock_picker_pools WHERE pool_type = ?"
                    f"{active_filter} ORDER BY is_active DESC, priority DESC, added_at"
                )
                results = conn.execute(query, (pool_type,)).fetchall()
            else:
                query = (
                    "SELECT * FROM stock_picker_pools WHERE TRUE"
                    f"{active_filter} ORDER BY pool_type, is_active DESC, priority DESC, added_at"
                )
                results = conn.execute(query).fetchall()
            
            pools = {'long_pool': [], 'short_pool': []}
            for row in results:
                data = {
                    'id': row[0],
                    'pool_type': row[1],
                    'symbol': row[2],
                    'name': row[3],
                    'added_at': str(row[4]),
                    'added_reason': row[5],
                    'is_active': row[6],
                    'priority': row[7]
                }
                
                if data['pool_type'] == 'LONG':
                    pools['long_pool'].append(data)
                else:
                    pools['short_pool'].append(data)
            
            return pools
    
    # ========== 分析功能 ==========
    
    async def analyze_pool(
        self,
        pool_type: Optional[str] = None,
        force_refresh: bool = False,
        progress_callback: Optional[callable] = None,
        job_id: Optional[str] = None,
    ) -> Dict:
        """批量分析股票池"""
        if pool_type:
            pool_type = self._validate_pool_type(pool_type)
        config = self.get_config()
        self.cache_duration = config['cache_duration']
        
        pools = self.get_pools(pool_type)
        all_stocks = []
        
        if not pool_type or pool_type == 'LONG':
            all_stocks.extend([(s, 'LONG') for s in pools['long_pool']])
        if not pool_type or pool_type == 'SHORT':
            all_stocks.extend([(s, 'SHORT') for s in pools['short_pool']])

        universe_snapshot = sorted(
            (
                {
                    "pool_id": stock["id"],
                    "symbol": stock["symbol"],
                    "pool_type": direction,
                }
                for stock, direction in all_stocks
            ),
            key=lambda item: (
                item["pool_type"],
                item["symbol"],
                item["pool_id"],
            ),
        )
        config = dict(config)
        config["_selection_context"] = "pool_ranking"
        config["_universe_snapshot"] = universe_snapshot
        config["_universe_version"] = sha256_json(universe_snapshot)
        
        total_count = len(all_stocks)
        logger.info(f"📊 开始分析 {total_count} 只股票...")
        
        if progress_callback:
            progress_callback({
                'status': 'running',
                'total': total_count,
                'completed': 0,
                'current': None,
                'log': f'开始分析 {total_count} 只股票...'
            })
        
        ai_creds = load_ai_credentials()
        api_key = ai_creds.get('DEEPSEEK_API_KEY')

        # 先用同一个 QuoteContext 批量增量同步，避免每只股票重复建连。
        if all_stocks:
            symbols = list(dict.fromkeys(stock['symbol'] for stock, _ in all_stocks))
            try:
                from .services import sync_history_candlesticks

                if progress_callback:
                    progress_callback({'log': f'📥 增量同步 {len(symbols)} 只股票日 K...'})
                sync_result = await asyncio.to_thread(
                    run_external_call,
                    "quote",
                    "candlestick_sync",
                    sync_history_candlesticks,
                    symbols=symbols,
                    period='day',
                    count=config['analysis_lookback'],
                    incremental=not force_refresh,
                    continue_on_error=True,
                )
                synced_count = sum(1 for count in sync_result.values() if count > 0)
                if progress_callback:
                    progress_callback({
                        'log': f'📥 行情同步完成: {synced_count}/{len(symbols)} 只有更新'
                    })
            except Exception as exc:
                # 同步失败时仍允许使用数据库已有数据完成量化分析。
                logger.warning("⚠️ 批量同步日 K 失败，尝试使用本地数据: %s", exc)
                if progress_callback:
                    progress_callback({'log': f'⚠️ 行情同步失败，使用本地数据: {exc}'})

        prepared_items = []
        results = []
        completed_count = 0

        def report_completed(symbol: str, message: str) -> None:
            nonlocal completed_count
            completed_count += 1
            if progress_callback:
                progress_callback({
                    'completed': completed_count,
                    'log': f'{message}: {symbol} ({completed_count}/{total_count})'
                })

        # 第一阶段只计算量化分，并检查与最新 K 线绑定的结果缓存。
        for stock, ptype in all_stocks:
            symbol = stock['symbol']
            try:
                if progress_callback:
                    progress_callback({
                        'current': symbol,
                        'log': f'📊 量化初筛: {symbol}'
                    })
                prepared = self._prepare_single_stock(
                    stock['id'],
                    symbol,
                    ptype,
                    ai_creds,
                    config,
                    force_refresh,
                    job_id,
                    progress_callback,
                )
                if prepared is None:
                    results.append(None)
                    report_completed(symbol, '⏭️ 跳过')
                elif prepared.get('cached_result') is not None:
                    results.append(prepared['cached_result'])
                    report_completed(symbol, '📋 使用缓存')
                else:
                    prepared_items.append(prepared)
            except Exception as exc:
                logger.error("❌ 量化初筛失败: %s - %s", symbol, exc)
                results.append(exc)
                report_completed(symbol, '❌ 失败')

        # 第二阶段冻结方向内量化排名，并仅让前 N 进入新闻和 AI。
        ai_pool_ids = set()
        for ptype in ('LONG', 'SHORT'):
            ranked = sorted(
                (
                    item
                    for item in prepared_items
                    if item['pool_type'] == ptype
                ),
                key=lambda item: (
                    -item['score']['total'],
                    item['symbol'],
                    item['pool_id'],
                ),
            )
            selected_ids = {
                item['pool_id']
                for item in (
                    ranked[:config['ai_top_n_per_pool']]
                    if api_key
                    else []
                )
            }
            ai_pool_ids.update(selected_ids)
            ranking_snapshot = [
                {
                    "rank": index,
                    "pool_id": item["pool_id"],
                    "symbol": item["symbol"],
                    "score_total": item["score"]["total"],
                }
                for index, item in enumerate(ranked, start=1)
            ]
            selection_version = sha256_json({
                "pool_type": ptype,
                "ai_top_n_per_pool": config["ai_top_n_per_pool"],
                "ranking": ranking_snapshot,
            })
            for index, item in enumerate(ranked, start=1):
                item_config = dict(item.get("config") or config)
                item_config["_selection_version"] = selection_version
                item_config["_selection_snapshot"] = ranking_snapshot
                item_config["_quant_rank"] = index
                item_config["_ai_selected"] = (
                    item["pool_id"] in selected_ids
                )
                item["config"] = item_config
        if api_key:
            if progress_callback:
                progress_callback({
                    'log': (
                        f'🤖 量化初筛完成: {len(prepared_items)} 只待计算，'
                        f'{len(ai_pool_ids)} 只进入 AI 深度分析'
                    )
                })

        semaphore = asyncio.Semaphore(5)

        async def finalize_with_limit(prepared: Dict):
            symbol = prepared['symbol']
            async with semaphore:
                try:
                    if progress_callback:
                        stage = 'AI 深度分析' if prepared['pool_id'] in ai_pool_ids else '量化结论'
                        progress_callback({
                            'current': symbol,
                            'log': f'{"🤖" if prepared["pool_id"] in ai_pool_ids else "📊"} {stage}: {symbol}'
                        })
                    result = await self._finalize_prepared_analysis(
                        prepared,
                        use_ai=prepared['pool_id'] in ai_pool_ids,
                        progress_callback=progress_callback,
                    )
                except Exception:
                    report_completed(symbol, '❌ 失败')
                    raise
                else:
                    report_completed(symbol, '✅ 完成')
                    return result

        tasks = [finalize_with_limit(item) for item in prepared_items]
        if tasks:
            results.extend(await asyncio.gather(*tasks, return_exceptions=True))
        
        # 统计成功、失败、跳过
        success_count = sum(1 for r in results if r is not None and not isinstance(r, Exception))
        skipped_count = sum(1 for r in results if r is None)
        failed_count = sum(1 for r in results if isinstance(r, Exception))
        
        logger.info(f"✅ 分析完成: 成功 {success_count}, 跳过 {skipped_count}, 失败 {failed_count}")
        
        if progress_callback:
            progress_callback({
                'status': 'completed',
                'log': f'✅ 分析完成: 成功 {success_count}, 跳过 {skipped_count}, 失败 {failed_count}'
            })
        
        return {
            'total': len(all_stocks),
            'success': success_count,
            'skipped': skipped_count,
            'failed': failed_count
        }
    
    async def _analyze_single_stock(
        self,
        pool_id: int,
        symbol: str,
        pool_type: str,
        force_refresh: bool = False,
        progress_callback: Optional[callable] = None,
        job_id: Optional[str] = None,
    ) -> Dict:
        """分析单只股票；批量入口会复用同一行情连接。"""
        try:
            logger.info(f"🔍 开始分析: {symbol}")
            pool_type = self._validate_pool_type(pool_type)
            config = dict(self.get_config())
            single_universe = [{
                "pool_id": pool_id,
                "symbol": self._normalize_symbol(symbol),
                "pool_type": pool_type,
            }]
            config["_selection_context"] = "single_stock"
            config["_universe_snapshot"] = single_universe
            config["_universe_version"] = sha256_json(single_universe)
            self.cache_duration = config['cache_duration']

            from .services import sync_history_candlesticks
            try:
                if progress_callback:
                    progress_callback({'log': f'📥 同步K线: {symbol}...'})

                sync_result = await asyncio.to_thread(
                    run_external_call,
                    "quote",
                    "candlestick_sync",
                    sync_history_candlesticks,
                    symbols=[symbol],
                    period='day',
                    count=config['analysis_lookback'],
                    incremental=not force_refresh,
                    continue_on_error=True,
                )
                kline_count = sync_result.get(symbol, 0)
                logger.info(f"📥 同步K线: {symbol} - {kline_count}条")
                if progress_callback:
                    progress_callback({'log': f'📥 同步K线: {symbol} - {kline_count}条'})
            except Exception as e:
                logger.warning(f"⚠️ 同步K线失败: {symbol} - {e}")
                if progress_callback:
                    progress_callback({'log': f'⚠️ 同步K线失败: {symbol} - {e}'})

            ai_creds = load_ai_credentials()
            prepared = self._prepare_single_stock(
                pool_id,
                symbol,
                pool_type,
                ai_creds,
                config,
                force_refresh,
                job_id,
                progress_callback,
            )
            if prepared is None:
                return None
            if prepared.get('cached_result') is not None:
                return prepared['cached_result']
            return await self._finalize_prepared_analysis(
                prepared,
                use_ai=bool(ai_creds.get('DEEPSEEK_API_KEY')),
                progress_callback=progress_callback,
            )
        except Exception as e:
            logger.error(f"❌ 分析失败: {symbol} - {e}")
            raise

    def _prepare_single_stock(
        self,
        pool_id: int,
        symbol: str,
        pool_type: str,
        ai_creds: Dict,
        config: Dict,
        force_refresh: bool,
        job_id: Optional[str] = None,
        progress_callback: Optional[callable] = None,
    ) -> Optional[Dict]:
        """Load local bars, check the versioned cache, and calculate quant factors."""
        from .ai_analyzer import calculate_technical_indicators

        klines = get_cached_candlesticks(
            symbol,
            limit=config['analysis_lookback'],
        )
        if not klines or len(klines) < 30:
            logger.warning(
                "⏭️ 跳过: %s - K线数据不足(%s条)",
                symbol,
                len(klines) if klines else 0,
            )
            if progress_callback:
                progress_callback({
                    'log': f'⏭️ 跳过: {symbol} - K线数据不足({len(klines) if klines else 0}条)'
                })
            return None

        # Without AI the analysis depth is known before ranking, so the final
        # quant-only result can be reused immediately. With AI enabled, defer
        # the lookup until Top N selection decides between "ai" and "quant".
        if not ai_creds.get('DEEPSEEK_API_KEY'):
            cache_key = self._build_cache_key(
                pool_id,
                symbol,
                pool_type,
                klines,
                ai_creds,
                config,
                analysis_mode='quant',
            )
            cached_result = self._get_cached_result(
                cache_key,
                force_refresh,
                config['cache_duration'],
            )
            if cached_result is not None:
                logger.info("📋 使用版本化缓存: %s", symbol)
                return {
                    'pool_id': pool_id,
                    'symbol': symbol,
                    'pool_type': pool_type,
                    'cached_result': cached_result,
                }

        indicators = calculate_technical_indicators(klines)
        score = self._calculate_advanced_score_v2(klines, pool_type)
        indicators.update({
            'trend_strength': score.get('trend_strength', 0.5),
            'momentum_direction': score.get('momentum_direction', 'neutral'),
        })
        return {
            'pool_id': pool_id,
            'symbol': symbol,
            'pool_type': pool_type,
            'klines': klines,
            'indicators': indicators,
            'score': score,
            'ai_creds': ai_creds,
            'config': config,
            'force_refresh': force_refresh,
            'job_id': job_id,
            'cache_checked': not bool(ai_creds.get('DEEPSEEK_API_KEY')),
        }

    def _build_cache_key(
        self,
        pool_id: int,
        symbol: str,
        pool_type: str,
        klines: List[Dict],
        ai_creds: Dict,
        config: Dict,
        analysis_mode: str,
    ) -> Tuple:
        latest = klines[-1]
        latest_marker = latest.get('ts')
        if isinstance(latest_marker, datetime):
            latest_marker = latest_marker.isoformat()
        latest_marker = (
            f"{latest_marker or len(klines)}:{latest.get('open', 0)}:"
            f"{latest.get('high', 0)}:{latest.get('low', 0)}:"
            f"{latest.get('close', 0)}:{latest.get('volume', 0)}"
        )
        return (
            pool_id,
            symbol,
            pool_type,
            str(latest_marker),
            self.SCORE_VERSION,
            self.AI_MODEL if ai_creds.get('DEEPSEEK_API_KEY') else 'quant-only',
            self.PROMPT_VERSION,
            bool(ai_creds.get('TAVILY_API_KEY')),
            analysis_mode,
            config.get('updated_at'),
            config['analysis_lookback'],
            config.get('_universe_version'),
            (
                config.get('_selection_version')
                if ai_creds.get('DEEPSEEK_API_KEY')
                else None
            ),
        )

    def _get_cached_result(
        self,
        cache_key: Tuple,
        force_refresh: bool,
        cache_duration: Optional[int] = None,
    ) -> Optional[Dict]:
        if force_refresh:
            record_stock_picker_cache("bypass")
            return None
        cached = self.cache.get(cache_key)
        if not cached:
            record_stock_picker_cache("miss")
            return None
        duration = cache_duration if cache_duration is not None else self.cache_duration
        if datetime.now() - cached['time'] < timedelta(seconds=duration):
            record_stock_picker_cache("hit")
            return cached['data']
        del self.cache[cache_key]
        record_stock_picker_cache("miss")
        return None

    async def _finalize_prepared_analysis(
        self,
        prepared: Dict,
        use_ai: bool,
        progress_callback: Optional[callable] = None,
    ) -> Dict:
        """Optionally enrich a quant result with AI, then persist and cache it."""
        from .ai_analyzer import DeepSeekAnalyzer

        symbol = prepared['symbol']
        pool_type = prepared['pool_type']
        score = prepared['score']
        indicators = prepared['indicators']
        klines = prepared['klines']
        ai_creds = prepared['ai_creds']
        config = prepared['config']
        api_key = ai_creds.get('DEEPSEEK_API_KEY')
        tavily_api_key = ai_creds.get('TAVILY_API_KEY')
        analysis_mode = 'ai' if use_ai and api_key else 'quant'
        cache_key = self._build_cache_key(
            prepared['pool_id'],
            symbol,
            pool_type,
            klines,
            ai_creds,
            config,
            analysis_mode=analysis_mode,
        )
        if not prepared.get('cache_checked'):
            cached_result = self._get_cached_result(
                cache_key,
                prepared.get('force_refresh', False),
                config['cache_duration'],
            )
            if cached_result is not None:
                logger.info("📋 使用版本化缓存: %s (%s)", symbol, analysis_mode)
                return cached_result

        scenario = "buy_focus" if pool_type == 'LONG' else "sell_focus"
        input_context: Optional[Dict[str, Any]] = None
        output_context: Optional[Dict[str, Any]] = None

        if use_ai and api_key:
            logger.info(
                "🤖 DeepSeek分析: %s (搜索引擎: %s)",
                symbol,
                '✅' if tavily_api_key else '❌',
            )
            if progress_callback:
                progress_callback({'log': f'🤖 DeepSeek分析: {symbol}...'})
            try:
                analyzer = DeepSeekAnalyzer(
                    api_key=api_key,
                    model=self.AI_MODEL,
                    base_url=ai_creds.get(
                        'DEEPSEEK_BASE_URL',
                        'https://api.deepseek.com',
                    ),
                    tavily_api_key=tavily_api_key,
                )
                analysis = await asyncio.to_thread(
                    analyzer.analyze_trading_opportunity,
                    symbol=symbol,
                    klines=klines,
                    scenario=scenario,
                    technical_indicators=indicators,
                    quant_score=score,
                )
            except Exception as exc:
                safe_error = sanitize_error(
                    exc,
                    secrets=(api_key, tavily_api_key),
                )
                analysis = {
                    "error": safe_error,
                }
                input_context = {
                    "request_status": "initialization_failed",
                    "request_reason": "analyzer_initialization_failed",
                    "symbol": symbol,
                    "scenario": scenario,
                    "model": self.AI_MODEL,
                    "temperature": 0.3,
                    "style": None,
                    "system_prompt": None,
                    "user_prompt": None,
                    "news_snapshot": None,
                    "response_format": {"type": "json_object"},
                }
                output_context = {
                    "status": "failed",
                    "raw_response": None,
                    "parsed_response": None,
                    "error_type": type(exc).__name__,
                    "error": safe_error,
                }
            else:
                input_context = analysis.pop("_ai_input_context", None)
                output_context = analysis.pop("_ai_output_context", None)
                analysis.pop("ai_raw_response", None)
                analysis.pop("ai_prompt", None)
            if analysis.get('error'):
                record_stock_picker_ai(degraded=True)
                ai_error = sanitize_error(
                    analysis['error'],
                    secrets=(api_key, tavily_api_key),
                )
                if input_context is None:
                    input_context = {
                        "request_status": "failed",
                        "request_reason": "ai_request_failed",
                        "symbol": symbol,
                        "scenario": scenario,
                        "model": self.AI_MODEL,
                        "temperature": None,
                        "style": None,
                        "system_prompt": None,
                        "user_prompt": None,
                        "news_snapshot": None,
                        "response_format": {"type": "json_object"},
                    }
                if output_context is None:
                    output_context = {
                        "status": "failed",
                        "raw_response": None,
                        "parsed_response": None,
                        "error_type": "AIRequestError",
                        "error": ai_error,
                    }
                logger.warning(
                    "⚠️ AI分析失败，回退到量化结论: %s - %s",
                    symbol,
                    ai_error,
                )
                analysis = self._build_quant_analysis(
                    score,
                    indicators,
                    pool_type,
                    ai_status='fallback',
                    ai_error=ai_error,
                )
            else:
                record_stock_picker_ai(degraded=False)
                if input_context is None:
                    input_context = {
                        "request_status": "completed",
                        "request_reason": None,
                        "symbol": symbol,
                        "scenario": scenario,
                        "model": self.AI_MODEL,
                        "temperature": None,
                        "style": None,
                        "system_prompt": None,
                        "user_prompt": None,
                        "news_snapshot": None,
                        "response_format": {"type": "json_object"},
                    }
                if output_context is None:
                    output_context = {
                        "status": "completed",
                        "raw_response": None,
                        "parsed_response": {
                            key: value
                            for key, value in analysis.items()
                            if key not in {"indicators", "score"}
                        },
                        "error_type": None,
                        "error": None,
                    }
        else:
            ai_status = 'disabled' if not api_key else 'skipped'
            if ai_status == 'disabled':
                logger.warning("⚠️ 未配置DeepSeek API，使用V2量化评分: %s", symbol)
            request_reason = (
                "deepseek_not_configured"
                if ai_status == "disabled"
                else "outside_ai_top_n"
            )
            input_context = {
                "request_status": "not_requested",
                "request_reason": request_reason,
                "symbol": symbol,
                "scenario": scenario,
                "model": self.AI_MODEL if api_key else None,
                "temperature": None,
                "style": None,
                "system_prompt": None,
                "user_prompt": None,
                "news_snapshot": None,
                "response_format": None,
            }
            output_context = {
                "status": "not_requested",
                "raw_response": None,
                "parsed_response": None,
                "error_type": None,
                "error": None,
            }
            analysis = self._build_quant_analysis(
                score,
                indicators,
                pool_type,
                ai_status=ai_status,
            )

        recommendation_score = self._calculate_recommendation_score_v2(
            score,
            analysis,
            pool_type,
        )
        recommendation_reason = self._generate_recommendation_reason(
            analysis,
            pool_type,
            recommendation_score,
            min_score_to_recommend=config['min_score_to_recommend'],
        )
        analysis['analysis_mode'] = analysis_mode
        ai_model = self.AI_MODEL if api_key else None
        (
            ai_input_snapshot,
            ai_input_hash,
            ai_output_snapshot,
        ) = self._build_ai_snapshot_payloads(
            symbol=symbol,
            pool_type=pool_type,
            klines=klines,
            indicators=indicators,
            score=score,
            config=config,
            analysis_mode=analysis_mode,
            api_key=api_key,
            tavily_api_key=tavily_api_key,
            score_version=self.SCORE_VERSION,
            prompt_version=self.PROMPT_VERSION,
            ai_model=ai_model,
            input_context=input_context,
            output_context=output_context,
        )
        result = self._save_analysis_result(
            pool_id=prepared['pool_id'],
            symbol=symbol,
            pool_type=pool_type,
            klines=klines,
            analysis=analysis,
            recommendation_score=recommendation_score,
            recommendation_reason=recommendation_reason,
            score_version=self.SCORE_VERSION,
            prompt_version=self.PROMPT_VERSION,
            ai_model=ai_model,
            analysis_mode=analysis_mode,
            job_id=prepared.get('job_id'),
            ai_input_snapshot=ai_input_snapshot,
            ai_input_hash=ai_input_hash,
            ai_output_snapshot=ai_output_snapshot,
            history_retention_days=config['history_retention_days'],
            max_history_per_stock=config['max_history_per_stock'],
        )
        self.cache[cache_key] = {
            'time': datetime.now(),
            'data': result,
        }
        logger.info(
            "✅ 分析完成: %s - 评分: %.1f, 推荐度: %.1f",
            symbol,
            score.get('total', 0),
            recommendation_score,
        )
        return result
    
    def _generate_recommendation_reason(
        self,
        analysis: Dict,
        pool_type: str,
        recommendation_score: float,
        min_score_to_recommend: int = 65,
    ) -> str:
        """生成推荐理由"""
        
        grade = analysis.get('score', {}).get('grade', 'C')
        confidence = analysis.get('confidence', 0.5)
        action = analysis.get('action', 'HOLD')
        ai_available = analysis.get('ai_status', 'available') == 'available'
        strong_threshold = min(100, min_score_to_recommend + 15)
        consider_threshold = max(0, min_score_to_recommend - 15)
        
        if pool_type == 'LONG':
            if recommendation_score >= strong_threshold:
                source = f"AI建议{action}" if ai_available else f"量化建议{action}"
                return f"强烈推荐买入：{grade}级评分 + 信心度{confidence:.0%} + {source}"
            elif recommendation_score >= min_score_to_recommend:
                return f"推荐买入：{grade}级评分 + 信心度{confidence:.0%}"
            elif recommendation_score >= consider_threshold:
                return f"可考虑买入：技术面尚可"
            else:
                return f"谨慎观望：评分较低或信号不足"
        else:  # SHORT
            if recommendation_score >= strong_threshold:
                return f"强烈推荐做空：弱势形态 + 信心度{confidence:.0%}"
            elif recommendation_score >= min_score_to_recommend:
                return f"推荐做空：技术面偏弱"
            elif recommendation_score >= consider_threshold:
                return f"可考虑做空：有下跌迹象"
            else:
                return f"谨慎观望：做空信号不足"
    
    # ========== V2.0 核心方法 ==========
    
    def _calculate_advanced_score_v2(
        self,
        klines: List[Dict],
        pool_type: str = "LONG"
    ) -> Dict:
        """
        V2.0 高级量化评分系统
        
        评分维度（100分制，科学配比）：
        1. 趋势评分（25分）- 多周期趋势一致性、趋势强度
        2. 动量评分（20分）- RSI、MACD、价格动量
        3. 支撑阻力（15分）- 关键价位分析
        4. 量价配合（15分）- 量能验证
        5. 形态评分（15分）- K线形态、图表形态
        6. 波动机会（10分）- 适度波动有利于交易
        """
        if not klines or len(klines) < 30:
            return self._empty_score_v2(pool_type)
        
        # 基础数据准备
        closes = np.array([k['close'] for k in klines])
        highs = np.array([k['high'] for k in klines])
        lows = np.array([k['low'] for k in klines])
        volumes = np.array([k.get('volume', 0) for k in klines])
        
        current_price = closes[-1]
        scores = {}
        signals = []
        
        # 1. 趋势评分（25分）
        trend_result = self._calc_trend_score_v2(
            closes, highs, lows, current_price, pool_type
        )
        scores['trend'] = trend_result['score']
        signals.extend(trend_result['signals'])
        trend_strength = trend_result['strength']
        
        # 2. 动量评分（20分）
        momentum_result = self._calc_momentum_score_v2(closes, volumes, pool_type)
        scores['momentum'] = momentum_result['score']
        signals.extend(momentum_result['signals'])
        momentum_direction = momentum_result['direction']
        
        # 3. 支撑阻力评分（15分）
        sr_result = self._calc_support_resistance_v2(
            closes, highs, lows, current_price, pool_type
        )
        scores['support_resistance'] = sr_result['score']
        signals.extend(sr_result['signals'])
        support_resistance = sr_result['levels']
        
        # 4. 量价配合评分（15分）
        volume_result = self._calc_volume_price_v2(closes, volumes, pool_type)
        scores['volume'] = volume_result['score']
        signals.extend(volume_result['signals'])
        
        # 5. 形态评分（15分）
        pattern_result = self._calc_pattern_score_v2(klines[-20:], pool_type)
        scores['pattern'] = pattern_result['score']
        signals.extend(pattern_result['signals'])
        
        # 6. 波动机会评分（10分）
        volatility_result = self._calc_volatility_v2(closes, highs, lows)
        scores['volatility'] = volatility_result['score']
        signals.extend(volatility_result['signals'])
        
        # 每个分项都是当前方向的机会分，因此无论做多还是做空都直接求和。
        total_score = sum(scores.values())
        
        # 评级
        grade = self._get_grade_v2(total_score)
        
        return {
            "total": round(total_score, 1),
            "breakdown": scores,
            "signals": signals,
            "grade": grade,
            "trend_strength": trend_strength,
            "support_resistance": support_resistance,
            "momentum_direction": momentum_direction,
            "opportunity_direction": pool_type,
            "current_price": current_price
        }
    
    def _calc_trend_score_v2(
        self,
        closes,
        highs,
        lows,
        current_price,
        pool_type: str = "LONG"
    ) -> Dict:
        """计算当前交易方向的趋势机会分（25分）。"""
        score = 0
        signals = []
        is_short = pool_type == "SHORT"
        
        # 计算多周期均线
        ma5 = np.mean(closes[-5:]) if len(closes) >= 5 else current_price
        ma10 = np.mean(closes[-10:]) if len(closes) >= 10 else current_price
        ma20 = np.mean(closes[-20:]) if len(closes) >= 20 else current_price
        ma60 = np.mean(closes[-60:]) if len(closes) >= 60 else None
        
        # 1. MA排列评分（10分）
        if is_short:
            if ma5 < ma10 < ma20:
                score += 8
                signals.append("📉 完美空头排列(MA5<MA10<MA20)")
                if ma60 and ma20 < ma60:
                    score += 2
                    signals.append("📉 长期空头确认(MA20<MA60)")
            elif ma5 < ma10:
                score += 5
                signals.append("📉 短期空头(MA5<MA10)")
            elif ma5 > ma10 > ma20:
                score += 2
                signals.append("⚠️ 多头排列，不利做空")
            else:
                score += 4
                signals.append("➡️ 均线纠缠")
        else:
            if ma5 > ma10 > ma20:
                score += 8
                signals.append("📈 完美多头排列(MA5>MA10>MA20)")
                if ma60 and ma20 > ma60:
                    score += 2
                    signals.append("📈 长期多头确认(MA20>MA60)")
            elif ma5 > ma10:
                score += 5
                signals.append("📈 短期多头(MA5>MA10)")
            elif ma5 < ma10 < ma20:
                score += 2
                signals.append("⚠️ 空头排列，不利做多")
            else:
                score += 4
                signals.append("➡️ 均线纠缠")
        
        # 2. 趋势强度ADX（8分）
        adx = self._calc_adx_v2(highs, lows, closes)
        if adx > 40:
            score += 8
            signals.append(f"💪 强趋势(ADX={adx:.1f})")
        elif adx > 25:
            score += 6
            signals.append(f"📊 中等趋势(ADX={adx:.1f})")
        elif adx > 15:
            score += 3
            signals.append(f"➡️ 弱趋势(ADX={adx:.1f})")
        else:
            score += 1
            signals.append(f"⚠️ 无趋势(ADX={adx:.1f})")
        
        # 3. 价格位置（7分）
        price_vs_ma20 = (current_price - ma20) / ma20 * 100 if ma20 > 0 else 0
        if is_short:
            if price_vs_ma20 < -5:
                score += 7
                signals.append(f"💪 价格弱势({price_vs_ma20:+.1f}% vs MA20)")
            elif price_vs_ma20 < 0:
                score += 5
                signals.append("📉 价格在MA20下方")
            elif price_vs_ma20 < 3:
                score += 3
                signals.append("➡️ 价格接近MA20")
            else:
                score += 1
                signals.append(f"⚠️ 价格偏强(+{price_vs_ma20:.1f}% vs MA20)")
        else:
            if price_vs_ma20 > 5:
                score += 7
                signals.append(f"💪 价格强势(+{price_vs_ma20:.1f}% vs MA20)")
            elif price_vs_ma20 > 0:
                score += 5
                signals.append("📈 价格在MA20上方")
            elif price_vs_ma20 > -3:
                score += 3
                signals.append("➡️ 价格接近MA20")
            else:
                score += 1
                signals.append(f"📉 价格弱势({price_vs_ma20:+.1f}%)")
        
        if ma5 > ma10 and current_price > ma20:
            trend_direction = "bullish"
        elif ma5 < ma10 and current_price < ma20:
            trend_direction = "bearish"
        else:
            trend_direction = "neutral"

        expected_direction = "bearish" if is_short else "bullish"
        alignment = 1.0 if trend_direction == expected_direction else 0.5 if trend_direction == "neutral" else 0.25
        favorable_distance = -price_vs_ma20 if is_short else price_vs_ma20
        distance_factor = 1 + min(20, max(-10, favorable_distance)) / 20
        trend_strength = min(1.0, max(0.0, (adx / 50) * alignment * distance_factor))
        
        return {
            "score": score,
            "signals": signals,
            "strength": round(trend_strength, 2),
            "direction": trend_direction,
        }
    
    def _calc_adx_v2(self, highs, lows, closes, period=14) -> float:
        """计算ADX"""
        if len(closes) < period + 1:
            return 20.0
        try:
            high_diff = np.diff(highs)
            low_diff = -np.diff(lows)
            plus_dm = np.where((high_diff > low_diff) & (high_diff > 0), high_diff, 0)
            minus_dm = np.where((low_diff > high_diff) & (low_diff > 0), low_diff, 0)
            
            tr1 = highs[1:] - lows[1:]
            tr2 = np.abs(highs[1:] - closes[:-1])
            tr3 = np.abs(lows[1:] - closes[:-1])
            tr = np.maximum(np.maximum(tr1, tr2), tr3)
            
            atr = self._ema_v2(tr, period)
            plus_di = 100 * self._ema_v2(plus_dm, period) / (atr + 1e-10)
            minus_di = 100 * self._ema_v2(minus_dm, period) / (atr + 1e-10)
            dx = 100 * np.abs(plus_di - minus_di) / (plus_di + minus_di + 1e-10)
            adx = self._ema_v2(dx, period)
            return float(adx[-1]) if len(adx) > 0 else 20.0
        except:
            return 20.0
    
    def _calc_momentum_score_v2(
        self,
        closes,
        volumes,
        pool_type: str = "LONG"
    ) -> Dict:
        """计算当前交易方向的动量机会分（20分）。"""
        score = 0
        signals = []
        is_short = pool_type == "SHORT"
        
        # 1. RSI评分（8分）
        rsi = self._calc_rsi_v2(closes)
        if is_short:
            if 40 <= rsi <= 60:
                score += 8
                signals.append(f"✅ RSI做空区间({rsi:.1f})")
            elif 60 < rsi <= 70:
                score += 7
                signals.append(f"🟠 RSI高位回落区({rsi:.1f})")
            elif rsi > 70:
                score += 6
                signals.append(f"⚠️ RSI超买，注意逼空风险({rsi:.1f})")
            elif 30 <= rsi < 40:
                score += 5
                signals.append(f"📉 RSI偏弱({rsi:.1f})")
            elif 20 <= rsi < 30:
                score += 3
                signals.append(f"⚠️ RSI接近超卖({rsi:.1f})")
            else:
                score += 2
                signals.append(f"🔴 RSI深度超卖，不宜追空({rsi:.1f})")
        else:
            if 40 <= rsi <= 60:
                score += 8
                signals.append(f"✅ RSI健康({rsi:.1f})")
            elif 30 <= rsi < 40:
                score += 7
                signals.append(f"🟢 RSI超卖反弹区({rsi:.1f})")
            elif 60 < rsi <= 70:
                score += 5
                signals.append(f"⚠️ RSI偏高({rsi:.1f})")
            elif 20 <= rsi < 30:
                score += 6
                signals.append(f"🟢 RSI深度超卖({rsi:.1f})")
            elif rsi > 70:
                score += 3
                signals.append(f"🔴 RSI超买({rsi:.1f})")
            else:
                score += 2
        
        # 2. MACD评分（8分）
        macd, signal, hist = self._calc_macd_v2(closes)
        if is_short:
            if macd < signal and hist < 0:
                score += 8
                signals.append("📉 MACD死叉")
            elif macd > signal and hist > 0:
                score += 2
                signals.append("⚠️ MACD金叉，不利做空")
            elif macd < signal:
                score += 5
                signals.append("➡️ MACD收敛向下")
            else:
                score += 4
        else:
            if macd > signal and hist > 0:
                score += 8
                signals.append("📈 MACD金叉")
            elif macd < signal and hist < 0:
                score += 2
                signals.append("📉 MACD死叉")
            elif macd > signal:
                score += 5
                signals.append("➡️ MACD收敛向上")
            else:
                score += 4
        
        # 3. 价格动量（4分）
        momentum = 0.0
        if len(closes) >= 6:
            momentum = (closes[-1] / closes[-6] - 1) * 100
            if is_short:
                if momentum < -5:
                    score += 4
                    signals.append(f"📉 5日下跌动量强劲({momentum:+.1f}%)")
                elif momentum < -2:
                    score += 3
                    signals.append(f"📉 5日动量向下({momentum:+.1f}%)")
                elif momentum < 2:
                    score += 2
                else:
                    score += 1
                    signals.append(f"⚠️ 5日动量上涨(+{momentum:.1f}%)")
            else:
                if momentum > 5:
                    score += 4
                    signals.append(f"🚀 5日动量强劲(+{momentum:.1f}%)")
                elif momentum > 2:
                    score += 3
                    signals.append(f"📈 5日动量向上(+{momentum:.1f}%)")
                elif momentum > -2:
                    score += 2
                else:
                    score += 1
                    signals.append(f"📉 5日动量下跌({momentum:+.1f}%)")

        bullish_votes = int(macd > signal and hist > 0) + int(momentum > 1)
        bearish_votes = int(macd < signal and hist < 0) + int(momentum < -1)
        if bullish_votes > bearish_votes:
            direction = "bullish"
        elif bearish_votes > bullish_votes:
            direction = "bearish"
        else:
            direction = "neutral"
        
        return {"score": score, "signals": signals, "direction": direction}
    
    def _calc_rsi_v2(self, closes, period=14) -> float:
        """计算RSI"""
        if len(closes) < period + 1:
            return 50.0
        deltas = np.diff(closes)
        gains = np.where(deltas > 0, deltas, 0)
        losses = np.where(deltas < 0, -deltas, 0)
        avg_gain = np.mean(gains[-period:])
        avg_loss = np.mean(losses[-period:])
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return float(100 - (100 / (1 + rs)))
    
    def _calc_macd_v2(self, closes, fast=12, slow=26, signal=9):
        """计算MACD"""
        if len(closes) < slow:
            return 0.0, 0.0, 0.0
        ema_fast = self._ema_v2(closes, fast)
        ema_slow = self._ema_v2(closes, slow)
        macd_line = ema_fast - ema_slow
        signal_line = self._ema_v2(macd_line, signal)
        histogram = macd_line - signal_line
        return float(macd_line[-1]), float(signal_line[-1]), float(histogram[-1])
    
    def _calc_support_resistance_v2(
        self,
        closes,
        highs,
        lows,
        current_price,
        pool_type: str = "LONG"
    ) -> Dict:
        """计算当前交易方向的支撑阻力机会分（15分）。"""
        score = 0
        signals = []
        is_short = pool_type == "SHORT"
        
        # 查找支撑阻力位
        levels = self._find_sr_levels_v2(highs, lows, closes)
        support = levels['support']
        resistance = levels['resistance']
        
        if is_short:
            # 做空更关注上方阻力保护和下方支撑空间。
            if resistance > current_price:
                dist = (resistance - current_price) / current_price * 100
                if dist <= 3:
                    score += 8
                    signals.append(f"🔴 接近强阻力(距离{dist:.1f}%)")
                elif dist <= 5:
                    score += 6
                    signals.append("🔴 阻力位压制")
                elif dist <= 10:
                    score += 4
                else:
                    score += 2
            else:
                score += 3

            if 0 < support < current_price:
                space = (current_price - support) / current_price * 100
                if space > 15:
                    score += 7
                    signals.append(f"📉 下跌空间大(-{space:.1f}%)")
                elif space > 8:
                    score += 5
                    signals.append("📉 下跌空间适中")
                elif space > 3:
                    score += 3
                else:
                    score += 1
                    signals.append("⚠️ 接近支撑位")
            else:
                score += 4
        else:
            # 做多关注下方支撑保护和上方阻力空间。
            if support > 0:
                dist = (current_price - support) / current_price * 100
                if 0 < dist <= 3:
                    score += 8
                    signals.append(f"🟢 接近强支撑(距离{dist:.1f}%)")
                elif dist <= 5:
                    score += 6
                    signals.append("🟢 支撑位保护")
                elif dist <= 10:
                    score += 4
                else:
                    score += 2
            else:
                score += 3

            if resistance > current_price:
                space = (resistance - current_price) / current_price * 100
                if space > 15:
                    score += 7
                    signals.append(f"🚀 上涨空间大(+{space:.1f}%)")
                elif space > 8:
                    score += 5
                    signals.append("📈 上涨空间适中")
                elif space > 3:
                    score += 3
                else:
                    score += 1
                    signals.append("⚠️ 接近阻力位")
            else:
                score += 4
        
        return {"score": score, "signals": signals, "levels": levels}
    
    def _find_sr_levels_v2(self, highs, lows, closes, lookback=60) -> Dict:
        """查找支撑阻力位"""
        if len(closes) < lookback:
            lookback = len(closes)
        
        recent_highs = highs[-lookback:]
        recent_lows = lows[-lookback:]
        current = closes[-1]
        
        high_peaks = []
        low_troughs = []
        
        for i in range(2, len(recent_highs) - 2):
            if (recent_highs[i] > recent_highs[i-1] and recent_highs[i] > recent_highs[i-2] and
                recent_highs[i] > recent_highs[i+1] and recent_highs[i] > recent_highs[i+2]):
                high_peaks.append(recent_highs[i])
            if (recent_lows[i] < recent_lows[i-1] and recent_lows[i] < recent_lows[i-2] and
                recent_lows[i] < recent_lows[i+1] and recent_lows[i] < recent_lows[i+2]):
                low_troughs.append(recent_lows[i])
        
        support = max([t for t in low_troughs if t < current], default=0)
        resistance = min([p for p in high_peaks if p > current], default=0)
        
        return {"support": support, "resistance": resistance}
    
    def _calc_volume_price_v2(
        self,
        closes,
        volumes,
        pool_type: str = "LONG"
    ) -> Dict:
        """计算当前交易方向的量价配合分（15分）。"""
        score = 0
        signals = []
        is_short = pool_type == "SHORT"
        
        if len(volumes) < 10 or np.sum(volumes) == 0:
            return {"score": 7, "signals": ["❓ 成交量数据不足"]}
        
        vol_ma5 = np.mean(volumes[-5:])
        vol_ma20 = np.mean(volumes[-20:]) if len(volumes) >= 20 else vol_ma5
        vol_ratio = vol_ma5 / vol_ma20 if vol_ma20 > 0 else 1.0
        
        # 量能趋势（8分）
        if vol_ratio > 1.5:
            score += 8
            signals.append(f"🔥 成交量放大({vol_ratio:.2f}x)")
        elif vol_ratio > 1.2:
            score += 6
            signals.append(f"📈 成交量温和放大")
        elif vol_ratio > 0.8:
            score += 4
            signals.append(f"➡️ 成交量平稳")
        else:
            score += 2
            signals.append(f"📉 成交量萎缩")
        
        # 量价关系（7分）
        price_chg = (closes[-1] / closes[-6] - 1) * 100 if len(closes) >= 6 else 0
        if is_short:
            if price_chg < 0 and vol_ratio > 1.2:
                score += 7
                signals.append("✅ 量价齐跌")
            elif price_chg < 0 and vol_ratio < 0.8:
                score += 4
                signals.append("⚠️ 价跌量缩")
            elif price_chg > 0 and vol_ratio > 1.2:
                score += 2
                signals.append("⚠️ 放量上涨")
            elif price_chg > 0 and vol_ratio < 0.8:
                score += 5
                signals.append("➡️ 缩量反弹")
            else:
                score += 4
        else:
            if price_chg > 0 and vol_ratio > 1.2:
                score += 7
                signals.append("✅ 量价齐升")
            elif price_chg > 0 and vol_ratio < 0.8:
                score += 4
                signals.append("⚠️ 价升量缩")
            elif price_chg < 0 and vol_ratio > 1.2:
                score += 2
                signals.append("⚠️ 放量下跌")
            elif price_chg < 0 and vol_ratio < 0.8:
                score += 5
                signals.append("➡️ 缩量回调")
            else:
                score += 4
        
        return {"score": score, "signals": signals}
    
    def _calc_pattern_score_v2(
        self,
        klines: List[Dict],
        pool_type: str = "LONG"
    ) -> Dict:
        """计算当前交易方向的 K 线形态分（15分）。"""
        score = 0
        signals = []
        is_short = pool_type == "SHORT"
        
        if len(klines) < 3:
            return {"score": 7, "signals": ["❓ K线数据不足"]}
        
        k1, k2, k3 = klines[-3], klines[-2], klines[-1]
        
        def is_bullish(k):
            return k.get('close', 0) >= k.get('open', 0)
        
        def body_size(k):
            return abs(k.get('close', 0) - k.get('open', 0))
        
        def full_range(k):
            return k.get('high', 0) - k.get('low', 0)
        
        def lower_shadow(k):
            return min(k.get('close', 0), k.get('open', 0)) - k.get('low', 0)
        
        def upper_shadow(k):
            return k.get('high', 0) - max(k.get('close', 0), k.get('open', 0))
        
        last_range = full_range(k3)
        
        # 单K线形态（5分）
        if last_range > 0:
            if is_short:
                if upper_shadow(k3) / last_range > 0.6 and body_size(k3) / last_range < 0.3:
                    score += 5
                    signals.append("🌠 射击之星(看跌反转)")
                elif lower_shadow(k3) / last_range > 0.6 and body_size(k3) / last_range < 0.3:
                    score += 3
                    signals.append("⚠️ 锤子线，不利追空")
                elif not is_bullish(k3) and body_size(k3) / last_range > 0.7:
                    score += 4
                    signals.append("📉 大阴线")
                elif is_bullish(k3) and body_size(k3) / last_range > 0.7:
                    score += 1
                    signals.append("📈 大阳线")
                elif body_size(k3) / last_range < 0.1:
                    score += 2
                    signals.append("✖️ 十字星")
                else:
                    score += 2
            else:
                if lower_shadow(k3) / last_range > 0.6 and body_size(k3) / last_range < 0.3:
                    score += 5
                    signals.append("🔨 锤子线(看涨反转)")
                elif upper_shadow(k3) / last_range > 0.6 and body_size(k3) / last_range < 0.3:
                    score += 3
                    signals.append("🔨 倒锤子")
                elif is_bullish(k3) and body_size(k3) / last_range > 0.7:
                    score += 4
                    signals.append("📈 大阳线")
                elif not is_bullish(k3) and body_size(k3) / last_range > 0.7:
                    score += 1
                    signals.append("📉 大阴线")
                elif body_size(k3) / last_range < 0.1:
                    score += 2
                    signals.append("✖️ 十字星")
                else:
                    score += 2
        else:
            score += 2
        
        # 组合形态（10分）
        if is_short:
            if not is_bullish(k1) and not is_bullish(k2) and not is_bullish(k3):
                if k3['close'] < k2['close'] < k1['close']:
                    score += 10
                    signals.append("📉 黑三兵(强势看跌)")
                else:
                    score += 5
            elif is_bullish(k1) and body_size(k2) < body_size(k1) * 0.3 and not is_bullish(k3):
                score += 8
                signals.append("🌙 黄昏之星(顶部反转)")
            elif not is_bullish(k1) and is_bullish(k2) and not is_bullish(k3):
                if k3['close'] < k1['close']:
                    score += 7
                    signals.append("💥 空方炮")
            elif is_bullish(k1) and is_bullish(k2) and is_bullish(k3):
                score += 1
                signals.append("⚠️ 红三兵，不利做空")
            elif not is_bullish(k1) and body_size(k2) < body_size(k1) * 0.3 and is_bullish(k3):
                score += 2
                signals.append("⚠️ 早晨之星")
            else:
                score += 5
        else:
            if is_bullish(k1) and is_bullish(k2) and is_bullish(k3):
                if k3['close'] > k2['close'] > k1['close']:
                    score += 10
                    signals.append("🚀 红三兵(强势看涨)")
                else:
                    score += 5
            elif not is_bullish(k1) and body_size(k2) < body_size(k1) * 0.3 and is_bullish(k3):
                score += 8
                signals.append("⭐ 早晨之星(底部反转)")
            elif is_bullish(k1) and not is_bullish(k2) and is_bullish(k3):
                if k3['close'] > k1['close']:
                    score += 7
                    signals.append("💥 多方炮")
            elif not is_bullish(k1) and not is_bullish(k2) and not is_bullish(k3):
                score += 1
                signals.append("⚠️ 黑三兵")
            elif is_bullish(k1) and body_size(k2) < body_size(k1) * 0.3 and not is_bullish(k3):
                score += 2
                signals.append("🌙 黄昏之星")
            else:
                score += 5
        
        return {"score": score, "signals": signals}
    
    def _calc_volatility_v2(self, closes, highs, lows) -> Dict:
        """波动机会评分（10分）"""
        score = 0
        signals = []
        
        if len(closes) < 20:
            return {"score": 5, "signals": []}
        
        # 历史波动率
        returns = np.diff(closes) / closes[:-1]
        volatility = np.std(returns[-20:]) * np.sqrt(252) * 100
        
        if 25 <= volatility <= 45:
            score += 7
            signals.append(f"✅ 波动适中({volatility:.1f}%年化)")
        elif 15 <= volatility < 25:
            score += 5
            signals.append(f"➡️ 波动偏低({volatility:.1f}%)")
        elif 45 < volatility <= 60:
            score += 5
            signals.append(f"⚠️ 波动偏高({volatility:.1f}%)")
        elif volatility > 60:
            score += 3
            signals.append(f"🔴 高波动风险({volatility:.1f}%)")
        else:
            score += 2
        
        # ATR评分
        tr = np.maximum(
            highs[-20:] - lows[-20:],
            np.maximum(
                np.abs(highs[-20:] - closes[-21:-1]),
                np.abs(lows[-20:] - closes[-21:-1])
            )
        )
        atr_pct = np.mean(tr) / closes[-1] * 100
        if 2 <= atr_pct <= 5:
            score += 3
        elif atr_pct > 5:
            score += 2
        else:
            score += 1
        
        return {"score": score, "signals": signals}
    
    def _ema_v2(self, data, period) -> np.ndarray:
        """计算EMA"""
        alpha = 2 / (period + 1)
        ema = np.zeros_like(data, dtype=float)
        ema[0] = data[0]
        for i in range(1, len(data)):
            ema[i] = alpha * data[i] + (1 - alpha) * ema[i-1]
        return ema
    
    def _empty_score_v2(self, pool_type: str = "LONG") -> Dict:
        return {
            "total": 50,
            "breakdown": {
                "trend": 12,
                "momentum": 10,
                "support_resistance": 8,
                "volume": 7,
                "pattern": 7,
                "volatility": 6,
            },
            "signals": ["数据不足"],
            "grade": "C", "trend_strength": 0.5, "support_resistance": {},
            "momentum_direction": "neutral", "opportunity_direction": pool_type,
            "current_price": 0
        }
    
    def _get_grade_v2(self, score) -> str:
        if score >= 80: return "A"
        elif score >= 65: return "B"
        elif score >= 50: return "C"
        else: return "D"

    def _build_quant_analysis(
        self,
        score: Dict,
        indicators: Dict,
        pool_type: str,
        ai_status: str,
        ai_error: Optional[str] = None,
    ) -> Dict:
        """Build the deterministic fallback without discarding the quant signal."""
        reasoning = list(score.get('signals', []))[:8]
        if ai_status == 'disabled':
            reasoning.insert(0, "AI未配置，当前结论来自统一量化评分")
        elif ai_status == 'skipped':
            reasoning.insert(0, "量化初筛未进入AI深度分析，当前结论来自统一量化评分")
        elif ai_error:
            reasoning.insert(0, f"AI分析不可用，已回退到量化结论: {ai_error}")

        analysis = {
            'action': self._determine_action_v2(score, pool_type),
            'confidence': self._calculate_confidence_v2(score, pool_type),
            'reasoning': reasoning,
            'score': score,
            'indicators': dict(indicators),
            'ai_status': ai_status,
        }
        if ai_error:
            analysis['ai_error'] = ai_error
        return analysis
    
    def _determine_action_v2(self, score: Dict, pool_type: str) -> str:
        """V2: 根据评分确定行动"""
        total = score['total']
        trend = score.get('trend_strength', 0.5)
        momentum = score.get('momentum_direction', 'neutral')
        
        if pool_type == 'LONG':
            if total >= 75 and trend > 0.6:
                return 'BUY'
            elif total >= 65 and momentum == 'bullish':
                return 'BUY'
            elif total >= 60:
                return 'BUY'
            else:
                return 'HOLD'
        else:  # SHORT
            if total >= 75 and trend > 0.6:
                return 'SELL'
            elif total >= 65 and momentum == 'bearish':
                return 'SELL'
            elif total >= 60:
                return 'SELL'
            return 'HOLD'
    
    def _calculate_confidence_v2(self, score: Dict, pool_type: str) -> float:
        """V2: 根据评分计算信心度"""
        total = score['total']
        trend = score.get('trend_strength', 0.5)
        
        # 基础信心度
        if total >= 80:
            base = 0.90
        elif total >= 70:
            base = 0.80
        elif total >= 60:
            base = 0.70
        elif total >= 50:
            base = 0.60
        else:
            base = 0.50
        
        # 趋势强度加成
        confidence = base + trend * 0.05
        
        return min(0.95, max(0.50, confidence))
    
    def _calculate_recommendation_score_v2(
        self,
        score_result: Dict,
        ai_analysis: Optional[Dict],
        pool_type: str
    ) -> float:
        """
        计算 0-100 的方向化推荐度。

        量化机会分占 70 分，AI 与目标方向的一致性占 20 分，
        方向化趋势强度占 10 分。三项都有明确上下界。
        """
        quant_score = score_result.get('total', 50)
        trend_strength = score_result.get('trend_strength', 0.5)
        ai_confidence = 0.5
        ai_action = "HOLD"
        ai_status = "available"
        if ai_analysis:
            ai_confidence = ai_analysis.get('confidence', 0.5)
            ai_action = ai_analysis.get('action', 'HOLD')
            ai_status = ai_analysis.get('ai_status', 'available')
        
        expected_action = "BUY" if pool_type == "LONG" else "SELL"
        opposite_action = "SELL" if pool_type == "LONG" else "BUY"
        if ai_status != "available":
            # No independent AI opinion: keep the component neutral instead of
            # counting the quant signal for a second time.
            ai_alignment = 0.5
        elif ai_action == expected_action:
            ai_alignment = ai_confidence
        elif ai_action == "HOLD":
            ai_alignment = 0.5 * (1 - ai_confidence)
        elif ai_action == opposite_action:
            ai_alignment = 0.0
        else:
            ai_alignment = 0.25

        recommendation = (
            quant_score * 0.70 +
            ai_alignment * 20 +
            trend_strength * 10
        )

        return round(min(100, max(0, recommendation)), 1)

    @staticmethod
    def _snapshot_config(config: Dict) -> Dict:
        """Keep only persisted analysis configuration in the immutable input."""
        return {
            **{
                key: config.get(key)
                for key in StockPickerService.DEFAULT_CONFIG
            },
            "updated_at": config.get("updated_at"),
        }

    def _build_ai_snapshot_payloads(
        self,
        *,
        symbol: str,
        pool_type: str,
        klines: List[Dict],
        indicators: Dict,
        score: Dict,
        config: Dict,
        analysis_mode: str,
        api_key: Optional[str],
        tavily_api_key: Optional[str],
        score_version: str,
        prompt_version: str,
        ai_model: Optional[str],
        input_context: Optional[Dict[str, Any]],
        output_context: Optional[Dict[str, Any]],
    ) -> Tuple[str, str, str]:
        """Build canonical input/output snapshots and the input integrity hash."""
        captured_at = utc_now_iso()
        config_snapshot = self._snapshot_config(config)
        universe_snapshot = config.get("_universe_snapshot") or [{
            "pool_id": config.get("_pool_id"),
            "symbol": symbol,
            "pool_type": pool_type,
        }]
        universe_version = config.get("_universe_version") or sha256_json(
            universe_snapshot
        )
        selection_snapshot = config.get("_selection_snapshot") or [{
            "rank": 1,
            "pool_id": universe_snapshot[0].get("pool_id"),
            "symbol": symbol,
            "score_total": score.get("total"),
        }]
        selection_version = config.get("_selection_version") or sha256_json({
            "pool_type": pool_type,
            "ai_top_n_per_pool": config.get("ai_top_n_per_pool"),
            "ranking": selection_snapshot,
        })
        config_version = sha256_json(config_snapshot)
        context = input_context or {}
        request_status = context.get(
            "request_status",
            "not_requested",
        )
        input_snapshot = {
            "version": AI_INPUT_SNAPSHOT_VERSION,
            "captured_at": captured_at,
            "request_status": request_status,
            "request_reason": context.get("request_reason"),
            "symbol": symbol,
            "pool_type": pool_type,
            "scenario": context.get("scenario"),
            "analysis_mode": analysis_mode,
            "data_as_of": (
                str(klines[-1].get("ts"))
                if klines and klines[-1].get("ts") is not None
                else None
            ),
            "data_definition_version": DATA_DEFINITION_VERSION,
            "score_version": score_version,
            "prompt_version": prompt_version,
            "ai_model": ai_model,
            "temperature": context.get("temperature"),
            "style": context.get("style"),
            "current_positions": context.get("current_positions"),
            "system_prompt": context.get("system_prompt"),
            "user_prompt": context.get("user_prompt"),
            "response_format": context.get(
                "response_format",
                {"type": "json_object"} if request_status != "not_requested" else None,
            ),
            "news_enabled": bool(tavily_api_key),
            "news_snapshot": context.get("news_snapshot"),
            "quant_score": score,
            "technical_indicators": indicators,
            "klines_hash": klines_hash(klines),
            "config_version": config_version,
            "config": config_snapshot,
            "universe_version": universe_version,
            "universe_snapshot": universe_snapshot,
            "selection_context": config.get(
                "_selection_context",
                "single_stock",
            ),
            "selection_version": selection_version,
            "selection": {
                "quant_rank": config.get("_quant_rank", 1),
                "ai_top_n_per_pool": config.get(
                    "ai_top_n_per_pool"
                ),
                "ai_selected": config.get(
                    "_ai_selected",
                    request_status != "not_requested",
                ),
                "ranking": selection_snapshot,
            },
        }
        if api_key is None:
            input_snapshot["ai_model"] = None
        input_hash = snapshot_hash(input_snapshot)

        output = output_context or {}
        output_snapshot = {
            "version": AI_OUTPUT_SNAPSHOT_VERSION,
            "captured_at": captured_at,
            "status": output.get(
                "status",
                "not_requested",
            ),
            "raw_response": output.get("raw_response"),
            "parsed_response": output.get("parsed_response"),
            "error_type": output.get("error_type"),
            "error": sanitize_error(
                output.get("error"),
                secrets=(
                    secret
                    for secret in (
                        api_key,
                        tavily_api_key,
                    )
                    if secret
                ),
            ) if output.get("error") else None,
        }
        return (
            canonical_json(input_snapshot),
            input_hash,
            canonical_json(output_snapshot),
        )
    
    def _save_analysis_result(self, **kwargs) -> Dict:
        """保存分析结果"""
        
        analysis = kwargs['analysis']
        score = analysis.get('score', {})
        breakdown = score.get('breakdown', {})
        indicators = analysis.get('indicators', {})
        klines = kwargs.get('klines', [])
        data_as_of = klines[-1].get('ts') if klines else None
        indicators_json = canonical_json(indicators)
        klines_snapshot_json = canonical_json(klines)
        ai_input_snapshot = kwargs.get("ai_input_snapshot")
        if ai_input_snapshot is not None and not isinstance(
            ai_input_snapshot,
            str,
        ):
            ai_input_snapshot = canonical_json(ai_input_snapshot)
        ai_output_snapshot = kwargs.get("ai_output_snapshot")
        if ai_output_snapshot is not None and not isinstance(
            ai_output_snapshot,
            str,
        ):
            ai_output_snapshot = canonical_json(ai_output_snapshot)
        
        with get_connection() as conn:
            conn.execute("""
                INSERT INTO stock_picker_analysis (
                    pool_id, symbol, pool_type,
                    current_price, price_change_1d, price_change_5d,
                    score_total, score_grade,
                    score_trend, score_momentum, score_volume, 
                    score_volatility, score_pattern,
                    ai_action, ai_confidence, ai_reasoning,
                    ai_status, ai_error, indicators, signals,
                    recommendation_score, recommendation_reason,
                    klines_snapshot, data_as_of,
                    score_version, prompt_version, ai_model,
                    analysis_mode, job_id,
                    score_support_resistance,
                    ai_input_snapshot, ai_input_hash,
                    ai_output_snapshot
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?
                )
            """, (
                kwargs['pool_id'],
                kwargs['symbol'],
                kwargs['pool_type'],
                indicators.get('current_price', 0),
                indicators.get('price_change_1d', 0),
                indicators.get('price_change_5d', 0),
                score.get('total', 0),
                score.get('grade', 'C'),
                breakdown.get('trend', 0),
                breakdown.get('momentum', 0),
                breakdown.get('volume', 0),
                breakdown.get('volatility', 0),
                breakdown.get('pattern', 0),
                analysis.get('action', 'HOLD'),
                analysis.get('confidence', 0),
                json.dumps(analysis.get('reasoning', []), ensure_ascii=False),
                analysis.get('ai_status'),
                analysis.get('ai_error'),
                indicators_json,
                json.dumps(score.get('signals', []), ensure_ascii=False),
                kwargs['recommendation_score'],
                kwargs['recommendation_reason'],
                klines_snapshot_json,
                data_as_of,
                kwargs.get('score_version', self.SCORE_VERSION),
                kwargs.get('prompt_version', self.PROMPT_VERSION),
                kwargs.get('ai_model'),
                kwargs.get('analysis_mode'),
                kwargs.get('job_id'),
                breakdown.get('support_resistance', 0),
                ai_input_snapshot,
                kwargs.get('ai_input_hash'),
                ai_output_snapshot,
            ))
            self._prune_analysis_history(
                conn,
                kwargs['pool_id'],
                kwargs.get('history_retention_days', 90),
                kwargs.get('max_history_per_stock', 30),
            )

        parsed_ai_input, _ = parse_snapshot(ai_input_snapshot)
        ai_snapshot_metadata = (
            parsed_ai_input
            if isinstance(parsed_ai_input, dict)
            else {}
        )
        return {
            'symbol': kwargs['symbol'],
            'pool_type': kwargs['pool_type'],
            'score': score,
            'recommendation_score': kwargs['recommendation_score'],
            'recommendation_reason': kwargs['recommendation_reason'],
            'analysis': analysis,
            'data_as_of': str(data_as_of) if data_as_of is not None else None,
            'score_version': kwargs.get('score_version', self.SCORE_VERSION),
            'prompt_version': kwargs.get('prompt_version', self.PROMPT_VERSION),
            'ai_model': kwargs.get('ai_model'),
            'analysis_mode': kwargs.get('analysis_mode'),
            'job_id': kwargs.get('job_id'),
            'ai_snapshot': {
                'version': (
                    ai_snapshot_metadata.get('version')
                ),
                'request_status': ai_snapshot_metadata.get(
                    'request_status'
                ),
                'input_hash': kwargs.get('ai_input_hash'),
                'available': ai_input_snapshot is not None,
            },
        }

    def _prune_analysis_history(
        self,
        conn,
        pool_id: int,
        retention_days: int,
        max_history_per_stock: int,
    ) -> None:
        cutoff = datetime.now() - timedelta(days=max(1, int(retention_days)))
        max_history = max(1, int(max_history_per_stock))
        conn.execute(
            """
            DELETE FROM stock_picker_analysis
            WHERE pool_id = ? AND analysis_time < ?
            """,
            (pool_id, cutoff),
        )
        conn.execute(
            f"""
            DELETE FROM stock_picker_analysis
            WHERE pool_id = ?
              AND id NOT IN (
                  SELECT id
                  FROM stock_picker_analysis
                  WHERE pool_id = ?
                  ORDER BY analysis_time DESC, id DESC
                  LIMIT {max_history}
              )
            """,
            (pool_id, pool_id),
        )
    
    def get_analysis_results(
        self,
        pool_type: Optional[str] = None,
        sort_by: str = 'recommendation',
        limit: int = 100
    ) -> Dict:
        """获取分析结果（排序）- 🔥 修复：只返回当前股票池中的分析结果"""
        
        with get_connection() as conn:
            # 获取最新的分析结果 - 只返回当前股票池中的股票
            query = """
                SELECT
                    a.id,
                    a.pool_id,
                    a.symbol,
                    a.pool_type,
                    a.analysis_time,
                    a.current_price,
                    a.price_change_1d,
                    a.price_change_5d,
                    a.score_total,
                    a.score_grade,
                    a.score_trend,
                    a.score_momentum,
                    a.score_volume,
                    a.score_volatility,
                    a.score_pattern,
                    a.ai_action,
                    a.ai_confidence,
                    a.ai_reasoning,
                    a.indicators,
                    a.signals,
                    a.recommendation_score,
                    a.recommendation_reason,
                    a.klines_snapshot,
                    a.score_support_resistance,
                    p.name,
                    p.added_reason,
                    a.ai_status,
                    a.ai_error,
                    a.data_as_of,
                    a.score_version,
                    a.prompt_version,
                    a.ai_model,
                    a.analysis_mode,
                    a.job_id,
                    a.ai_input_hash,
                    json_extract_string(
                        TRY_CAST(a.ai_input_snapshot AS JSON),
                        '$.version'
                    ),
                    json_extract_string(
                        TRY_CAST(a.ai_input_snapshot AS JSON),
                        '$.request_status'
                    )
                FROM stock_picker_analysis a
                JOIN stock_picker_pools p ON a.pool_id = p.id
                WHERE p.is_active = TRUE
                AND a.pool_id = p.id
            """
            
            params = []
            if pool_type:
                query += " AND a.pool_type = ?"
                params.append(pool_type)
            
            # 🔥 修复：只取当前股票池中每只股票最新的分析
            # 通过 pool_id 确保分析结果对应的股票还在池中
            query += """
                AND a.id IN (
                    SELECT MAX(a2.id) 
                    FROM stock_picker_analysis a2
                    JOIN stock_picker_pools p2 ON a2.pool_id = p2.id
                    WHERE p2.is_active = TRUE
                    GROUP BY a2.symbol, a2.pool_type
                )
            """
            
            # 排序
            if sort_by == 'recommendation':
                query += " ORDER BY a.recommendation_score DESC"
            elif sort_by == 'score':
                query += " ORDER BY a.score_total DESC"
            elif sort_by == 'confidence':
                query += " ORDER BY a.ai_confidence DESC"
            
            query += f" LIMIT {limit}"
            
            results = conn.execute(query, params).fetchall()
            
            # 分组和格式化
            long_results = []
            short_results = []
            
            for row in results:
                data = {
                    'id': row[0],
                    'pool_id': row[1],
                    'symbol': row[2],
                    'pool_type': row[3],
                    'analysis_time': str(row[4]),
                    'current_price': row[5],
                    'price_change_1d': row[6],
                    'price_change_5d': row[7],
                    'score': {
                        'total': row[8],
                        'grade': row[9],
                        'breakdown': {
                            'trend': row[10],
                            'momentum': row[11],
                            'volume': row[12],
                            'volatility': row[13],
                            'pattern': row[14],
                            'support_resistance': row[23] or 0
                        }
                    },
                    'ai_decision': {
                        'action': row[15],
                        'confidence': row[16],
                        'reasoning': json.loads(row[17]) if row[17] else [],
                        'status': row[26],
                        'error': row[27],
                    },
                    'indicators': json.loads(row[18]) if row[18] else {},
                    'signals': json.loads(row[19]) if row[19] else [],
                    'recommendation_score': row[20],
                    'recommendation_reason': row[21],
                    'name': row[24],
                    'added_reason': row[25],
                    'metadata': {
                        'data_as_of': str(row[28]) if row[28] else None,
                        'score_version': row[29],
                        'prompt_version': row[30],
                        'ai_model': row[31],
                        'analysis_mode': row[32],
                        'job_id': row[33],
                        'ai_snapshot_version': row[35],
                        'ai_request_status': row[36],
                        'ai_input_hash': row[34],
                        'ai_snapshot_available': bool(
                            row[34] and row[35]
                        ),
                    },
                }
                
                if data['pool_type'] == 'LONG':
                    long_results.append(data)
                else:
                    short_results.append(data)
            
            return {
                'long_analysis': long_results,
                'short_analysis': short_results,
                'stats': {
                    'long_count': len(long_results),
                    'short_count': len(short_results),
                    'long_avg_score': sum(r['score']['total'] for r in long_results) / len(long_results) if long_results else 0,
                    'short_avg_score': sum(r['score']['total'] for r in short_results) / len(short_results) if short_results else 0
                }
            }

    def get_analysis_history(
        self,
        symbol: str,
        pool_type: Optional[str] = None,
        limit: int = 30,
    ) -> List[Dict]:
        """Return retained analysis history with version and job metadata."""
        symbol = self._normalize_symbol(symbol)
        if pool_type:
            pool_type = self._validate_pool_type(pool_type)
        safe_limit = min(500, max(1, int(limit)))
        query = """
            SELECT
                id, pool_id, symbol, pool_type, analysis_time,
                score_total, score_grade, recommendation_score,
                recommendation_reason, ai_action, ai_confidence,
                ai_status, ai_error, data_as_of, score_version,
                prompt_version, ai_model, analysis_mode, job_id,
                ai_input_hash,
                json_extract_string(
                    TRY_CAST(ai_input_snapshot AS JSON),
                    '$.version'
                ),
                json_extract_string(
                    TRY_CAST(ai_input_snapshot AS JSON),
                    '$.request_status'
                )
            FROM stock_picker_analysis
            WHERE symbol = ?
        """
        params = [symbol]
        if pool_type:
            query += " AND pool_type = ?"
            params.append(pool_type)
        query += f" ORDER BY analysis_time DESC, id DESC LIMIT {safe_limit}"
        with get_connection() as conn:
            rows = conn.execute(query, params).fetchall()
        history = []
        for row in rows:
            history.append({
                'id': row[0],
                'pool_id': row[1],
                'symbol': row[2],
                'pool_type': row[3],
                'analysis_time': str(row[4]),
                'score_total': row[5],
                'score_grade': row[6],
                'recommendation_score': row[7],
                'recommendation_reason': row[8],
                'ai_action': row[9],
                'ai_confidence': row[10],
                'ai_status': row[11],
                'ai_error': row[12],
                'data_as_of': str(row[13]) if row[13] else None,
                'score_version': row[14],
                'prompt_version': row[15],
                'ai_model': row[16],
                'analysis_mode': row[17],
                'job_id': row[18],
                'ai_input_hash': row[19],
                'ai_snapshot_version': row[20],
                'ai_request_status': row[21],
            })
        return history

    def get_analysis_snapshot(self, analysis_id: int) -> Optional[Dict]:
        """Return one immutable AI snapshot with server-side integrity checks."""
        if int(analysis_id) <= 0:
            raise ValueError("analysis_id 必须为正整数")
        with get_connection() as conn:
            row = conn.execute(
                """
                SELECT
                    id, pool_id, symbol, pool_type, analysis_time,
                    indicators, klines_snapshot,
                    ai_input_snapshot, ai_input_hash,
                    ai_output_snapshot
                FROM stock_picker_analysis
                WHERE id = ?
                """,
                (int(analysis_id),),
            ).fetchone()
        if not row:
            return None

        indicators, indicators_error = parse_snapshot(row[5])
        klines, klines_error = parse_snapshot(row[6])
        input_snapshot, input_error = parse_snapshot(row[7])
        output_snapshot, output_error = parse_snapshot(row[9])
        stored_input_hash = row[8]

        recomputed_input_hash = None
        input_hash_valid = None
        expected_klines_hash = None
        actual_klines_hash = None
        klines_hash_valid = None
        indicators_match = None
        if isinstance(input_snapshot, dict):
            recomputed_input_hash = snapshot_hash(input_snapshot)
            if stored_input_hash:
                input_hash_valid = (
                    recomputed_input_hash == stored_input_hash
                )
            expected_klines_hash = input_snapshot.get("klines_hash")
            if klines is not None and expected_klines_hash:
                actual_klines_hash = klines_hash(klines)
                klines_hash_valid = (
                    actual_klines_hash == expected_klines_hash
                )
            snapshot_indicators = input_snapshot.get(
                "technical_indicators"
            )
            if indicators is not None and snapshot_indicators is not None:
                indicators_match = (
                    sha256_json(indicators)
                    == sha256_json(snapshot_indicators)
                )

        validation_values = (
            input_hash_valid,
            klines_hash_valid,
            indicators_match,
        )
        hash_valid = None
        if stored_input_hash or input_snapshot is not None:
            hash_valid = all(value is True for value in validation_values)

        return {
            "analysis_id": row[0],
            "pool_id": row[1],
            "symbol": row[2],
            "pool_type": row[3],
            "analysis_time": str(row[4]),
            "legacy_record": input_snapshot is None,
            "ai_input_hash": stored_input_hash,
            "hash_valid": hash_valid,
            "integrity": {
                "recomputed_input_hash": recomputed_input_hash,
                "input_hash_valid": input_hash_valid,
                "expected_klines_hash": expected_klines_hash,
                "actual_klines_hash": actual_klines_hash,
                "klines_hash_valid": klines_hash_valid,
                "indicators_match": indicators_match,
                "parse_errors": {
                    "indicators": indicators_error,
                    "klines": klines_error,
                    "ai_input": input_error,
                    "ai_output": output_error,
                },
            },
            "ai_input_snapshot": input_snapshot,
            "ai_output_snapshot": output_snapshot,
            "indicators_snapshot": indicators,
            "klines_snapshot": klines,
        }


# 全局实例
_stock_picker_service = None


def get_stock_picker_service() -> StockPickerService:
    """获取选股服务单例"""
    global _stock_picker_service
    if _stock_picker_service is None:
        _stock_picker_service = StockPickerService()
    return _stock_picker_service
