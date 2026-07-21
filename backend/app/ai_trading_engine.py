"""
AI 自动交易引擎 - 核心执行逻辑
"""
import asyncio
from datetime import datetime
from typing import Dict, List, Optional
import logging

from .ai_analyzer import DeepSeekAnalyzer
from .repositories import (
    get_ai_trading_config,
    load_ai_credentials,
    save_ai_analysis,
    save_ai_trade,
    get_ai_positions,
    create_ai_position,
    update_ai_position,
    delete_ai_position,
    get_daily_trades_count,
    get_daily_pnl,
    update_analysis_trigger_status,
    update_ai_trade_status,
)
from .trading_api import get_trading_api, OrderRequest, OrderSide, OrderType

logger = logging.getLogger(__name__)


class AiTradingEngine:
    """AI 自动交易引擎"""
    
    def __init__(self):
        self.analyzer: Optional[DeepSeekAnalyzer] = None
        self.running = False
        self.task: Optional[asyncio.Task] = None
        self.config: Optional[Dict] = None
        self.listeners: List[asyncio.Queue] = []  # WebSocket 监听器
    
    async def start(self):
        """启动自动交易引擎"""
        if self.running:
            logger.warning("⚠️  AI Trading Engine is already running")
            raise ValueError("AI Trading Engine is already running")
        
        # 加载配置
        self.config = get_ai_trading_config()
        if not self.config:
            # 如果没有配置，创建默认配置
            self.config = {
                'enabled': True,
                'symbols': [],
                'check_interval_minutes': 5,
                'ai_model': 'deepseek-chat',
                'ai_temperature': 0.3,
                'min_confidence': 0.70,  # 降低到0.70，更容易触发交易
                'max_daily_trades': 20,
                'max_loss_per_day': 5000,
                'fixed_amount_per_trade': 10000,
            }
        
        # 优先从 settings 表读取 API Key（加密存储）
        ai_creds = load_ai_credentials()
        api_key = ai_creds.get('DEEPSEEK_API_KEY', '').strip()
        
        # 如果 settings 没有，尝试从 config 读取
        if not api_key:
            api_key = self.config.get('ai_api_key', '').strip()
        
        if not api_key:
            logger.error("❌ DeepSeek API Key 未配置")
            raise ValueError("DeepSeek API Key 未配置。请前往「基础配置」页面设置 AI 配置")
        
        # 初始化 AI 分析器
        try:
            self.analyzer = DeepSeekAnalyzer(
                api_key=api_key,
                model=self.config.get('ai_model', 'deepseek-chat'),
                temperature=self.config.get('ai_temperature', 0.3)
            )
        except Exception as e:
            logger.error(f"❌ 初始化 DeepSeek 失败: {e}")
            raise ValueError(f"初始化 DeepSeek 失败: {e}")
        
        self.running = True
        self.task = asyncio.create_task(self._run_loop())
        logger.info("🤖 AI Trading Engine started")
    
    async def stop(self):
        """停止自动交易引擎"""
        self.running = False
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        logger.info("🛑 AI Trading Engine stopped")
    
    def is_running(self) -> bool:
        """检查引擎是否运行中"""
        return self.running
    
    async def trigger_immediate_analysis(self):
        """立即触发一次分析（不等待定时器）"""
        if not self.config:
            raise ValueError("AI Trading Engine is not configured")
        
        if not self.analyzer:
            raise ValueError("AI Trading Engine is not started")
        
        symbols = self.config.get('symbols', [])
        if not symbols:
            logger.warning("⚠️ No symbols to analyze")
            return {"analyzed": 0, "message": "No symbols configured"}
        
        logger.info(f"🚀 Triggering immediate analysis for {len(symbols)} symbols...")
        
        analyzed_count = 0
        for symbol in symbols:
            try:
                await self._process_symbol(symbol)
                analyzed_count += 1
            except Exception as e:
                logger.error(f"Failed to analyze {symbol}: {e}", exc_info=True)
        
        logger.info(f"✅ Immediate analysis completed: {analyzed_count}/{len(symbols)} symbols")
        return {"analyzed": analyzed_count, "total": len(symbols), "message": f"分析完成: {analyzed_count}/{len(symbols)} 只股票"}
    
    def add_listener(self) -> asyncio.Queue:
        """添加 WebSocket 监听器"""
        queue = asyncio.Queue(maxsize=100)
        self.listeners.append(queue)
        logger.info(f"📡 Added AI trading listener, total: {len(self.listeners)}")
        return queue
    
    def remove_listener(self, queue: asyncio.Queue):
        """移除 WebSocket 监听器"""
        if queue in self.listeners:
            self.listeners.remove(queue)
            logger.info(f"📡 Removed AI trading listener, total: {len(self.listeners)}")
    
    async def _broadcast(self, message: Dict):
        """广播消息到所有监听器"""
        dead_queues = []
        for queue in self.listeners:
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                logger.warning("⚠️  Listener queue full, dropping message")
            except Exception as e:
                logger.error(f"❌ Failed to broadcast to listener: {e}")
                dead_queues.append(queue)
        
        # 移除失败的队列
        for queue in dead_queues:
            self.remove_listener(queue)
    
    async def _run_loop(self):
        """主循环 - 定期检查和交易"""
        if not self.config:
            return
        
        symbols = self.config.get('symbols', [])
        interval_minutes = self.config.get('check_interval_minutes', 5)
        
        logger.info(f"📊 监控股票池: {symbols}")
        logger.info(f"⏱️  检查间隔: {interval_minutes} 分钟")
        
        while self.running:
            try:
                logger.info(f"🔄 AI Trading cycle started for {len(symbols)} symbols")
                
                # 检查每日限制
                if self._check_daily_limits():
                    logger.warning("⚠️  Daily limits reached, skipping this cycle")
                    await asyncio.sleep(interval_minutes * 60)
                    continue
                
                # 遍历每只股票
                for symbol in symbols:
                    if not self.running:
                        break
                    
                    try:
                        await self._process_symbol(symbol)
                    except Exception as e:
                        logger.error(f"Error processing {symbol}: {e}", exc_info=True)
                    
                    # 避免请求过快
                    await asyncio.sleep(2)
                
                # 更新持仓状态
                await self._update_positions()
                
                # 等待下一轮
                logger.info(f"💤 Sleeping for {interval_minutes} minutes...")
                await asyncio.sleep(interval_minutes * 60)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in AI trading loop: {e}", exc_info=True)
                await asyncio.sleep(60)  # 发生错误时等待 1 分钟再试
    
    def _check_daily_limits(self) -> bool:
        """检查是否达到每日限制"""
        if not self.config:
            return True
        
        # 检查交易次数
        today_trades = get_daily_trades_count()
        max_trades = self.config.get('max_daily_trades', 20)
        if today_trades >= max_trades:
            logger.warning(f"今日交易次数 {today_trades} 已达上限 {max_trades}")
            return True
        
        # 检查每日亏损
        today_pnl = get_daily_pnl()
        max_loss = self.config.get('max_loss_per_day', 5000)
        if today_pnl <= -max_loss:
            logger.warning(f"今日亏损 ${today_pnl:.2f} 已达上限 ${max_loss}")
            return True
        
        return False
    
    async def _process_symbol(self, symbol: str):
        """处理单只股票的分析和交易"""
        logger.info(f"📊 Analyzing {symbol}...")
        
        # 推送：开始分析
        await self._broadcast({
            'type': 'log',
            'data': {'message': f'📊 开始分析: {symbol}'}
        })
        
        # 1. 获取最新 K 线数据
        await self._broadcast({
            'type': 'log',
            'data': {'message': f'📥 获取K线数据: {symbol}...'}
        })
        
        klines = await self._get_klines(symbol)
        if not klines or len(klines) < 20:
            logger.warning(f"Not enough kline data for {symbol}")
            await self._broadcast({
                'type': 'log',
                'data': {'message': f'⚠️ K线数据不足: {symbol} ({len(klines) if klines else 0}条)'}
            })
            return
        
        await self._broadcast({
            'type': 'log',
            'data': {'message': f'✅ K线数据: {symbol} - {len(klines)}条'}
        })
        
        # 2. 获取当前持仓
        current_positions = get_ai_positions()
        has_position = symbol in current_positions
        
        # 3. AI 分析（专注买入机会）
        await self._broadcast({
            'type': 'log',
            'data': {'message': f'🤖 DeepSeek分析中: {symbol}...'}
        })
        
        analysis = await asyncio.to_thread(
            self.analyzer.analyze_trading_opportunity,
            symbol,
            klines,
            current_positions,
            "buy_focus",
        )
        
        # 4. 保存分析记录
        # 序列化K线数据（将datetime转换为字符串）
        serialized_klines = []
        for kline in klines:
            serialized_kline = kline.copy()
            if 'ts' in serialized_kline:
                # 如果是datetime对象，转换为ISO格式字符串
                from datetime import datetime
                if isinstance(serialized_kline['ts'], datetime):
                    serialized_kline['ts'] = serialized_kline['ts'].isoformat()
            serialized_klines.append(serialized_kline)
        
        analysis_id = save_ai_analysis(
            symbol=symbol,
            kline_snapshot=serialized_klines,
            indicators=analysis.get('indicators', {}),
            current_price=klines[-1].get('close', 0),
            ai_response=analysis
        )
        
        logger.info(
            f"🤖 AI Decision for {symbol}: {analysis['action']} "
            f"(confidence: {analysis['confidence']:.2%})"
        )
        
        # 推送：AI决策结果
        await self._broadcast({
            'type': 'log',
            'data': {'message': f"✅ AI决策: {symbol} - {analysis['action']} (信心度: {analysis['confidence']:.0%})"}
        })
        
        # 广播AI分析结果和K线数据
        await self._broadcast({
            'type': 'ai_analysis',
            'data': {
                'id': analysis_id,
                'symbol': symbol,
                'analysis_time': datetime.now().isoformat(),
                'action': analysis['action'],
                'confidence': analysis['confidence'],
                'reasoning': analysis.get('reasoning', []),
                'current_price': klines[-1].get('close', 0),
                'klines': serialized_klines[-20:],  # 最近20根K线
                'indicators': analysis.get('indicators', {})
            }
        })
        
        # 5. 判断是否执行交易
        should_trade, reason = self._should_execute_trade(
            analysis, has_position
        )
        
        if not should_trade:
            logger.info(f"⏭️  Skip trading {symbol}: {reason}")
            await self._broadcast({
                'type': 'log',
                'data': {'message': f'⏭️  跳过交易: {symbol} - {reason}'}
            })
            update_analysis_trigger_status(analysis_id, False, skip_reason=reason)
            return
        
        # 6. 执行交易
        await self._broadcast({
            'type': 'log',
            'data': {'message': f'💰 开始执行交易: {symbol} - {analysis["action"]}'}
        })
        
        await self._execute_trade(
            symbol=symbol,
            analysis=analysis,
            analysis_id=analysis_id,
            current_position=current_positions.get(symbol)
        )
    
    async def _get_klines(self, symbol: str, count: int = 100) -> List[Dict]:
        """获取 K 线数据"""
        try:
            # 从数据库或 API 获取 K 线数据
            from .services import get_cached_candlesticks
            
            # 获取最近的数据
            klines = await asyncio.to_thread(
                get_cached_candlesticks,
                symbol,
                "day",
                count,
            )
            
            return klines
        except Exception as e:
            logger.error(f"获取 K 线数据失败 {symbol}: {e}")
            return []
    
    def _should_execute_trade(
        self,
        analysis: Dict,
        has_position: bool
    ) -> tuple:
        """判断是否应该执行交易"""
        if not self.config:
            return False, "配置未加载"
        
        action = analysis.get('action', 'HOLD')
        confidence = analysis.get('confidence', 0)
        min_confidence = self.config.get('min_confidence', 0.75)
        
        # 信心度不足
        if confidence < min_confidence:
            return False, f"信心度 {confidence:.2%} < 阈值 {min_confidence:.2%}"
        
        # HOLD 信号
        if action == 'HOLD':
            return False, "AI 建议 HOLD"
        
        # 买入但已有持仓
        if action == 'BUY' and has_position:
            return False, "已有持仓，不能重复买入"
        
        # 卖出但没有持仓
        if action == 'SELL' and not has_position:
            return False, "无持仓可卖"
        
        return True, "通过所有检查"
    
    async def _execute_trade(
        self,
        symbol: str,
        analysis: Dict,
        analysis_id: int,
        current_position: Optional[Dict]
    ):
        """执行交易（模拟模式）"""
        action = analysis.get('action', 'HOLD')
        
        try:
            if action == 'BUY':
                await self._execute_buy(symbol, analysis, analysis_id)
            elif action == 'SELL':
                await self._execute_sell(symbol, analysis, analysis_id, current_position)
            
            # 标记分析已触发交易
            # update_analysis_trigger_status(analysis_id, True, trade_id)
            
        except Exception as e:
            logger.error(f"Failed to execute {action} for {symbol}: {e}", exc_info=True)
            # 保存失败的交易记录
            save_ai_trade(
                analysis_id=analysis_id,
                symbol=symbol,
                action=action,
                order_type='MARKET',
                order_quantity=0,
                status='FAILED',
                error_message=str(e),
                ai_confidence=analysis.get('confidence', 0),
                ai_reasoning="\n".join(analysis.get('reasoning', []))
            )
    
    async def _execute_buy(
        self,
        symbol: str,
        analysis: Dict,
        analysis_id: int
    ):
        """执行买入"""
        if not self.config:
            return
        
        # 计算买入数量
        await self._broadcast({
            'type': 'log',
            'data': {'message': f'📊 计算买入数量: {symbol}...'}
        })
        
        quantity = self._calculate_buy_quantity(symbol, analysis)
        if quantity <= 0:
            logger.warning(f"计算的买入数量为 0，跳过 {symbol}")
            await self._broadcast({
                'type': 'log',
                'data': {'message': f'⚠️ 买入数量为0，跳过: {symbol}'}
            })
            return
        
        # 使用 AI 建议的价格
        price = analysis.get('entry_price_max', 0)
        
        await self._broadcast({
            'type': 'log',
            'data': {'message': f'✅ 买入数量: {symbol} x {quantity} (建议价≤${price:.2f})'}
        })
        
        # 检查是否启用真实交易
        enable_real_trading = self.config.get('enable_real_trading', False)
        
        if enable_real_trading:
            # 真实交易模式
            logger.info(f"💰 真实买入: {symbol} x {quantity} @ 市价")
            
            await self._broadcast({
                'type': 'log',
                'data': {'message': f'💰 真实买入: {symbol} x {quantity} @ 市价'}
            })
            
            try:
                trading_api = get_trading_api()
                
                # 创建订单请求
                order_request = OrderRequest(
                    symbol=symbol,
                    side=OrderSide.BUY,
                    quantity=quantity,
                    order_type=OrderType.MARKET,
                    remark=f"AI Trading - Confidence: {analysis.get('confidence', 0):.2%}"
                )
                
                # 下单
                await self._broadcast({
                    'type': 'log',
                    'data': {'message': f'📤 提交买入订单: {symbol}...'}
                })
                
                order_response = await trading_api.place_order(order_request)
                
                if order_response.status.value in ['submitted', 'filled', 'partial_filled']:
                    # 订单成功
                    logger.info(f"✅ 订单提交成功: {order_response.order_id}")
                    
                    await self._broadcast({
                        'type': 'log',
                        'data': {'message': f'✅ 订单已提交: {order_response.order_id}'}
                    })
                    
                    # 等待订单成交
                    await self._broadcast({
                        'type': 'log',
                        'data': {'message': f'⏳ 等待成交: {symbol}...'}
                    })
                    
                    await asyncio.sleep(2)  # 给市价单一点时间成交
                    
                    # 查询订单状态
                    await self._broadcast({
                        'type': 'log',
                        'data': {'message': f'🔍 查询订单状态: {symbol}...'}
                    })
                    
                    final_status = await trading_api.get_order_status(order_response.order_id)
                    
                    filled_qty = final_status.filled_quantity if final_status else quantity
                    filled_price = final_status.filled_price if final_status and final_status.filled_price else price
                    order_status = final_status.status.value if final_status else 'submitted'
                    
                    # 保存交易记录
                    trade_id = save_ai_trade(
                        analysis_id=analysis_id,
                        symbol=symbol,
                        action='BUY',
                        order_type='MARKET',
                        order_quantity=quantity,
                        order_price=None,  # 市价单
                        status=order_status.upper(),
                        stop_loss_price=analysis.get('stop_loss'),
                        take_profit_price=analysis.get('take_profit'),
                        ai_confidence=analysis.get('confidence', 0),
                        ai_reasoning="\n".join(analysis.get('reasoning', [])),
                        filled_price=filled_price,
                        filled_quantity=filled_qty,
                        longbridge_order_id=order_response.order_id
                    )
                    
                    # 如果完全成交，创建持仓记录
                    if order_status in ['filled'] and filled_qty > 0:
                        create_ai_position(
                            symbol=symbol,
                            quantity=filled_qty,
                            avg_cost=filled_price,
                            open_trade_id=trade_id,
                            stop_loss_price=analysis.get('stop_loss'),
                            take_profit_price=analysis.get('take_profit')
                        )
                        logger.info(f"✅ 买入成功: {symbol} x {filled_qty} @ ${filled_price:.2f}")
                        
                        await self._broadcast({
                            'type': 'log',
                            'data': {'message': f'🎉 买入成功: {symbol} x {filled_qty} @ ${filled_price:.2f}'}
                        })
                    else:
                        await self._broadcast({
                            'type': 'log',
                            'data': {'message': f'⏳ 订单状态: {symbol} - {order_status} (成交{filled_qty}/{quantity})'}
                        })
                    
                    # 更新分析状态
                    update_analysis_trigger_status(analysis_id, True, trade_id)
                    
                else:
                    # 订单失败
                    logger.error(f"❌ 订单失败: {order_response.error_message}")
                    
                    await self._broadcast({
                        'type': 'log',
                        'data': {'message': f'❌ 订单失败: {symbol} - {order_response.error_message}'}
                    })
                    
                    save_ai_trade(
                        analysis_id=analysis_id,
                        symbol=symbol,
                        action='BUY',
                        order_type='MARKET',
                        order_quantity=quantity,
                        status='FAILED',
                        ai_confidence=analysis.get('confidence', 0),
                        ai_reasoning="\n".join(analysis.get('reasoning', [])),
                        error_message=order_response.error_message
                    )
                    
            except Exception as e:
                logger.error(f"❌ 下单异常: {e}", exc_info=True)
                
                await self._broadcast({
                    'type': 'log',
                    'data': {'message': f'❌ 下单异常: {symbol} - {str(e)}'}
                })
                
                save_ai_trade(
                    analysis_id=analysis_id,
                    symbol=symbol,
                    action='BUY',
                    order_type='MARKET',
                    order_quantity=quantity,
                    status='FAILED',
                    ai_confidence=analysis.get('confidence', 0),
                    ai_reasoning="\n".join(analysis.get('reasoning', [])),
                    error_message=str(e)
                )
        else:
            # 模拟交易模式
            logger.info(f"💰 模拟买入: {symbol} x {quantity} @ ${price:.2f}")
            
            await self._broadcast({
                'type': 'log',
                'data': {'message': f'💰 模拟买入: {symbol} x {quantity} @ ${price:.2f}'}
            })
            
            trade_id = save_ai_trade(
                analysis_id=analysis_id,
                symbol=symbol,
                action='BUY',
                order_type='MARKET',
                order_quantity=quantity,
                order_price=price,
                status='SIMULATED',
                stop_loss_price=analysis.get('stop_loss'),
                take_profit_price=analysis.get('take_profit'),
                ai_confidence=analysis.get('confidence', 0),
                ai_reasoning="\n".join(analysis.get('reasoning', [])),
                filled_price=price,
                filled_quantity=quantity,
                longbridge_order_id=f"SIMULATED_{datetime.now().strftime('%Y%m%d%H%M%S')}"
            )
            
            # 创建持仓记录
            create_ai_position(
                symbol=symbol,
                quantity=quantity,
                avg_cost=price,
                open_trade_id=trade_id,
                stop_loss_price=analysis.get('stop_loss'),
                take_profit_price=analysis.get('take_profit')
            )
            
            await self._broadcast({
                'type': 'log',
                'data': {'message': f'✅ 模拟持仓已创建: {symbol} x {quantity}'}
            })
            
            # 更新分析状态
            update_analysis_trigger_status(analysis_id, True, trade_id)
            
            logger.info(f"✅ 模拟买入完成: {symbol}, trade_id: {trade_id}")
    
    async def _execute_sell(
        self,
        symbol: str,
        analysis: Dict,
        analysis_id: int,
        position: Dict
    ):
        """执行卖出"""
        quantity = position['quantity']
        price = analysis.get('entry_price_min', 0)
        avg_cost = position['avg_cost']
        
        await self._broadcast({
            'type': 'log',
            'data': {'message': f'📊 准备卖出: {symbol} x {quantity} (成本${avg_cost:.2f})'}
        })
        
        # 检查是否启用真实交易
        enable_real_trading = self.config.get('enable_real_trading', False)
        
        if enable_real_trading:
            # 真实交易模式
            logger.info(f"💸 真实卖出: {symbol} x {quantity} @ 市价")
            
            await self._broadcast({
                'type': 'log',
                'data': {'message': f'💸 真实卖出: {symbol} x {quantity} @ 市价'}
            })
            
            try:
                trading_api = get_trading_api()
                
                # 创建订单请求
                order_request = OrderRequest(
                    symbol=symbol,
                    side=OrderSide.SELL,
                    quantity=quantity,
                    order_type=OrderType.MARKET,
                    remark=f"AI Trading - Confidence: {analysis.get('confidence', 0):.2%}"
                )
                
                # 下单
                await self._broadcast({
                    'type': 'log',
                    'data': {'message': f'📤 提交卖出订单: {symbol}...'}
                })
                
                order_response = await trading_api.place_order(order_request)
                
                if order_response.status.value in ['submitted', 'filled', 'partial_filled']:
                    # 订单成功
                    logger.info(f"✅ 订单提交成功: {order_response.order_id}")
                    
                    await self._broadcast({
                        'type': 'log',
                        'data': {'message': f'✅ 订单已提交: {order_response.order_id}'}
                    })
                    
                    # 等待订单成交
                    await self._broadcast({
                        'type': 'log',
                        'data': {'message': f'⏳ 等待成交: {symbol}...'}
                    })
                    
                    await asyncio.sleep(2)
                    
                    # 查询订单状态
                    await self._broadcast({
                        'type': 'log',
                        'data': {'message': f'🔍 查询订单状态: {symbol}...'}
                    })
                    
                    final_status = await trading_api.get_order_status(order_response.order_id)
                    
                    filled_qty = final_status.filled_quantity if final_status else quantity
                    filled_price = final_status.filled_price if final_status and final_status.filled_price else price
                    order_status = final_status.status.value if final_status else 'submitted'
                    
                    # 计算盈亏
                    pnl = (filled_price - avg_cost) * filled_qty
                    pnl_percent = (filled_price / avg_cost - 1) * 100
                    
                    # 保存交易记录
                    trade_id = save_ai_trade(
                        analysis_id=analysis_id,
                        symbol=symbol,
                        action='SELL',
                        order_type='MARKET',
                        order_quantity=quantity,
                        order_price=None,
                        status=order_status.upper(),
                        ai_confidence=analysis.get('confidence', 0),
                        ai_reasoning="\n".join(analysis.get('reasoning', [])),
                        filled_price=filled_price,
                        filled_quantity=filled_qty,
                        longbridge_order_id=order_response.order_id
                    )
                    
                    # 更新盈亏
                    from .db import get_connection
                    with get_connection() as conn:
                        conn.execute("""
                            UPDATE ai_trades
                            SET pnl = ?, pnl_percent = ?
                            WHERE id = ?
                        """, (pnl, pnl_percent, trade_id))
                    
                    # 如果完全成交，删除持仓
                    if order_status in ['filled']:
                        delete_ai_position(symbol)
                        logger.info(
                            f"✅ 卖出成功: {symbol} x {filled_qty} @ ${filled_price:.2f}, "
                            f"PnL: ${pnl:.2f} ({pnl_percent:+.2f}%)"
                        )
                        
                        await self._broadcast({
                            'type': 'log',
                            'data': {'message': f'🎉 卖出成功: {symbol} x {filled_qty} @ ${filled_price:.2f} (盈亏: ${pnl:.2f} / {pnl_percent:+.2f}%)'}
                        })
                    else:
                        await self._broadcast({
                            'type': 'log',
                            'data': {'message': f'⏳ 订单状态: {symbol} - {order_status} (成交{filled_qty}/{quantity})'}
                        })
                    
                    # 更新分析状态
                    update_analysis_trigger_status(analysis_id, True, trade_id)
                    
                else:
                    # 订单失败
                    logger.error(f"❌ 订单失败: {order_response.error_message}")
                    
                    await self._broadcast({
                        'type': 'log',
                        'data': {'message': f'❌ 订单失败: {symbol} - {order_response.error_message}'}
                    })
                    
                    save_ai_trade(
                        analysis_id=analysis_id,
                        symbol=symbol,
                        action='SELL',
                        order_type='MARKET',
                        order_quantity=quantity,
                        status='FAILED',
                        ai_confidence=analysis.get('confidence', 0),
                        ai_reasoning="\n".join(analysis.get('reasoning', [])),
                        error_message=order_response.error_message
                    )
                    
            except Exception as e:
                logger.error(f"❌ 下单异常: {e}", exc_info=True)
                
                await self._broadcast({
                    'type': 'log',
                    'data': {'message': f'❌ 下单异常: {symbol} - {str(e)}'}
                })
                
                save_ai_trade(
                    analysis_id=analysis_id,
                    symbol=symbol,
                    action='SELL',
                    order_type='MARKET',
                    order_quantity=quantity,
                    status='FAILED',
                    ai_confidence=analysis.get('confidence', 0),
                    ai_reasoning="\n".join(analysis.get('reasoning', [])),
                    error_message=str(e)
                )
        else:
            # 模拟交易模式
            logger.info(f"💸 模拟卖出: {symbol} x {quantity} @ ${price:.2f}")
            
            await self._broadcast({
                'type': 'log',
                'data': {'message': f'💸 模拟卖出: {symbol} x {quantity} @ ${price:.2f}'}
            })
            
            trade_id = save_ai_trade(
                analysis_id=analysis_id,
                symbol=symbol,
                action='SELL',
                order_type='MARKET',
                order_quantity=quantity,
                order_price=price,
                status='SIMULATED',
                ai_confidence=analysis.get('confidence', 0),
                ai_reasoning="\n".join(analysis.get('reasoning', [])),
                filled_price=price,
                filled_quantity=quantity,
                longbridge_order_id=f"SIMULATED_{datetime.now().strftime('%Y%m%d%H%M%S')}"
            )
            
            # 计算盈亏
            pnl = (price - avg_cost) * quantity
            pnl_percent = (price / avg_cost - 1) * 100
            
            # 更新交易记录的盈亏
            from .db import get_connection
            with get_connection() as conn:
                conn.execute("""
                    UPDATE ai_trades
                    SET pnl = ?, pnl_percent = ?
                    WHERE id = ?
                """, (pnl, pnl_percent, trade_id))
            
            # 删除持仓
            delete_ai_position(symbol)
            
            # 更新分析状态
            update_analysis_trigger_status(analysis_id, True, trade_id)
            
            logger.info(
                f"✅ 模拟卖出完成: {symbol}, "
                f"PnL: ${pnl:.2f} ({pnl_percent:+.2f}%)"
            )
            
            await self._broadcast({
                'type': 'log',
                'data': {'message': f'✅ 模拟卖出完成: {symbol} (盈亏: ${pnl:.2f} / {pnl_percent:+.2f}%)'}
            })
    
    def _calculate_buy_quantity(
        self,
        symbol: str,
        analysis: Dict
    ) -> int:
        """计算买入数量"""
        if not self.config:
            return 0
        
        method = self.config.get('position_sizing_method', 'fixed_amount')
        
        if method == 'fixed_amount':
            # 固定金额
            amount = self.config.get('fixed_amount_per_trade', 10000)
            price = analysis.get('entry_price_max', 0)
            if price > 0:
                return int(amount / price)
        
        elif method == 'ai_advice':
            # 使用 AI 建议
            return analysis.get('position_size_advice', 100)
        
        # 默认 100 股
        return 100
    
    async def _update_positions(self):
        """更新所有持仓的当前价格和盈亏"""
        positions = get_ai_positions()
        for symbol, pos in positions.items():
            try:
                # 获取最新价格
                klines = await self._get_klines(symbol, count=1)
                if klines:
                    current_price = klines[-1].get('close', 0)
                    unrealized_pnl = (current_price - pos['avg_cost']) * pos['quantity']
                    unrealized_pnl_percent = (current_price / pos['avg_cost'] - 1) * 100
                    
                    # 更新持仓
                    update_ai_position(
                        symbol=symbol,
                        current_price=current_price,
                        unrealized_pnl=unrealized_pnl,
                        unrealized_pnl_percent=unrealized_pnl_percent
                    )
                    
                    logger.debug(
                        f"持仓更新: {symbol} @ ${current_price:.2f}, "
                        f"盈亏: ${unrealized_pnl:.2f} ({unrealized_pnl_percent:+.2f}%)"
                    )
            except Exception as e:
                logger.error(f"Error updating position for {symbol}: {e}")


# 全局引擎实例
_ai_trading_engine: Optional[AiTradingEngine] = None


def get_ai_trading_engine() -> AiTradingEngine:
    """获取 AI 交易引擎单例"""
    global _ai_trading_engine
    if _ai_trading_engine is None:
        _ai_trading_engine = AiTradingEngine()
    return _ai_trading_engine
