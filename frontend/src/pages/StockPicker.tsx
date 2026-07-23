/**
 * 智能选股页面 - 现代化重构版
 */
import React, { useState, useEffect } from 'react';
import {
  FilterList,
  TrendingUp,
  TrendingDown,
  Add,
  Delete,
  DeleteSweep,
  Analytics,
  ExpandMore,
  ExpandLess,
  Close,
  Search as SearchIcon,
  Refresh,
} from '@mui/icons-material';
import {
  PageHeader,
  Card,
  CardHeader,
  Button,
  Badge,
  Input,
  Tabs,
  ProgressBar,
  Alert,
  EmptyState,
  LoadingSpinner,
} from '../components/ui';
import {
  getPools,
  getAnalysisResults,
  addStock,
  batchAddStocks,
  removeStock,
  toggleStock,
  clearPool,
  analyzeStocks,
  searchSecurities,
  getScreenerStrategies,
  searchScreenerCandidates,
  importScreenerCandidates,
  type Stock,
  type Analysis,
  type PoolsResponse,
  type AnalysisResponse,
  type SecurityMarket,
  type SecuritySearchItem,
  type ScreenerMarket,
  type ScreenerStrategy,
  type ScreenerCandidate,
  type ScreenerSearchResponse,
  type ScreenerIndexFilters,
} from '../api/stockPicker';
import { API_BASE } from '../api/client';

export default function StockPicker() {
  const [pools, setPools] = useState<PoolsResponse>({ long_pool: [], short_pool: [] });
  const [analysis, setAnalysis] = useState<AnalysisResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [analyzing, setAnalyzing] = useState(false);
  const [showAddDialog, setShowAddDialog] = useState(false);
  const [showDiscoveryDialog, setShowDiscoveryDialog] = useState(false);
  const [addDialogType, setAddDialogType] = useState<'LONG' | 'SHORT'>('LONG');
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  const [analysisLogs, setAnalysisLogs] = useState<string[]>([]);
  const [showLogs, setShowLogs] = useState(false);
  const [analysisProgress, setAnalysisProgress] = useState({
    current: '',
    total: 0,
    completed: 0,
    status: 'idle' as 'idle' | 'queued' | 'running' | 'completed' | 'error',
  });

  const loadPools = async () => {
    try {
      setLoading(true);
      const data = await getPools();
      setPools(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : '加载失败');
    } finally {
      setLoading(false);
    }
  };

  const loadAnalysis = async () => {
    try {
      const data = await getAnalysisResults({ sort_by: 'recommendation' });
      setAnalysis(data);
    } catch (err) {
      console.error('加载分析结果失败:', err);
    }
  };

  const handleAnalyze = async (
    poolType?: 'LONG' | 'SHORT',
    forceRefresh = false,
  ) => {
    setAnalyzing(true);
    setError(null);
    setAnalysisLogs([]);
    setShowLogs(true);
    setAnalysisProgress({ current: '', total: 0, completed: 0, status: 'queued' });

    try {
      const result = await analyzeStocks({
        pool_type: poolType,
        force_refresh: forceRefresh,
      });
      setSuccess(result.message);

      const eventSource = new EventSource(
        `${API_BASE}/api/stock-picker/analysis/progress/${result.job_id}`,
      );
      eventSource.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data);
          setAnalysisProgress({
            current: data.current || '',
            total: data.total || 0,
            completed: data.completed || 0,
            status: data.status || 'queued',
          });
          if (data.logs && data.logs.length > 0) {
            setAnalysisLogs(data.logs.map((log: { message: string }) => log.message));
          }
          if (data.status === 'completed') {
            eventSource.close();
            const summary = data.result;
            setSuccess(
              summary
                ? `分析完成：成功 ${summary.success}，跳过 ${summary.skipped}，失败 ${summary.failed}`
                : '分析完成',
            );
            setAnalyzing(false);
            loadAnalysis();
          } else if (data.status === 'error') {
            eventSource.close();
            setError(data.error || '分析任务失败');
            setAnalyzing(false);
          }
        } catch (e) {
          console.error('解析进度数据失败:', e);
        }
      };
      eventSource.onerror = () => {
        eventSource.close();
        setError('分析进度连接已断开');
        setAnalyzing(false);
      };
    } catch (err) {
      setError(err instanceof Error ? err.message : '分析失败');
      setAnalyzing(false);
    }
  };

  const handleRemove = async (id: number) => {
    if (!confirm('确定要删除这只股票吗？')) return;
    try {
      await removeStock(id);
      setSuccess('删除成功');
      await Promise.all([loadPools(), loadAnalysis()]);
    } catch (err) {
      setError(err instanceof Error ? err.message : '删除失败');
    }
  };

  const handleToggle = async (id: number) => {
    try {
      await toggleStock(id);
      setSuccess('股票状态已更新');
      await Promise.all([loadPools(), loadAnalysis()]);
    } catch (err) {
      setError(err instanceof Error ? err.message : '状态更新失败');
    }
  };

  const handleClear = async (type: 'LONG' | 'SHORT') => {
    const poolName = type === 'LONG' ? '做多' : '做空';
    if (!confirm(`确定要清空${poolName}股票池吗？此操作不可恢复！`)) return;
    try {
      await clearPool(type);
      setSuccess(`${poolName}股票池已清空`);
      loadPools();
      loadAnalysis();
    } catch (err) {
      setError(err instanceof Error ? err.message : '清空失败');
    }
  };

  const openAddDialog = (type: 'LONG' | 'SHORT') => {
    setAddDialogType(type);
    setShowAddDialog(true);
  };

  useEffect(() => {
    loadPools();
    loadAnalysis();
  }, []);

  if (loading) {
    return <LoadingSpinner size="lg" text="加载股票池..." />;
  }

  return (
    <div className="space-y-6 animate-fade-in">
      <PageHeader
        title="智能选股分析"
        description="AI驱动的多维度量化评分系统"
        icon={<FilterList />}
        actions={
          <div className="flex gap-2">
            <Button
              variant="secondary"
              onClick={() => setShowDiscoveryDialog(true)}
              icon={<SearchIcon className="w-4 h-4" />}
            >
              主动发现
            </Button>
            <Button
              onClick={() => handleAnalyze()}
              loading={analyzing}
              icon={<Analytics className="w-4 h-4" />}
            >
              分析全部
            </Button>
            <Button
              variant="secondary"
              onClick={() => handleAnalyze(undefined, true)}
              disabled={analyzing}
              icon={<Refresh className="w-4 h-4" />}
            >
              强制重算
            </Button>
          </div>
        }
      />

      {/* 消息提示 */}
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
      {analysis && (
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
          <StatCard
            label="做多股票"
            value={analysis.stats.long_count}
            color="emerald"
            icon={<TrendingUp className="w-5 h-5" />}
          />
          <StatCard
            label="做多平均分"
            value={`${analysis.stats.long_avg_score.toFixed(1)}`}
            color="emerald"
          />
          <StatCard
            label="做空股票"
            value={analysis.stats.short_count}
            color="red"
            icon={<TrendingDown className="w-5 h-5" />}
          />
          <StatCard
            label="做空平均分"
            value={`${analysis.stats.short_avg_score.toFixed(1)}`}
            color="red"
          />
        </div>
      )}

      {/* 股票池 */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <StockPoolCard
          title="做多股票池"
          type="LONG"
          stocks={pools.long_pool}
          analysis={analysis?.long_analysis || []}
          onAdd={() => openAddDialog('LONG')}
          onRemove={handleRemove}
          onToggle={handleToggle}
          onAnalyze={() => handleAnalyze('LONG')}
          onForceAnalyze={() => handleAnalyze('LONG', true)}
          onClear={() => handleClear('LONG')}
          analyzing={analyzing}
        />
        <StockPoolCard
          title="做空股票池"
          type="SHORT"
          stocks={pools.short_pool}
          analysis={analysis?.short_analysis || []}
          onAdd={() => openAddDialog('SHORT')}
          onRemove={handleRemove}
          onToggle={handleToggle}
          onAnalyze={() => handleAnalyze('SHORT')}
          onForceAnalyze={() => handleAnalyze('SHORT', true)}
          onClear={() => handleClear('SHORT')}
          analyzing={analyzing}
        />
      </div>

      {/* 分析进度 */}
      {showLogs && (
        <Card>
          <CardHeader
            title="分析进度"
            icon={<Analytics className="w-5 h-5" />}
            action={
              <button
                aria-label="关闭分析进度"
                onClick={() => setShowLogs(false)}
                className="p-1 hover:bg-slate-100 dark:hover:bg-slate-700 rounded"
              >
                <Close className="w-5 h-5 text-slate-500" />
              </button>
            }
          />

          {analysisProgress.status !== 'idle' && (
            <div className="mb-4">
              <div className="flex justify-between text-sm mb-2">
                <span className="text-slate-600 dark:text-slate-400">
                  {analysisProgress.status === 'running'
                    ? `正在分析: ${analysisProgress.current}`
                    : analysisProgress.status === 'completed'
                      ? '分析完成'
                      : '准备中...'}
                </span>
                <span className="font-medium text-slate-900 dark:text-white">
                  {analysisProgress.completed} / {analysisProgress.total}
                </span>
              </div>
              <ProgressBar
                value={analysisProgress.completed}
                max={analysisProgress.total}
                variant={analysisProgress.status === 'completed' ? 'success' : 'default'}
              />
            </div>
          )}

          {analysisLogs.length > 0 && (
            <div className="bg-slate-900 rounded-lg p-4 max-h-48 overflow-y-auto font-mono text-sm">
              {analysisLogs.map((log, i) => (
                <div key={i} className="text-emerald-400 mb-1">
                  {log}
                </div>
              ))}
              {analyzing && <span className="text-emerald-400 animate-pulse">▋</span>}
            </div>
          )}
        </Card>
      )}

      {/* 添加股票对话框 */}
      {showAddDialog && (
        <AddStockDialog
          type={addDialogType}
          onClose={() => setShowAddDialog(false)}
          onSuccess={() => {
            setShowAddDialog(false);
            loadPools();
            setSuccess('添加成功');
          }}
        />
      )}

      {showDiscoveryDialog && (
        <StockDiscoveryDialog
          onClose={() => setShowDiscoveryDialog(false)}
          onComplete={(successCount, failedCount) => {
            setShowDiscoveryDialog(false);
            loadPools();
            if (failedCount > 0) {
              setError(`成功导入 ${successCount} 只，失败 ${failedCount} 只`);
            } else {
              setSuccess(`已导入 ${successCount} 只候选股票，可继续执行量化分析`);
            }
          }}
        />
      )}
    </div>
  );
}

// 统计卡片
function StatCard({
  label,
  value,
  color,
  icon,
}: {
  label: string;
  value: string | number;
  color: 'emerald' | 'red';
  icon?: React.ReactNode;
}) {
  const colorClasses = {
    emerald: 'bg-emerald-50 dark:bg-emerald-900/20 border-emerald-200 dark:border-emerald-800',
    red: 'bg-red-50 dark:bg-red-900/20 border-red-200 dark:border-red-800',
  };
  const textColor = color === 'emerald' ? 'text-emerald-600 dark:text-emerald-400' : 'text-red-600 dark:text-red-400';

  return (
    <div className={`rounded-xl border p-4 ${colorClasses[color]}`}>
      <div className="flex items-center justify-between">
        <div>
          <p className="text-sm text-slate-600 dark:text-slate-400">{label}</p>
          <p className={`text-2xl font-bold ${textColor}`}>{value}</p>
        </div>
        {icon && <span className={textColor}>{icon}</span>}
      </div>
    </div>
  );
}

// 股票池卡片
function StockPoolCard({
  title,
  type,
  stocks,
  analysis,
  onAdd,
  onRemove,
  onToggle,
  onAnalyze,
  onForceAnalyze,
  onClear,
  analyzing,
}: {
  title: string;
  type: 'LONG' | 'SHORT';
  stocks: Stock[];
  analysis: Analysis[];
  onAdd: () => void;
  onRemove: (id: number) => void;
  onToggle: (id: number) => void;
  onAnalyze: () => void;
  onForceAnalyze: () => void;
  onClear: () => void;
  analyzing: boolean;
}) {
  const isLong = type === 'LONG';
  const borderColor = isLong
    ? 'border-l-emerald-500'
    : 'border-l-red-500';
  const activeCount = stocks.filter((stock) => stock.is_active).length;
  const stockRows = stocks
    .map((stock) => ({
      stock,
      analysis: analysis.find((item) => item.pool_id === stock.id),
    }))
    .sort((left, right) => {
      if (left.stock.is_active !== right.stock.is_active) {
        return left.stock.is_active ? -1 : 1;
      }
      return (right.analysis?.recommendation_score ?? -1)
        - (left.analysis?.recommendation_score ?? -1);
    });

  return (
    <Card className={`border-l-4 ${borderColor}`}>
      <CardHeader
        title={title}
        icon={isLong ? <TrendingUp className="w-5 h-5 text-emerald-500" /> : <TrendingDown className="w-5 h-5 text-red-500" />}
        action={
          <div className="flex gap-2">
            <Badge variant={isLong ? 'success' : 'danger'}>
              {activeCount} 启用 / {stocks.length} 总计
            </Badge>
          </div>
        }
      />

      <div className="flex gap-2 mb-4">
        <Button size="sm" variant="secondary" onClick={onAdd} icon={<Add className="w-4 h-4" />}>
          添加
        </Button>
        <Button size="sm" variant="secondary" onClick={onAnalyze} disabled={analyzing} icon={<Analytics className="w-4 h-4" />}>
          分析
        </Button>
        <Button size="sm" variant="secondary" onClick={onForceAnalyze} disabled={analyzing} icon={<Refresh className="w-4 h-4" />}>
          重算
        </Button>
        <Button size="sm" variant="danger" onClick={onClear} disabled={stocks.length === 0} icon={<DeleteSweep className="w-4 h-4" />}>
          清空
        </Button>
      </div>

      <div className="space-y-3 max-h-[500px] overflow-y-auto">
        {stocks.length === 0 ? (
          <EmptyState
            title="股票池为空"
            description="添加股票后可执行分析"
            icon={<Analytics />}
          />
        ) : (
          stockRows.map(({ stock, analysis: item }, index) => (
            <StockItem
              key={stock.id}
              rank={item ? index + 1 : undefined}
              stock={stock}
              analysis={item}
              type={type}
              onRemove={() => onRemove(stock.id)}
              onToggle={() => onToggle(stock.id)}
            />
          ))
        )}
      </div>
    </Card>
  );
}

// 股票项
function StockItem({
  rank,
  stock,
  analysis,
  type,
  onRemove,
  onToggle,
}: {
  rank?: number;
  stock: Stock;
  analysis?: Analysis;
  type: 'LONG' | 'SHORT';
  onRemove: () => void;
  onToggle: () => void;
}) {
  const [expanded, setExpanded] = useState(false);

  if (!analysis) {
    return (
      <div className={`rounded-lg p-4 ${stock.is_active ? 'bg-slate-50 dark:bg-slate-800/50' : 'bg-slate-100/60 dark:bg-slate-900/40 opacity-75'}`}>
        <div className="flex items-start justify-between gap-3">
          <div>
            <div className="flex items-center gap-2">
              <span className="font-bold text-slate-900 dark:text-white">{stock.symbol}</span>
              <Badge variant={stock.is_active ? 'info' : 'default'}>
                {stock.is_active ? '待分析' : '已停用'}
              </Badge>
            </div>
            {stock.name && <p className="text-sm text-slate-500 dark:text-slate-400">{stock.name}</p>}
            {stock.added_reason && <p className="mt-2 text-sm text-slate-500">{stock.added_reason}</p>}
          </div>
          <div className="flex items-center gap-2">
            <button
              onClick={onToggle}
              className="text-xs font-medium text-cyan-600 dark:text-cyan-400 hover:text-cyan-700"
            >
              {stock.is_active ? '停用' : '启用'}
            </button>
            <button
              aria-label={`删除 ${stock.symbol}`}
              onClick={onRemove}
              className="p-1 text-slate-400 hover:text-red-500 transition-colors"
            >
              <Delete className="w-4 h-4" />
            </button>
          </div>
        </div>
      </div>
    );
  }

  const gradeStyles: Record<string, string> = {
    A: 'bg-emerald-100 text-emerald-700 dark:bg-emerald-900/50 dark:text-emerald-400',
    B: 'bg-amber-100 text-amber-700 dark:bg-amber-900/50 dark:text-amber-400',
    C: 'bg-orange-100 text-orange-700 dark:bg-orange-900/50 dark:text-orange-400',
    D: 'bg-red-100 text-red-700 dark:bg-red-900/50 dark:text-red-400',
  };

  const priceChangeColor = analysis.price_change_1d >= 0
    ? 'text-emerald-600 dark:text-emerald-400'
    : 'text-red-600 dark:text-red-400';
  const aiStatus = analysis.ai_decision.status || 'available';
  const aiStatusLabels: Record<string, string> = {
    available: 'AI 已完成',
    disabled: 'AI 未配置',
    skipped: '量化初筛',
    fallback: 'AI 已降级',
    error: 'AI 错误',
  };
  const aiStatusVariant: 'success' | 'warning' | 'default' = aiStatus === 'available'
    ? 'success'
    : aiStatus === 'fallback' || aiStatus === 'error'
      ? 'warning'
      : 'default';

  return (
    <div className="bg-slate-50 dark:bg-slate-800/50 rounded-lg p-4 hover:bg-slate-100 dark:hover:bg-slate-800 transition-colors">
      <div className="flex items-start justify-between">
        <div className="flex items-center gap-3">
          <span className="text-sm font-medium text-slate-400 w-6">#{rank}</span>
          <div>
            <div className="flex items-center gap-2">
              <span className="font-bold text-slate-900 dark:text-white">{analysis.symbol}</span>
              <span className={`px-2 py-0.5 rounded text-xs font-medium ${gradeStyles[analysis.score.grade] || gradeStyles.D}`}>
                {analysis.score.grade}级
              </span>
            </div>
            {analysis.name && (
              <p className="text-sm text-slate-500 dark:text-slate-400">{analysis.name}</p>
            )}
          </div>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={onToggle}
            className="text-xs font-medium text-cyan-600 dark:text-cyan-400 hover:text-cyan-700"
          >
            {stock.is_active ? '停用' : '启用'}
          </button>
          <button
            aria-label={`删除 ${analysis.symbol}`}
            onClick={onRemove}
            className="p-1 text-slate-400 hover:text-red-500 transition-colors"
          >
            <Delete className="w-4 h-4" />
          </button>
        </div>
      </div>

      {/* 价格和评分 */}
      <div className="mt-3 flex items-end justify-between">
        <div>
          {analysis.current_price > 0 && (
            <div className="flex items-baseline gap-2">
              <span className="text-xl font-bold text-slate-900 dark:text-white">
                ${analysis.current_price.toFixed(2)}
              </span>
              <span className={`text-sm font-medium ${priceChangeColor}`}>
                {analysis.price_change_1d >= 0 ? '+' : ''}{analysis.price_change_1d.toFixed(2)}%
              </span>
            </div>
          )}
        </div>
        <div className="text-right">
          <p className="text-sm text-slate-500">机会分</p>
          <p className="text-xl font-bold text-cyan-600 dark:text-cyan-400">
            {analysis.recommendation_score.toFixed(0)}/100
          </p>
          <p className="text-xs text-slate-500">技术分 {analysis.score.total.toFixed(0)}</p>
        </div>
      </div>

      {/* 推荐理由 */}
      <p className="mt-3 text-sm text-slate-600 dark:text-slate-400 line-clamp-2">
        {analysis.recommendation_reason}
      </p>

      {/* 信号标签 */}
      <div className="mt-3 flex flex-wrap gap-1">
        {analysis.signals.slice(0, 3).map((signal, i) => (
          <span key={i} className="px-2 py-0.5 bg-cyan-100 dark:bg-cyan-900/30 text-cyan-700 dark:text-cyan-400 rounded text-xs">
            {signal}
          </span>
        ))}
      </div>

      {/* 展开详情 */}
      <button
        onClick={() => setExpanded(!expanded)}
        className="mt-3 flex items-center gap-1 text-sm text-cyan-600 dark:text-cyan-400 hover:text-cyan-700"
      >
        {expanded ? <ExpandLess className="w-4 h-4" /> : <ExpandMore className="w-4 h-4" />}
        {expanded ? '收起' : '详情'}
      </button>

      {expanded && (
        <div className="mt-4 pt-4 border-t border-slate-200 dark:border-slate-700 space-y-4">
          {/* 评分细节 */}
          <div>
            <p className="text-sm font-medium text-slate-700 dark:text-slate-300 mb-2">评分细节</p>
            <div className="space-y-2">
              <ScoreRow label="趋势" value={analysis.score.breakdown.trend} max={25} />
              <ScoreRow label="动量" value={analysis.score.breakdown.momentum} max={20} />
              <ScoreRow label="支撑阻力" value={analysis.score.breakdown.support_resistance || 0} max={15} />
              <ScoreRow label="量能" value={analysis.score.breakdown.volume} max={15} />
              <ScoreRow label="形态" value={analysis.score.breakdown.pattern} max={15} />
              <ScoreRow label="波动" value={analysis.score.breakdown.volatility} max={10} />
            </div>
          </div>

          {/* AI分析 */}
          <div>
            <div className="flex items-center justify-between gap-2 mb-2">
              <p className="text-sm font-medium text-slate-700 dark:text-slate-300">分析结论</p>
              <Badge variant={aiStatusVariant}>
                {aiStatusLabels[aiStatus] || aiStatus}
              </Badge>
            </div>
            {analysis.metadata && (
              <p className="mb-2 text-xs text-slate-500">
                数据截止 {analysis.metadata.data_as_of || '-'}
                {' · '}评分 {analysis.metadata.score_version || '-'}
                {' · '}模式 {analysis.metadata.analysis_mode || '-'}
              </p>
            )}
            <ul className="space-y-1">
              {analysis.ai_decision.reasoning.map((reason, i) => (
                <li key={i} className="text-sm text-slate-600 dark:text-slate-400 flex items-start gap-2">
                  <span className="text-cyan-500 mt-1">•</span>
                  {reason}
                </li>
              ))}
            </ul>
          </div>
        </div>
      )}
    </div>
  );
}

// 评分行
function ScoreRow({ label, value, max }: { label: string; value: number; max: number }) {
  const percentage = Math.min(100, Math.max(0, (value / max) * 100));
  return (
    <div className="flex items-center gap-3">
      <span className="text-xs text-slate-500 w-14">{label}</span>
      <div className="flex-1 h-1.5 bg-slate-200 dark:bg-slate-700 rounded-full overflow-hidden">
        <div
          className="h-full bg-cyan-500 rounded-full transition-all"
          style={{ width: `${percentage}%` }}
        />
      </div>
      <span className="text-xs font-medium text-slate-600 dark:text-slate-400 w-12 text-right">
        {value.toFixed(0)}/{max}
      </span>
    </div>
  );
}

function StockDiscoveryDialog({
  onClose,
  onComplete,
}: {
  onClose: () => void;
  onComplete: (successCount: number, failedCount: number) => void;
}) {
  const [market, setMarket] = useState<ScreenerMarket>('US');
  const [poolType, setPoolType] = useState<'LONG' | 'SHORT'>('LONG');
  const [strategies, setStrategies] = useState<ScreenerStrategy[]>([]);
  const [selectedStrategyId, setSelectedStrategyId] = useState<number | ''>('');
  const [result, setResult] = useState<ScreenerSearchResponse | null>(null);
  const [selectedSymbols, setSelectedSymbols] = useState<Set<string>>(new Set());
  const [loadingStrategies, setLoadingStrategies] = useState(false);
  const [searching, setSearching] = useState(false);
  const [importing, setImporting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showFilters, setShowFilters] = useState(false);
  const [filterInputs, setFilterInputs] = useState({
    min_turnover: '',
    min_market_value: '',
    min_turnover_rate: '',
    min_pe_ttm: '',
    max_pe_ttm: '',
    max_pb: '',
    min_capital_flow: '',
    min_volume_ratio: '',
    min_market_rs_10d: '',
    min_market_rs_half_year: '',
    min_industry_rs_10d: '',
    min_industry_rs_half_year: '',
    max_days_to_cover: '',
    max_short_ratio: '',
    max_short_ratio_change: '',
  });

  useEffect(() => {
    let cancelled = false;
    setLoadingStrategies(true);
    setError(null);
    setStrategies([]);
    setSelectedStrategyId('');
    setResult(null);
    setSelectedSymbols(new Set());

    getScreenerStrategies({ market })
      .then((response) => {
        if (cancelled) return;
        setStrategies(response.items);
        setSelectedStrategyId(response.items[0]?.id || '');
      })
      .catch((err) => {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : '获取选股策略失败');
        }
      })
      .finally(() => {
        if (!cancelled) setLoadingStrategies(false);
      });

    return () => {
      cancelled = true;
    };
  }, [market]);

  const selectedStrategy = strategies.find(
    (strategy) => strategy.id === selectedStrategyId,
  );

  const runSearch = async (page = 0) => {
    if (!selectedStrategy) {
      setError('请选择 Longbridge 选股策略');
      return;
    }
    setSearching(true);
    setError(null);
    try {
      const filters = Object.fromEntries(
        Object.entries(filterInputs)
          .filter(([, value]) => value.trim() !== '')
          .map(([key, value]) => [key, Number(value)]),
      ) as ScreenerIndexFilters;
      const response = await searchScreenerCandidates({
        market,
        strategyId: selectedStrategy.id,
        page,
        size: 20,
        filters,
        targetDirection: poolType,
      });
      setResult(response);
      setSelectedSymbols(new Set(response.items.map((item) => item.symbol)));
    } catch (err) {
      setError(err instanceof Error ? err.message : '主动选股失败');
    } finally {
      setSearching(false);
    }
  };

  const toggleCandidate = (candidate: ScreenerCandidate) => {
    setSelectedSymbols((current) => {
      const next = new Set(current);
      if (next.has(candidate.symbol)) {
        next.delete(candidate.symbol);
      } else {
        next.add(candidate.symbol);
      }
      return next;
    });
  };

  const handleImport = async () => {
    if (!selectedStrategy || !result) return;
    const selectedItems = result.items.filter((item) => selectedSymbols.has(item.symbol));
    if (selectedItems.length === 0) {
      setError('请至少选择一只候选股票');
      return;
    }

    setImporting(true);
    setError(null);
    try {
      const imported = await importScreenerCandidates({
        poolType,
        strategy: selectedStrategy,
        items: selectedItems,
      });
      if (imported.success_count === 0 && imported.failed.length > 0) {
        setError(imported.failed[0]?.error || '候选股票导入失败');
        return;
      }
      onComplete(imported.success_count, imported.failed.length);
    } catch (err) {
      setError(err instanceof Error ? err.message : '候选股票导入失败');
    } finally {
      setImporting(false);
    }
  };

  const selectedCount = selectedSymbols.size;
  const formatIndex = (value: number | null | undefined) => {
    if (value == null) return '-';
    const absolute = Math.abs(value);
    if (absolute >= 1_000_000_000) return `${(value / 1_000_000_000).toFixed(1)}B`;
    if (absolute >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
    if (absolute >= 1_000) return `${(value / 1_000).toFixed(1)}K`;
    return value.toFixed(2);
  };
  const indicatorNumber = (value: unknown) => {
    const number = Number(value);
    return Number.isFinite(number) ? number : null;
  };
  const filterDefinitions: Array<
    [keyof typeof filterInputs, string, string]
  > = [
    ['min_turnover', '最低成交额', '市场币种'],
    ['min_market_value', '最低总市值', '市场币种'],
    ['min_turnover_rate', '最低换手率', 'SDK 数值'],
    ['min_volume_ratio', '最低量比', '例如 1'],
    ['min_pe_ttm', '最低 PE(TTM)', '例如 0'],
    ['max_pe_ttm', '最高 PE(TTM)', '例如 40'],
    ['max_pb', '最高 PB', '例如 8'],
    ['min_capital_flow', '最低资金流', '可输入负数'],
    ['min_market_rs_10d', '最低市场 RS(10日)', '方向化差值'],
    ['min_market_rs_half_year', '最低市场 RS(半年)', '方向化差值'],
    ['min_industry_rs_10d', '最低行业 RS(10日)', '同页行业中位数'],
    ['min_industry_rs_half_year', '最低行业 RS(半年)', '同页行业中位数'],
    ...(poolType === 'SHORT'
      ? [
          ['max_days_to_cover', '最高回补天数', '例如 5'],
          ['max_short_ratio', '最高做空比例', 'SDK 数值'],
          ['max_short_ratio_change', '最高做空比例增量', '可输入负数'],
        ] as Array<[keyof typeof filterInputs, string, string]>
      : []),
  ];

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4">
      <div className="max-h-[calc(100vh-2rem)] w-full max-w-3xl overflow-y-auto rounded-xl bg-white p-6 shadow-2xl dark:bg-slate-800">
        <div className="mb-5 flex items-start justify-between gap-4">
          <div>
            <h3 className="text-xl font-bold text-slate-900 dark:text-white">
              Longbridge 主动发现
            </h3>
            <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">
              使用官方 Screener 生成候选集，导入后继续走现有量化初筛和 Top N AI。
            </p>
          </div>
          <button
            aria-label="关闭主动发现弹窗"
            onClick={onClose}
            className="rounded p-1 hover:bg-slate-100 dark:hover:bg-slate-700"
          >
            <Close className="h-5 w-5 text-slate-500" />
          </button>
        </div>

        <div className="grid gap-4 md:grid-cols-2">
          <div>
            <label className="mb-2 block text-sm font-medium text-slate-700 dark:text-slate-300">
              市场
            </label>
            <div className="grid grid-cols-4 gap-2">
              {([
                ['US', '美股'],
                ['HK', '港股'],
                ['CN', 'A股'],
                ['SG', '新加坡'],
              ] as Array<[ScreenerMarket, string]>).map(([value, label]) => (
                <button
                  key={value}
                  type="button"
                  onClick={() => setMarket(value)}
                  className={`rounded-lg border px-2 py-2 text-sm font-medium transition-colors ${
                    market === value
                      ? 'border-cyan-500 bg-cyan-50 text-cyan-700 dark:bg-cyan-900/30 dark:text-cyan-300'
                      : 'border-slate-200 text-slate-600 dark:border-slate-600 dark:text-slate-300'
                  }`}
                >
                  {label}
                </button>
              ))}
            </div>
          </div>

          <div>
            <label className="mb-2 block text-sm font-medium text-slate-700 dark:text-slate-300">
              导入方向
            </label>
            <div className="grid grid-cols-2 gap-2">
              {([
                ['LONG', '做多池'],
                ['SHORT', '做空池'],
              ] as Array<['LONG' | 'SHORT', string]>).map(([value, label]) => (
                <button
                  key={value}
                  type="button"
                  onClick={() => {
                    setPoolType(value);
                    setResult(null);
                    setSelectedSymbols(new Set());
                    if (value === 'LONG') {
                      setFilterInputs((current) => ({
                        ...current,
                        max_days_to_cover: '',
                        max_short_ratio: '',
                        max_short_ratio_change: '',
                      }));
                    }
                  }}
                  className={`rounded-lg border px-3 py-2 text-sm font-medium transition-colors ${
                    poolType === value
                      ? value === 'LONG'
                        ? 'border-emerald-500 bg-emerald-50 text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-300'
                        : 'border-red-500 bg-red-50 text-red-700 dark:bg-red-900/30 dark:text-red-300'
                      : 'border-slate-200 text-slate-600 dark:border-slate-600 dark:text-slate-300'
                  }`}
                >
                  {label}
                </button>
              ))}
            </div>
          </div>
        </div>

        <div className="mt-4 flex items-end gap-3">
          <div className="flex-1">
            <label
              htmlFor="screener-strategy"
              className="mb-1 block text-sm font-medium text-slate-700 dark:text-slate-300"
            >
              官方策略
            </label>
            <select
              id="screener-strategy"
              value={selectedStrategyId}
              onChange={(event) => {
                setSelectedStrategyId(Number(event.target.value));
                setResult(null);
                setSelectedSymbols(new Set());
              }}
              disabled={loadingStrategies || strategies.length === 0}
              className="w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm
                text-slate-900 focus:border-cyan-500 focus:outline-none focus:ring-2
                focus:ring-cyan-500/50 disabled:opacity-60 dark:border-slate-600
                dark:bg-slate-900 dark:text-white"
            >
              {strategies.length === 0 && (
                <option value="">
                  {loadingStrategies ? '正在加载策略…' : '没有可用策略'}
                </option>
              )}
              {strategies.map((strategy) => (
                <option key={strategy.id} value={strategy.id}>
                  {strategy.name}{strategy.source === 'user' ? '（我的策略）' : ''}
                </option>
              ))}
            </select>
          </div>
          <Button
            type="button"
            onClick={() => runSearch(0)}
            loading={searching}
            disabled={!selectedStrategy}
            icon={<SearchIcon className="h-4 w-4" />}
          >
            筛选候选
          </Button>
        </div>

        {selectedStrategy?.description && (
          <p className="mt-2 text-sm text-slate-500 dark:text-slate-400">
            {selectedStrategy.description}
          </p>
        )}

        <div className="mt-3 rounded-lg border border-slate-200 dark:border-slate-700">
          <button
            type="button"
            onClick={() => setShowFilters((value) => !value)}
            className="flex w-full items-center justify-between px-3 py-2 text-left text-sm font-medium text-slate-700 dark:text-slate-300"
          >
            <span>流动性与估值过滤（可选）</span>
            {showFilters
              ? <ExpandLess className="h-4 w-4" />
              : <ExpandMore className="h-4 w-4" />}
          </button>
          {showFilters && (
            <div className="grid gap-3 border-t border-slate-200 p-3 sm:grid-cols-2 lg:grid-cols-4 dark:border-slate-700">
              {filterDefinitions.map(
                ([key, label, placeholder]) => (
                  <label key={key} className="text-xs text-slate-500">
                    <span className="mb-1 block">{label}</span>
                    <input
                      type="number"
                      step="any"
                      value={filterInputs[key]}
                      onChange={(event) => {
                        setFilterInputs((current) => ({
                          ...current,
                          [key]: event.target.value,
                        }));
                        setResult(null);
                        setSelectedSymbols(new Set());
                      }}
                      placeholder={placeholder}
                      className="w-full rounded-md border border-slate-300 bg-white px-2 py-1.5
                        text-sm text-slate-900 focus:border-cyan-500 focus:outline-none
                        dark:border-slate-600 dark:bg-slate-900 dark:text-white"
                    />
                  </label>
                ),
              )}
              <button
                type="button"
                onClick={() => {
                  setFilterInputs({
                    min_turnover: '',
                    min_market_value: '',
                    min_turnover_rate: '',
                    min_pe_ttm: '',
                    max_pe_ttm: '',
                    max_pb: '',
                    min_capital_flow: '',
                    min_volume_ratio: '',
                    min_market_rs_10d: '',
                    min_market_rs_half_year: '',
                    min_industry_rs_10d: '',
                    min_industry_rs_half_year: '',
                    max_days_to_cover: '',
                    max_short_ratio: '',
                    max_short_ratio_change: '',
                  });
                  setResult(null);
                  setSelectedSymbols(new Set());
                }}
                className="self-end text-left text-xs text-cyan-600 hover:text-cyan-700 dark:text-cyan-400"
              >
                清空过滤条件
              </button>
            </div>
          )}
        </div>

        {error && (
          <div className="mt-4">
            <Alert type="error">{error}</Alert>
          </div>
        )}

        {result && (
          <div className="mt-5">
            <div className="mb-2 flex items-center justify-between gap-3">
              <div>
                <p className="text-sm text-slate-600 dark:text-slate-400">
                  第 {result.page + 1} 页，共 {result.total} 个候选；已选择 {selectedCount} 个
                </p>
                <p className="text-xs text-slate-500">
                  市场基准 {result.relative_strength.benchmark_symbol}；
                  行业 RS 为本页同行中位数差
                </p>
                {result.filters.excluded > 0 && (
                  <p className="text-xs text-amber-600 dark:text-amber-400">
                    本页按指标过滤 {result.filters.excluded} 只，
                    保留 {result.filters.after}/{result.filters.before} 只
                  </p>
                )}
              </div>
              <button
                type="button"
                onClick={() => {
                  if (selectedCount === result.items.length) {
                    setSelectedSymbols(new Set());
                  } else {
                    setSelectedSymbols(new Set(result.items.map((item) => item.symbol)));
                  }
                }}
                className="text-sm text-cyan-600 hover:text-cyan-700 dark:text-cyan-400"
              >
                {selectedCount === result.items.length ? '取消全选' : '全选本页'}
              </button>
            </div>

            {result.enrichment.status === 'fallback' && (
              <div className="mb-2">
                <Alert type="warning">
                  实时指标暂不可用，当前仅展示 Screener 原始结果。
                </Alert>
              </div>
            )}
            {poolType === 'SHORT' && (
              <p className="mb-2 text-xs text-amber-600 dark:text-amber-400">
                做空指标仅用于衡量拥挤度与逼空风险，不代表可借券、融券成本或实际可成交性。
              </p>
            )}
            {poolType === 'SHORT' && result.short_risk.status === 'fallback' && (
              <div className="mb-2">
                <Alert type="warning">
                  做空拥挤度数据暂不可用，未应用相关过滤。
                </Alert>
              </div>
            )}

            <div className="max-h-80 space-y-2 overflow-y-auto rounded-lg border border-slate-200 p-2 dark:border-slate-700">
              {result.items.length === 0 ? (
                <EmptyState
                  title="没有符合条件的股票"
                  description="可尝试其他策略或市场"
                  icon={<SearchIcon />}
                />
              ) : (
                result.items.map((candidate) => (
                  <label
                    key={candidate.symbol}
                    className="flex cursor-pointer items-start gap-3 rounded-lg p-3 hover:bg-slate-50 dark:hover:bg-slate-700/50"
                  >
                    <input
                      type="checkbox"
                      checked={selectedSymbols.has(candidate.symbol)}
                      onChange={() => toggleCandidate(candidate)}
                      className="mt-1 h-4 w-4 rounded border-slate-300 text-cyan-600 focus:ring-cyan-500"
                    />
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center justify-between gap-3">
                        <div className="min-w-0">
                          <span className="font-medium text-slate-900 dark:text-white">
                            {candidate.symbol}
                          </span>
                          <span className="ml-2 truncate text-sm text-slate-500">
                            {candidate.name}
                          </span>
                        </div>
                        <span className="text-xs text-slate-400">#{candidate.rank}</span>
                      </div>
                      <div className="mt-1 flex flex-wrap gap-x-4 gap-y-1 text-xs text-slate-500">
                        <span>成交额 {formatIndex(candidate.indexes.turnover)}</span>
                        <span>市值 {formatIndex(candidate.indexes.total_market_value)}</span>
                        <span>换手 {formatIndex(candidate.indexes.turnover_rate)}</span>
                        <span>资金流 {formatIndex(candidate.indexes.capital_flow)}</span>
                        <span>
                          市场RS(10日) {formatIndex(
                            candidate.relative_strength.market_rs_10d,
                          )}
                        </span>
                        <span>
                          行业RS(10日) {formatIndex(
                            candidate.relative_strength.industry_rs_10d,
                          )}
                        </span>
                        <span>
                          PE {formatIndex(
                            candidate.indexes.pe_ttm_ratio
                              ?? indicatorNumber(candidate.indicators.pettm),
                          )}
                        </span>
                        <span>
                          PB {formatIndex(
                            candidate.indexes.pb_ratio
                              ?? indicatorNumber(candidate.indicators.pbmrq),
                          )}
                        </span>
                        {candidate.indicators.industry != null && (
                          <span>行业 {String(candidate.indicators.industry)}</span>
                        )}
                        {poolType === 'SHORT' && (
                          <>
                            <span>
                              做空比例 {formatIndex(candidate.short_risk.short_ratio)}
                            </span>
                            <span>
                              回补天数 {formatIndex(candidate.short_risk.days_to_cover)}
                            </span>
                          </>
                        )}
                      </div>
                    </div>
                  </label>
                ))
              )}
            </div>

            <div className="mt-3 flex items-center justify-between">
              <div className="flex gap-2">
                <Button
                  type="button"
                  size="sm"
                  variant="secondary"
                  onClick={() => runSearch(result.page - 1)}
                  disabled={searching || result.page === 0}
                >
                  上一页
                </Button>
                <Button
                  type="button"
                  size="sm"
                  variant="secondary"
                  onClick={() => runSearch(result.page + 1)}
                  disabled={searching || !result.has_more}
                >
                  下一页
                </Button>
              </div>
              <p className="text-xs text-slate-500">
                导入只更新观察池，不会自动下单
              </p>
            </div>
          </div>
        )}

        <div className="mt-5 flex gap-3 border-t border-slate-200 pt-4 dark:border-slate-700">
          <Button type="button" variant="secondary" onClick={onClose} className="flex-1">
            取消
          </Button>
          <Button
            type="button"
            onClick={handleImport}
            loading={importing}
            disabled={!result || selectedCount === 0}
            className="flex-1"
          >
            导入 {selectedCount > 0 ? `${selectedCount} 只` : '候选'}
          </Button>
        </div>
      </div>
    </div>
  );
}

// 添加股票对话框
function AddStockDialog({
  type,
  onClose,
  onSuccess,
}: {
  type: 'LONG' | 'SHORT';
  onClose: () => void;
  onSuccess: () => void;
}) {
  const [batchMode, setBatchMode] = useState(false);
  const [manualMode, setManualMode] = useState(false);
  const [market, setMarket] = useState<SecurityMarket>('US');
  const [securityQuery, setSecurityQuery] = useState('');
  const [securityOptions, setSecurityOptions] = useState<SecuritySearchItem[]>([]);
  const [selectedSecurity, setSelectedSecurity] = useState<SecuritySearchItem | null>(null);
  const [searching, setSearching] = useState(false);
  const [hasSearched, setHasSearched] = useState(false);
  const [searchError, setSearchError] = useState<string | null>(null);
  const [activeOption, setActiveOption] = useState(0);
  const [symbol, setSymbol] = useState('');
  const [batchSymbols, setBatchSymbols] = useState('');
  const [name, setName] = useState('');
  const [reason, setReason] = useState('');
  const [clearBeforeAdd, setClearBeforeAdd] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (batchMode || manualMode || selectedSecurity || !securityQuery.trim()) {
      setSecurityOptions([]);
      setHasSearched(false);
      setSearching(false);
      return;
    }

    const controller = new AbortController();
    const timer = window.setTimeout(async () => {
      setSearching(true);
      setSearchError(null);
      try {
        const response = await searchSecurities({
          market,
          query: securityQuery.trim(),
          signal: controller.signal,
        });
        setSecurityOptions(response.items);
        setActiveOption(0);
        setHasSearched(true);
      } catch (err) {
        if (err instanceof DOMException && err.name === 'AbortError') return;
        setSecurityOptions([]);
        setHasSearched(true);
        setSearchError(err instanceof Error ? err.message : '搜索股票失败');
      } finally {
        if (!controller.signal.aborted) setSearching(false);
      }
    }, 300);

    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [batchMode, manualMode, market, securityQuery, selectedSecurity]);

  const selectSecurity = (security: SecuritySearchItem) => {
    setSelectedSecurity(security);
    setSecurityQuery(security.symbol);
    setSecurityOptions([]);
    setSearchError(null);
    setError(null);
  };

  const changeMarket = (nextMarket: SecurityMarket) => {
    setMarket(nextMarket);
    setSelectedSecurity(null);
    setSecurityQuery('');
    setSecurityOptions([]);
    setSearchError(null);
    setHasSearched(false);
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();

    if (batchMode) {
      if (!batchSymbols.trim()) {
        setError('请输入股票代码');
        return;
      }

      setLoading(true);
      setError(null);

      try {
        if (clearBeforeAdd) {
          await clearPool(type);
        }

        const symbols = batchSymbols
          .replace(/[\[\]'"`]/g, '')
          .split(/[,\s\n]+/)
          .map((s) => s.trim().toUpperCase())
          .filter((s) => s.length > 0);

        if (symbols.length === 0) {
          setError('请输入有效的股票代码');
          return;
        }

        const result = await batchAddStocks({ pool_type: type, symbols });

        if (result.failed.length > 0) {
          setError(`成功添加 ${result.success_count} 只，失败 ${result.failed.length} 只`);
        }

        onSuccess();
      } catch (err) {
        setError(err instanceof Error ? err.message : '批量添加失败');
      } finally {
        setLoading(false);
      }
    } else {
      const resolvedSymbol = manualMode ? symbol.trim().toUpperCase() : selectedSecurity?.symbol;
      const resolvedName = manualMode
        ? name.trim() || undefined
        : selectedSecurity?.name || selectedSecurity?.name_en || undefined;

      if (!resolvedSymbol) {
        setError(manualMode ? '请输入股票代码' : '请先搜索并选择一只股票');
        return;
      }

      setLoading(true);
      setError(null);

      try {
        await addStock({
          pool_type: type,
          symbol: resolvedSymbol,
          name: resolvedName,
          added_reason: reason.trim() || undefined,
        });
        onSuccess();
      } catch (err) {
        setError(err instanceof Error ? err.message : '添加失败');
      } finally {
        setLoading(false);
      }
    }
  };

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4">
      <div className="bg-white dark:bg-slate-800 rounded-xl shadow-2xl max-w-md w-full p-6">
        <div className="flex items-center justify-between mb-6">
          <h3 className="text-xl font-bold text-slate-900 dark:text-white">
            添加到{type === 'LONG' ? '做多' : '做空'}池
          </h3>
          <button aria-label="关闭添加弹窗" onClick={onClose} className="p-1 hover:bg-slate-100 dark:hover:bg-slate-700 rounded">
            <Close className="w-5 h-5 text-slate-500" />
          </button>
        </div>

        <Tabs
          tabs={[
            { id: 'single', label: '单个添加' },
            { id: 'batch', label: '批量添加' },
          ]}
          activeTab={batchMode ? 'batch' : 'single'}
          onChange={(id) => setBatchMode(id === 'batch')}
        />

        <form onSubmit={handleSubmit} className="mt-4 space-y-4">
          {batchMode ? (
            <>
              <div>
                <label className="block text-sm font-medium text-slate-700 dark:text-slate-300 mb-1">
                  股票代码列表
                </label>
                <textarea
                  value={batchSymbols}
                  onChange={(e) => setBatchSymbols(e.target.value)}
                  placeholder="每行一个或用逗号分隔&#10;AAPL.US, MSFT.US, GOOGL.US"
                  rows={6}
                  className="w-full px-3 py-2 rounded-lg border border-slate-300 dark:border-slate-600
                    bg-white dark:bg-slate-900 text-slate-900 dark:text-white font-mono text-sm
                    focus:outline-none focus:ring-2 focus:ring-cyan-500/50 resize-none"
                />
              </div>
              <label className="flex items-center gap-2 cursor-pointer">
                <input
                  type="checkbox"
                  checked={clearBeforeAdd}
                  onChange={(e) => setClearBeforeAdd(e.target.checked)}
                  className="w-4 h-4 rounded border-slate-300 text-cyan-600 focus:ring-cyan-500"
                />
                <span className="text-sm text-slate-600 dark:text-slate-400">
                  添加前清空现有股票
                </span>
              </label>
            </>
          ) : (
            <>
              {!manualMode ? (
                <>
                  <div>
                    <label className="block text-sm font-medium text-slate-700 dark:text-slate-300 mb-2">
                      市场
                    </label>
                    <div className="grid grid-cols-3 gap-2">
                      {([
                        ['US', '美股'],
                        ['HK', '港股'],
                        ['CN', 'A股'],
                      ] as Array<[SecurityMarket, string]>).map(([value, label]) => (
                        <button
                          key={value}
                          type="button"
                          onClick={() => changeMarket(value)}
                          className={`rounded-lg border px-3 py-2 text-sm font-medium transition-colors ${
                            market === value
                              ? 'border-cyan-500 bg-cyan-50 text-cyan-700 dark:bg-cyan-900/30 dark:text-cyan-300'
                              : 'border-slate-200 text-slate-600 hover:border-slate-300 dark:border-slate-600 dark:text-slate-300'
                          }`}
                        >
                          {label}
                        </button>
                      ))}
                    </div>
                  </div>

                  <div className="relative">
                    <label
                      htmlFor="security-search"
                      className="block text-sm font-medium text-slate-700 dark:text-slate-300 mb-1"
                    >
                      搜索股票
                    </label>
                    <div className="relative">
                      <SearchIcon className="absolute left-3 top-2.5 w-5 h-5 text-slate-400" />
                      <input
                        id="security-search"
                        role="combobox"
                        aria-autocomplete="list"
                        aria-expanded={securityOptions.length > 0}
                        aria-controls="security-search-options"
                        aria-activedescendant={
                          securityOptions.length > 0
                            ? `security-option-${activeOption}`
                            : undefined
                        }
                        value={securityQuery}
                        onChange={(event) => {
                          setSecurityQuery(event.target.value);
                          setSelectedSecurity(null);
                          setHasSearched(false);
                          setError(null);
                        }}
                        onKeyDown={(event) => {
                          if (event.key === 'ArrowDown' && securityOptions.length > 0) {
                            event.preventDefault();
                            setActiveOption((current) => (current + 1) % securityOptions.length);
                          } else if (event.key === 'ArrowUp' && securityOptions.length > 0) {
                            event.preventDefault();
                            setActiveOption((current) => (
                              current === 0 ? securityOptions.length - 1 : current - 1
                            ));
                          } else if (event.key === 'Enter' && securityOptions[activeOption]) {
                            event.preventDefault();
                            selectSecurity(securityOptions[activeOption]);
                          } else if (event.key === 'Escape') {
                            setSecurityOptions([]);
                          }
                        }}
                        placeholder="输入代码、中文或英文名称"
                        autoComplete="off"
                        className="w-full rounded-lg border border-slate-300 bg-white py-2 pl-10 pr-20
                          text-slate-900 placeholder:text-slate-400 focus:border-cyan-500 focus:outline-none
                          focus:ring-2 focus:ring-cyan-500/50 dark:border-slate-600 dark:bg-slate-900
                          dark:text-white"
                      />
                      {searching && (
                        <span className="absolute right-3 top-2.5 text-xs text-cyan-600 dark:text-cyan-400">
                          搜索中…
                        </span>
                      )}
                    </div>

                    {securityOptions.length > 0 && (
                      <div
                        id="security-search-options"
                        role="listbox"
                        className="absolute z-20 mt-1 max-h-64 w-full overflow-y-auto rounded-lg border
                          border-slate-200 bg-white p-1 shadow-xl dark:border-slate-600 dark:bg-slate-900"
                      >
                        {securityOptions.map((option, index) => (
                          <button
                            id={`security-option-${index}`}
                            key={option.symbol}
                            type="button"
                            role="option"
                            aria-selected={index === activeOption}
                            onMouseEnter={() => setActiveOption(index)}
                            onMouseDown={(event) => event.preventDefault()}
                            onClick={() => selectSecurity(option)}
                            className={`w-full rounded-md px-3 py-2 text-left transition-colors ${
                              index === activeOption
                                ? 'bg-cyan-50 dark:bg-cyan-900/30'
                                : 'hover:bg-slate-50 dark:hover:bg-slate-800'
                            }`}
                          >
                            <div className="flex items-center justify-between gap-3">
                              <span className="font-medium text-slate-900 dark:text-white">
                                {option.name}
                              </span>
                              <span className="font-mono text-sm text-cyan-700 dark:text-cyan-300">
                                {option.symbol}
                              </span>
                            </div>
                            {option.name_en && option.name_en !== option.name && (
                              <p className="mt-0.5 truncate text-xs text-slate-500 dark:text-slate-400">
                                {option.name_en}
                              </p>
                            )}
                          </button>
                        ))}
                      </div>
                    )}

                    {!searching && hasSearched && securityOptions.length === 0 && !searchError && (
                      <p className="mt-1.5 text-sm text-slate-500 dark:text-slate-400">
                        没有找到匹配标的，可尝试完整代码或切换市场。
                      </p>
                    )}
                    {searchError && (
                      <p className="mt-1.5 text-sm text-red-500">{searchError}</p>
                    )}
                  </div>

                  {selectedSecurity && (
                    <div className="rounded-lg border border-cyan-200 bg-cyan-50 p-3 dark:border-cyan-800 dark:bg-cyan-900/20">
                      <div className="flex items-start justify-between gap-3">
                        <div>
                          <p className="font-medium text-cyan-900 dark:text-cyan-100">
                            {selectedSecurity.name}
                          </p>
                          <p className="mt-0.5 font-mono text-sm text-cyan-700 dark:text-cyan-300">
                            {selectedSecurity.symbol}
                          </p>
                        </div>
                        <button
                          type="button"
                          aria-label="清除已选择股票"
                          onClick={() => {
                            setSelectedSecurity(null);
                            setSecurityQuery('');
                          }}
                          className="rounded p-1 text-cyan-600 hover:bg-cyan-100 dark:hover:bg-cyan-900/50"
                        >
                          <Close className="h-4 w-4" />
                        </button>
                      </div>
                    </div>
                  )}

                  <button
                    type="button"
                    onClick={() => {
                      setManualMode(true);
                      setSearchError(null);
                      setError(null);
                    }}
                    className="text-sm text-cyan-600 hover:text-cyan-700 dark:text-cyan-400"
                  >
                    找不到股票？手动输入代码
                  </button>
                </>
              ) : (
                <>
                  <Input
                    label="股票代码"
                    value={symbol}
                    onChange={(e) => setSymbol(e.target.value)}
                    placeholder="例如: AAPL.US"
                    required
                  />
                  <Input
                    label="股票名称（可选）"
                    value={name}
                    onChange={(e) => setName(e.target.value)}
                    placeholder="例如: Apple Inc."
                  />
                  <button
                    type="button"
                    onClick={() => {
                      setManualMode(false);
                      setError(null);
                    }}
                    className="text-sm text-cyan-600 hover:text-cyan-700 dark:text-cyan-400"
                  >
                    返回官方股票搜索
                  </button>
                </>
              )}
              <div>
                <label className="block text-sm font-medium text-slate-700 dark:text-slate-300 mb-1">
                  添加理由（可选）
                </label>
                <textarea
                  value={reason}
                  onChange={(e) => setReason(e.target.value)}
                  placeholder="例如: 科技龙头，业绩稳定"
                  rows={2}
                  className="w-full px-3 py-2 rounded-lg border border-slate-300 dark:border-slate-600
                    bg-white dark:bg-slate-900 text-slate-900 dark:text-white text-sm
                    focus:outline-none focus:ring-2 focus:ring-cyan-500/50 resize-none"
                />
              </div>
            </>
          )}

          {error && <Alert type="error">{error}</Alert>}

          <div className="flex gap-3 pt-2">
            <Button type="button" variant="secondary" onClick={onClose} className="flex-1">
              取消
            </Button>
            <Button type="submit" loading={loading} className="flex-1">
              {batchMode ? '批量添加' : '添加'}
            </Button>
          </div>
        </form>
      </div>
    </div>
  );
}
