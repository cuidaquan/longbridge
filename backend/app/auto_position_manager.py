"""
自动仓位管理引擎
自动识别持仓，智能决策买卖操作
"""
import asyncio
import logging
from typing import Dict, List, Optional
from datetime import datetime

from .services import get_positions, get_account_balance
from .repositories import (
    load_ai_credentials,
    fetch_latest_prices,
    get_ai_trading_config,
)
from .position_calculator import PositionCalculator, PositionSizeMethod
from .ai_analyzer import DeepSeekAnalyzer

logger = logging.getLogger(__name__)


class AutoPositionManager:
    """自动仓位管理器"""
    
    def __init__(self):
        self.running = False
        self.task: Optional[asyncio.Task] = None
        self.analyzer: Optional[DeepSeekAnalyzer] = None
        self.check_interval_minutes = 30  # 默认30分钟检查一次
        self.config: Dict = {}
        self.recent_logs: List[str] = []  # 存储最近50条运行日志
        self.max_logs = 50  # 最多保留50条日志
        
    async def start(self, config: Optional[Dict] = None):
        """启动自动仓位管理"""
        if self.running:
            logger.warning("⚠️  Auto Position Manager is already running")
            raise ValueError("自动仓位管理已在运行中")
        
        # 加载配置
        self.config = config or self._load_default_config()
        
        # 检查是否启用
        if not self.config.get('enabled', False):
            logger.info("自动仓位管理未启用")
            raise ValueError("自动仓位管理未启用，请在配置中开启")
        
        # 初始化 AI 分析器（如果启用）
        if self.config.get('use_ai_analysis', True):
            api_key = self._get_ai_api_key()
            if api_key:
                try:
                    self.analyzer = DeepSeekAnalyzer(
                        api_key=api_key,
                        model='deepseek-chat',
                        temperature=0.3
                    )
                    logger.info("✅ AI 分析器已启用")
                except Exception as e:
                    logger.warning(f"AI 分析器初始化失败: {e}，将使用规则引擎")
                    self.analyzer = None
        
        self.check_interval_minutes = self.config.get('check_interval_minutes', 30)
        self.running = True
        self.task = asyncio.create_task(self._run_loop())
        logger.info(f"🤖 自动仓位管理已启动（检查间隔: {self.check_interval_minutes} 分钟）")
        
    async def stop(self):
        """停止自动仓位管理"""
        self.running = False
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        logger.info("🛑 自动仓位管理已停止")
        
    def is_running(self) -> bool:
        """检查是否运行中"""
        return self.running
    
    def get_recent_logs(self) -> List[str]:
        """获取最近的运行日志"""
        return self.recent_logs.copy()
    
    def _add_log(self, message: str):
        """添加日志到recent_logs"""
        from datetime import datetime
        timestamp = datetime.now().strftime('%H:%M:%S')
        log_entry = f"[{timestamp}] {message}"
        self.recent_logs.append(log_entry)
        # 保持最多max_logs条记录
        if len(self.recent_logs) > self.max_logs:
            self.recent_logs = self.recent_logs[-self.max_logs:]
        logger.info(message)
        
    async def _run_loop(self):
        """主循环 - 定期检查和调整仓位"""
        from datetime import datetime
        
        self._add_log("=" * 60)
        self._add_log("🤖 自动仓位管理系统已启动")
        self._add_log(f"检查间隔: {self.check_interval_minutes}分钟 | 止损: {self.config.get('auto_stop_loss_percent', -5.0)}% | 止盈: {self.config.get('auto_take_profit_percent', 15.0)}%")
        self._add_log(f"AI分析: {'✅' if self.config.get('use_ai_analysis', False) else '❌'} | 真实交易: {'⚠️ 已启用' if self.config.get('enable_real_trading', False) else '模拟模式'}")
        self._add_log("=" * 60)
        
        check_count = 0
        
        while self.running:
            try:
                check_count += 1
                self._add_log("")
                self._add_log(f"⏰ 第 {check_count} 轮检查 - {datetime.now().strftime('%H:%M:%S')}")
                
                # 获取当前持仓和账户信息
                self._add_log("📊 获取持仓信息...")
                positions = await asyncio.to_thread(get_positions)
                self._add_log(f"✅ 发现 {len(positions)} 个持仓")
                
                self._add_log("💰 获取账户余额...")
                account_balance = await asyncio.to_thread(get_account_balance)
                self._add_log(f"✅ 可用资金: ${account_balance.get('total_cash', 0):.2f}")
                
                # 分析并调整仓位
                self._add_log("🔍 开始分析持仓...")
                await self._analyze_and_adjust_positions(positions, account_balance)
                
                self._add_log(f"✅ 第 {check_count} 轮完成，{self.check_interval_minutes}分钟后再检查")
                
                # 等待下一轮检查
                logger.info(f"💤 等待 {self.check_interval_minutes} 分钟后再次检查...")
                await asyncio.sleep(self.check_interval_minutes * 60)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"仓位检查出错: {e}", exc_info=True)
                await asyncio.sleep(60)  # 出错后等待1分钟再试
                
    async def _analyze_and_adjust_positions(
        self,
        positions: List[Dict],
        account_balance: Dict
    ):
        """分析并调整仓位"""
        if not positions:
            logger.info("📭 当前无持仓，跳过分析")
            return
            
        # 创建仓位计算器
        calculator = PositionCalculator(
            account_balance=account_balance,
            current_positions=positions
        )
        
        # 获取所有持仓股票代码
        symbols = [pos.get('symbol', '') for pos in positions if pos.get('symbol')]
        
        # 获取最新价格
        prices = fetch_latest_prices(symbols)
        
        # 分析每个持仓
        for idx, position in enumerate(positions, 1):
            if not self.running:
                break
                
            symbol = position.get('symbol', '')
            if not symbol:
                continue
            
            self._add_log(f"📌 [{idx}/{len(positions)}] {symbol}")
                
            try:
                await self._analyze_single_position(
                    symbol=symbol,
                    position=position,
                    current_price=prices.get(symbol, {}).get('price', 0),
                    calculator=calculator
                )
            except Exception as e:
                self._add_log(f"❌ {symbol} 分析出错: {str(e)}")
                logger.error(f"分析 {symbol} 时出错: {e}", exc_info=True)
                
            # 避免请求过快
            await asyncio.sleep(1)
            
    async def _analyze_single_position(
        self,
        symbol: str,
        position: Dict,
        current_price: float,
        calculator: PositionCalculator
    ):
        """分析单个持仓并决定操作"""
        qty = float(position.get('qty', 0) or 0)
        avg_price = float(position.get('avg_price', 0) or 0)
        
        if qty <= 0 or avg_price <= 0 or current_price <= 0:
            return
            
        # 计算盈亏
        pnl_percent = (current_price / avg_price - 1) * 100
        market_value = qty * current_price
        
        self._add_log(f"   成本${avg_price:.2f} → 现价${current_price:.2f} ({pnl_percent:+.2f}%)")
        
        # 决策逻辑
        action = await self._make_decision(
            symbol=symbol,
            position=position,
            current_price=current_price,
            pnl_percent=pnl_percent,
            market_value=market_value
        )
        
        if action == 'SELL':
            self._add_log(f"   💸 决策: 卖出")
            # 执行卖出
            await self._execute_sell(
                symbol=symbol,
                position=position,
                current_price=current_price,
                calculator=calculator,
                reason=f"盈亏: {pnl_percent:+.2f}%"
            )
        elif action == 'BUY':
            self._add_log(f"   💰 决策: 加仓")
            # 执行加仓
            await self._execute_buy(
                symbol=symbol,
                current_price=current_price,
                calculator=calculator,
                reason="补仓操作"
            )
        else:
            self._add_log(f"   ✅ 决策: 保持")
            
    async def _make_decision(
        self,
        symbol: str,
        position: Dict,
        current_price: float,
        pnl_percent: float,
        market_value: float
    ) -> str:
        """
        决策买卖操作
        返回: 'BUY', 'SELL', 'HOLD'
        """
        # 规则1: 止损
        stop_loss_threshold = self.config.get('auto_stop_loss_percent', -5.0)
        if pnl_percent <= stop_loss_threshold:
            logger.warning(f"⚠️  {symbol} 触发止损: {pnl_percent:+.2f}% <= {stop_loss_threshold}%")
            return 'SELL'
            
        # 规则2: 止盈
        take_profit_threshold = self.config.get('auto_take_profit_percent', 15.0)
        if pnl_percent >= take_profit_threshold:
            logger.info(f"💰 {symbol} 触发止盈: {pnl_percent:+.2f}% >= {take_profit_threshold}%")
            return 'SELL'
            
        # 规则3: AI 分析（如果启用）
        if self.analyzer and self.config.get('use_ai_analysis', True):
            try:
                ai_decision = await self._get_ai_decision(symbol, position, current_price)
                if ai_decision:
                    return ai_decision
            except Exception as e:
                logger.error(f"AI 分析失败: {e}")
                
        # 规则4: 跌太多时考虑补仓
        rebalance_threshold = self.config.get('auto_rebalance_percent', -10.0)
        if pnl_percent <= rebalance_threshold:
            max_position_value = self.config.get('max_position_value', 50000)
            if market_value < max_position_value:
                logger.info(f"📈 {symbol} 考虑补仓: {pnl_percent:+.2f}% <= {rebalance_threshold}%")
                return 'BUY'
                
        return 'HOLD'
        
    async def _get_ai_decision(
        self,
        symbol: str,
        position: Dict,
        current_price: float
    ) -> Optional[str]:
        """使用 AI 分析做决策"""
        try:
            # 获取 K 线数据
            from .services import get_cached_candlesticks
            klines = await asyncio.to_thread(get_cached_candlesticks, symbol, 'day', 60)
            
            if not klines or len(klines) < 20:
                return None
                
            # AI 分析（专注卖出时机和风险控制）
            analysis = await asyncio.to_thread(
                self.analyzer.analyze_trading_opportunity,
                symbol,
                klines,
                {symbol: position},
                "sell_focus",
            )
            
            action = analysis.get('action', 'HOLD')
            confidence = analysis.get('confidence', 0)
            min_confidence = self.config.get('min_ai_confidence', 0.7)
            
            if confidence >= min_confidence:
                logger.info(
                    f"🤖 AI 建议: {symbol} -> {action} "
                    f"(信心度: {confidence:.2%})"
                )
                return action
                
        except Exception as e:
            logger.error(f"AI 分析出错: {e}")
            
        return None
        
    async def _execute_sell(
        self,
        symbol: str,
        position: Dict,
        current_price: float,
        calculator: PositionCalculator,
        reason: str
    ):
        """执行卖出操作"""
        qty = float(position.get('qty', 0) or 0)
        
        # 计算卖出比例
        sell_ratio = self.config.get('sell_ratio', 1.0)  # 默认全卖
        sell_qty = int(qty * sell_ratio)
        
        if sell_qty <= 0:
            return
            
        logger.info(f"💸 准备卖出: {symbol} x {sell_qty} @ ${current_price:.2f}")
        logger.info(f"   原因: {reason}")
        
        # 如果只是模拟模式
        if not self.config.get('enable_real_trading', False):
            logger.info(f"⚠️  模拟模式 - 不执行实际交易")
            self._record_trade('SELL', symbol, sell_qty, current_price, reason, 'SIMULATION', None)
            return
            
        # 真实交易模式
        try:
            from .trading_api import get_trading_api, OrderRequest, OrderSide, OrderType
            
            trading_api = get_trading_api()
            order_request = OrderRequest(
                symbol=symbol,
                order_type=OrderType.LIMIT,
                side=OrderSide.SELL,
                quantity=sell_qty,
                price=current_price
            )
            
            logger.info(f"🔄 提交卖出订单: {symbol} x {sell_qty}")
            result = await trading_api.place_order(order_request)
            
            if result.success:
                logger.info(f"✅ 卖出订单已提交: 订单ID {result.order_id}")
                self._record_trade('SELL', symbol, sell_qty, current_price, reason, 'FILLED', result.order_id)
            else:
                logger.error(f"❌ 卖出订单失败: {result.message}")
                self._record_trade('SELL', symbol, sell_qty, current_price, reason, 'FAILED', None, result.message)
                
        except Exception as e:
            logger.error(f"❌ 真实交易执行失败: {e}", exc_info=True)
            self._record_trade('SELL', symbol, sell_qty, current_price, reason, 'ERROR', None, str(e))
        
    async def _execute_buy(
        self,
        symbol: str,
        current_price: float,
        calculator: PositionCalculator,
        reason: str
    ):
        """执行买入操作"""
        # 计算买入数量
        calculation = calculator.calculate_buy_quantity(
            symbol=symbol,
            current_price=current_price,
            method=PositionSizeMethod.PERCENTAGE,
            target_allocation=self.config.get('position_allocation', 0.05),
            max_risk_per_trade=0.02,
            stop_loss_pct=0.05
        )
        
        buy_qty = calculation.quantity
        
        if buy_qty <= 0:
            logger.warning(f"计算的买入数量为 0，跳过 {symbol}")
            return
            
        logger.info(f"💰 准备买入: {symbol} x {buy_qty} @ ${current_price:.2f}")
        logger.info(f"   原因: {reason}")
        logger.info(f"   预估成本: ${calculation.estimated_cost:.2f}")
        
        # 如果只是模拟模式
        if not self.config.get('enable_real_trading', False):
            logger.info(f"⚠️  模拟模式 - 不执行实际交易")
            self._record_trade('BUY', symbol, buy_qty, current_price, reason, 'SIMULATION', None)
            return
            
        # 真实交易模式
        try:
            from .trading_api import get_trading_api, OrderRequest, OrderSide, OrderType
            
            trading_api = get_trading_api()
            order_request = OrderRequest(
                symbol=symbol,
                order_type=OrderType.LIMIT,
                side=OrderSide.BUY,
                quantity=buy_qty,
                price=current_price
            )
            
            logger.info(f"🔄 提交买入订单: {symbol} x {buy_qty}")
            result = await trading_api.place_order(order_request)
            
            if result.success:
                logger.info(f"✅ 买入订单已提交: 订单ID {result.order_id}")
                self._record_trade('BUY', symbol, buy_qty, current_price, reason, 'FILLED', result.order_id)
            else:
                logger.error(f"❌ 买入订单失败: {result.message}")
                self._record_trade('BUY', symbol, buy_qty, current_price, reason, 'FAILED', None, result.message)
                
        except Exception as e:
            logger.error(f"❌ 真实交易执行失败: {e}", exc_info=True)
            self._record_trade('BUY', symbol, buy_qty, current_price, reason, 'ERROR', None, str(e))
        
    def _record_trade(
        self,
        action: str,
        symbol: str,
        quantity: int,
        price: float,
        reason: str,
        status: str = 'SIMULATION',
        order_id: str = None,
        error_message: str = None
    ):
        """记录交易（模拟或真实）"""
        from .db import get_connection
        
        try:
            with get_connection() as conn:
                # 确保表存在（添加更多字段）
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS auto_position_trades (
                        id INTEGER PRIMARY KEY,
                        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        action TEXT NOT NULL,
                        symbol TEXT NOT NULL,
                        quantity INTEGER NOT NULL,
                        price DOUBLE NOT NULL,
                        total_value DOUBLE NOT NULL,
                        reason TEXT,
                        status TEXT DEFAULT 'SIMULATION',
                        order_id TEXT,
                        error_message TEXT
                    )
                """)
                
                next_id_row = conn.execute(
                    "SELECT COALESCE(MAX(id), 0) + 1 FROM auto_position_trades"
                ).fetchone()
                next_id = int(next_id_row[0]) if next_id_row else 1

                # 插入记录
                conn.execute("""
                    INSERT INTO auto_position_trades 
                    (id, action, symbol, quantity, price, total_value, reason, status, order_id, error_message)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    next_id,
                    action,
                    symbol,
                    quantity,
                    price,
                    quantity * price,
                    reason,
                    status,
                    order_id,
                    error_message
                ))
                
                logger.info(f"✅ 交易已记录: {status}")
        except Exception as e:
            logger.error(f"记录交易失败: {e}", exc_info=True)
            
    def _load_default_config(self) -> Dict:
        """加载默认配置"""
        # 尝试从数据库加载
        from .repositories import get_connection
        
        try:
            with get_connection() as conn:
                # 确保表存在
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS auto_position_config (
                        id INTEGER PRIMARY KEY,
                        enabled BOOLEAN DEFAULT false,
                        check_interval_minutes INTEGER DEFAULT 30,
                        use_ai_analysis BOOLEAN DEFAULT true,
                        min_ai_confidence DOUBLE DEFAULT 0.7,
                        auto_stop_loss_percent DOUBLE DEFAULT -5.0,
                        auto_take_profit_percent DOUBLE DEFAULT 15.0,
                        auto_rebalance_percent DOUBLE DEFAULT -10.0,
                        max_position_value DOUBLE DEFAULT 50000,
                        position_allocation DOUBLE DEFAULT 0.05,
                        sell_ratio DOUBLE DEFAULT 1.0,
                        enable_real_trading BOOLEAN DEFAULT false,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                
                # 读取配置
                row = conn.execute("SELECT * FROM auto_position_config WHERE id = 1").fetchone()
                
                if row:
                    columns = [desc[0] for desc in conn.description]
                    return dict(zip(columns, row))
                    
        except Exception as e:
            logger.error(f"加载配置失败: {e}")
            
        # 返回默认值
        return {
            'enabled': False,
            'check_interval_minutes': 30,
            'use_ai_analysis': True,
            'min_ai_confidence': 0.7,
            'auto_stop_loss_percent': -5.0,
            'auto_take_profit_percent': 15.0,
            'auto_rebalance_percent': -10.0,
            'max_position_value': 50000,
            'position_allocation': 0.05,
            'sell_ratio': 1.0,
            'enable_real_trading': False,
        }
        
    def _get_ai_api_key(self) -> Optional[str]:
        """获取 AI API Key"""
        # 优先从 settings 表读取
        ai_creds = load_ai_credentials()
        api_key = ai_creds.get('DEEPSEEK_API_KEY', '').strip()
        
        # 如果没有，从 ai_trading_config 读取
        if not api_key:
            config = get_ai_trading_config()
            if config:
                api_key = config.get('ai_api_key', '').strip()
                
        return api_key if api_key else None


# 全局实例
_auto_position_manager: Optional[AutoPositionManager] = None


def get_auto_position_manager() -> AutoPositionManager:
    """获取自动仓位管理器单例"""
    global _auto_position_manager
    if _auto_position_manager is None:
        _auto_position_manager = AutoPositionManager()
    return _auto_position_manager
