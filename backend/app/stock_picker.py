"""
智能选股服务 - V2.0 优化版
主要优化：
1. 重新设计评分维度和权重（更科学的配比）
2. 添加趋势强度、支撑阻力分析
3. 添加相对强度(RS)分析  
4. 优化推荐度计算公式
5. 增加多周期分析
"""
from typing import List, Dict, Optional, Tuple
from datetime import datetime, timedelta
import asyncio
import logging
import json
import numpy as np

from .db import get_connection
from .services import get_cached_candlesticks
from .repositories import load_ai_credentials

logger = logging.getLogger(__name__)


class StockPickerService:
    """智能选股服务"""
    
    def __init__(self):
        self.cache = {}  # 简单缓存
        self.cache_duration = 300  # 5分钟
    
    # ========== 股票池管理 ==========
    
    def add_stock(self, pool_type: str, symbol: str, **kwargs) -> int:
        """添加股票到池"""
        with get_connection() as conn:
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
        success = []
        failed = []
        
        for symbol in symbols:
            try:
                stock_id = self.add_stock(pool_type, symbol.strip())
                if stock_id:
                    success.append(symbol)
                    logger.info(f"✅ 添加成功: {symbol} (ID: {stock_id})")
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
            conn.execute("DELETE FROM stock_picker_pools WHERE id = ?", (pool_id,))
    
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
            cache_keys_to_remove = [k for k in self.cache.keys() if k.endswith(f"_{pool_type}")]
            for key in cache_keys_to_remove:
                del self.cache[key]
            logger.info(f"🧹 清理缓存: {len(cache_keys_to_remove)}条")
            
            logger.info(f"✅ 清空股票池: {pool_type} - {count}只股票")
            return count
    
    def toggle_active(self, pool_id: int):
        """切换激活状态"""
        with get_connection() as conn:
            conn.execute("""
                UPDATE stock_picker_pools 
                SET is_active = NOT is_active 
                WHERE id = ?
            """, (pool_id,))
    
    def get_pools(self, pool_type: Optional[str] = None) -> Dict:
        """获取股票池"""
        with get_connection() as conn:
            if pool_type:
                query = "SELECT * FROM stock_picker_pools WHERE pool_type = ? AND is_active = TRUE ORDER BY priority DESC, added_at"
                results = conn.execute(query, (pool_type,)).fetchall()
            else:
                results = conn.execute(
                    "SELECT * FROM stock_picker_pools WHERE is_active = TRUE ORDER BY pool_type, priority DESC, added_at"
                ).fetchall()
            
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
        progress_callback: Optional[callable] = None
    ) -> Dict:
        """批量分析股票池"""
        
        pools = self.get_pools(pool_type)
        all_stocks = []
        
        if not pool_type or pool_type == 'LONG':
            all_stocks.extend([(s, 'LONG') for s in pools['long_pool']])
        if not pool_type or pool_type == 'SHORT':
            all_stocks.extend([(s, 'SHORT') for s in pools['short_pool']])
        
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
        
        # 并发分析（限制并发数避免API限流）
        results = []
        completed_count = 0
        semaphore = asyncio.Semaphore(5)  # 最多5个并发
        
        async def analyze_with_limit(stock, ptype):
            nonlocal completed_count
            async with semaphore:
                symbol = stock['symbol']
                if progress_callback:
                    progress_callback({
                        'current': symbol,
                        'log': f'正在分析: {symbol}'
                    })
                
                result = await self._analyze_single_stock(
                    stock['id'], 
                    symbol, 
                    ptype,
                    force_refresh,
                    progress_callback  # 传递回调函数
                )
                
                completed_count += 1
                if progress_callback:
                    progress_callback({
                        'completed': completed_count,
                        'log': f'完成: {symbol} ({completed_count}/{total_count})'
                    })
                
                return result
        
        tasks = [analyze_with_limit(stock, ptype) for stock, ptype in all_stocks]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
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
        progress_callback: Optional[callable] = None
    ) -> Dict:
        """分析单只股票"""
        
        # 1. 检查缓存
        cache_key = f"{symbol}_{pool_type}"
        if not force_refresh and cache_key in self.cache:
            cached = self.cache[cache_key]
            if datetime.now() - cached['time'] < timedelta(seconds=self.cache_duration):
                logger.info(f"📋 使用缓存: {symbol}")
                return cached['data']
        
        try:
            logger.info(f"🔍 开始分析: {symbol}")
            
            # 2. 先同步K线数据（调用API）- 在线程池中执行避免阻塞
            from .services import sync_history_candlesticks
            kline_count = 0
            try:
                if progress_callback:
                    progress_callback({'log': f'📥 同步K线: {symbol}...'})
                
                sync_result = await asyncio.to_thread(
                    sync_history_candlesticks,
                    symbols=[symbol],
                    period='day',
                    count=1000  # ⬆️ 增加到1000条K线
                )
                kline_count = sync_result.get(symbol, 0)
                logger.info(f"📥 同步K线: {symbol} - {kline_count}条")
                
                if progress_callback:
                    progress_callback({'log': f'📥 同步K线: {symbol} - {kline_count}条'})
            except Exception as e:
                logger.warning(f"⚠️ 同步K线失败: {symbol} - {e}")
                if progress_callback:
                    progress_callback({'log': f'⚠️ 同步K线失败: {symbol} - {e}'})
            
            # 3. 获取K线数据（从缓存读取）
            klines = get_cached_candlesticks(symbol, limit=1000)  # ⬆️ 获取1000条K线
            if not klines or len(klines) < 30:
                # 无法获取K线，直接跳过此股票
                logger.warning(f"⏭️ 跳过: {symbol} - K线数据不足({len(klines) if klines else 0}条)")
                if progress_callback:
                    progress_callback({'log': f'⏭️ 跳过: {symbol} - K线数据不足({len(klines) if klines else 0}条)'})
                return None  # 返回None表示跳过
            
            # 3. 构建统一技术快照，并按配置执行 DeepSeek 深度分析。
            from .ai_analyzer import DeepSeekAnalyzer, calculate_technical_indicators
            
            # 获取AI凭据（使用正确的函数）
            ai_creds = load_ai_credentials()
            api_key = ai_creds.get('DEEPSEEK_API_KEY')
            base_url = ai_creds.get('DEEPSEEK_BASE_URL', 'https://api.deepseek.com')
            tavily_api_key = ai_creds.get('TAVILY_API_KEY')  # ⬆️ 获取Tavily API Key
            
            indicators = calculate_technical_indicators(klines)
            v2_score = self._calculate_advanced_score_v2(klines, pool_type)
            indicators.update({
                'trend_strength': v2_score.get('trend_strength', 0.5),
                'momentum_direction': v2_score.get('momentum_direction', 'neutral'),
            })
            
            if not api_key:
                logger.warning(f"⚠️ 未配置DeepSeek API，使用V2量化评分: {symbol}")
                analysis = self._build_quant_analysis(
                    v2_score,
                    indicators,
                    pool_type,
                    ai_status='disabled',
                )
            else:
                # 使用DeepSeek AI分析（集成Tavily搜索）
                logger.info(f"🤖 DeepSeek分析: {symbol} (搜索引擎: {'✅' if tavily_api_key else '❌'})")
                if progress_callback:
                    progress_callback({'log': f'🤖 DeepSeek分析: {symbol}...'})
                
                analyzer = DeepSeekAnalyzer(
                    api_key=api_key, 
                    base_url=base_url,
                    tavily_api_key=tavily_api_key  # ⬆️ 传递Tavily API Key
                )
                
                # 调用AI分析（在线程池中执行，避免阻塞事件循环）
                analysis = await asyncio.to_thread(
                    analyzer.analyze_trading_opportunity,
                    symbol=symbol,
                    klines=klines,
                    scenario="buy_focus" if pool_type == 'LONG' else "sell_focus",
                    technical_indicators=indicators,
                    quant_score=v2_score,
                )

                if analysis.get('error'):
                    ai_error = analysis['error']
                    logger.warning(f"⚠️ AI分析失败，回退到量化结论: {symbol} - {ai_error}")
                    analysis = self._build_quant_analysis(
                        v2_score,
                        indicators,
                        pool_type,
                        ai_status='fallback',
                        ai_error=ai_error,
                    )
                
                logger.info(f"🤖 AI决策: {symbol} - {analysis['action']} (信心度: {analysis['confidence']:.2f}, V2评分: {v2_score['total']:.1f})")
                if progress_callback:
                    progress_callback({'log': f"🤖 AI决策: {symbol} - {analysis['action']} (信心度: {analysis['confidence']:.2f}, V2评分: {v2_score['total']:.1f})"})
            
            # 4. 计算推荐度 - ⬆️ V2.0: 使用新的推荐度算法
            recommendation_score = self._calculate_recommendation_score_v2(
                v2_score, analysis, pool_type
            )
            
            recommendation_reason = self._generate_recommendation_reason(
                analysis, pool_type, recommendation_score
            )
            
            # 5. 保存结果
            result = self._save_analysis_result(
                pool_id=pool_id,
                symbol=symbol,
                pool_type=pool_type,
                klines=klines,
                analysis=analysis,
                recommendation_score=recommendation_score,
                recommendation_reason=recommendation_reason
            )
            
            # 6. 更新缓存
            self.cache[cache_key] = {
                'time': datetime.now(),
                'data': result
            }
            
            logger.info(
                f"✅ 分析完成: {symbol} - 评分: {analysis.get('score', {}).get('total', 0):.1f}, "
                f"推荐度: {recommendation_score:.1f}"
            )
            return result
            
        except Exception as e:
            logger.error(f"❌ 分析失败: {symbol} - {e}")
            raise
    
    def _determine_action(self, score: Dict, pool_type: str) -> str:
        """根据评分确定行动"""
        total_score = score['total']
        
        if pool_type == 'LONG':
            if total_score >= 80:
                return 'BUY'
            elif total_score >= 65:
                return 'BUY'
            else:
                return 'HOLD'
        else:  # SHORT
            if total_score <= 40:
                return 'SELL'
            elif total_score <= 50:
                return 'SELL'
            else:
                return 'HOLD'
    
    def _calculate_confidence(self, score: Dict, pool_type: str) -> float:
        """根据评分计算信心度"""
        total_score = score['total']
        grade = score['grade']
        
        # 基于评级映射信心度
        if grade == 'A':
            base_confidence = 0.85
        elif grade == 'B':
            base_confidence = 0.75
        elif grade == 'C':
            base_confidence = 0.65
        else:
            base_confidence = 0.50
        
        # 做空池需要反转逻辑
        if pool_type == 'SHORT':
            base_confidence = 1.0 - (total_score / 100) * 0.5 + 0.5
        
        return min(0.95, max(0.50, base_confidence))
    
    def _generate_reasoning(self, score: Dict, indicators: Dict, pool_type: str) -> List[str]:
        """生成推理理由"""
        reasons = []
        signals = score.get('signals', [])
        
        # 添加评分相关理由
        reasons.append(f"量化评分: {score['total']:.1f}/100 ({score['grade']}级)")
        
        # 添加主要信号
        for signal in signals[:5]:  # 最多5个信号
            reasons.append(signal)
        
        # 根据池类型添加特定理由
        if pool_type == 'LONG':
            if score['total'] >= 80:
                reasons.append("多个买入信号共振，强烈推荐")
            elif score['total'] >= 65:
                reasons.append("技术面良好，推荐买入")
        else:
            if score['total'] <= 40:
                reasons.append("技术面偏弱，适合做空")
            elif score['total'] <= 50:
                reasons.append("弱势形态，可考虑做空")
        
        return reasons
    
    def calculate_recommendation_score(
        self, 
        analysis: Dict, 
        pool_type: str
    ) -> float:
        """
        计算推荐度（0-100）⬆️ V3.0优化：增加波动性权重
        
        新公式：
        - 做多: 评分*0.4 + 信心度*50*0.2 + 信号强度*0.2 + 波动性*0.2
        - 做空: (100-评分)*0.4 + 信心度*50*0.2 + 信号强度*0.2 + 波动性*0.2
        """
        
        score_total = analysis.get('score', {}).get('total', 50)
        confidence = analysis.get('confidence', 0.5)
        signals = analysis.get('score', {}).get('signals', [])
        volatility_score = analysis.get('score', {}).get('breakdown', {}).get('volatility', 0)
        
        # 计算信号强度（0-20）
        signal_strength = self._calculate_signal_strength(signals)
        
        # 波动性归一化到0-20分
        volatility_weight = min(20, (volatility_score / 25) * 20)
        
        if pool_type == 'LONG':
            # 做多：高分好
            recommendation = (
                score_total * 0.4 +           # 降低评分权重（原0.5）
                confidence * 50 * 0.2 +       # 降低信心度权重（原0.3）
                signal_strength * 0.2 +       # 保持信号权重
                volatility_weight             # ⬆️ 新增波动性权重（20%）
            )
        else:  # SHORT
            # 做空：低分好
            recommendation = (
                (100 - score_total) * 0.4 +
                confidence * 50 * 0.2 +
                signal_strength * 0.2 +
                volatility_weight
            )
        
        return min(100, max(0, recommendation))
    
    def _calculate_signal_strength(self, signals: List[str]) -> float:
        """计算信号强度（0-20）"""
        
        strong_patterns = [
            "多头排列", "MACD强势金叉", "红三兵", "锤子线形态",
            "空头排列", "黑三兵", "吊颈线形态"
        ]
        medium_patterns = [
            "MACD金叉", "适度放量", "明显放量", "RSI健康",
            "接近布林下轨", "RSI超卖", "价格在MA20上方"
        ]
        
        score = 0
        for signal in signals:
            if any(p in signal for p in strong_patterns):
                score += 5
            elif any(p in signal for p in medium_patterns):
                score += 3
            else:
                score += 1
        
        return min(20, score)
    
    def _generate_recommendation_reason(
        self,
        analysis: Dict,
        pool_type: str,
        recommendation_score: float
    ) -> str:
        """生成推荐理由"""
        
        grade = analysis.get('score', {}).get('grade', 'C')
        confidence = analysis.get('confidence', 0.5)
        action = analysis.get('action', 'HOLD')
        
        if pool_type == 'LONG':
            if recommendation_score >= 80:
                return f"强烈推荐买入：{grade}级评分 + 信心度{confidence:.0%} + AI建议{action}"
            elif recommendation_score >= 65:
                return f"推荐买入：{grade}级评分 + 信心度{confidence:.0%}"
            elif recommendation_score >= 50:
                return f"可考虑买入：技术面尚可"
            else:
                return f"谨慎观望：评分较低或信号不足"
        else:  # SHORT
            if recommendation_score >= 80:
                return f"强烈推荐做空：弱势形态 + 信心度{confidence:.0%}"
            elif recommendation_score >= 65:
                return f"推荐做空：技术面偏弱"
            elif recommendation_score >= 50:
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
        if ai_analysis:
            ai_confidence = ai_analysis.get('confidence', 0.5)
            ai_action = ai_analysis.get('action', 'HOLD')
        
        expected_action = "BUY" if pool_type == "LONG" else "SELL"
        opposite_action = "SELL" if pool_type == "LONG" else "BUY"
        if ai_action == expected_action:
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
    
    def _save_analysis_result(self, **kwargs) -> Dict:
        """保存分析结果"""
        
        analysis = kwargs['analysis']
        score = analysis.get('score', {})
        breakdown = score.get('breakdown', {})
        indicators = analysis.get('indicators', {})
        
        with get_connection() as conn:
            conn.execute("""
                INSERT INTO stock_picker_analysis (
                    pool_id, symbol, pool_type,
                    current_price, price_change_1d, price_change_5d,
                    score_total, score_grade,
                    score_trend, score_momentum, score_volume, 
                    score_volatility, score_pattern,
                    ai_action, ai_confidence, ai_reasoning,
                    signals, recommendation_score, recommendation_reason,
                    score_support_resistance
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                json.dumps(score.get('signals', []), ensure_ascii=False),
                kwargs['recommendation_score'],
                kwargs['recommendation_reason'],
                breakdown.get('support_resistance', 0)
            ))
        
        return {
            'symbol': kwargs['symbol'],
            'pool_type': kwargs['pool_type'],
            'score': score,
            'recommendation_score': kwargs['recommendation_score'],
            'recommendation_reason': kwargs['recommendation_reason'],
            'analysis': analysis
        }
    
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
                    p.added_reason
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
                        'reasoning': json.loads(row[17]) if row[17] else []
                    },
                    'signals': json.loads(row[19]) if row[19] else [],
                    'recommendation_score': row[20],
                    'recommendation_reason': row[21],
                    'name': row[24],
                    'added_reason': row[25]
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


# 全局实例
_stock_picker_service = None


def get_stock_picker_service() -> StockPickerService:
    """获取选股服务单例"""
    global _stock_picker_service
    if _stock_picker_service is None:
        _stock_picker_service = StockPickerService()
    return _stock_picker_service
