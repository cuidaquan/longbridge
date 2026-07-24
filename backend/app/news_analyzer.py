"""
新闻和舆情分析器 - 使用 Tavily 搜索引擎
"""
from typing import Dict, List, Optional
import logging
from datetime import datetime, timedelta

from .external_service_resilience import run_external_call
from .stock_picker_ai_snapshots import (
    NEWS_SNAPSHOT_VERSION,
    sanitize_error,
    utc_now_iso,
)

logger = logging.getLogger(__name__)

try:
    from tavily import TavilyClient
    HAS_TAVILY = True
except ImportError:
    HAS_TAVILY = False
    TavilyClient = None


class NewsAnalyzer:
    """新闻和舆情分析器"""
    
    def __init__(self, api_key: str):
        if not HAS_TAVILY:
            raise ImportError("需要安装 tavily-python: pip install tavily-python")
        
        if not api_key:
            raise ValueError("必须提供 Tavily API Key")
        
        self.client = TavilyClient(api_key)
        logger.info("✅ Tavily 搜索引擎已初始化")
    
    def search_stock_news(
        self,
        symbol: str,
        company_name: Optional[str] = None,
        days: int = 7
    ) -> Dict:
        """
        搜索股票相关新闻
        
        Args:
            symbol: 股票代码
            company_name: 公司名称（可选）
            days: 搜索最近几天的新闻
        
        Returns:
            {
                "news_count": 5,
                "sentiment_score": 0.7,  # -1到1，正面/负面
                "sentiment_label": "POSITIVE/NEUTRAL/NEGATIVE",
                "key_topics": ["财报", "新产品", ...],
                "news_items": [
                    {
                        "title": "标题",
                        "url": "链接",
                        "published_date": "2024-01-01",
                        "content": "摘要",
                        "score": 0.95
                    }
                ],
                "summary": "新闻总结",
                "impact_score": 8.5  # 0-10，新闻对股价的影响程度
            }
        """
        query = self._build_search_query(symbol, company_name)
        observed_at = utc_now_iso()
        snapshot_metadata = {
            "snapshot_version": NEWS_SNAPSHOT_VERSION,
            "status": "available",
            "observed_at": observed_at,
            "symbol": symbol,
            "query": query,
            "window_days": days,
            "source": "tavily",
            "search_depth": "advanced",
            "max_results": 10,
        }

        try:
            logger.info(f"🔍 搜索新闻: {query}")
            
            # 调用Tavily搜索
            response = run_external_call(
                "news",
                "search_stock_news",
                self.client.search,
                query=query,
                search_depth="advanced",  # 深度搜索
                max_results=10,
                include_domains=[
                    "finance.yahoo.com",
                    "seekingalpha.com",
                    "marketwatch.com",
                    "bloomberg.com",
                    "reuters.com",
                    "cnbc.com",
                    "investing.com",
                ],
                days=days,  # 最近N天
            )
            
            # 解析搜索结果
            analysis = self._analyze_search_results(response, symbol)
            analysis.update(snapshot_metadata)
            
            logger.info(
                f"✅ 新闻分析: {symbol} - "
                f"{analysis['news_count']}条新闻, "
                f"情绪:{analysis['sentiment_label']}, "
                f"影响度:{analysis['impact_score']:.1f}"
            )
            
            return analysis
            
        except Exception as e:
            safe_error = sanitize_error(e)
            logger.error(f"❌ 新闻搜索失败: {symbol} - {safe_error}")
            return {
                **snapshot_metadata,
                "status": "error",
                "news_count": 0,
                "sentiment_score": 0,
                "sentiment_label": "NEUTRAL",
                "key_topics": [],
                "news_items": [],
                "summary": "新闻搜索失败",
                "impact_score": 0,
                "error": safe_error,
                "error_type": type(e).__name__,
            }
    
    def _build_search_query(self, symbol: str, company_name: Optional[str]) -> str:
        """构建搜索查询"""
        # 清理股票代码（去除.US/.HK等后缀用于搜索）
        clean_symbol = symbol.split('.')[0]
        
        if company_name:
            # 如果有公司名称，优先使用
            query = f"{company_name} stock {clean_symbol} news earnings"
        else:
            # 只用股票代码
            query = f"{clean_symbol} stock news price movement"
        
        return query
    
    def _analyze_search_results(self, response: Dict, symbol: str) -> Dict:
        """分析搜索结果"""
        results = response.get('results', [])
        
        if not results:
            return {
                "news_count": 0,
                "sentiment_score": 0,
                "sentiment_label": "NEUTRAL",
                "key_topics": [],
                "news_items": [],
                "summary": "未找到相关新闻",
                "impact_score": 0
            }
        
        # 提取新闻项
        news_items = []
        for item in results[:10]:  # 最多10条
            news_items.append({
                "title": item.get('title', ''),
                "url": item.get('url', ''),
                "published_date": item.get('published_date'),
                "content": item.get('content', '')[:500],  # 摘要限制500字
                "score": item.get('score', 0)
            })
        
        # 分析情绪
        sentiment_analysis = self._analyze_sentiment(news_items)
        
        # 提取关键主题
        key_topics = self._extract_topics(news_items)
        
        # 生成总结
        summary = self._generate_summary(news_items, sentiment_analysis)
        
        # 计算影响分数（0-10）
        impact_score = self._calculate_impact_score(
            news_count=len(news_items),
            sentiment_score=sentiment_analysis['score'],
            avg_relevance=sum(n['score'] for n in news_items) / len(news_items) if news_items else 0
        )
        
        return {
            "news_count": len(news_items),
            "sentiment_score": sentiment_analysis['score'],
            "sentiment_label": sentiment_analysis['label'],
            "key_topics": key_topics,
            "news_items": news_items,
            "summary": summary,
            "impact_score": impact_score
        }
    
    def _analyze_sentiment(self, news_items: List[Dict]) -> Dict:
        """
        分析新闻情绪
        
        通过关键词检测正面/负面情绪
        """
        positive_keywords = [
            "surge", "jump", "rally", "gain", "rise", "beat", "exceed", 
            "strong", "growth", "profit", "bullish", "upgrade", "buy",
            "record", "high", "outperform", "positive", "optimistic",
            "breakthrough", "success", "winner"
        ]
        
        negative_keywords = [
            "plunge", "drop", "fall", "loss", "decline", "miss", "weak",
            "bearish", "downgrade", "sell", "concern", "risk", "problem",
            "low", "underperform", "negative", "pessimistic", "crisis",
            "failure", "warning", "cut", "reduce"
        ]
        
        positive_count = 0
        negative_count = 0
        total_words = 0
        
        for item in news_items:
            text = (item.get('title', '') + ' ' + item.get('content', '')).lower()
            
            # 计数正面词
            for keyword in positive_keywords:
                positive_count += text.count(keyword)
            
            # 计数负面词
            for keyword in negative_keywords:
                negative_count += text.count(keyword)
            
            total_words += len(text.split())
        
        # 计算情绪分数（-1到1）
        if positive_count + negative_count == 0:
            sentiment_score = 0
        else:
            sentiment_score = (positive_count - negative_count) / (positive_count + negative_count)
        
        # 分类
        if sentiment_score > 0.2:
            label = "POSITIVE"
        elif sentiment_score < -0.2:
            label = "NEGATIVE"
        else:
            label = "NEUTRAL"
        
        return {
            "score": sentiment_score,
            "label": label,
            "positive_count": positive_count,
            "negative_count": negative_count
        }
    
    def _extract_topics(self, news_items: List[Dict]) -> List[str]:
        """提取关键主题"""
        topics = set()
        
        # 关键主题词
        topic_keywords = {
            "财报": ["earnings", "report", "revenue", "profit", "eps"],
            "并购": ["merger", "acquisition", "deal", "buyout"],
            "新产品": ["product", "launch", "release", "innovation"],
            "监管": ["regulatory", "sec", "fda", "approval"],
            "诉讼": ["lawsuit", "legal", "court", "settlement"],
            "分析师": ["analyst", "rating", "upgrade", "downgrade"],
            "高管": ["ceo", "executive", "management", "resignation"],
            "市场": ["market", "sector", "industry", "competition"]
        }
        
        for item in news_items:
            text = (item.get('title', '') + ' ' + item.get('content', '')).lower()
            
            for topic_name, keywords in topic_keywords.items():
                if any(keyword in text for keyword in keywords):
                    topics.add(topic_name)
        
        return list(topics)[:5]  # 最多5个主题
    
    def _generate_summary(self, news_items: List[Dict], sentiment: Dict) -> str:
        """生成新闻总结"""
        if not news_items:
            return "无相关新闻"
        
        news_count = len(news_items)
        sentiment_label = sentiment['label']
        sentiment_desc = {
            "POSITIVE": "正面",
            "NEGATIVE": "负面",
            "NEUTRAL": "中性"
        }[sentiment_label]
        
        # 取最相关的3条新闻标题
        top_titles = [item['title'] for item in news_items[:3]]
        
        summary = f"发现{news_count}条相关新闻，整体情绪{sentiment_desc}。"
        if top_titles:
            summary += f" 主要内容：{'; '.join(top_titles[:2])}"
        
        return summary
    
    def _calculate_impact_score(
        self,
        news_count: int,
        sentiment_score: float,
        avg_relevance: float
    ) -> float:
        """
        计算新闻影响分数（0-10）
        
        综合考虑：
        - 新闻数量
        - 情绪强度
        - 相关性
        """
        # 新闻数量因子（0-3分）
        if news_count >= 10:
            count_factor = 3.0
        elif news_count >= 5:
            count_factor = 2.0
        elif news_count >= 2:
            count_factor = 1.0
        else:
            count_factor = 0.5
        
        # 情绪因子（0-4分）
        sentiment_factor = abs(sentiment_score) * 4
        
        # 相关性因子（0-3分）
        relevance_factor = avg_relevance * 3
        
        impact = count_factor + sentiment_factor + relevance_factor
        
        return min(10, max(0, impact))


def get_news_analyzer(api_key: str) -> Optional[NewsAnalyzer]:
    """获取新闻分析器实例"""
    try:
        return NewsAnalyzer(api_key)
    except Exception as e:
        logger.error(f"❌ 无法初始化新闻分析器: {e}")
        return None


