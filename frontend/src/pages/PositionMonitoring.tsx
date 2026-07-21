/**
 * 持仓监控管理页面 - 现代化重构版
 */
import { useEffect, useState, useRef, useCallback } from "react";
import {
  Settings,
  PlayArrow,
  Pause,
  Block,
  TrendingUp,
  TrendingDown,
  CheckCircle,
  Warning,
  Visibility,
  VisibilityOff,
  Refresh,
  Close,
} from "@mui/icons-material";
import {
  PageHeader,
  Card,
  CardHeader,
  Button,
  Badge,
  Select,
  Alert,
  LoadingSpinner,
  EmptyState,
} from "../components/ui";
import { API_BASE, resolveWsUrl } from "../api/client";

interface PositionMonitoring {
  symbol: string;
  name: string;
  quantity: number;
  avg_cost: number;
  current_price: number;
  market_value: number;
  pnl: number;
  pnl_ratio: number;
  monitoring_status: "enabled" | "disabled" | "paused";
  strategy_mode: "auto" | "alert_only" | "disabled" | "balanced";
  enabled_strategies: string[];
  stop_loss_ratio: number;
  take_profit_ratio: number;
  max_position_ratio: number;
  cooldown_minutes: number;
  notes?: string;
}

interface GlobalSettings {
  global_enabled: boolean;
  market_hours_only: boolean;
  max_daily_trades: number;
  max_total_exposure: number;
  emergency_stop: boolean;
  risk_level: string;
  notifications_enabled: boolean;
  excluded_symbols: string[];
}

interface Strategy {
  id: string;
  name: string;
  description: string;
}

const AVAILABLE_STRATEGIES: Strategy[] = [
  { id: "ma_crossover", name: "均线交叉", description: "基于MA金叉死叉" },
  { id: "rsi_oversold", name: "RSI超卖反弹", description: "RSI指标反转" },
  { id: "breakout", name: "突破策略", description: "价格突破关键位" },
  { id: "bollinger_bands", name: "布林带", description: "均值回归" },
  { id: "macd_divergence", name: "MACD背离", description: "趋势反转" },
];

export default function PositionMonitoringPage() {
  const [positions, setPositions] = useState<PositionMonitoring[]>([]);
  const [globalSettings, setGlobalSettings] = useState<GlobalSettings | null>(null);
  const [loading, setLoading] = useState(true);
  const [selectedPositions, setSelectedPositions] = useState<Set<string>>(new Set());
  const [editPosition, setEditPosition] = useState<PositionMonitoring | null>(null);
  const [showSettings, setShowSettings] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  const [wsConnected, setWsConnected] = useState(false);
  const [lastUpdate, setLastUpdate] = useState<Date | null>(null);
  const [priceFlash, setPriceFlash] = useState<Record<string, "up" | "down" | null>>({});
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimeout = useRef<ReturnType<typeof setTimeout> | null>(null);
  const shouldReconnect = useRef(true);
  const priceFlashTimeouts = useRef<Record<string, ReturnType<typeof setTimeout>>>({});

  const loadPositions = async () => {
    try {
      const base = API_BASE;
      const response = await fetch(`${base}/monitoring/positions`);
      if (response.ok) {
        const data = await response.json();
        setPositions(data.positions);
        setGlobalSettings(data.global_settings);
      } else {
        setError("加载持仓失败");
      }
    } catch (e) {
      setError("无法连接到后端服务");
    } finally {
      setLoading(false);
    }
  };

  const updatePositionMonitoring = async (symbol: string, updates: any) => {
    try {
      const base = API_BASE;
      const response = await fetch(`${base}/monitoring/position/${symbol}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(updates),
      });
      if (response.ok) {
        await loadPositions();
        setEditPosition(null);
        setSuccess("更新成功");
      } else {
        setError("更新失败，请检查配置值");
      }
    } catch (e) {
      setError("更新失败");
    }
  };

  const batchUpdateMonitoring = async (status: string) => {
    if (selectedPositions.size === 0) return;
    try {
      const base = API_BASE;
      const response = await fetch(`${base}/monitoring/batch-update`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          symbols: Array.from(selectedPositions),
          config: { monitoring_status: status },
        }),
      });
      if (response.ok) {
        await loadPositions();
        setSelectedPositions(new Set());
        setSuccess(`已更新 ${selectedPositions.size} 个持仓`);
      } else {
        setError("批量更新失败");
      }
    } catch (e) {
      setError("批量更新失败");
    }
  };

  const toggleMonitoring = async (symbol: string, enabled: boolean) => {
    await updatePositionMonitoring(symbol, {
      monitoring_status: enabled ? "enabled" : "paused",
    });
  };

  const excludePosition = async (symbol: string) => {
    try {
      const base = API_BASE;
      const response = await fetch(`${base}/monitoring/exclude/${symbol}`, {
        method: "POST",
      });
      if (response.ok) {
        await loadPositions();
        setSuccess(`已排除 ${symbol}`);
      }
    } catch (e) {
      setError("排除失败");
    }
  };

  const includePosition = async (symbol: string) => {
    try {
      const response = await fetch(`${API_BASE}/monitoring/include/${symbol}`, {
        method: "POST",
      });
      if (response.ok) {
        await loadPositions();
        setSuccess(`已恢复 ${symbol} 的监控`);
      } else {
        setError("恢复监控失败");
      }
    } catch (e) {
      setError("恢复监控失败");
    }
  };

  const updateGlobalSettings = async (settings: GlobalSettings) => {
    try {
      const base = API_BASE;
      const response = await fetch(`${base}/monitoring/global-settings`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(settings),
      });
      if (response.ok) {
        setGlobalSettings(settings);
        setShowSettings(false);
        setSuccess("全局设置已保存");
      } else {
        setError("保存设置失败，请检查配置值");
      }
    } catch (e) {
      setError("保存设置失败");
    }
  };

  const connectWebSocket = useCallback(() => {
    const wsUrl = resolveWsUrl("/ws/quotes");
    const ws = new WebSocket(wsUrl);

    ws.onopen = () => {
      setWsConnected(true);
      setError(null);
    };

    ws.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        if (data.type === "quote" && data.symbol) {
          updatePositionPrice(data.symbol, data.last_done || data.close);
        }
        if (data.type === "portfolio_update") {
          setLastUpdate(new Date());
          loadPositions();
        }
      } catch (e) {
        console.error("Error parsing WebSocket message:", e);
      }
    };

    ws.onerror = () => setWsConnected(false);
    ws.onclose = () => {
      setWsConnected(false);
      if (shouldReconnect.current) {
        reconnectTimeout.current = setTimeout(connectWebSocket, 3000);
      }
    };

    wsRef.current = ws;
  }, []);

  const updatePositionPrice = (symbol: string, newPrice: number) => {
    setPositions((prev) => {
      return prev.map((pos) => {
        if (pos.symbol === symbol) {
          const oldPrice = pos.current_price;
          const pnl = (newPrice - pos.avg_cost) * pos.quantity;
          const pnl_ratio = (newPrice - pos.avg_cost) / pos.avg_cost;

          if (newPrice > oldPrice) {
            setPriceFlash((f) => ({ ...f, [symbol]: "up" }));
          } else if (newPrice < oldPrice) {
            setPriceFlash((f) => ({ ...f, [symbol]: "down" }));
          }

          if (priceFlashTimeouts.current[symbol]) {
            clearTimeout(priceFlashTimeouts.current[symbol]);
          }
          priceFlashTimeouts.current[symbol] = setTimeout(() => {
            setPriceFlash((f) => ({ ...f, [symbol]: null }));
          }, 1000);

          return { ...pos, current_price: newPrice, market_value: newPrice * pos.quantity, pnl, pnl_ratio };
        }
        return pos;
      });
    });
  };

  useEffect(() => {
    shouldReconnect.current = true;
    loadPositions();
    connectWebSocket();
    const interval = setInterval(loadPositions, 30000);
    return () => {
      clearInterval(interval);
      shouldReconnect.current = false;
      if (reconnectTimeout.current) clearTimeout(reconnectTimeout.current);
      if (wsRef.current) wsRef.current.close();
      Object.values(priceFlashTimeouts.current).forEach(clearTimeout);
    };
  }, [connectWebSocket]);

  useEffect(() => {
    if (success || error) {
      const timer = setTimeout(() => {
        setSuccess(null);
        setError(null);
      }, 5000);
      return () => clearTimeout(timer);
    }
  }, [success, error]);

  if (loading) {
    return <LoadingSpinner size="lg" text="加载持仓监控..." />;
  }

  const totalPnl = positions.reduce((sum, p) => sum + p.pnl, 0);
  const totalMarketValue = positions.reduce((sum, p) => sum + p.market_value, 0);
  const totalCost = positions.reduce((sum, p) => sum + p.avg_cost * p.quantity, 0);
  const totalPnlRatio = totalCost > 0 ? totalPnl / totalCost : 0;
  const activeCount = positions.filter((p) => p.monitoring_status === "enabled").length;

  return (
    <div className="space-y-6 animate-fade-in">
      <PageHeader
        title="持仓监控管理"
        description="为每个持仓配置个性化的监控策略"
        icon={<Visibility />}
        actions={
          <div className="flex items-center gap-3">
            <Badge variant={wsConnected ? "success" : "default"}>
              {wsConnected ? "● 实时" : "○ 离线"}
            </Badge>
            <span className="text-sm text-slate-500">监控中: {activeCount}</span>
            <Button variant="secondary" onClick={() => setShowSettings(true)} icon={<Settings className="w-4 h-4" />}>
              全局设置
            </Button>
            <Button variant="ghost" onClick={loadPositions} icon={<Refresh className="w-4 h-4" />}>
              刷新
            </Button>
          </div>
        }
      />

      {error && (
        <Alert type="error" onClose={() => setError(null)}>
          {error}
        </Alert>
      )}
      {success && (
        <Alert type="success" onClose={() => setSuccess(null)}>
          {success}
        </Alert>
      )}

      {/* 统计卡片 */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
        <StatCard label="总市值" value={`$${totalMarketValue.toFixed(2)}`} subtext={`成本: $${totalCost.toFixed(2)}`} />
        <StatCard
          label="总盈亏"
          value={`${totalPnl >= 0 ? "+" : ""}$${totalPnl.toFixed(2)}`}
          subtext={`${totalPnl >= 0 ? "+" : ""}${(totalPnlRatio * 100).toFixed(2)}%`}
          color={totalPnl >= 0 ? "emerald" : "red"}
          icon={totalPnl >= 0 ? <TrendingUp className="w-5 h-5" /> : <TrendingDown className="w-5 h-5" />}
        />
        <StatCard label="持仓数量" value={positions.length.toString()} subtext={`监控中: ${activeCount}`} />
        <StatCard
          label="最后更新"
          value={lastUpdate ? lastUpdate.toLocaleTimeString() : "--:--:--"}
          subtext={wsConnected ? "实时推送中" : "等待连接..."}
        />
      </div>

      {/* 批量操作 */}
      {selectedPositions.size > 0 && (
        <Card>
          <div className="flex items-center gap-4">
            <span className="text-sm text-slate-600 dark:text-slate-400">已选择 {selectedPositions.size} 个持仓</span>
            <Button size="sm" variant="success" onClick={() => batchUpdateMonitoring("enabled")} icon={<PlayArrow className="w-4 h-4" />}>
              启用监控
            </Button>
            <Button size="sm" variant="warning" onClick={() => batchUpdateMonitoring("paused")} icon={<Pause className="w-4 h-4" />}>
              暂停监控
            </Button>
            <Button size="sm" variant="danger" onClick={() => batchUpdateMonitoring("disabled")} icon={<Block className="w-4 h-4" />}>
              禁用监控
            </Button>
            <Button size="sm" variant="ghost" onClick={() => setSelectedPositions(new Set())}>
              清除选择
            </Button>
          </div>
        </Card>
      )}

      {/* 持仓表格 */}
      <Card padding="none">
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-800/50">
                <th className="text-left py-3 px-4">
                  <input
                    type="checkbox"
                    checked={selectedPositions.size === positions.length && positions.length > 0}
                    onChange={(e) => {
                      if (e.target.checked) {
                        setSelectedPositions(new Set(positions.map((p) => p.symbol)));
                      } else {
                        setSelectedPositions(new Set());
                      }
                    }}
                    className="rounded border-slate-300"
                  />
                </th>
                <th className="text-left py-3 px-4 font-medium text-slate-500">股票</th>
                <th className="text-center py-3 px-4 font-medium text-slate-500">状态</th>
                <th className="text-center py-3 px-4 font-medium text-slate-500">策略模式</th>
                <th className="text-right py-3 px-4 font-medium text-slate-500">持仓</th>
                <th className="text-right py-3 px-4 font-medium text-slate-500">成本</th>
                <th className="text-right py-3 px-4 font-medium text-slate-500">现价</th>
                <th className="text-right py-3 px-4 font-medium text-slate-500">盈亏</th>
                <th className="text-center py-3 px-4 font-medium text-slate-500">止损/止盈</th>
                <th className="text-left py-3 px-4 font-medium text-slate-500">启用策略</th>
                <th className="text-center py-3 px-4 font-medium text-slate-500">操作</th>
              </tr>
            </thead>
            <tbody>
              {positions.map((position) => {
                const isSelected = selectedPositions.has(position.symbol);
                const isActive = position.monitoring_status === "enabled";
                const flash = priceFlash[position.symbol];
                const isExcluded = position.monitoring_status === "disabled";

                return (
                  <tr
                    key={position.symbol}
                    className={`border-b border-slate-100 dark:border-slate-700/50 transition-colors ${
                      flash === "up"
                        ? "bg-emerald-50 dark:bg-emerald-900/10"
                        : flash === "down"
                          ? "bg-red-50 dark:bg-red-900/10"
                          : "hover:bg-slate-50 dark:hover:bg-slate-800/50"
                    } ${isExcluded ? "opacity-50" : ""}`}
                  >
                    <td className="py-3 px-4">
                      <input
                        type="checkbox"
                        checked={isSelected}
                        onChange={(e) => {
                          const newSelected = new Set(selectedPositions);
                          if (e.target.checked) {
                            newSelected.add(position.symbol);
                          } else {
                            newSelected.delete(position.symbol);
                          }
                          setSelectedPositions(newSelected);
                        }}
                        className="rounded border-slate-300"
                      />
                    </td>
                    <td className="py-3 px-4">
                      <div className="flex items-center gap-2">
                        {isActive ? (
                          <CheckCircle className="w-4 h-4 text-emerald-500" />
                        ) : isExcluded ? (
                          <Block className="w-4 h-4 text-red-500" />
                        ) : (
                          <Pause className="w-4 h-4 text-amber-500" />
                        )}
                        <div>
                          <div className="font-medium text-slate-900 dark:text-white">{position.symbol}</div>
                          <div className="text-xs text-slate-500">{position.name}</div>
                        </div>
                      </div>
                    </td>
                    <td className="py-3 px-4 text-center">
                      <button
                        onClick={() => toggleMonitoring(position.symbol, !isActive)}
                        disabled={isExcluded}
                        className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors ${
                          isActive ? "bg-cyan-500" : "bg-slate-300 dark:bg-slate-600"
                        } ${isExcluded ? "opacity-50 cursor-not-allowed" : "cursor-pointer"}`}
                      >
                        <span
                          className={`inline-block h-4 w-4 transform rounded-full bg-white transition-transform ${
                            isActive ? "translate-x-6" : "translate-x-1"
                          }`}
                        />
                      </button>
                    </td>
                    <td className="py-3 px-4 text-center">
                      <span
                        className={`text-xs px-2 py-1 rounded font-medium ${
                          position.strategy_mode === "auto"
                            ? "bg-emerald-100 text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-400"
                            : position.strategy_mode === "alert_only"
                              ? "bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-400"
                              : "bg-slate-100 text-slate-600 dark:bg-slate-700 dark:text-slate-400"
                        }`}
                      >
                        {position.strategy_mode === "auto" ? "自动执行" : position.strategy_mode === "alert_only" ? "仅提醒" : "禁用"}
                      </span>
                    </td>
                    <td className="py-3 px-4 text-right">{position.quantity}</td>
                    <td className="py-3 px-4 text-right">${position.avg_cost.toFixed(2)}</td>
                    <td className="py-3 px-4 text-right font-medium">${position.current_price.toFixed(2)}</td>
                    <td className="py-3 px-4 text-right">
                      <div className={position.pnl >= 0 ? "text-emerald-600 dark:text-emerald-400" : "text-red-600 dark:text-red-400"}>
                        <div className="font-medium">
                          {position.pnl >= 0 ? "+" : ""}${position.pnl.toFixed(2)}
                        </div>
                        <div className="text-xs">
                          {position.pnl_ratio >= 0 ? "+" : ""}
                          {(position.pnl_ratio * 100).toFixed(2)}%
                        </div>
                      </div>
                    </td>
                    <td className="py-3 px-4 text-center">
                      <div className="text-xs">
                        <span className="text-red-500">
                          -{(position.stop_loss_ratio * 100).toFixed(0)}%
                        </span>
                        <span className="text-slate-400 mx-1">/</span>
                        <span className="text-emerald-500">
                          +{(position.take_profit_ratio * 100).toFixed(0)}%
                        </span>
                      </div>
                    </td>
                    <td className="py-3 px-4">
                      <div className="flex flex-wrap gap-1">
                        {position.enabled_strategies.slice(0, 2).map((strategyId) => {
                          const strategy = AVAILABLE_STRATEGIES.find((s) => s.id === strategyId);
                          return (
                            <span
                              key={strategyId}
                              className="text-xs px-1.5 py-0.5 bg-slate-100 dark:bg-slate-700 rounded text-slate-600 dark:text-slate-400"
                            >
                              {strategy?.name || strategyId}
                            </span>
                          );
                        })}
                        {position.enabled_strategies.length > 2 && (
                          <span className="text-xs text-slate-400">+{position.enabled_strategies.length - 2}</span>
                        )}
                        {position.enabled_strategies.length === 0 && <span className="text-xs text-slate-400">无</span>}
                      </div>
                    </td>
                    <td className="py-3 px-4 text-center">
                      <div className="flex items-center justify-center gap-1">
                        <Button size="sm" variant="ghost" onClick={() => setEditPosition(position)}>
                          <Settings className="w-4 h-4" />
                        </Button>
                        {isExcluded ? (
                          <Button size="sm" variant="ghost" onClick={() => includePosition(position.symbol)}>
                            <Visibility className="w-4 h-4 text-emerald-500" />
                          </Button>
                        ) : (
                          <Button size="sm" variant="ghost" onClick={() => excludePosition(position.symbol)}>
                            <Block className="w-4 h-4 text-red-500" />
                          </Button>
                        )}
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
        {positions.length === 0 && (
          <EmptyState title="暂无持仓" description="请先买入股票或检查 API 配置" icon={<Visibility />} />
        )}
      </Card>

      {/* 编辑持仓对话框 */}
      {editPosition && (
        <Dialog
          title={`编辑监控: ${editPosition.symbol}`}
          onClose={() => setEditPosition(null)}
          size="lg"
        >
          <div className="space-y-6">
            <Select
              label="策略模式"
              value={editPosition.strategy_mode}
              onChange={(e) =>
                setEditPosition({
                  ...editPosition,
                  strategy_mode: e.target.value as PositionMonitoring["strategy_mode"],
                })
              }
              options={[
                { value: "auto", label: "自动执行" },
                { value: "alert_only", label: "仅提醒" },
                { value: "disabled", label: "禁用" },
              ]}
            />

            <div>
              <label className="block text-sm font-medium text-slate-700 dark:text-slate-300 mb-2">启用的策略</label>
              <div className="space-y-2">
                {AVAILABLE_STRATEGIES.map((strategy) => (
                  <label key={strategy.id} className="flex items-start gap-3 p-2 rounded hover:bg-slate-50 dark:hover:bg-slate-800 cursor-pointer">
                    <input
                      type="checkbox"
                      checked={editPosition.enabled_strategies.includes(strategy.id)}
                      onChange={(e) => {
                        const strategies = [...editPosition.enabled_strategies];
                        if (e.target.checked) {
                          strategies.push(strategy.id);
                        } else {
                          const index = strategies.indexOf(strategy.id);
                          if (index > -1) strategies.splice(index, 1);
                        }
                        setEditPosition({ ...editPosition, enabled_strategies: strategies });
                      }}
                      className="mt-1 rounded border-slate-300"
                    />
                    <div>
                      <div className="font-medium text-slate-900 dark:text-white">{strategy.name}</div>
                      <div className="text-sm text-slate-500">{strategy.description}</div>
                    </div>
                  </label>
                ))}
              </div>
            </div>

            <div className="grid grid-cols-2 gap-4">
              <div>
                <label className="block text-sm font-medium text-slate-700 dark:text-slate-300 mb-2">
                  止损: {(editPosition.stop_loss_ratio * 100).toFixed(0)}%
                </label>
                <input
                  type="range"
                  min="1"
                  max="30"
                  step="1"
                  value={editPosition.stop_loss_ratio * 100}
                  onChange={(e) => setEditPosition({ ...editPosition, stop_loss_ratio: Number(e.target.value) / 100 })}
                  className="w-full"
                />
              </div>
              <div>
                <label className="block text-sm font-medium text-slate-700 dark:text-slate-300 mb-2">
                  止盈: {(editPosition.take_profit_ratio * 100).toFixed(0)}%
                </label>
                <input
                  type="range"
                  min="2"
                  max="100"
                  step="1"
                  value={editPosition.take_profit_ratio * 100}
                  onChange={(e) => setEditPosition({ ...editPosition, take_profit_ratio: Number(e.target.value) / 100 })}
                  className="w-full"
                />
              </div>
            </div>

            <div>
              <label className="block text-sm font-medium text-slate-700 dark:text-slate-300 mb-1">备注</label>
              <textarea
                value={editPosition.notes || ""}
                onChange={(e) => setEditPosition({ ...editPosition, notes: e.target.value })}
                rows={2}
                className="w-full px-3 py-2 rounded-lg border border-slate-300 dark:border-slate-600
                  bg-white dark:bg-slate-900 text-slate-900 dark:text-white text-sm
                  focus:outline-none focus:ring-2 focus:ring-cyan-500/50 resize-none"
              />
            </div>

            <div className="flex gap-3 pt-4 border-t border-slate-200 dark:border-slate-700">
              <Button variant="secondary" onClick={() => setEditPosition(null)} className="flex-1">
                取消
              </Button>
              <Button
                onClick={() =>
                  updatePositionMonitoring(editPosition.symbol, {
                    strategy_mode: editPosition.strategy_mode,
                    enabled_strategies: editPosition.enabled_strategies,
                    stop_loss_ratio: editPosition.stop_loss_ratio,
                    take_profit_ratio: editPosition.take_profit_ratio,
                    notes: editPosition.notes,
                  })
                }
                className="flex-1"
              >
                保存
              </Button>
            </div>
          </div>
        </Dialog>
      )}

      {/* 全局设置对话框 */}
      {showSettings && globalSettings && (
        <Dialog title="全局监控设置" onClose={() => setShowSettings(false)}>
          <div className="space-y-6">
            <label className="flex items-center gap-3 cursor-pointer">
              <input
                type="checkbox"
                checked={globalSettings.global_enabled}
                onChange={(e) => setGlobalSettings({ ...globalSettings, global_enabled: e.target.checked })}
                className="rounded border-slate-300"
              />
              <span className="text-sm text-slate-700 dark:text-slate-300">启用全局持仓监控</span>
            </label>

            <label className="flex items-center gap-3 cursor-pointer">
              <input
                type="checkbox"
                checked={globalSettings.market_hours_only}
                onChange={(e) => setGlobalSettings({ ...globalSettings, market_hours_only: e.target.checked })}
                className="rounded border-slate-300"
              />
              <span className="text-sm text-slate-700 dark:text-slate-300">仅在交易时段运行</span>
            </label>

            <label className="flex items-center gap-3 cursor-pointer">
              <input
                type="checkbox"
                checked={globalSettings.notifications_enabled}
                onChange={(e) => setGlobalSettings({ ...globalSettings, notifications_enabled: e.target.checked })}
                className="rounded border-slate-300"
              />
              <span className="text-sm text-slate-700 dark:text-slate-300">启用监控通知</span>
            </label>

            <label className="flex items-center gap-3 cursor-pointer">
              <input
                type="checkbox"
                checked={globalSettings.emergency_stop}
                onChange={(e) => setGlobalSettings({ ...globalSettings, emergency_stop: e.target.checked })}
                className="rounded border-slate-300"
              />
              <span className="text-sm font-medium text-red-600 dark:text-red-400">紧急停止所有自动操作</span>
            </label>

            <div>
              <label className="block text-sm font-medium text-slate-700 dark:text-slate-300 mb-2">
                每日最大交易次数: {globalSettings.max_daily_trades}
              </label>
              <input
                type="range"
                min="1"
                max="100"
                step="1"
                value={globalSettings.max_daily_trades}
                onChange={(e) => setGlobalSettings({ ...globalSettings, max_daily_trades: Number(e.target.value) })}
                className="w-full"
              />
            </div>

            <div>
              <label className="block text-sm font-medium text-slate-700 dark:text-slate-300 mb-2">
                最大总敞口: {(globalSettings.max_total_exposure * 100).toFixed(0)}%
              </label>
              <input
                type="range"
                min="10"
                max="100"
                step="5"
                value={globalSettings.max_total_exposure * 100}
                onChange={(e) => setGlobalSettings({ ...globalSettings, max_total_exposure: Number(e.target.value) / 100 })}
                className="w-full"
              />
            </div>

            <Select
              label="风险等级"
              value={globalSettings.risk_level}
              onChange={(e) => setGlobalSettings({ ...globalSettings, risk_level: e.target.value })}
              options={[
                { value: "low", label: "低" },
                { value: "medium", label: "中" },
                { value: "high", label: "高" },
              ]}
            />

            <div className="flex gap-3 pt-4 border-t border-slate-200 dark:border-slate-700">
              <Button variant="secondary" onClick={() => setShowSettings(false)} className="flex-1">
                取消
              </Button>
              <Button onClick={() => updateGlobalSettings(globalSettings)} className="flex-1">
                保存设置
              </Button>
            </div>
          </div>
        </Dialog>
      )}
    </div>
  );
}

// 统计卡片
function StatCard({
  label,
  value,
  subtext,
  icon,
  color,
}: {
  label: string;
  value: string;
  subtext?: string;
  icon?: React.ReactNode;
  color?: "emerald" | "red";
}) {
  const colorClasses = {
    emerald: "text-emerald-600 dark:text-emerald-400",
    red: "text-red-600 dark:text-red-400",
  };

  return (
    <div className="bg-white dark:bg-slate-800 rounded-xl border border-slate-200 dark:border-slate-700 p-4">
      <div className="flex items-center justify-between">
        <div>
          <p className="text-sm text-slate-500 dark:text-slate-400">{label}</p>
          <p className={`text-xl font-bold ${color ? colorClasses[color] : "text-slate-900 dark:text-white"}`}>{value}</p>
          {subtext && <p className={`text-xs ${color ? colorClasses[color] : "text-slate-500"}`}>{subtext}</p>}
        </div>
        {icon && <span className={color ? colorClasses[color] : "text-slate-400"}>{icon}</span>}
      </div>
    </div>
  );
}

// 对话框组件
function Dialog({
  title,
  children,
  onClose,
  size = "md",
}: {
  title: string;
  children: React.ReactNode;
  onClose: () => void;
  size?: "md" | "lg";
}) {
  const sizeClasses = { md: "max-w-md", lg: "max-w-2xl" };

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4">
      <div className={`bg-white dark:bg-slate-800 rounded-xl shadow-2xl w-full ${sizeClasses[size]} max-h-[90vh] overflow-y-auto`}>
        <div className="flex items-center justify-between p-4 border-b border-slate-200 dark:border-slate-700">
          <h3 className="text-lg font-bold text-slate-900 dark:text-white">{title}</h3>
          <button onClick={onClose} className="p-1 hover:bg-slate-100 dark:hover:bg-slate-700 rounded-lg transition-colors">
            <Close className="w-5 h-5 text-slate-500" />
          </button>
        </div>
        <div className="p-4">{children}</div>
      </div>
    </div>
  );
}
