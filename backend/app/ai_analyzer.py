"""
AI 分析器 - 使用 DeepSeek 分析 K 线数据并给出交易决策
"""
from typing import Dict, List, Optional
import json
import logging
import os
from datetime import datetime

try:
    from openai import OpenAI
    HAS_OPENAI = True
except ImportError:
    HAS_OPENAI = False
    OpenAI = None

import pandas as pd
import numpy as np

from .external_service_resilience import run_external_call

logger = logging.getLogger(__name__)


def calculate_technical_indicators(klines: List[Dict]) -> Dict:
    """Build the shared technical snapshot used by quant scoring and AI prompts."""
    if not klines or len(klines) < 20:
        return {}

    try:
        df = pd.DataFrame(klines)
        for col in ['open', 'high', 'low', 'close', 'volume']:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce')

        indicators = {}
        if len(df) >= 5:
            ma5_val = df['close'].rolling(5).mean().iloc[-1]
            indicators['ma5'] = float(ma5_val) if not pd.isna(ma5_val) else 0.0
        if len(df) >= 10:
            ma10_val = df['close'].rolling(10).mean().iloc[-1]
            indicators['ma10'] = float(ma10_val) if not pd.isna(ma10_val) else 0.0
        if len(df) >= 20:
            ma20_val = df['close'].rolling(20).mean().iloc[-1]
            indicators['ma20'] = float(ma20_val) if not pd.isna(ma20_val) else 0.0
        if len(df) >= 60:
            ma60_val = df['close'].rolling(60).mean().iloc[-1]
            indicators['ma60'] = float(ma60_val) if not pd.isna(ma60_val) else None

        if len(df) >= 14:
            delta = df['close'].diff()
            gain = (delta.where(delta > 0, 0)).rolling(14).mean()
            loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
            rs = gain / loss
            rsi = 100 - (100 / (1 + rs))
            indicators['rsi'] = float(rsi.iloc[-1]) if not pd.isna(rsi.iloc[-1]) else 50.0

        if len(df) >= 26:
            exp1 = df['close'].ewm(span=12).mean()
            exp2 = df['close'].ewm(span=26).mean()
            macd = exp1 - exp2
            signal = macd.ewm(span=9).mean()
            macd_val = macd.iloc[-1]
            signal_val = signal.iloc[-1]
            indicators['macd'] = float(macd_val) if not pd.isna(macd_val) else 0.0
            indicators['macd_signal'] = float(signal_val) if not pd.isna(signal_val) else 0.0
            hist_val = (macd - signal).iloc[-1]
            indicators['macd_histogram'] = float(hist_val) if not pd.isna(hist_val) else 0.0

        if len(df) >= 20:
            sma20 = df['close'].rolling(20).mean()
            std20 = df['close'].rolling(20).std()
            bb_upper = (sma20 + 2 * std20).iloc[-1]
            bb_middle = sma20.iloc[-1]
            bb_lower = (sma20 - 2 * std20).iloc[-1]
            indicators['bollinger_upper'] = float(bb_upper) if not pd.isna(bb_upper) else 0.0
            indicators['bollinger_middle'] = float(bb_middle) if not pd.isna(bb_middle) else 0.0
            indicators['bollinger_lower'] = float(bb_lower) if not pd.isna(bb_lower) else 0.0

        if len(df) >= 5:
            volume_ma5 = df['volume'].rolling(5).mean().iloc[-1]
            if not pd.isna(volume_ma5) and volume_ma5 > 0:
                indicators['volume_ma5'] = float(volume_ma5)
                curr_vol = df['volume'].iloc[-1]
                indicators['volume_ratio'] = (
                    float(curr_vol / volume_ma5) if not pd.isna(curr_vol) else 1.0
                )
            else:
                indicators['volume_ratio'] = 1.0

        if len(df) >= 2:
            close_1 = df['close'].iloc[-1]
            close_2 = df['close'].iloc[-2]
            indicators['price_change_1d'] = (
                float((close_1 / close_2 - 1) * 100)
                if not pd.isna(close_1) and not pd.isna(close_2) and close_2 > 0
                else 0.0
            )
        if len(df) >= 6:
            close_1 = df['close'].iloc[-1]
            close_6 = df['close'].iloc[-6]
            indicators['price_change_5d'] = (
                float((close_1 / close_6 - 1) * 100)
                if not pd.isna(close_1) and not pd.isna(close_6) and close_6 > 0
                else 0.0
            )

        curr_price = df['close'].iloc[-1]
        indicators['current_price'] = float(curr_price) if not pd.isna(curr_price) else 0.0
        return indicators
    except Exception as exc:
        logger.error(f"计算指标失败: {exc}")
        return {}


class DeepSeekAnalyzer:
    """DeepSeek AI 分析器 - 集成新闻舆情"""
    
    def __init__(
        self, 
        api_key: str, 
        model: str = "deepseek-chat", 
        temperature: float = 0.3, 
        base_url: str = "https://api.deepseek.com",
        tavily_api_key: Optional[str] = None
    ):
        if not HAS_OPENAI:
            raise ImportError("需要安装 openai 库: pip install openai")
        
        if not api_key:
            raise ValueError("必须提供 DeepSeek API Key")
        
        self.client = OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=25.0,
            max_retries=0,
        )
        self.model = model
        self.temperature = temperature
        # 🎨 支持战术型分析风格（通过环境变量或配置控制）
        self.style = os.getenv('AI_ANALYSIS_STYLE', 'professional')  # 'professional' or 'tactical'
        logger.info(f"🎨 AI 分析风格: {self.style}")
        
        # 🔍 集成新闻分析器
        self.news_analyzer = None
        if tavily_api_key:
            try:
                from .news_analyzer import get_news_analyzer
                self.news_analyzer = get_news_analyzer(tavily_api_key)
                if self.news_analyzer:
                    logger.info("✅ 新闻分析器已集成")
            except Exception as e:
                logger.warning(f"⚠️ 新闻分析器初始化失败: {e}")
    
    def analyze_trading_opportunity(
        self,
        symbol: str,
        klines: List[Dict],
        current_positions: Optional[Dict] = None,
        scenario: str = "general",
        technical_indicators: Optional[Dict] = None,
        quant_score: Optional[Dict] = None,
    ) -> Dict:
        """
        分析交易机会
        
        Args:
            symbol: 股票代码
            klines: K线数据列表
            current_positions: 当前持仓情况
            scenario: 分析场景
                - general: 全面分析（默认）
                - buy_focus: 专注买入机会（AI交易用）
                - sell_focus: 专注卖出时机（智能持仓用）
        
        Returns:
            {
                "action": "BUY/SELL/HOLD",
                "confidence": 0.85,
                "reasoning": ["理由1", "理由2", ...],
                "entry_price_min": 320.0,
                "entry_price_max": 322.0,
                "stop_loss": 315.0,
                "take_profit": 330.0,
                "risk_level": "MEDIUM",
                "position_size_advice": 1000,
                "indicators": {...},
                "score": {
                    "total": 75,
                    "breakdown": {...}
                }
            }
        """
        indicators = dict(technical_indicators or {})
        score = dict(quant_score or {})
        try:
            # 1. 复用调用方快照；其他业务未传入时保持向后兼容。
            if not indicators:
                indicators = calculate_technical_indicators(klines)
            
            # 2. 🔍 获取新闻分析（如果启用）
            news_analysis = None
            if self.news_analyzer:
                try:
                    logger.info(f"🔍 获取{symbol}的新闻分析...")
                    news_analysis = self.news_analyzer.search_stock_news(
                        symbol=symbol,
                        days=7  # 最近7天
                    )
                    logger.info(f"✅ 新闻分析完成: {news_analysis['news_count']}条新闻")
                except Exception as e:
                    logger.warning(f"⚠️ 新闻分析失败: {e}")
                    news_analysis = None
            
            # 3. 选股业务传入统一机会分；其他业务继续使用分析器内置评分。
            if not score:
                score = self._calculate_score(klines, indicators, scenario, news_analysis)
            
            # 4. 构建提示词（包含新闻信息）
            prompt = self._build_prompt(symbol, klines, indicators, current_positions, scenario, score, news_analysis)
            
            # 4. 调用 DeepSeek
            logger.info(f"🤖 调用 DeepSeek 分析 {symbol} (场景: {scenario})...")
            
            response = run_external_call(
                "ai",
                "chat_completion",
                self.client.chat.completions.create,
                model=self.model,
                messages=[
                    {
                        "role": "system",
                        "content": self._get_system_prompt(scenario),
                    },
                    {
                        "role": "user",
                        "content": prompt,
                    },
                ],
                temperature=self.temperature,
                response_format={"type": "json_object"},  # 强制返回 JSON
            )
            
            ai_response = response.choices[0].message.content
            result = self._parse_ai_response(ai_response, klines[-1].get('close', 0))
            
            # 添加指标和评分到结果中
            result['indicators'] = indicators
            result['score'] = score
            result['ai_status'] = 'available'
            result['ai_raw_response'] = ai_response
            result['ai_prompt'] = prompt
            
            logger.info(
                f"✅ AI 决策: {symbol} -> {result['action']} "
                f"(信心度: {result['confidence']:.2%}, 量化评分: {score['total']}/100)"
            )
            
            return result
            
        except Exception as e:
            logger.error(f"❌ AI 分析失败 {symbol}: {e}", exc_info=True)
            return {
                "action": "HOLD",
                "confidence": 0.0,
                "reasoning": [f"AI 分析失败: {str(e)}"],
                "error": str(e),
                "ai_status": "error",
                "indicators": indicators,
                "score": score,
            }
    
    def _calculate_indicators(self, klines: List[Dict]) -> Dict:
        """Backward-compatible wrapper for the shared indicator builder."""
        return calculate_technical_indicators(klines)
    
    def _calculate_score(
        self, 
        klines: List[Dict], 
        indicators: Dict, 
        scenario: str = "general",
        news_analysis: Optional[Dict] = None
    ) -> Dict:
        """
        计算量化评分（0-100分）- 🆕 集成新闻舆情 V3.1
        
        评分维度（舆情增强版）：
        1. 趋势评分（15分）- MA排列、价格位置 ⬇️
        2. 动量评分（18分）- RSI、MACD ⬇️
        3. 波动评分（25分）- 布林带、振幅 ⬆️ 保持
        4. 量能评分（12分）- 成交量 ⬇️
        5. K线形态评分（10分）- 最近K线形态
        6. 新闻舆情评分（20分）- 新闻数量、情绪、影响 ⬆️⬆️ 翻倍
        
        Returns:
            {
                "total": 75,
                "breakdown": {
                    "trend": 16,
                    "momentum": 18,
                    "volatility": 22,
                    "volume": 12,
                    "pattern": 8,
                    "news": 9
                },
                "signals": ["MA5>MA20", "MACD金叉", "正面新闻", ...],
                "grade": "B"  # A(80+), B(60-79), C(40-59), D(<40)
            }
        """
        if not indicators:
            return {"total": 50, "breakdown": {}, "signals": [], "grade": "C"}
        
        current_price = indicators.get('current_price', 0)
        if current_price == 0:
            return {"total": 50, "breakdown": {}, "signals": [], "grade": "C"}
        
        scores = {}
        signals = []
        
        # 1. 趋势评分（15分）⬇️ 进一步降低权重，为新闻让路
        trend_score = 0
        ma5 = indicators.get('ma5', 0)
        ma20 = indicators.get('ma20', 0)
        ma60 = indicators.get('ma60')
        
        if ma5 > 0 and ma20 > 0:
            # MA排列（8分）
            if ma5 > ma20:
                if ma60 and ma20 > ma60:
                    trend_score += 8  # 完美多头排列
                    signals.append("多头排列(MA5>MA20>MA60)")
                else:
                    trend_score += 6  # MA5>MA20
                    signals.append("短期趋势向上(MA5>MA20)")
            elif ma5 < ma20:
                trend_score += 1  # 空头排列，给低分
                signals.append("短期趋势向下(MA5<MA20)")
            else:
                trend_score += 4  # 均线粘合
            
            # 价格vs MA20位置（7分）
            price_vs_ma20 = (current_price - ma20) / ma20 * 100
            if price_vs_ma20 > 5:
                trend_score += 7  # 强势在MA20上方
                signals.append(f"价格强势(+{price_vs_ma20:.1f}%)")
            elif price_vs_ma20 > 0:
                trend_score += 6  # 在MA20上方
                signals.append(f"价格在MA20上方(+{price_vs_ma20:.1f}%)")
            elif price_vs_ma20 > -3:
                trend_score += 4  # 接近MA20
                signals.append(f"价格接近MA20({price_vs_ma20:+.1f}%)")
            elif price_vs_ma20 > -5:
                trend_score += 2  # 略低于MA20
            else:
                trend_score += 1  # 明显弱势
                signals.append(f"价格弱势({price_vs_ma20:+.1f}%)")
        
        scores['trend'] = trend_score
        
        # 2. 动量评分（18分）⬇️ 略微降低权重
        momentum_score = 0
        rsi = indicators.get('rsi', 50)
        macd = indicators.get('macd', 0)
        macd_signal = indicators.get('macd_signal', 0)
        macd_hist = indicators.get('macd_histogram', 0)
        
        # RSI评分（9分）
        if 40 <= rsi <= 60:
            momentum_score += 9  # 健康区间
            signals.append(f"RSI健康({rsi:.1f})")
        elif 30 <= rsi < 40:
            momentum_score += 7  # 超卖区反弹机会
            signals.append(f"RSI超卖反弹区({rsi:.1f})")
        elif 60 < rsi <= 70:
            momentum_score += 6  # 偏强但未超买
            signals.append(f"RSI偏强({rsi:.1f})")
        elif 25 <= rsi < 30:
            momentum_score += 5  # 深度超卖
            signals.append(f"RSI深度超卖({rsi:.1f})")
        elif 70 < rsi <= 80:
            momentum_score += 3  # 超买区
            signals.append(f"RSI超买({rsi:.1f})")
        else:
            momentum_score += 2  # 极端值
        
        # MACD评分（9分）
        if macd > macd_signal and macd_hist > 0:
            if macd_hist > 0.1:  # 相对值，可能需要根据实际调整
                momentum_score += 9  # 强势金叉
                signals.append("MACD强势金叉")
            else:
                momentum_score += 7  # 金叉
                signals.append("MACD金叉")
        elif macd < macd_signal and macd_hist < 0:
            momentum_score += 2  # 死叉
            signals.append("MACD死叉")
        elif macd > macd_signal and macd_hist < 0:
            momentum_score += 5  # 即将金叉
            signals.append("MACD收敛向上")
        else:
            momentum_score += 3  # 即将死叉
        
        scores['momentum'] = momentum_score
        
        # 3. 量能评分（12分）⬇️ 降低权重
        volume_score = 0
        volume_ratio = indicators.get('volume_ratio', 1.0)
        
        if volume_ratio >= 1.5:
            volume_score = 12  # 明显放量
            signals.append(f"明显放量({volume_ratio:.1f}x)")
        elif volume_ratio >= 1.3:
            volume_score = 10  # 放量
            signals.append(f"适度放量({volume_ratio:.1f}x)")
        elif volume_ratio >= 1.0:
            volume_score = 7  # 正常量
            signals.append("成交量正常")
        elif volume_ratio >= 0.8:
            volume_score = 5  # 略微缩量
            signals.append("略微缩量")
        else:
            volume_score = 3  # 明显缩量
            signals.append(f"明显缩量({volume_ratio:.1f}x)")
        
        scores['volume'] = volume_score
        
        # 4. 波动评分（25分）⬆️ 大幅提升权重（核心优化）
        volatility_score = 0
        
        # 4.1 历史波动率分析（10分）- 新增
        if len(klines) >= 20:
            closes = [k['close'] for k in klines[-20:]]
            returns = [((closes[i] - closes[i-1]) / closes[i-1]) for i in range(1, len(closes))]
            volatility_20d = np.std(returns) * np.sqrt(252) * 100  # 年化波动率
            
            if volatility_20d > 50:  # 高波动
                volatility_score += 10
                signals.append(f"高波动({volatility_20d:.1f}%年化)⬆️")
            elif volatility_20d > 30:  # 中等波动
                volatility_score += 7
                signals.append(f"中等波动({volatility_20d:.1f}%年化)")
            elif volatility_20d > 15:  # 低波动
                volatility_score += 4
                signals.append(f"低波动({volatility_20d:.1f}%年化)")
            else:  # 极低波动
                volatility_score += 1
                signals.append(f"波动过低({volatility_20d:.1f}%年化)⬇️")
        
        # 4.2 近期波动（8分）
        price_change_5d = abs(indicators.get('price_change_5d', 0))
        if price_change_5d > 10:  # 5日涨跌超10%
            volatility_score += 8
            signals.append(f"近期大幅波动({price_change_5d:.1f}%)⬆️")
        elif price_change_5d > 5:
            volatility_score += 6
            signals.append(f"近期明显波动({price_change_5d:.1f}%)")
        elif price_change_5d > 2:
            volatility_score += 4
            signals.append(f"近期小幅波动({price_change_5d:.1f}%)")
        else:
            volatility_score += 1
            signals.append(f"近期波动不足({price_change_5d:.1f}%)⬇️")
        
        # 4.3 振幅分析（7分）- 新增
        if len(klines) >= 5:
            recent_klines = klines[-5:]
            amplitudes = [((k['high'] - k['low']) / k['low'] * 100) for k in recent_klines if k['low'] > 0]
            if amplitudes:
                avg_amplitude = np.mean(amplitudes)
                
                if avg_amplitude > 5:  # 日均振幅5%+
                    volatility_score += 7
                    signals.append(f"振幅充足({avg_amplitude:.1f}%)⬆️")
                elif avg_amplitude > 3:
                    volatility_score += 4
                    signals.append(f"振幅一般({avg_amplitude:.1f}%)")
                else:
                    volatility_score += 1
                    signals.append(f"振幅不足({avg_amplitude:.1f}%)⬇️")
        
        scores['volatility'] = volatility_score
        
        # 5. K线形态评分（10分）⬇️ 降低权重
        pattern_score = 0
        if len(klines) >= 3:
            k1, k2, k3 = klines[-3], klines[-2], klines[-1]
            
            # 辅助函数
            def is_bullish(k):
                return k.get('close', 0) >= k.get('open', 0)
            
            def body_size(k):
                return abs(k.get('close', 0) - k.get('open', 0))
            
            def upper_shadow(k):
                return k.get('high', 0) - max(k.get('close', 0), k.get('open', 0))
            
            def lower_shadow(k):
                return min(k.get('close', 0), k.get('open', 0)) - k.get('low', 0)
            
            def full_range(k):
                return k.get('high', 0) - k.get('low', 0)
            
            # 最近一根K线分析
            last_body = body_size(k3)
            last_upper = upper_shadow(k3)
            last_lower = lower_shadow(k3)
            last_range = full_range(k3)
            
            # 看涨形态
            if is_bullish(k3):
                pattern_score += 5  # 阳线基础分
                
                # 锤子线：长下影线
                if last_range > 0 and last_lower / last_range > 0.5 and last_body > 0:
                    pattern_score += 5
                    signals.append("锤子线形态(看涨)")
                
                # 红三兵：连续三根阳线
                elif is_bullish(k1) and is_bullish(k2):
                    pattern_score += 5
                    signals.append("红三兵(看涨)")
                
                # 多方炮：阳-阴-阳
                elif is_bullish(k1) and not is_bullish(k2):
                    pattern_score += 4
                    signals.append("多方炮形态(看涨)")
                
                else:
                    pattern_score += 1  # 普通阳线
            else:
                pattern_score += 2  # 阴线基础分
                
                # 吊颈线：长上影线
                if last_range > 0 and last_upper / last_range > 0.5:
                    pattern_score -= 2  # 减分
                    signals.append("吊颈线形态(看跌)")
                
                # 黑三兵：连续三根阴线
                elif not is_bullish(k1) and not is_bullish(k2):
                    pattern_score -= 2
                    signals.append("黑三兵(看跌)")
        else:
            pattern_score = 5  # 数据不足时给中等分
        
        scores['pattern'] = max(0, min(10, pattern_score))  # 限制在0-10之间
        
        # 6. 新闻舆情评分（20分）⬆️⬆️ 翻倍权重（核心优化）
        news_score = 0
        if news_analysis:
            news_count = news_analysis.get('news_count', 0)
            sentiment_score = news_analysis.get('sentiment_score', 0)  # -1到1
            sentiment_label = news_analysis.get('sentiment_label', 'NEUTRAL')
            impact_score = news_analysis.get('impact_score', 0)  # 0-10
            
            # 基于影响分数计算，并翻倍到0-20分
            news_score = impact_score * 2
            
            # 添加信号（强调舆情重要性）
            if news_count > 0:
                if sentiment_label == "POSITIVE":
                    signals.append(f"📰 正面新闻({news_count}条, 影响{impact_score:.1f}/10, 评分{news_score:.1f}/20)⬆️⬆️")
                elif sentiment_label == "NEGATIVE":
                    signals.append(f"📰 负面新闻({news_count}条, 影响{impact_score:.1f}/10, 评分{news_score:.1f}/20)⬇️⬇️")
                else:
                    signals.append(f"📰 中性新闻({news_count}条, 评分{news_score:.1f}/20)")
            else:
                signals.append("🔍 无相关新闻（0分）")
        else:
            # 没有新闻分析时给10分（中性，20分的一半）
            news_score = 10
            signals.append("🔍 未启用新闻分析（默认10/20分）")
        
        scores['news'] = news_score
        
        # 计算总分
        total_score = sum(scores.values())
        
        # 根据场景调整（buy_focus更关注买入信号）
        if scenario == "buy_focus":
            # 如果趋势和动量都不错，额外加分
            if scores['trend'] >= 16 and scores['momentum'] >= 16:
                total_score = min(100, total_score + 5)
                signals.append("多因子共振(加分)")
            # 如果波动性高且有正面新闻，额外加分
            if scores.get('volatility', 0) >= 20 and scores.get('news', 0) >= 7:
                total_score = min(100, total_score + 3)
                signals.append("高波动+正面新闻(加分)⬆️")
        
        # 评级
        if total_score >= 80:
            grade = "A"
        elif total_score >= 65:
            grade = "B"
        elif total_score >= 50:
            grade = "C"
        else:
            grade = "D"
        
        return {
            "total": round(total_score, 1),
            "breakdown": scores,
            "signals": signals,
            "grade": grade
        }
    
    def _get_system_prompt(self, scenario: str = "general") -> str:
        """系统提示词 - 根据场景定制"""
        
        # 🎨 如果是战术型风格，使用战术型 Prompt
        if self.style == 'tactical':
            return self._get_tactical_system_prompt(scenario)
        
        base_prompt = """你是一个专业的量化交易分析师和自动交易系统。

【角色定位】
- 专业量化分析师
- K线形态识别专家
- 风险管理专家
- 自动化交易系统

【K线形态识别能力】
你必须识别以下K线形态：

🟢 看涨形态：
- 锤子线/倒锤子线：下影线长（≥2倍实体），实体小，出现在底部
- 早晨之星：三根K线，先跌后涨的转折（第一根阴线，第二根小实体，第三根阳线）
- 多方炮：两阳夹一阴，突破形态
- 红三兵：三根阳线，逐步走高，成交量递增
- 上升趋势线突破：价格突破趋势线阻力，伴随放量

🔴 看跌形态：
- 吊颈线/射击之星：上影线长（≥2倍实体），实体小，出现在顶部
- 黄昏之星：三根K线，先涨后跌的转折（第一根阳线，第二根小实体，第三根阴线）
- 空方炮：两阴夹一阳，下跌加速形态
- 黑三兵：三根阴线，逐步走低，成交量放大
- 下降趋势线跌破：价格跌破支撑线，伴随放量

⚖️ 中性形态：
- 十字星：开盘等于收盘，多空平衡
- 孕线：小K线被前一根大K线包含，盘整信号
- 平顶/平底：多根K线高点/低点相同，强阻力/支撑

【分析原则】
1. K线形态识别：首先识别K线形态，这是重要的市场信号
2. 多因子参考：综合考虑技术指标，至少 2 个指标支持即可
3. 趋势判断：优先顺应主趋势，但也要捕捉反转机会
4. 量能观察：价格突破配合量能更佳（但非必需）
5. 风险控制：设置合理止损，在风险可控下积极捕捉机会

【信心度标准】（调整后更实用）
- 0.85-1.0: 多个强烈信号共振（K线形态+技术指标+量能）→ 强烈推荐
- 0.70-0.85: 明确信号，2个以上指标支持 → 推荐交易
- 0.60-0.70: 信号存在但不够强烈 → 可以尝试小仓位
- <0.60: 信号矛盾或不明确 → 建议 HOLD

【买入条件】（至少满足 2 条即可考虑）
✓ 趋势：价格接近或突破MA20，或在关键支撑位获得支撑
✓ 动量：MACD 即将金叉/已金叉，或 RSI 从超卖反弹（25-55区间）
✓ 量能：成交量放大（1.3倍以上），或虽正常但价格形态良好
✓ 形态：出现看涨K线形态（锤子线、早晨之星、红三兵等）
✓ 位置：价格在布林带中下轨，或回踩重要支撑位

【卖出条件】（满足任一条）
✗ 止损触发：价格跌破止损位
✗ 止盈触发：达到目标收益
✗ 趋势反转：MACD 死叉 + 跌破 MA20
✗ 量能衰竭：价格滞涨 + 成交量萎缩
✗ RSI 超买（>70）且出现顶背离

"""
        
        # 根据场景添加专门的指导
        if scenario == "buy_focus":
            scenario_guide = """

【当前任务：寻找买入机会】🎯

你的职责是积极发现买入机会！重点关注：

1. 📊 K线形态：是否出现看涨信号（锤子线、早晨之星、红三兵、多方炮等）？
2. 📈 趋势判断：是否处于上升趋势，或出现反转迹象？
3. 💪 量能观察：成交量是否配合（放量更好，正常量也可接受）？
4. 🎯 支撑位：是否在关键支撑位获得支撑？价格是否合理？
5. 📉 超卖机会：RSI是否在超卖区反弹，或处于中性偏低位置(<55)？

买入信号强度判断（实用标准）：
- 高信心(0.85-1.0)：看涨K线形态 + MACD金叉 + 放量 → **强烈推荐BUY**
- 中信心(0.70-0.84)：有看涨形态或2个技术指标支持 → **推荐BUY**  
- 低信心(0.60-0.69)：有一定看涨迹象但不够强 → **可尝试小仓位BUY**
- 极低(<0.60)：信号矛盾或明显看跌 → **返回HOLD**

⚠️ 重要原则：
- 这是自动交易系统，需要积极捕捉机会（不要过于保守）
- 信心度 ≥ 0.70 时应该给出BUY建议（有2个指标支持即可）
- 只要不是明显的下跌趋势，都可以考虑买入机会
- K线形态识别是重要参考，但不是唯一标准
- 合理设置止损止盈，控制风险即可
"""
        
        elif scenario == "sell_focus":
            scenario_guide = """

【当前任务：持仓风险控制】🛡️

你的职责是管理已有持仓的止盈止损！重点关注：

1. 🚨 K线形态：是否出现看跌反转信号（吊颈线、黄昏之星、黑三兵等）？
2. 📉 趋势转折：是否跌破上升趋势，进入下跌通道？
3. ⚠️ 量能异常：是否出现放量下跌，恐慌性抛售？
4. 💥 阻力位：是否在阻力位遇阻回落？
5. 🔥 超买风险：RSI是否超买(>70)且出现顶背离？
6. 💰 盈亏状况：当前持仓的盈亏情况如何？

卖出信号强度判断：
- 高信心(0.8-1.0)：看跌K线形态 + MACD死叉 + 放量下跌 + 跌破支撑
- 中信心(0.75-0.8)：有看跌形态 + 部分技术指标支持
- 低信心(<0.75)：信号矛盾或不明确 → **建议 HOLD 继续持有**

特别提醒：
- 这是已有持仓，重点是保护利润和控制风险
- 如果亏损接近 -5%，即使信号不强也应考虑止损
- 如果盈利超过 +15%，即使信号不强也可考虑止盈
- K线形态反转是最重要的卖出信号
"""
        
        else:  # general
            scenario_guide = """

【全面分析】
综合评估买入和卖出机会，给出最优建议。
重点关注K线形态、技术指标和成交量的共振。
"""
        
        return base_prompt + scenario_guide + """

【分析流程要求】⭐ V2.0 升级
你必须按照以下步骤进行系统性分析：

STEP 1: 市场环境识别
- 当前是趋势市场还是震荡市场？
- 整体风险偏好如何？

STEP 2: 技术指标时间序列分析
- 观察指标序列的变化趋势（而非单点数值）
- MA、MACD、RSI 的动态演变
- 寻找指标共振信号

STEP 3: K线形态识别
- 识别最近3-5根K线的组合形态
- 判断是否出现反转或持续信号

STEP 4: 持仓管理评估（如有持仓）
- 当前盈亏状态
- 是否触发止损/止盈条件
- 失效条件是否满足

STEP 5: 风险收益评估
- 入场价格区间合理性
- 止损止盈设置
- 风险收益比计算（建议≥2:1）

STEP 6: 信心度评级
- 根据信号强度给出0-1的信心度
- 信心度必须有充分依据

【输出格式】⭐ V2.0 双输出模式
必须返回 JSON，包含两个部分：

{
  "chain_of_thought": "展示你的完整思考过程，包括：1) 市场环境判断 2) 技术指标分析（观察序列趋势）3) K线形态识别 4) 持仓评估（如有）5) 风险收益计算 6) 最终决策理由。请详细写出每一步的推理逻辑。",
  
  "action": "BUY/SELL/HOLD",
  "confidence": 0.0-1.0,
  "reasoning": ["理由1（含K线形态描述）", "理由2", "理由3"],
  "kline_pattern": "识别到的K线形态名称（如：锤子线、黄昏之星等，如无明显形态可写'无明显形态'）",
  "entry_price_range": [最小价格, 最大价格],
  "stop_loss": 止损价格（建议5-8%的止损空间）,
  "take_profit": 止盈价格（建议10-15%的止盈目标）,
  "risk_level": "LOW/MEDIUM/HIGH",
  "position_size_advice": 建议股数,
  "risk_reward_ratio": 风险收益比（数字，例如2.5表示盈亏比2.5:1）,
  "technical_signals": {
    "ma_trend": "MA趋势状态（如：突破MA20、MA5上穿MA20等）",
    "macd_status": "MACD状态（如：金叉、死叉、即将金叉等）",
    "rsi_status": "RSI状态（如：45中性偏多、超卖反弹等）",
    "volume_status": "成交量状态（如：放量1.5倍、缩量等）"
  }
}

⚠️ 关键要求：
1. **chain_of_thought 必须详细**：至少100字，展示完整的分析逻辑
2. **时间序列观察**：分析指标的演变趋势，而非仅看当前值
3. **信号共振**：至少2个技术指标支持才能给出BUY/SELL
4. **风险优先**：止损止盈必须合理，风险收益比≥2:1

⚠️ 决策标准：
- 信心度 ≥ 0.70 且有买入理由 → 返回 BUY
- 信心度 ≥ 0.70 且有卖出理由 → 返回 SELL  
- 信心度 < 0.60 或信号矛盾 → 返回 HOLD
- 0.60-0.69 之间 → 根据具体情况判断（偏向机会）"""
    
    def _format_score_section(self, score: Dict) -> str:
        """Format either the shared stock-picker score or the legacy AI score."""
        breakdown = score.get('breakdown', {})
        signals = score.get('signals', [])
        signal_text = chr(10).join([f'  ✓ {item}' for item in signals[:12]])

        if 'support_resistance' in breakdown:
            direction = score.get('opportunity_direction', 'LONG')
            direction_label = '做空' if direction == 'SHORT' else '做多'
            return f"""【统一机会评分 V2】⭐
方向: {direction_label}
总分: {score.get('total', 50)}/100 分 | 评级: {score.get('grade', 'C')}
细分维度（所有分项均为当前方向的机会分，越高越好）:
  • 趋势评分: {breakdown.get('trend', 0)}/25
  • 动量评分: {breakdown.get('momentum', 0)}/20
  • 支撑阻力: {breakdown.get('support_resistance', 0)}/15
  • 量价评分: {breakdown.get('volume', 0)}/15
  • 形态评分: {breakdown.get('pattern', 0)}/15
  • 波动评分: {breakdown.get('volatility', 0)}/10

检测到的信号:
{signal_text}

评分口径：
- 80+分(A级): 当前方向机会很强
- 65-79分(B级): 当前方向机会较好
- 50-64分(C级): 中性观察
- <50分(D级): 当前方向机会较弱"""

        return f"""【量化评分系统 V3.1】⭐ (舆情增强版)
总分: {score.get('total', 50)}/100 分 | 评级: {score.get('grade', 'C')}
细分维度（舆情增强）:
  • 趋势评分: {breakdown.get('trend', 0)}/15
  • 动量评分: {breakdown.get('momentum', 0)}/18
  • 波动评分: {breakdown.get('volatility', 0)}/25
  • 量能评分: {breakdown.get('volume', 0)}/12
  • 形态评分: {breakdown.get('pattern', 0)}/10
  • 新闻舆情: {breakdown.get('news', 0)}/20

检测到的信号:
{signal_text}"""

    def _build_prompt(
        self,
        symbol: str,
        klines: List[Dict],
        indicators: Dict,
        current_positions: Optional[Dict],
        scenario: str = "general",
        score: Optional[Dict] = None,
        news_analysis: Optional[Dict] = None  # ⬆️ 新增新闻参数
    ) -> str:
        """构建用户提示词（包含K线形态详情、量化评分和新闻舆情）"""
        current_price = indicators.get('current_price', klines[-1].get('close', 0))
        
        # 如果没有提供评分，使用默认值
        if score is None:
            score = {"total": 50, "breakdown": {}, "signals": [], "grade": "C"}
        score_section = self._format_score_section(score)
        
        # 持仓情况
        position_info = "当前无持仓"
        if current_positions and symbol in current_positions:
            pos = current_positions[symbol]
            position_info = f"""当前持仓情况：
- 持仓数量: {pos.get('quantity', 0)} 股
- 持仓成本: ${pos.get('avg_cost', 0):.2f}
- 当前盈亏: {pos.get('unrealized_pnl_percent', 0):.2f}%"""
        
        # 格式化指标（防御性处理 None 值）
        ma5 = indicators.get('ma5') or 0
        ma20 = indicators.get('ma20') or 0
        ma60 = indicators.get('ma60') or 0
        rsi = indicators.get('rsi') or 50
        macd = indicators.get('macd') or 0
        macd_signal = indicators.get('macd_signal') or 0
        macd_hist = indicators.get('macd_histogram') or 0
        bb_upper = indicators.get('bollinger_upper') or 0
        bb_middle = indicators.get('bollinger_middle') or 0
        bb_lower = indicators.get('bollinger_lower') or 0
        volume_ratio = indicators.get('volume_ratio') or 1.0
        price_change_1d = indicators.get('price_change_1d') or 0
        price_change_5d = indicators.get('price_change_5d') or 0
        
        # 📊 增强K线形态描述（最近10根，更详细）
        recent_klines = klines[-10:] if len(klines) >= 10 else klines
        kline_detail = []
        for i, k in enumerate(recent_klines):
            open_price = k.get('open', 0)
            close_price = k.get('close', 0)
            high_price = k.get('high', 0)
            low_price = k.get('low', 0)
            
            # 判断阴阳线
            is_bullish = close_price >= open_price
            bar_type = "🟢阳线" if is_bullish else "🔴阴线"
            
            # 计算实体和影线
            body_size = abs(close_price - open_price)
            upper_shadow = high_price - max(close_price, open_price)
            lower_shadow = min(close_price, open_price) - low_price
            total_range = high_price - low_price if high_price > low_price else 0.01
            
            # 实体和影线占比
            body_ratio = (body_size / total_range * 100) if total_range > 0 else 0
            upper_ratio = (upper_shadow / total_range * 100) if total_range > 0 else 0
            lower_ratio = (lower_shadow / total_range * 100) if total_range > 0 else 0
            
            kline_detail.append(
                f"  {i+1}. {bar_type} | "
                f"开:{open_price:.2f} 高:{high_price:.2f} 低:{low_price:.2f} 收:{close_price:.2f} | "
                f"实体:{body_size:.2f}({body_ratio:.0f}%) "
                f"上影:{upper_shadow:.2f}({upper_ratio:.0f}%) "
                f"下影:{lower_shadow:.2f}({lower_ratio:.0f}%) | "
                f"量:{k.get('volume', 0):,.0f}"
            )
        
        kline_text = "\n".join(kline_detail)
        
        # ⭐ V2.0: 提取价格时间序列（最近10根K线）
        series_length = min(10, len(klines))
        price_series = [klines[-(series_length-i)].get('close', 0) for i in range(series_length)]
        price_seq = ", ".join([f"${p:.2f}" for p in price_series])
        
        # 🔍 构建新闻舆情信息（如果有）
        news_section = ""
        if news_analysis:
            news_count = news_analysis.get('news_count', 0)
            sentiment_label = news_analysis.get('sentiment_label', 'NEUTRAL')
            sentiment_score = news_analysis.get('sentiment_score', 0)
            impact_score = news_analysis.get('impact_score', 0)
            summary = news_analysis.get('summary', '')
            key_topics = news_analysis.get('key_topics', [])
            
            sentiment_icon = "📈" if sentiment_label == "POSITIVE" else "📉" if sentiment_label == "NEGATIVE" else "➡️"
            
            news_section = f"""
=== 🔍 新闻舆情分析（过去7天）===

新闻数量: {news_count}条
情绪倾向: {sentiment_icon} {sentiment_label} ({sentiment_score:+.2f})
影响评分: {impact_score:.1f}/10
关键主题: {', '.join(key_topics) if key_topics else '无'}
总结: {summary}
"""
            # 添加最重要的几条新闻标题
            news_items = news_analysis.get('news_items', [])[:3]
            if news_items:
                news_section += "\n重要新闻:\n"
                for idx, item in enumerate(news_items, 1):
                    news_section += f"  {idx}. {item.get('title', '未知')} (相关度: {item.get('score', 0):.2f})\n"
            
            news_section += "\n---"
        
        prompt = f"""======= AI 交易系统分析请求 =======

分析股票: {symbol}
分析时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
数据范围: {len(klines)}根K线
数据顺序: OLDEST（最早） → NEWEST（最新）

---

=== 当前市场快照 ===

当前价格: ${current_price:.2f}
价格波动: 1日 {price_change_1d:+.2f}% | 5日 {price_change_5d:+.2f}%
{position_info}

---
{news_section}

---

=== 股票 {symbol} 完整数据 ===

【实时指标概览】
current_price = ${float(current_price):.2f}
current_ma5 = ${float(ma5):.2f}
current_ma20 = ${float(ma20):.2f}
current_rsi = {float(rsi):.1f}
current_macd = {float(macd):.4f}
current_volume_ratio = {float(volume_ratio):.2f}x

【价格时间序列】（最近{series_length}根K线，oldest → latest）
收盘价序列: [{price_seq}]

【技术指标矩阵】
┌─ 趋势指标 ──────────────┐
│ MA5:  ${float(ma5):.2f}
│ MA20: ${float(ma20):.2f}
│ MA60: {f'${float(ma60):.2f}' if ma60 and ma60 > 0 else 'N/A'}
│ 价格 vs MA20: {'上方 ↑' if current_price > ma20 and ma20 > 0 else '下方 ↓'} ({((current_price/ma20-1)*100) if ma20 > 0 else 0:+.2f}%)
│ MA5 vs MA20: {'金叉 ⚡' if ma5 > ma20 else '死叉 ⚠️'}
└──────────────────────────┘

┌─ 动量指标 ──────────────┐
│ RSI(14): {float(rsi):.1f} {'(超买 🔴)' if rsi > 70 else '(超卖 🟢)' if rsi < 30 else '(中性 ⚪)'}
│ MACD: {float(macd):.4f}
│ Signal: {float(macd_signal):.4f}
│ Histogram: {float(macd_hist):.4f} {'(金叉 ⚡)' if macd > macd_signal else '(死叉 ⚠️)'}
│ MACD 趋势: {'向上' if macd_hist > 0 else '向下'}
└──────────────────────────┘

┌─ 波动指标 ──────────────┐
│ 布林上轨: ${float(bb_upper):.2f}
│ 布林中轨: ${float(bb_middle):.2f}
│ 布林下轨: ${float(bb_lower):.2f}
│ 位置: {'接近上轨 (偏贵)' if current_price > bb_middle and bb_middle > 0 else '接近下轨 (偏便宜)'}
│ 布林带宽: {((bb_upper - bb_lower) / bb_middle * 100) if bb_middle > 0 else 0:.1f}%
└──────────────────────────┘

┌─ 量能分析 ──────────────┐
│ 量比: {float(volume_ratio):.2f}x {'(放量 📈)' if volume_ratio > 1.5 else '(缩量 📉)' if volume_ratio < 0.8 else '(正常)'}
│ 状态: {'成交活跃' if volume_ratio > 1.3 else '成交清淡' if volume_ratio < 0.8 else '成交平稳'}
└──────────────────────────┘

{score_section}

【K线形态详情】(最近10根，从旧到新)
{kline_text}

📌 K线形态分析重点：
- 观察最后3根K线的组合，是否形成经典形态（锤子线、早晨之星、黄昏之星等）？
- 上下影线的比例说明什么？长下影=支撑强，长上影=阻力大
- 实体大小反映多空力量：大阳线=强势，大阴线=弱势，十字星=平衡
- 成交量的变化如何配合价格？放量上涨=强势，放量下跌=恐慌
- 是否有连续的红三兵（看涨）或黑三兵（看跌）？
"""
        
        # 根据场景添加特定提示
        if scenario == "buy_focus":
            prompt += """
🎯 买入场景分析要点：
- 优先识别看涨K线形态（锤子线、早晨之星、红三兵、多方炮等）
- 是否有超卖反弹机会？RSI < 40 都可关注
- 是否在支撑位企稳？或突破阻力位？
- 成交量是否配合价格走势？（放量最佳，正常量也可）
- MACD是否金叉或即将金叉？
- 价格相对布林带的位置如何？中下轨区域更安全

💡 决策建议：
- 如果有2个以上正面信号，信心度应 ≥ 0.70，建议BUY
- 如果有明显看涨K线形态，即使其他指标中性也可考虑
- 只要不是明显下跌趋势，都可以给出积极建议
"""
        elif scenario == "sell_focus":
            prompt += f"""
🛡️ 卖出场景提示：
- 重点识别看跌K线形态（吊颈线、黄昏之星、空方炮）
- 是否在阻力位遇阻？
- 当前盈亏情况：{position_info}
- 是否应该止盈或止损？
"""
        
        prompt += f"""
【综合评估任务】
1. 参考量化评分（当前: {score['total']}/100, 评级: {score['grade']}）
2. 仔细分析K线形态，识别是否有看涨/看跌信号
3. 综合技术指标（MA、RSI、MACD、布林带），至少2个指标支持即可
4. 评估量能配合情况（放量更好，正常量也可接受）
5. 给出明确的BUY/SELL/HOLD决策和信心度
6. 在reasoning中详细说明决策依据（包括量化评分、K线形态、技术指标、量能等）

⚠️ 重要提醒：
- 这是自动交易系统，需要积极捕捉机会（不要过度保守）
- 量化评分 ≥ 65分(B级) 时，应给予更高信心度
- 量化评分 ≥ 80分(A级) 时，强烈推荐交易
- 信心度 ≥ 0.70 即可交易（有2个指标支持即可）
- 只要不是明显的反向信号，都可以考虑给出交易建议
- 必须以 JSON 格式返回决策

💡 信心度参考（结合量化评分）：
- 评分80+且有买入信号 → 信心度0.85+
- 评分65-79且有买入信号 → 信心度0.75-0.85
- 评分50-64且有买入迹象 → 信心度0.65-0.75
- 评分<50或信号矛盾 → 信心度<0.65，建议HOLD"""
        
        return prompt
    
    def _parse_ai_response(self, raw_response: str, current_price: float) -> Dict:
        """解析并验证 AI 响应"""
        try:
            result = json.loads(raw_response)
        except json.JSONDecodeError:
            # 尝试提取 JSON
            import re
            json_match = re.search(r'\{.*\}', raw_response, re.DOTALL)
            if json_match:
                result = json.loads(json_match.group())
            else:
                raise ValueError("无法解析 AI 响应为 JSON")
        
        # 验证必需字段
        required_fields = ['action', 'confidence', 'reasoning']
        for field in required_fields:
            if field not in result:
                raise ValueError(f"AI 响应缺少字段: {field}")
        
        # 标准化 action
        action = result.get('action', 'HOLD').upper()
        if action not in ['BUY', 'SELL', 'HOLD']:
            logger.warning(f"无效的 action: {action}，默认为 HOLD")
            action = 'HOLD'
        
        # 验证 confidence 范围
        confidence = float(result.get('confidence', 0))
        if not 0 <= confidence <= 1:
            logger.warning(f"confidence 超出范围: {confidence}，截断到 [0,1]")
            confidence = max(0.0, min(1.0, confidence))
        
        # 验证 reasoning 是列表且有内容
        reasoning = result.get('reasoning', [])
        if not isinstance(reasoning, list) or len(reasoning) == 0:
            reasoning = ["AI 未提供详细理由"]
        
        # 解析价格范围
        entry_range = result.get('entry_price_range', [current_price * 0.99, current_price * 1.01])
        if len(entry_range) >= 2 and entry_range[0] is not None and entry_range[1] is not None:
            entry_price_min = float(entry_range[0])
            entry_price_max = float(entry_range[1])
        else:
            entry_price_min = current_price * 0.99
            entry_price_max = current_price * 1.01
        
        # 解析止损止盈
        stop_loss_val = result.get('stop_loss')
        stop_loss = float(stop_loss_val) if stop_loss_val is not None else current_price * 0.95
        take_profit_val = result.get('take_profit')
        take_profit = float(take_profit_val) if take_profit_val is not None else current_price * 1.05
        
        # ⭐ V2.0: 提取新增字段
        chain_of_thought = result.get('chain_of_thought', '')
        if chain_of_thought:
            logger.info(f"🧠 AI 思考过程: {chain_of_thought[:200]}..." if len(chain_of_thought) > 200 else f"🧠 AI 思考过程: {chain_of_thought}")
        
        return {
            'action': action,
            'confidence': confidence,
            'reasoning': reasoning,
            'entry_price_min': float(entry_price_min),
            'entry_price_max': float(entry_price_max),
            'stop_loss': float(stop_loss) if stop_loss else None,
            'take_profit': float(take_profit) if take_profit else None,
            'risk_level': result.get('risk_level', 'MEDIUM'),
            'position_size_advice': int(result.get('position_size_advice', 100)),
            # ⭐ V2.0 新增字段
            'chain_of_thought': chain_of_thought,
            'kline_pattern': result.get('kline_pattern', '无明显形态'),
            'risk_reward_ratio': float(result.get('risk_reward_ratio', 0)) if result.get('risk_reward_ratio') else None,
            'technical_signals': result.get('technical_signals', {}),
        }
    
    def _get_tactical_system_prompt(self, scenario: str = "general") -> str:
        """🎯 战术型交易员风格 System Prompt（类似 RockAlpha）"""
        
        base_tactical = """You are an elite institutional trader with 15+ years of experience managing a $500M portfolio. Your trading philosophy emphasizes discipline, asymmetric risk-reward, and tactical patience.

ANALYSIS FRAMEWORK:
1. **Market Context** - Identify current regime (trending, ranging, volatile)
2. **Catalyst Mapping** - What events are ahead? When? (earnings, Fed, macro data)
3. **Position Rationale** - Why this setup exists, what binary you're capturing
4. **Timing Logic** - Why now vs. waiting for more information
5. **Execution Plan** - Specific entry/exit levels and risk management
6. **Conviction Rating** - Confidence in the thesis (0-100%)

WRITING STYLE:
- Use tactical language: "fortress logic", "explosive asymmetry", "binary setup", "catalyst convergence"
- Be time-aware: Reference upcoming events and their timeline
- Show discipline: "Acting now surrenders the edge", "Patience delivers", "Information wins"
- Reference specific levels: Always cite exact prices and technical levels
- Market comparison: Compare to sector, benchmark (SPY), related names
- End with a memorable trading principle that captures your thesis

OUTPUT FORMAT (JSON):
{
  "action": "BUY" | "SELL" | "HOLD",
  "confidence": 0.85,
  "chain_of_thought": "Complete tactical analysis (150-300 words) - Write like an elite trader's internal monologue. Reference specific price levels, time until catalysts, risk calculations. Show discipline over action. Use phrases like 'fortress logic holds firm', 'explosive asymmetry', 'binary resolution', 'institutions voting with size'.",
  "reasoning": [
    "Key point 1 with specific data and levels",
    "Key point 2 with specific data and levels",
    "Key point 3 with specific data and levels"
  ],
  "entry_price_max": 150.50,
  "stop_loss": 145.00,
  "take_profit": 165.00,
  "risk_reward_ratio": 3.0,
  "kline_pattern": "Breakout above resistance with volume confirmation",
  "technical_signals": {
    "ma_trend": "MA20 golden cross MA50, bullish alignment",
    "macd_status": "Just flipped bullish, early momentum signal",
    "rsi_status": "RSI 58 - room to run before overbought",
    "volume_status": "Surge 2.3x average - institutional buying"
  },
  "catalyst_map": "Upcoming catalysts timeline (e.g., 'Fed decision in 4.5h, earnings in 6.5h')",
  "position_rationale": "Why this setup exists (e.g., 'Built for binary resolution when catalysts align')",
  "tactical_principle": "Memorable trading wisdom (e.g., 'Trade the reaction, not the drumroll. Information wins.')"
}

CRITICAL RULES:
1. **chain_of_thought** must be 150-300 words, written like an elite trader's internal monologue
2. Show your work: reference specific price levels, percentages, ratios
3. Time-awareness: mention upcoming events and their timing when relevant
4. Emphasize discipline: sometimes HOLD is the strongest move (e.g., waiting for Fed clarity)
5. Market internals: compare to sector performance, benchmark, related names
6. Risk-first: always define risk before reward, use stop-loss levels
7. End with a principle: create a memorable one-liner that captures your thesis
"""

        # 根据场景调整指导
        if scenario == "buy_focus":
            scenario_guide = """

【CURRENT TASK: HUNT FOR ASYMMETRIC ENTRIES】🎯

You're actively seeking high-conviction BUY opportunities. Focus on:

1. 📈 Breakout Setups: Clean breaks above resistance with volume confirmation
2. 🔄 Trend Reversals: Bullish patterns (hammer, morning star) after consolidation
3. 📊 Institutional Footprints: Volume surges, golden crosses, sector rotation
4. 🎯 Catalyst Positioning: Setups before positive catalysts (earnings, events)
5. ⚖️ Asymmetric Risk-Reward: Look for 3:1 or better setups with clean stops

CONVICTION THRESHOLDS:
- High (0.85-1.0): Multiple technical confirmations + volume + catalyst support → **BUY aggressively**
- Medium (0.70-0.84): 2+ technical signals align, clean risk definition → **BUY tactically**
- Low (0.60-0.69): Single strong signal, cautious entry → **Small position BUY**
- Very Low (<0.60): Signals conflict or setup incomplete → **HOLD and wait**

TACTICAL PRINCIPLES:
- "Breakouts with volume don't ask permission. Execute."
- "Institutional footprints > retail noise. Follow the smart money."
- "Risk 1 to make 3. Asymmetry is everything."
"""
        
        elif scenario == "sell_focus":
            scenario_guide = """

【CURRENT TASK: POSITION RISK MANAGEMENT】🛡️

You're managing existing positions. Focus on preservation and tactical exits:

1. 🚨 Reversal Patterns: Bearish signals (shooting star, evening star, dark cloud)
2. 📉 Momentum Loss: MACD bearish divergence, RSI overbought rollover
3. ⚠️ Volume Red Flags: Distribution patterns, climax tops
4. 💰 Profit Protection: Lock gains when risk-reward deteriorates
5. 🔥 Catalyst Risk: Exit before uncertain binary events

SELL CONVICTION:
- High (0.85-1.0): Bearish pattern + momentum loss + distribution → **SELL decisively**
- Medium (0.75-0.84): Technical breakdown + fading strength → **SELL tactically**
- Low (<0.75): Weak signals, position still healthy → **HOLD position**

SPECIAL CASES:
- Loss near -5%: Consider stop-loss even if signals weak
- Profit > +15%: Consider profit-taking even if signals neutral
- Before binary events: Reduce exposure to unknowable outcomes

TACTICAL PRINCIPLES:
- "Riding winners is discipline. Riding them into reversals is ego."
- "Protect gains. Capital preservation > catching last tick."
- "Unknown catalysts deserve reduced exposure. Edge requires information."
"""
        
        else:  # general
            scenario_guide = """

【COMPREHENSIVE OPPORTUNITY ASSESSMENT】

Evaluate both buy and sell opportunities with equal weight. Deliver the highest-conviction call.
Focus on signal quality, risk-reward asymmetry, and tactical timing.
"""

        return base_tactical + scenario_guide + """

【DATA YOU'LL RECEIVE】
The user prompt will contain:
- Current price and recent price action
- Technical indicators (MA, MACD, RSI, Volume)
- Position status (if any)
- K-line data

Your job: Transform this data into actionable intelligence with elite trader mindset.

Remember: 
- Specificity > generality (cite exact levels)
- Discipline > action (HOLD is often strongest)
- Information > speculation (wait for catalysts when unclear)
- Asymmetry > symmetry (only trade when R:R ≥ 2:1)
"""
