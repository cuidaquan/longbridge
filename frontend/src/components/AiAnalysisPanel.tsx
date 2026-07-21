/**
 * AI 分析面板 - 实时显示 AI 的思考过程
 * 类似 RockAlpha 的 Model Chats 功能
 */
import { useEffect, useState, useRef } from 'react';
import {
  Box,
  Paper,
  Typography,
  Chip,
  Divider,
  IconButton,
  Collapse,
} from '@mui/material';
import {
  ExpandMore as ExpandMoreIcon,
  ExpandLess as ExpandLessIcon,
  SmartToy as SmartToyIcon,
  TrendingUp as TrendingUpIcon,
  TrendingDown as TrendingDownIcon,
  Remove as RemoveIcon,
} from '@mui/icons-material';

interface AnalysisMessage {
  id: number;
  symbol: string;
  timestamp: string;
  action: 'BUY' | 'SELL' | 'HOLD';
  confidence: number;
  chainOfThought: string;
  reasoning: string[];
  currentPrice: number;
  indicators?: {
    ma_trend?: string;
    macd_status?: string;
    rsi_status?: string;
    volume_status?: string;
  };
  klinePattern?: string;
  riskRewardRatio?: number;
}

interface AiAnalysisPanelProps {
  wsUrl: string;  // WebSocket URL
  maxMessages?: number;  // 最多显示多少条消息
}

export default function AiAnalysisPanel({ 
  wsUrl, 
  maxMessages = 20 
}: AiAnalysisPanelProps) {
  const [messages, setMessages] = useState<AnalysisMessage[]>([]);
  const [expandedId, setExpandedId] = useState<number | null>(null);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const ws = useRef<WebSocket | null>(null);

  // 滚动到底部
  const scrollToBottom = () => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  };

  useEffect(() => {
    scrollToBottom();
  }, [messages]);

  // WebSocket 连接
  useEffect(() => {
    let disposed = false;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;

    const connectWs = () => {
      if (disposed) return;
      const socket = new WebSocket(wsUrl);
      ws.current = socket;

      socket.onopen = () => {
        console.log('✅ AI Analysis WebSocket connected');
      };

      socket.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data);
          
          // 只处理 ai_analysis 类型的消息
          if (data.type === 'ai_analysis') {
            const analysis = data.data;
            
            const newMessage: AnalysisMessage = {
              id: analysis.id || Date.now(),
              symbol: analysis.symbol,
              timestamp: analysis.analysis_time,
              action: analysis.action,
              confidence: analysis.confidence,
              chainOfThought: analysis.chain_of_thought || '',
              reasoning: analysis.reasoning || [],
              currentPrice: analysis.current_price,
              indicators: analysis.technical_signals || analysis.indicators,
              klinePattern: analysis.kline_pattern,
              riskRewardRatio: analysis.risk_reward_ratio,
            };

            setMessages(prev => {
              const updated = [...prev, newMessage];
              // 限制消息数量
              if (updated.length > maxMessages) {
                return updated.slice(-maxMessages);
              }
              return updated;
            });
          }
        } catch (e) {
          console.error('Failed to parse WebSocket message:', e);
        }
      };

      socket.onerror = (error) => {
        if (!disposed) {
          console.error('❌ WebSocket error:', error);
        }
      };

      socket.onclose = () => {
        if (!disposed) {
          console.log('🔌 WebSocket closed, reconnecting in 3s...');
          reconnectTimer = setTimeout(connectWs, 3000);
        }
      };
    };

    connectWs();

    return () => {
      disposed = true;
      if (reconnectTimer) clearTimeout(reconnectTimer);
      const socket = ws.current;
      if (socket) {
        socket.onopen = null;
        socket.onmessage = null;
        socket.onerror = null;
        socket.onclose = null;
        socket.close();
        ws.current = null;
      }
    };
  }, [wsUrl, maxMessages]);

  const getActionIcon = (action: string) => {
    switch (action) {
      case 'BUY':
        return <TrendingUpIcon sx={{ color: '#4caf50', fontSize: 20 }} />;
      case 'SELL':
        return <TrendingDownIcon sx={{ color: '#f44336', fontSize: 20 }} />;
      default:
        return <RemoveIcon sx={{ color: '#9e9e9e', fontSize: 20 }} />;
    }
  };

  const getActionColor = (action: string) => {
    switch (action) {
      case 'BUY':
        return '#4caf50';
      case 'SELL':
        return '#f44336';
      default:
        return '#9e9e9e';
    }
  };

  const formatTime = (timestamp: string) => {
    const date = new Date(timestamp);
    return date.toLocaleTimeString('zh-CN', { 
      hour: '2-digit', 
      minute: '2-digit',
      second: '2-digit'
    });
  };

  const toggleExpand = (id: number) => {
    setExpandedId(expandedId === id ? null : id);
  };

  return (
    <Box
      sx={{
        height: '100%',
        display: 'flex',
        flexDirection: 'column',
        bgcolor: '#f5f5f5',
      }}
    >
      {/* 标题栏 */}
      <Box
        sx={{
          p: 2,
          bgcolor: 'white',
          borderBottom: '1px solid #e0e0e0',
          display: 'flex',
          alignItems: 'center',
          gap: 1,
        }}
      >
        <SmartToyIcon color="primary" />
        <Typography variant="h6" sx={{ fontWeight: 600 }}>
          AI 实时分析
        </Typography>
        <Chip 
          label={`${messages.length} 条`} 
          size="small" 
          sx={{ ml: 'auto' }}
        />
      </Box>

      {/* 消息列表 */}
      <Box
        sx={{
          flex: 1,
          overflowY: 'auto',
          p: 2,
          display: 'flex',
          flexDirection: 'column',
          gap: 2,
        }}
      >
        {messages.length === 0 ? (
          <Box 
            sx={{ 
              textAlign: 'center', 
              py: 4,
              color: 'text.secondary'
            }}
          >
            <SmartToyIcon sx={{ fontSize: 48, mb: 2, opacity: 0.3 }} />
            <Typography variant="body2">
              等待 AI 分析中...
            </Typography>
          </Box>
        ) : (
          messages.map((msg) => (
            <Paper
              key={msg.id}
              elevation={1}
              sx={{
                p: 2.5,
                mb: 2,
                borderRadius: 2,
                bgcolor: '#ffffff',
                border: '1px solid #e0e0e0',
                transition: 'all 0.2s',
                '&:hover': {
                  boxShadow: 3,
                  borderColor: getActionColor(msg.action),
                },
              }}
            >
              {/* 🎯 RockAlpha 风格头部 */}
              <Box sx={{ display: 'flex', alignItems: 'center', mb: 2 }}>
                {/* AI 标识 */}
                <Box
                  sx={{
                    width: 32,
                    height: 32,
                    borderRadius: '50%',
                    bgcolor: getActionColor(msg.action),
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    mr: 1.5,
                  }}
                >
                  <SmartToyIcon sx={{ color: 'white', fontSize: 18 }} />
                </Box>
                
                {/* 股票和操作 */}
                <Box sx={{ flex: 1 }}>
                  <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
                    <Typography variant="subtitle1" sx={{ fontWeight: 600, color: '#1a1a1a' }}>
                      {msg.symbol}
                    </Typography>
                    <Chip
                      label={msg.action}
                      size="small"
                      sx={{
                        bgcolor: getActionColor(msg.action),
                        color: 'white',
                        fontWeight: 600,
                        fontSize: '0.7rem',
                        height: 20,
                      }}
                    />
                  </Box>
                  <Typography variant="caption" sx={{ color: '#666', fontSize: '0.7rem' }}>
                    {formatTime(msg.timestamp)} • 信心度 {(msg.confidence * 100).toFixed(0)}%
                  </Typography>
                </Box>
              </Box>

              {/* 🎨 RockAlpha 风格：完整文本展示（默认展开） */}
              {msg.chainOfThought ? (
                <Typography
                  sx={{
                    color: '#2c3e50',
                    fontSize: '0.875rem',
                    lineHeight: 1.8,
                    whiteSpace: 'pre-wrap',
                    wordBreak: 'break-word',
                    mb: 2,
                    fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif',
                  }}
                >
                  {msg.chainOfThought}
                </Typography>
              ) : (
                // 如果没有完整思考过程，显示推理要点
                <Box sx={{ mb: 2 }}>
                  {msg.reasoning.map((reason, idx) => (
                    <Typography 
                      key={idx}
                      sx={{ 
                        color: '#2c3e50',
                        fontSize: '0.875rem',
                        lineHeight: 1.8,
                        mb: 1,
                      }}
                    >
                      {reason}
                    </Typography>
                  ))}
                </Box>
              )}

              {/* 底部：价格和技术指标 */}
              <Box sx={{ 
                display: 'flex', 
                flexWrap: 'wrap',
                gap: 1,
                pt: 2,
                borderTop: '1px solid #f0f0f0',
              }}>
                <Chip 
                  label={`💰 $${msg.currentPrice.toFixed(2)}`}
                  size="small"
                  variant="outlined"
                  sx={{ fontSize: '0.7rem', height: 24 }}
                />
                {msg.riskRewardRatio && (
                  <Chip 
                    label={`⚖️ R:R ${msg.riskRewardRatio.toFixed(1)}:1`}
                    size="small"
                    variant="outlined"
                    sx={{ fontSize: '0.7rem', height: 24 }}
                  />
                )}
                {msg.indicators?.ma_trend && (
                  <Chip 
                    label={msg.indicators.ma_trend}
                    size="small"
                    variant="outlined"
                    sx={{ fontSize: '0.7rem', height: 24 }}
                  />
                )}
                {msg.indicators?.macd_status && (
                  <Chip 
                    label={msg.indicators.macd_status}
                    size="small"
                    variant="outlined"
                    sx={{ fontSize: '0.7rem', height: 24 }}
                  />
                )}
              </Box>

              {/* 战术原则（如果有）*/}
              {msg.chainOfThought && msg.chainOfThought.includes('.') && (
                <Box sx={{ 
                  mt: 2,
                  pt: 2,
                  borderTop: '1px solid #f0f0f0',
                }}>
                  <Typography
                    sx={{
                      color: '#7c3aed',
                      fontSize: '0.8rem',
                      fontWeight: 500,
                      fontStyle: 'italic',
                    }}
                  >
                    {/* 提取最后一句话作为格言 */}
                    💡 {msg.chainOfThought.split('.').filter(s => s.trim()).slice(-1)[0]}.
                  </Typography>
                </Box>
              )}
            </Paper>
          ))
        )}
        <div ref={messagesEndRef} />
      </Box>
    </Box>
  );
}
