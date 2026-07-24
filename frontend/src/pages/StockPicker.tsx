/**
 * 智能选股页面 - 现代化重构版
 */
import React, { useState, useEffect, useRef } from 'react';
import {
  FilterList,
  TrendingUp,
  TrendingDown,
  Add,
  Delete,
  DeleteSweep,
  Analytics,
  Psychology,
  ExpandMore,
  ExpandLess,
  Close,
  Search as SearchIcon,
  Refresh,
  Storage,
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
  getAnalysisSnapshot,
  addStock,
  batchAddStocks,
  removeStock,
  toggleStock,
  clearPool,
  analyzeStocks,
  getStockPickerAnalysisJob,
  searchSecurities,
  getScreenerStrategies,
  searchScreenerCandidates,
  importScreenerCandidates,
  runStockPickerBacktest,
  getStockPickerBacktests,
  runStockPickerAIIncrementEvaluation,
  getStockPickerAIIncrementEvaluations,
  getStockPickerConfig,
  updateStockPickerConfig,
  getStockPickerFactorCoverage,
  type Stock,
  type Analysis,
  type PoolsResponse,
  type AnalysisResponse,
  type StockPickerAnalysisSnapshot,
  type SecurityMarket,
  type SecuritySearchItem,
  type ScreenerMarket,
  type ScreenerStrategy,
  type ScreenerCandidate,
  type ScreenerSearchResponse,
  type ScreenerIndexFilters,
  type StockPickerBacktestReport,
  type StockPickerBacktestHistoryItem,
  type StockPickerAIIncrementEvaluationReport,
  type StockPickerAIIncrementEvaluationHistoryItem,
  type StockPickerConfig,
  type StockPickerFactorCoverage,
  type StockPickerAnalysisJob,
  StockPickerAnalysisJobNotFoundError,
} from '../api/stockPicker';
import { API_BASE } from '../api/client';
import {
  clearActiveStockPickerTask,
  loadActiveStockPickerTask,
  saveActiveStockPickerTask,
  type ActiveStockPickerTask,
} from '../lib/stockPickerTaskRecovery';

export default function StockPicker() {
  const [pools, setPools] = useState<PoolsResponse>({ long_pool: [], short_pool: [] });
  const [analysis, setAnalysis] = useState<AnalysisResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [analyzing, setAnalyzing] = useState(false);
  const [showAddDialog, setShowAddDialog] = useState(false);
  const [showDiscoveryDialog, setShowDiscoveryDialog] = useState(false);
  const [showBacktestDialog, setShowBacktestDialog] = useState(false);
  const [showAIEvaluationDialog, setShowAIEvaluationDialog] = useState(false);
  const [showSnapshotDialog, setShowSnapshotDialog] = useState(false);
  const [addDialogType, setAddDialogType] = useState<'LONG' | 'SHORT'>('LONG');
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  const [analysisLogs, setAnalysisLogs] = useState<string[]>([]);
  const [showLogs, setShowLogs] = useState(false);
  const [analysisProgress, setAnalysisProgress] = useState({
    current: '',
    total: 0,
    completed: 0,
    status: 'idle' as
      | 'idle'
      | 'queued'
      | 'running'
      | 'completed'
      | 'error'
      | 'recovering',
  });
  const analysisEventSourceRef = useRef<EventSource | null>(null);
  const analysisReconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(
    null,
  );
  const activeAnalysisTaskRef = useRef<ActiveStockPickerTask | null>(null);
  const analysisRecoveryPendingRef = useRef(false);
  const mountedRef = useRef(true);

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

  function closeAnalysisProgressConnection() {
    analysisEventSourceRef.current?.close();
    analysisEventSourceRef.current = null;
    if (analysisReconnectTimerRef.current) {
      clearTimeout(analysisReconnectTimerRef.current);
      analysisReconnectTimerRef.current = null;
    }
  }

  function clearTrackedAnalysisTask() {
    closeAnalysisProgressConnection();
    activeAnalysisTaskRef.current = null;
    clearActiveStockPickerTask();
  }

  function finishMissingAnalysisTask(
    task: ActiveStockPickerTask,
    currentRuntimeId: string | null,
  ) {
    clearTrackedAnalysisTask();
    setAnalyzing(false);
    setSuccess(null);
    setAnalysisProgress((current) => ({
      ...current,
      current: '',
      status: 'error',
    }));
    setError(
      currentRuntimeId && currentRuntimeId !== task.runtime_id
        ? '后端服务已重启，原分析任务已终止；已重新加载持久化结果，请重新发起分析'
        : '分析任务不存在或已过期；已重新加载持久化结果，请重新发起分析',
    );
    void loadAnalysis();
  }

  function applyAnalysisJobSnapshot(job: StockPickerAnalysisJob): boolean {
    if (activeAnalysisTaskRef.current?.job_id !== job.job_id) return true;
    setAnalysisProgress({
      current: job.current || '',
      total: job.total || 0,
      completed: job.completed || 0,
      status: job.status,
    });
    setAnalysisLogs(job.logs.map((log) => log.message));

    if (job.status === 'completed') {
      clearTrackedAnalysisTask();
      setAnalyzing(false);
      setSuccess(
        job.result
          ? `分析完成：成功 ${job.result.success}，跳过 ${job.result.skipped}，失败 ${job.result.failed}`
          : '分析完成',
      );
      void loadAnalysis();
      return true;
    }
    if (job.status === 'error') {
      clearTrackedAnalysisTask();
      setAnalyzing(false);
      setSuccess(null);
      setError(job.error || '分析任务失败');
      void loadAnalysis();
      return true;
    }
    return false;
  }

  function scheduleAnalysisRecovery(
    task: ActiveStockPickerTask,
    delay = 2000,
  ) {
    if (analysisReconnectTimerRef.current) {
      clearTimeout(analysisReconnectTimerRef.current);
    }
    analysisReconnectTimerRef.current = setTimeout(() => {
      analysisReconnectTimerRef.current = null;
      void recoverAnalysisTask(task, false);
    }, delay);
  }

  async function recoverAnalysisTask(
    task: ActiveStockPickerTask,
    restoredFromPageLoad: boolean,
  ) {
    if (
      analysisRecoveryPendingRef.current
      || activeAnalysisTaskRef.current?.job_id !== task.job_id
    ) {
      return;
    }
    analysisRecoveryPendingRef.current = true;
    try {
      const job = await getStockPickerAnalysisJob(task.job_id);
      if (
        !mountedRef.current
        || activeAnalysisTaskRef.current?.job_id !== task.job_id
      ) {
        return;
      }
      if (job.runtime_id !== task.runtime_id) {
        finishMissingAnalysisTask(task, job.runtime_id);
        return;
      }
      if (applyAnalysisJobSnapshot(job)) return;
      setError(null);
      if (restoredFromPageLoad) {
        setSuccess('已恢复进行中的分析任务');
        connectAnalysisProgress(task);
      } else {
        analysisReconnectTimerRef.current = setTimeout(() => {
          analysisReconnectTimerRef.current = null;
          connectAnalysisProgress(task);
        }, 1000);
      }
    } catch (err) {
      if (
        !mountedRef.current
        || activeAnalysisTaskRef.current?.job_id !== task.job_id
      ) {
        return;
      }
      if (err instanceof StockPickerAnalysisJobNotFoundError) {
        finishMissingAnalysisTask(task, err.runtimeId);
        return;
      }
      setAnalysisProgress((current) => ({
        ...current,
        status: 'recovering',
      }));
      setSuccess(null);
      setError('后端暂时不可达，正在等待恢复分析任务');
      scheduleAnalysisRecovery(task);
    } finally {
      analysisRecoveryPendingRef.current = false;
    }
  }

  function connectAnalysisProgress(task: ActiveStockPickerTask) {
    if (
      !mountedRef.current
      || activeAnalysisTaskRef.current?.job_id !== task.job_id
    ) {
      return;
    }
    closeAnalysisProgressConnection();
    const eventSource = new EventSource(
      `${API_BASE}/api/stock-picker/analysis/progress/${encodeURIComponent(task.job_id)}`,
    );
    analysisEventSourceRef.current = eventSource;
    eventSource.onmessage = (event) => {
      try {
        const job = JSON.parse(event.data) as StockPickerAnalysisJob;
        if (job.runtime_id !== task.runtime_id) {
          finishMissingAnalysisTask(task, job.runtime_id);
          return;
        }
        applyAnalysisJobSnapshot(job);
      } catch (err) {
        console.error('解析进度数据失败:', err);
      }
    };
    eventSource.onerror = () => {
      if (analysisEventSourceRef.current === eventSource) {
        analysisEventSourceRef.current = null;
      }
      eventSource.close();
      setAnalysisProgress((current) => ({
        ...current,
        status: 'recovering',
      }));
      void recoverAnalysisTask(task, false);
    };
  }

  const handleAnalyze = async (
    poolType?: 'LONG' | 'SHORT',
    forceRefresh = false,
  ) => {
    clearTrackedAnalysisTask();
    setAnalyzing(true);
    setError(null);
    setSuccess(null);
    setAnalysisLogs([]);
    setShowLogs(true);
    setAnalysisProgress({ current: '', total: 0, completed: 0, status: 'queued' });

    try {
      const result = await analyzeStocks({
        pool_type: poolType,
        force_refresh: forceRefresh,
      });
      const task = {
        job_id: result.job_id,
        runtime_id: result.runtime_id,
        created_at: result.created_at,
      };
      activeAnalysisTaskRef.current = task;
      saveActiveStockPickerTask(task);
      setSuccess(result.message);
      connectAnalysisProgress(task);
    } catch (err) {
      clearTrackedAnalysisTask();
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
    mountedRef.current = true;
    void loadPools();
    void loadAnalysis();
    const storedTask = loadActiveStockPickerTask();
    if (storedTask) {
      activeAnalysisTaskRef.current = storedTask;
      setAnalyzing(true);
      setShowLogs(true);
      setAnalysisProgress({
        current: '',
        total: 0,
        completed: 0,
        status: 'recovering',
      });
      void recoverAnalysisTask(storedTask, true);
    }
    return () => {
      mountedRef.current = false;
      closeAnalysisProgressConnection();
    };
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
          <div className="flex flex-wrap gap-2 sm:justify-end">
            <Button
              variant="secondary"
              onClick={() => setShowSnapshotDialog(true)}
              icon={<Storage className="w-4 h-4" />}
            >
              快照管理
            </Button>
            <Button
              variant="secondary"
              onClick={() => setShowAIEvaluationDialog(true)}
              icon={<Psychology className="w-4 h-4" />}
            >
              AI 增量评估
            </Button>
            <Button
              variant="secondary"
              onClick={() => setShowBacktestDialog(true)}
              icon={<Analytics className="w-4 h-4" />}
            >
              回测评估
            </Button>
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
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
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
                      : analysisProgress.status === 'recovering'
                        ? '后端连接中断，正在恢复任务...'
                        : analysisProgress.status === 'error'
                          ? '分析任务已终止'
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

      {showBacktestDialog && (
        <StockPickerBacktestDialog onClose={() => setShowBacktestDialog(false)} />
      )}

      {showAIEvaluationDialog && (
        <StockPickerAIIncrementEvaluationDialog
          onClose={() => setShowAIEvaluationDialog(false)}
        />
      )}

      {showSnapshotDialog && (
        <StockPickerSnapshotDialog onClose={() => setShowSnapshotDialog(false)} />
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

      <div className="mb-4 flex flex-wrap gap-2">
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
  const [snapshotExpanded, setSnapshotExpanded] = useState(false);
  const [snapshot, setSnapshot] = useState<StockPickerAnalysisSnapshot | null>(null);
  const [snapshotLoading, setSnapshotLoading] = useState(false);
  const [snapshotError, setSnapshotError] = useState<string | null>(null);

  useEffect(() => {
    setSnapshot(null);
    setSnapshotExpanded(false);
    setSnapshotError(null);
  }, [analysis?.id]);

  const handleSnapshotToggle = async () => {
    if (!analysis) return;
    if (snapshot) {
      setSnapshotExpanded(!snapshotExpanded);
      return;
    }
    setSnapshotLoading(true);
    setSnapshotError(null);
    try {
      const loaded = await getAnalysisSnapshot(analysis.id);
      setSnapshot(loaded);
      setSnapshotExpanded(true);
    } catch (err) {
      setSnapshotError(err instanceof Error ? err.message : '获取分析快照失败');
    } finally {
      setSnapshotLoading(false);
    }
  };

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
  const snapshotHash = analysis.metadata?.ai_input_hash;
  const hashStatus = snapshot?.hash_valid;
  const hashStatusLabel = hashStatus === true
    ? '完整性已验证'
    : hashStatus === false
      ? '完整性校验失败'
      : '待校验';
  const hashStatusVariant: 'success' | 'warning' | 'default' = hashStatus === true
    ? 'success'
    : hashStatus === false
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
              <div className="mb-2 space-y-1 text-xs text-slate-500">
                <p>
                  数据截止 {analysis.metadata.data_as_of || '-'}
                  {' · '}评分 {analysis.metadata.score_version || '-'}
                  {' · '}模式 {analysis.metadata.analysis_mode || '-'}
                </p>
                <p>
                  AI 输入 {analysis.metadata.ai_snapshot_version || '历史记录无快照'}
                  {' · '}状态 {analysis.metadata.ai_request_status || '-'}
                  {' · '}哈希 {snapshotHash ? snapshotHash.slice(0, 12) : '-'}
                </p>
              </div>
            )}
            <ul className="space-y-1">
              {analysis.ai_decision.reasoning.map((reason, i) => (
                <li key={i} className="text-sm text-slate-600 dark:text-slate-400 flex items-start gap-2">
                  <span className="text-cyan-500 mt-1">•</span>
                  {reason}
                </li>
              ))}
            </ul>
            {analysis.metadata?.ai_snapshot_available ? (
              <div className="mt-3">
                <button
                  type="button"
                  onClick={handleSnapshotToggle}
                  disabled={snapshotLoading}
                  className="flex items-center gap-1 text-xs font-medium text-cyan-600 hover:text-cyan-700 disabled:opacity-50 dark:text-cyan-400"
                >
                  {snapshotExpanded ? <ExpandLess className="w-4 h-4" /> : <ExpandMore className="w-4 h-4" />}
                  {snapshotLoading
                    ? '加载快照...'
                    : snapshotExpanded
                      ? '收起 AI/新闻快照'
                      : '查看 AI/新闻快照'}
                </button>
                {snapshotError && (
                  <p className="mt-2 text-xs text-red-600 dark:text-red-400">
                    {snapshotError}
                  </p>
                )}
                {snapshotExpanded && snapshot && (
                  <div className="mt-3 space-y-3 rounded-lg border border-slate-200 bg-white p-3 dark:border-slate-700 dark:bg-slate-900/40">
                    <div className="flex flex-wrap items-center gap-2 text-xs">
                      <Badge variant={hashStatusVariant}>{hashStatusLabel}</Badge>
                      <span className="text-slate-500">
                        {snapshot.ai_input_snapshot?.version || '-'}
                      </span>
                      <span className="text-slate-500">
                        请求 {snapshot.ai_input_snapshot?.request_status || '-'}
                      </span>
                      <span className="break-all font-mono text-slate-500">
                        {snapshot.ai_input_hash || '-'}
                      </span>
                    </div>
                    <div className="grid gap-2 text-xs text-slate-500 sm:grid-cols-2">
                      <p>模型：{snapshot.ai_input_snapshot?.ai_model || '-'}</p>
                      <p>风格：{snapshot.ai_input_snapshot?.style || '-'}</p>
                      <p>新闻观测：{
                        typeof snapshot.ai_input_snapshot?.news_snapshot?.observed_at === 'string'
                          ? snapshot.ai_input_snapshot.news_snapshot.observed_at
                          : '-'
                      }</p>
                      <p>K 线哈希：{snapshot.ai_input_snapshot?.klines_hash?.slice(0, 12) || '-'}</p>
                      <p>配置版本：{snapshot.ai_input_snapshot?.config_version?.slice(0, 12) || '-'}</p>
                      <p>股票宇宙：{snapshot.ai_input_snapshot?.universe_version?.slice(0, 12) || '-'}</p>
                      <p>量化排名：{snapshot.ai_input_snapshot?.selection_version?.slice(0, 12) || '-'}</p>
                      <p>
                        Top N 状态：排名 {snapshot.ai_input_snapshot?.selection?.quant_rank || '-'}
                        {' · '}{snapshot.ai_input_snapshot?.request_reason === 'deepseek_not_configured'
                          ? 'AI 未配置'
                          : snapshot.ai_input_snapshot?.selection?.ai_selected
                            ? '已入选'
                            : '未入选'}
                      </p>
                    </div>
                    {snapshot.ai_input_snapshot?.system_prompt && (
                      <details>
                        <summary className="cursor-pointer text-xs font-medium text-slate-700 dark:text-slate-300">
                          System prompt
                        </summary>
                        <pre className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap break-words rounded bg-slate-100 p-2 text-xs text-slate-600 dark:bg-slate-800 dark:text-slate-300">
                          {snapshot.ai_input_snapshot.system_prompt}
                        </pre>
                      </details>
                    )}
                    {snapshot.ai_input_snapshot?.user_prompt && (
                      <details>
                        <summary className="cursor-pointer text-xs font-medium text-slate-700 dark:text-slate-300">
                          User prompt
                        </summary>
                        <pre className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap break-words rounded bg-slate-100 p-2 text-xs text-slate-600 dark:bg-slate-800 dark:text-slate-300">
                          {snapshot.ai_input_snapshot.user_prompt}
                        </pre>
                      </details>
                    )}
                    {snapshot.ai_input_snapshot?.news_snapshot && (
                      <details>
                        <summary className="cursor-pointer text-xs font-medium text-slate-700 dark:text-slate-300">
                          新闻输入
                        </summary>
                        <pre className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap break-words rounded bg-slate-100 p-2 text-xs text-slate-600 dark:bg-slate-800 dark:text-slate-300">
                          {JSON.stringify(snapshot.ai_input_snapshot.news_snapshot, null, 2)}
                        </pre>
                      </details>
                    )}
                    {snapshot.ai_output_snapshot && (
                      <details>
                        <summary className="cursor-pointer text-xs font-medium text-slate-700 dark:text-slate-300">
                          AI 输出（{snapshot.ai_output_snapshot.status}）
                        </summary>
                        <pre className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap break-words rounded bg-slate-100 p-2 text-xs text-slate-600 dark:bg-slate-800 dark:text-slate-300">
                          {JSON.stringify({
                            raw_response: snapshot.ai_output_snapshot.raw_response,
                            parsed_response: snapshot.ai_output_snapshot.parsed_response,
                            error_type: snapshot.ai_output_snapshot.error_type,
                            error: snapshot.ai_output_snapshot.error,
                          }, null, 2)}
                        </pre>
                      </details>
                    )}
                  </div>
                )}
              </div>
            ) : (
              <p className="mt-3 text-xs text-slate-500">
                此历史记录创建于 AI/新闻快照功能之前。
              </p>
            )}
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

const SNAPSHOT_FACTOR_LABELS: Record<string, string> = {
  market_rs: '市场 RS',
  fundamental_quality: '财务质量',
  expectations: '预期方向',
  financial_event: '财报事件',
  corporate_action: '公司行动',
  trade_status: '交易状态',
  depth: '盘口深度',
  margin: '保证金',
  short_risk: '做空拥挤度',
  short_capacity: '账户预估卖空能力',
};

function StockPickerSnapshotDialog({ onClose }: { onClose: () => void }) {
  const [config, setConfig] = useState<StockPickerConfig | null>(null);
  const [coverage, setCoverage] = useState<StockPickerFactorCoverage | null>(null);
  const [snapshotEnabled, setSnapshotEnabled] = useState(false);
  const [pollInterval, setPollInterval] = useState(900);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);

  const loadData = async () => {
    setLoading(true);
    setError(null);
    setSuccess(null);
    try {
      const [configResult, coverageResult] = await Promise.allSettled([
        getStockPickerConfig(),
        getStockPickerFactorCoverage(365),
      ]);
      const loadErrors: string[] = [];
      if (configResult.status === 'fulfilled') {
        setConfig(configResult.value);
        setSnapshotEnabled(configResult.value.factor_snapshot_enabled);
        setPollInterval(configResult.value.factor_snapshot_poll_interval);
      } else {
        setConfig(null);
        loadErrors.push(
          configResult.reason instanceof Error
            ? configResult.reason.message
            : '加载自动采集配置失败',
        );
      }
      if (coverageResult.status === 'fulfilled') {
        setCoverage(coverageResult.value);
      } else {
        setCoverage(null);
        loadErrors.push(
          coverageResult.reason instanceof Error
            ? coverageResult.reason.message
            : '加载快照覆盖率失败',
        );
      }
      if (loadErrors.length > 0) {
        setError(loadErrors.join('；'));
      }
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void loadData();
  }, []);

  const saveConfig = async () => {
    if (!config) {
      setError('自动采集配置尚未加载成功，请刷新后重试');
      return;
    }
    if (!Number.isInteger(pollInterval) || pollInterval < 300 || pollInterval > 3600) {
      setError('轮询间隔必须是 300～3600 秒之间的整数');
      return;
    }
    if (
      !config.factor_snapshot_enabled
      && snapshotEnabled
      && !confirm(
        '启用后，服务会在 US/HK 收盘后持续调用 Longbridge、Fundamental 和交易接口，并为 US SHORT 查询账户预估可卖空数量。确认启用吗？',
      )
    ) {
      return;
    }

    setSaving(true);
    setError(null);
    setSuccess(null);
    try {
      const updated = await updateStockPickerConfig({
        factor_snapshot_enabled: snapshotEnabled,
        factor_snapshot_poll_interval: pollInterval,
      });
      setConfig(updated);
      setSnapshotEnabled(updated.factor_snapshot_enabled);
      setPollInterval(updated.factor_snapshot_poll_interval);
      setSuccess(
        updated.factor_snapshot_enabled
          ? '自动快照已启用，服务会在真实交易日收盘后执行'
          : '自动快照保持关闭',
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : '保存快照配置失败');
    } finally {
      setSaving(false);
    }
  };

  const formatPercent = (value: number) => `${(value * 100).toFixed(0)}%`;
  const formatDateTime = (value: string | null) => (
    value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '-'
  );

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4">
      <div className="max-h-[calc(100vh-2rem)] w-full max-w-5xl overflow-y-auto rounded-xl bg-white p-6 shadow-2xl dark:bg-slate-800">
        <div className="mb-5 flex items-start justify-between gap-4">
          <div>
            <h3 className="text-xl font-bold text-slate-900 dark:text-white">
              因子快照管理
            </h3>
            <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">
              查看 Fundamental、执行风险与美股账户卖空能力的点时覆盖；自动采集默认关闭。
            </p>
          </div>
          <button
            aria-label="关闭因子快照管理弹窗"
            onClick={onClose}
            className="rounded p-1 hover:bg-slate-100 dark:hover:bg-slate-700"
          >
            <Close className="h-5 w-5 text-slate-500" />
          </button>
        </div>

        {error && (
          <div className="mb-4">
            <Alert type="error" onClose={() => setError(null)}>
              {error}
            </Alert>
          </div>
        )}
        {success && (
          <div className="mb-4">
            <Alert type="success" onClose={() => setSuccess(null)}>
              {success}
            </Alert>
          </div>
        )}

        {loading && !coverage ? (
          <LoadingSpinner size="md" text="加载快照覆盖与配置..." />
        ) : (
          <div className="space-y-5">
            <Card className="shadow-none hover:shadow-none">
              <CardHeader
                title="自动采集配置"
                description="只有真实交易日当地 17:00 后才会采集；日历不可用时失败关闭。"
                action={
                  <span className="shrink-0 whitespace-nowrap">
                    <Badge
                      variant={snapshotEnabled ? 'success' : 'default'}
                      dot
                    >
                      {snapshotEnabled ? '已启用' : '已关闭'}
                    </Badge>
                  </span>
                }
              />
              <div className="grid gap-4 md:grid-cols-[minmax(0,1fr)_220px]">
                <label className="flex cursor-pointer items-start gap-3 rounded-lg border border-slate-200 p-4 dark:border-slate-700">
                  <input
                    type="checkbox"
                    checked={snapshotEnabled}
                    disabled={saving || loading || !config}
                    onChange={(event) => setSnapshotEnabled(event.target.checked)}
                    className="mt-1 h-4 w-4 rounded border-slate-300 text-cyan-600 focus:ring-cyan-500"
                  />
                  <span>
                    <span className="block text-sm font-semibold text-slate-900 dark:text-white">
                      启用收盘后自动快照
                    </span>
                    <span className="mt-1 block text-xs text-slate-500 dark:text-slate-400">
                      会持续消耗 Longbridge、Fundamental 与交易接口额度；启用时需要再次确认。
                    </span>
                  </span>
                </label>
                <Input
                  label="轮询间隔（秒）"
                  type="number"
                  min={300}
                  max={3600}
                  step={60}
                  value={pollInterval}
                  disabled={saving || loading || !config}
                  onChange={(event) => setPollInterval(Number(event.target.value))}
                  hint="允许 300～3600 秒，默认 900 秒"
                />
              </div>
              <div className="mt-4 flex flex-wrap items-center justify-between gap-3">
                <p className="text-xs text-slate-500 dark:text-slate-400">
                  当前配置更新时间：{formatDateTime(config?.updated_at || null)}
                </p>
                <div className="flex gap-2">
                  <Button
                    type="button"
                    variant="secondary"
                    onClick={loadData}
                    loading={loading}
                    disabled={saving}
                    icon={<Refresh className="h-4 w-4" />}
                  >
                    刷新
                  </Button>
                  <Button
                    type="button"
                    onClick={saveConfig}
                    loading={saving}
                    disabled={loading || !config}
                  >
                    保存配置
                  </Button>
                </div>
              </div>
            </Card>

            {coverage && (
              <>
                <Alert
                  type={coverage.ready_for_return_evaluation ? 'success' : 'warning'}
                  title={
                    coverage.ready_for_return_evaluation
                      ? '已满足收益评估门禁'
                      : '尚未满足收益评估门禁'
                  }
                >
                  原始 {coverage.raw_snapshot_count} 条，按日去重 {coverage.daily_snapshot_count} 条，
                  有效收盘后快照 {coverage.evaluation_snapshot_count} 条。每组至少需要
                  {' '}{coverage.minimums.observation_dates} 个观测日、
                  {formatPercent(coverage.minimums.factor_coverage)} 因子覆盖和
                  {' '}{coverage.minimums.distinct_symbols} 只股票。
                </Alert>

                <div className="grid gap-3 md:grid-cols-2">
                  {coverage.groups.map((group) => {
                    const factors = Object.values(group.factors);
                    const factorEntries = Object.entries(group.factors);
                    const readyFactors = factors.filter((factor) => factor.coverage_ready).length;
                    const phaseSummary = Object.entries(group.session_phase_counts)
                      .map(([phase, count]) => `${phase} ${count}`)
                      .join(' · ');
                    return (
                      <div
                        key={`${group.market}-${group.target_direction}`}
                        className="rounded-lg border border-slate-200 p-4 dark:border-slate-700"
                      >
                        <div className="flex items-center justify-between gap-2">
                          <p className="font-semibold text-slate-900 dark:text-white">
                            {group.market} · {group.target_direction}
                          </p>
                          <Badge
                            variant={group.ready_for_return_evaluation ? 'success' : 'warning'}
                            dot
                          >
                            {group.ready_for_return_evaluation ? '可评估' : '积累中'}
                          </Badge>
                        </div>
                        <div className="mt-3 grid grid-cols-3 gap-2 text-center">
                          <SnapshotMetric
                            label="观测日"
                            value={`${group.observation_dates}/${coverage.minimums.observation_dates}`}
                          />
                          <SnapshotMetric
                            label="股票"
                            value={`${group.distinct_symbols}/${coverage.minimums.distinct_symbols}`}
                          />
                          <SnapshotMetric
                            label="因子达标"
                            value={`${readyFactors}/${factors.length}`}
                          />
                        </div>
                        <div className="mt-3 space-y-1 text-xs text-slate-500 dark:text-slate-400">
                          <p>有效快照 {group.snapshot_count}；采集去重后 {group.captured_daily_snapshot_count}</p>
                          <p>最新快照：{formatDateTime(group.latest_observed_at)}</p>
                          <p>采集阶段：{phaseSummary || '-'}</p>
                        </div>
                        <div className="mt-3 grid gap-1.5 sm:grid-cols-2">
                          {factorEntries.map(([name, factor]) => (
                            <div
                              key={name}
                              className="flex items-center justify-between gap-2 rounded bg-slate-50 px-2 py-1.5 text-xs dark:bg-slate-900/50"
                            >
                              <span className="min-w-0 truncate text-slate-600 dark:text-slate-300">
                                {SNAPSHOT_FACTOR_LABELS[name] || name}
                              </span>
                              <span
                                className={
                                  factor.coverage_ready
                                    ? 'shrink-0 text-emerald-600 dark:text-emerald-400'
                                    : 'shrink-0 text-amber-600 dark:text-amber-400'
                                }
                              >
                                {formatPercent(factor.coverage)}
                              </span>
                            </div>
                          ))}
                        </div>
                      </div>
                    );
                  })}
                </div>

                <Card className="shadow-none hover:shadow-none">
                  <CardHeader
                    title="采集租约与失败"
                    description="租约持久化到本地数据库；运行超过 30 分钟可由后续轮询接管。"
                  />
                  <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
                    <SnapshotMetric
                      label="租约总数"
                      value={coverage.capture_runs.total_count}
                    />
                    <SnapshotMetric
                      label="运行中"
                      value={coverage.capture_runs.status_counts.running || 0}
                    />
                    <SnapshotMetric
                      label="已完成"
                      value={coverage.capture_runs.status_counts.completed || 0}
                    />
                    <SnapshotMetric
                      label="失败"
                      value={coverage.capture_runs.status_counts.failed || 0}
                    />
                  </div>

                  {coverage.capture_runs.active.length > 0 && (
                    <div className="mt-4 space-y-2">
                      <p className="text-sm font-medium text-slate-700 dark:text-slate-300">
                        运行中租约
                      </p>
                      {coverage.capture_runs.active.map((run) => (
                        <div
                          key={run.claim_id}
                          className="flex flex-wrap items-center justify-between gap-2 rounded-lg bg-cyan-50 px-3 py-2 text-xs text-cyan-800 dark:bg-cyan-900/20 dark:text-cyan-200"
                        >
                          <span>{run.market} · {run.target_direction} · {run.observation_date}</span>
                          <span>{run.lease_expired ? '租约已过期，等待接管' : `开始于 ${formatDateTime(run.started_at)}`}</span>
                        </div>
                      ))}
                    </div>
                  )}

                  {coverage.capture_runs.recent_failures.length > 0 && (
                    <div className="mt-4 space-y-2">
                      <p className="text-sm font-medium text-red-700 dark:text-red-300">
                        最近失败
                      </p>
                      {coverage.capture_runs.recent_failures.map((run) => (
                        <div
                          key={`${run.claim_id}-${run.completed_at}`}
                          className="rounded-lg bg-red-50 px-3 py-2 text-xs text-red-800 dark:bg-red-900/20 dark:text-red-200"
                        >
                          <p className="font-medium">
                            {run.market} · {run.target_direction} · {run.observation_date}
                          </p>
                          <p className="mt-1 break-words">{run.error || '未知错误'}</p>
                        </div>
                      ))}
                    </div>
                  )}

                  {coverage.capture_runs.active.length === 0
                    && coverage.capture_runs.recent_failures.length === 0 && (
                    <p className="mt-4 text-xs text-slate-500 dark:text-slate-400">
                      当前没有运行中租约或已记录失败。
                    </p>
                  )}
                </Card>
              </>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

function SnapshotMetric({
  label,
  value,
}: {
  label: string;
  value: string | number;
}) {
  return (
    <div className="rounded-lg bg-slate-50 px-3 py-2 dark:bg-slate-900/50">
      <p className="text-xs text-slate-500 dark:text-slate-400">{label}</p>
      <p className="mt-1 text-lg font-semibold text-slate-900 dark:text-white">{value}</p>
    </div>
  );
}

function StockPickerAIIncrementEvaluationDialog({
  onClose,
}: {
  onClose: () => void;
}) {
  const [poolType, setPoolType] = useState<'LONG' | 'SHORT'>('LONG');
  const [topK, setTopK] = useState(3);
  const [lookbackDays, setLookbackDays] = useState(365);
  const [minimumBatches, setMinimumBatches] = useState(20);
  const [minimumLabels, setMinimumLabels] = useState(60);
  const [minimumCompletionPercent, setMinimumCompletionPercent] = useState(90);
  const [bootstrapSamples, setBootstrapSamples] = useState(2000);
  const [bootstrapConfidencePercent, setBootstrapConfidencePercent] = (
    useState(95)
  );
  const [bootstrapBlockSize, setBootstrapBlockSize] = useState('');
  const [bootstrapSeed, setBootstrapSeed] = useState(20260724);
  const [newsMode, setNewsMode] = useState<
    'all' | 'enabled' | 'disabled'
  >('all');
  const [running, setRunning] = useState(false);
  const [loadingHistory, setLoadingHistory] = useState(true);
  const [history, setHistory] = useState<
    StockPickerAIIncrementEvaluationHistoryItem[]
  >([]);
  const [report, setReport] = useState<
    StockPickerAIIncrementEvaluationReport | null
  >(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    getStockPickerAIIncrementEvaluations(5)
      .then((response) => {
        if (!cancelled) setHistory(response.items);
      })
      .catch((err) => {
        if (!cancelled) {
          setError(
            err instanceof Error ? err.message : '获取 AI 增量评估历史失败',
          );
        }
      })
      .finally(() => {
        if (!cancelled) setLoadingHistory(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const runEvaluation = async () => {
    setRunning(true);
    setError(null);
    try {
      const result = await runStockPickerAIIncrementEvaluation({
        poolType,
        topK,
        lookbackDays,
        minimumCompleteBatches: minimumBatches,
        minimumLabeledRecords: minimumLabels,
        minimumAICompletionRate: minimumCompletionPercent / 100,
        bootstrapSamples,
        bootstrapConfidenceLevel: bootstrapConfidencePercent / 100,
        bootstrapBlockSize: (
          bootstrapBlockSize === ''
            ? null
            : Number(bootstrapBlockSize)
        ),
        bootstrapSeed,
        newsMode,
      });
      setReport(result);
      setHistory((current) => [
        {
          id: result.id || 0,
          created_at: new Date().toISOString(),
          pool_type: result.pool_type,
          evaluation_version: result.evaluation_version,
          parameters: result.parameters,
          result: {
            evaluation_version: result.evaluation_version,
            pool_type: result.pool_type,
            ready: result.ready,
            gate: result.gate,
            coverage: result.coverage,
            metrics: result.metrics,
            methodology: result.methodology,
          },
          ready: result.ready,
          data_as_of: result.coverage.latest_label_date,
        },
        ...current.filter((item) => item.id !== result.id),
      ].slice(0, 5));
    } catch (err) {
      setError(err instanceof Error ? err.message : 'AI 增量评估失败');
    } finally {
      setRunning(false);
    }
  };

  const openHistory = (
    item: StockPickerAIIncrementEvaluationHistoryItem,
  ) => {
    setPoolType(item.pool_type);
    setTopK(item.parameters.top_k);
    setLookbackDays(item.parameters.lookback_days);
    setMinimumBatches(item.parameters.minimum_complete_batches);
    setMinimumLabels(item.parameters.minimum_labeled_records);
    setMinimumCompletionPercent(
      item.parameters.minimum_ai_completion_rate * 100,
    );
    setBootstrapSamples(item.parameters.bootstrap_samples ?? 2000);
    setBootstrapConfidencePercent(
      (item.parameters.bootstrap_confidence_level ?? 0.95) * 100,
    );
    setBootstrapBlockSize(
      item.parameters.bootstrap_block_size == null
        ? ''
        : String(item.parameters.bootstrap_block_size),
    );
    setBootstrapSeed(item.parameters.bootstrap_seed ?? 20260724);
    setNewsMode(item.parameters.news_mode || 'all');
    setReport({
      ...item.result,
      id: item.id,
      parameters: item.parameters,
    });
    setError(null);
  };

  const formatPercent = (value: number | null | undefined) => (
    value == null ? '-' : `${(value * 100).toFixed(2)}%`
  );
  const gateLabels: Record<string, string> = {
    insufficient_ai_complete_batches: 'AI 完整批次数不足',
    insufficient_ai_completion_rate: 'AI 完成率不足',
  };
  const exclusionLabels: Record<string, string> = {
    missing_or_invalid_snapshot: '输入快照缺失或无法解析',
    unsupported_snapshot_version: '输入快照版本不支持',
    not_pool_ranking: '不是完整股票池排名批次',
    version_cohort_mismatch: '评分、提示词或模型版本不匹配',
    missing_news_mode: '新闻状态缺失',
    input_hash_invalid: '输入哈希校验失败',
    missing_selection: '选择上下文缺失',
    missing_ranking: '量化排名缺失',
    missing_selection_version: '选择版本缺失',
    selection_version_invalid: '选择版本校验失败',
    mixed_selection_versions: '批次包含多个选择版本',
    mixed_news_mode: '批次包含多个新闻状态',
    news_mode_mismatch: '不属于当前新闻 cohort',
    inconsistent_ranking: '批次排名不一致',
    invalid_ranking: '批次排名格式无效',
    incomplete_batch: '方向排名记录不完整',
    missing_quant_rank: '量化名次缺失',
    insufficient_ai_candidates: 'AI 候选数不足以比较 Top K',
    mixed_data_as_of: '批次包含多个数据截止日',
    missing_or_invalid_output_snapshot: 'AI 输出快照缺失或无法解析',
    unsupported_output_snapshot_version: 'AI 输出快照版本不支持',
    missing_analysis_values: '分析价格、分数或日期缺失',
    snapshot_record_mismatch: '快照与分析字段不一致',
    missing_parsed_ai_output: 'AI 解析输出缺失',
    invalid_ai_confidence: 'AI 信心度无效',
    derived_score_mismatch: '最终机会分重算不一致',
    ai_incomplete_batch: 'AI Top N 未全部成功',
  };
  const gateReasonLabel = (reason: string) => {
    const pairedMatch = reason.match(/^insufficient_paired_batches_(\d+)d$/);
    if (pairedMatch) return `${pairedMatch[1]} 日配对批次数不足`;
    const labelMatch = reason.match(/^insufficient_labeled_records_(\d+)d$/);
    if (labelMatch) return `${labelMatch[1]} 日后验标签不足`;
    return gateLabels[reason] || reason;
  };
  const inferenceReasonLabel = (reason: string | null) => {
    if (reason === 'insufficient_distinct_observation_dates') {
      return '不同观测日期不足';
    }
    if (reason === 'block_size_not_less_than_date_count') {
      return '时间块长度不小于观测日期数';
    }
    return reason || '推断未就绪';
  };
  const inferenceDirectionLabel = (
    direction: 'positive' | 'negative' | 'inconclusive' | null,
  ) => {
    if (direction === 'positive') return '正向';
    if (direction === 'negative') return '负向';
    return '不确定';
  };
  const parsedBootstrapBlockSize = (
    bootstrapBlockSize === '' ? null : Number(bootstrapBlockSize)
  );
  const parametersValid = (
    Number.isInteger(topK)
    && topK >= 1
    && topK <= 100
    && Number.isInteger(lookbackDays)
    && lookbackDays >= 1
    && lookbackDays <= 3650
    && Number.isInteger(minimumBatches)
    && minimumBatches >= 1
    && minimumBatches <= 1000
    && Number.isInteger(minimumLabels)
    && minimumLabels >= 1
    && minimumLabels <= 100000
    && Number.isFinite(minimumCompletionPercent)
    && minimumCompletionPercent >= 0
    && minimumCompletionPercent <= 100
    && Number.isInteger(bootstrapSamples)
    && bootstrapSamples >= 200
    && bootstrapSamples <= 100000
    && Number.isFinite(bootstrapConfidencePercent)
    && bootstrapConfidencePercent >= 80
    && bootstrapConfidencePercent <= 99
    && (
      parsedBootstrapBlockSize == null
      || (
        Number.isInteger(parsedBootstrapBlockSize)
        && parsedBootstrapBlockSize >= 2
        && parsedBootstrapBlockSize <= 3650
      )
    )
    && Number.isInteger(bootstrapSeed)
    && bootstrapSeed >= 0
    && bootstrapSeed <= 4294967295
  );
  const reportInferenceReady = Boolean(
    report?.metrics
    && report.parameters.horizons.every(
      (horizon) => (
        report.metrics?.[String(horizon)]
          ?.paired_delta_inference?.ready === true
      ),
    ),
  );

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4">
      <div className="max-h-[calc(100vh-2rem)] w-full max-w-4xl overflow-y-auto rounded-xl bg-white p-6 shadow-2xl dark:bg-slate-800">
        <div className="mb-5 flex items-start justify-between gap-4">
          <div>
            <h3 className="text-xl font-bold text-slate-900 dark:text-white">
              AI 增量评估
            </h3>
            <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">
              在相同 AI Top N 候选内，配对比较量化排名与最终机会分重排。
            </p>
          </div>
          <button
            type="button"
            aria-label="关闭 AI 增量评估弹窗"
            onClick={onClose}
            className="rounded p-1 hover:bg-slate-100 dark:hover:bg-slate-700"
          >
            <Close className="h-5 w-5 text-slate-500" />
          </button>
        </div>

        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          <div>
            <span className="mb-1 block text-sm font-medium text-slate-700 dark:text-slate-300">
              评估方向
            </span>
            <div className="grid grid-cols-2 gap-2">
              {(['LONG', 'SHORT'] as const).map((value) => (
                <button
                  key={value}
                  type="button"
                  disabled={running}
                  onClick={() => setPoolType(value)}
                  className={`rounded-lg border px-3 py-2 text-sm font-medium ${
                    poolType === value
                      ? value === 'LONG'
                        ? 'border-emerald-500 bg-emerald-50 text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-300'
                        : 'border-red-500 bg-red-50 text-red-700 dark:bg-red-900/30 dark:text-red-300'
                      : 'border-slate-200 text-slate-600 dark:border-slate-600 dark:text-slate-300'
                  }`}
                >
                  {value}
                </button>
              ))}
            </div>
          </div>
          <div>
            <span className="mb-1 block text-sm font-medium text-slate-700 dark:text-slate-300">
              新闻 cohort
            </span>
            <div className="grid grid-cols-3 gap-2">
              {([
                ['all', '全部'],
                ['enabled', '含新闻'],
                ['disabled', '无新闻'],
              ] as const).map(([value, label]) => (
                <button
                  key={value}
                  type="button"
                  disabled={running}
                  onClick={() => setNewsMode(value)}
                  className={`rounded-lg border px-2 py-2 text-xs font-medium ${
                    newsMode === value
                      ? 'border-cyan-500 bg-cyan-50 text-cyan-700 dark:bg-cyan-900/30 dark:text-cyan-300'
                      : 'border-slate-200 text-slate-600 dark:border-slate-600 dark:text-slate-300'
                  }`}
                >
                  {label}
                </button>
              ))}
            </div>
          </div>
          {([
            ['比较 Top K', topK, setTopK, 1, 100],
            ['回看天数', lookbackDays, setLookbackDays, 1, 3650],
            ['最少完整批次', minimumBatches, setMinimumBatches, 1, 1000],
            ['最少标签数', minimumLabels, setMinimumLabels, 1, 100000],
            [
              '最低 AI 完成率（%）',
              minimumCompletionPercent,
              setMinimumCompletionPercent,
              0,
              100,
            ],
            [
              'Bootstrap 次数',
              bootstrapSamples,
              setBootstrapSamples,
              200,
              100000,
            ],
            [
              '置信水平（%）',
              bootstrapConfidencePercent,
              setBootstrapConfidencePercent,
              80,
              99,
            ],
            [
              'Bootstrap 种子',
              bootstrapSeed,
              setBootstrapSeed,
              0,
              4294967295,
            ],
          ] as Array<
            [string, number, React.Dispatch<React.SetStateAction<number>>, number, number]
          >).map(([label, value, setter, min, max]) => (
            <label key={label} className="text-sm font-medium text-slate-700 dark:text-slate-300">
              <span className="mb-1 block">{label}</span>
              <input
                type="number"
                min={min}
                max={max}
                step="1"
                value={value}
                disabled={running}
                onChange={(event) => setter(Number(event.target.value))}
                className="w-full rounded-lg border border-slate-300 bg-white px-3 py-2
                  text-slate-900 focus:border-cyan-500 focus:outline-none focus:ring-2
                  focus:ring-cyan-500/30 disabled:opacity-60 dark:border-slate-600
                  dark:bg-slate-900 dark:text-white"
              />
            </label>
          ))}
          <label className="text-sm font-medium text-slate-700 dark:text-slate-300">
            <span className="mb-1 block">时间块长度（留空自动）</span>
            <input
              type="number"
              min={2}
              max={3650}
              step="1"
              value={bootstrapBlockSize}
              disabled={running}
              onChange={(event) => setBootstrapBlockSize(event.target.value)}
              className="w-full rounded-lg border border-slate-300 bg-white px-3 py-2
                text-slate-900 focus:border-cyan-500 focus:outline-none focus:ring-2
                focus:ring-cyan-500/30 disabled:opacity-60 dark:border-slate-600
                dark:bg-slate-900 dark:text-white"
            />
          </label>
        </div>

        <div className="mt-4 flex justify-end">
          <Button
            type="button"
            onClick={runEvaluation}
            loading={running}
            disabled={!parametersValid}
          >
            运行评估
          </Button>
        </div>

        {error && (
          <div className="mt-4">
            <Alert type="error">{error}</Alert>
          </div>
        )}

        {report && (
          <div className="mt-5 space-y-4">
            <Alert type={report.ready ? 'success' : 'warning'}>
              {report.ready
                ? reportInferenceReady
                  ? '样本门禁与时间聚类推断均已通过。'
                  : '样本门禁已通过；描述性指标可用，部分置信区间尚未就绪。'
                : `样本门禁未通过：${report.gate.reasons.map(gateReasonLabel).join('；')}`}
            </Alert>

            <p className="break-words text-xs text-slate-500 dark:text-slate-400">
              {report.parameters.score_version}
              {' · '}{report.parameters.prompt_version}
              {' · '}{report.parameters.ai_model}
              {' · '}新闻 {report.parameters.news_mode}
              {' · '}Bootstrap {report.parameters.bootstrap_samples ?? '-'}
              {' · '}置信水平 {formatPercent(
                report.parameters.bootstrap_confidence_level,
              )}
            </p>

            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
              <SnapshotMetric
                label="完整方向批次"
                value={`${report.coverage.structurally_complete_batches}/${report.coverage.raw_batches}`}
              />
              <SnapshotMetric
                label="AI 完整批次"
                value={report.coverage.ai_complete_batches}
              />
              <SnapshotMetric
                label="AI 完成率"
                value={formatPercent(report.coverage.ai_completion_rate)}
              />
              <SnapshotMetric
                label="最新后验标签"
                value={report.coverage.latest_label_date || '-'}
              />
            </div>

            <div className="overflow-x-auto rounded-lg border border-slate-200 dark:border-slate-700">
              <table className="min-w-full text-sm">
                <thead className="bg-slate-50 text-left text-xs uppercase text-slate-500 dark:bg-slate-900/50 dark:text-slate-400">
                  <tr>
                    <th className="px-3 py-2">持有期</th>
                    <th className="px-3 py-2">标签数</th>
                    <th className="px-3 py-2">配对批次</th>
                    <th className="px-3 py-2">量化 Top K</th>
                    <th className="px-3 py-2">最终分 Top K</th>
                    <th className="px-3 py-2">配对增量</th>
                    <th className="px-3 py-2">增量置信区间</th>
                    <th className="px-3 py-2">重排批次</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-200 dark:divide-slate-700">
                  {report.parameters.horizons.map((horizon) => {
                    const metric = report.metrics?.[String(horizon)];
                    const inference = metric?.paired_delta_inference;
                    return (
                      <tr key={horizon} className="text-slate-700 dark:text-slate-300">
                        <td className="px-3 py-2 font-medium">{horizon} 日</td>
                        <td className="px-3 py-2">
                          {report.coverage.labeled_records_by_horizon[String(horizon)] || 0}
                        </td>
                        <td className="px-3 py-2">
                          {report.coverage.paired_batches_by_horizon[String(horizon)] || 0}
                        </td>
                        <td className="px-3 py-2">
                          {metric ? formatPercent(metric.quant_top_k.average) : '-'}
                        </td>
                        <td className="px-3 py-2">
                          {metric ? formatPercent(metric.ai_top_k.average) : '-'}
                        </td>
                        <td className="px-3 py-2">
                          {metric ? formatPercent(metric.paired_delta.average) : '-'}
                        </td>
                        <td className="whitespace-nowrap px-3 py-2">
                          {!metric
                            ? '-'
                            : !inference
                              ? '旧版未计算'
                              : inference.ready
                                ? `${formatPercent(inference.lower)} ～ ${formatPercent(inference.upper)}（${inferenceDirectionLabel(inference.interval_direction)}）`
                                : inferenceReasonLabel(inference.reason)}
                        </td>
                        <td className="px-3 py-2">
                          {metric
                            ? `${metric.selection_changed_batches}/${metric.paired_batches}`
                            : '-'}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>

            {Object.keys(report.coverage.excluded_batches).length > 0 && (
              <p className="text-xs text-slate-500 dark:text-slate-400">
                排除批次：
                {' '}
                {Object.entries(report.coverage.excluded_batches)
                  .map(([reason, count]) => (
                    `${exclusionLabels[reason] || reason} ${count}`
                  ))
                  .join('；')}
              </p>
            )}

            <Alert type="info">
              {report.methodology.causal_limit}
            </Alert>
            {report.methodology.inference && (
              <Alert type="info">
                {report.methodology.inference}
                {report.methodology.inference_limit
                  ? `；${report.methodology.inference_limit}`
                  : ''}
              </Alert>
            )}
          </div>
        )}

        <div className="mt-6 border-t border-slate-200 pt-4 dark:border-slate-700">
          <p className="mb-2 text-sm font-semibold text-slate-900 dark:text-white">
            最近评估
          </p>
          {loadingHistory ? (
            <LoadingSpinner size="sm" text="加载评估历史..." />
          ) : history.length === 0 ? (
            <p className="text-xs text-slate-500 dark:text-slate-400">
              暂无评估记录
            </p>
          ) : (
            <div className="flex flex-wrap gap-2">
              {history.map((item) => (
                <button
                  key={item.id}
                  type="button"
                  onClick={() => openHistory(item)}
                  className="rounded-lg border border-slate-200 px-3 py-2 text-left text-xs text-slate-600 hover:border-cyan-400 dark:border-slate-600 dark:text-slate-300"
                >
                  <span className="block font-medium">
                    {item.pool_type} · Top {item.parameters.top_k}
                  </span>
                  <span className="mt-1 block">
                    {item.ready ? '门禁已通过' : '样本不足'} · {new Date(item.created_at).toLocaleString()}
                  </span>
                </button>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function StockPickerBacktestDialog({ onClose }: { onClose: () => void }) {
  const [poolType, setPoolType] = useState<'LONG' | 'SHORT'>('LONG');
  const [topN, setTopN] = useState(5);
  const [transactionCostBps, setTransactionCostBps] = useState(10);
  const [step, setStep] = useState(5);
  const [executionCostEnabled, setExecutionCostEnabled] = useState(false);
  const [orderNotional, setOrderNotional] = useState(100000);
  const [maxParticipationPercent, setMaxParticipationPercent] = useState(10);
  const [impactCoefficient, setImpactCoefficient] = useState(0.5);
  const [impactVolatilityLookback, setImpactVolatilityLookback] = useState(20);
  const [running, setRunning] = useState(false);
  const [loadingHistory, setLoadingHistory] = useState(true);
  const [history, setHistory] = useState<StockPickerBacktestHistoryItem[]>([]);
  const [report, setReport] = useState<StockPickerBacktestReport | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    getStockPickerBacktests(5)
      .then((response) => {
        if (!cancelled) setHistory(response.items);
      })
      .catch((err) => {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : '获取回测历史失败');
        }
      })
      .finally(() => {
        if (!cancelled) setLoadingHistory(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const runBacktest = async () => {
    setRunning(true);
    setError(null);
    try {
      const result = await runStockPickerBacktest({
        poolType,
        topN,
        transactionCostBps,
        step,
        orderNotional: executionCostEnabled ? orderNotional : null,
        maxParticipationRate: maxParticipationPercent / 100,
        impactCoefficient,
        impactVolatilityLookback,
      });
      setReport(result);
      setHistory((current) => [
        {
          id: result.id || 0,
          created_at: new Date().toISOString(),
          pool_type: result.pool_type,
          score_version: result.score_version,
          parameters: result.parameters,
          result: {
            score_version: result.score_version,
            pool_type: result.pool_type,
            metadata: result.metadata,
            data: result.data,
            execution: result.execution,
            selection: result.selection,
            periods: result.periods,
            walk_forward: result.walk_forward,
            methodology: result.methodology,
          },
          data_as_of: result.data.data_as_of,
        },
        ...current.filter((item) => item.id !== result.id),
      ].slice(0, 5));
    } catch (err) {
      setError(err instanceof Error ? err.message : '回测评估失败');
    } finally {
      setRunning(false);
    }
  };

  const openHistory = (item: StockPickerBacktestHistoryItem) => {
    setPoolType(item.pool_type);
    setTopN(item.parameters.top_n);
    setTransactionCostBps(item.parameters.transaction_cost_bps);
    setStep(item.parameters.step);
    const historicalOrderNotional = item.parameters.order_notional;
    setExecutionCostEnabled(historicalOrderNotional != null);
    setOrderNotional(historicalOrderNotional ?? 100000);
    setMaxParticipationPercent(
      (item.parameters.max_participation_rate ?? 0.1) * 100,
    );
    setImpactCoefficient(item.parameters.impact_coefficient ?? 0.5);
    setImpactVolatilityLookback(
      item.parameters.impact_volatility_lookback ?? 20,
    );
    setReport({
      ...item.result,
      id: item.id,
      parameters: item.parameters,
    });
    setError(null);
  };

  const formatPercent = (value: number | null | undefined) => (
    value == null ? '-' : `${(value * 100).toFixed(2)}%`
  );
  const formatPrecisePercent = (value: number | null | undefined) => (
    value == null ? '-' : `${(value * 100).toFixed(4)}%`
  );
  const formatBps = (value: number | null | undefined) => (
    value == null ? '-' : `${(value * 10000).toFixed(2)} bps`
  );
  const validation = report?.periods.validation;
  const overlap = report
    ? report.parameters.step < Math.max(...report.parameters.horizons)
    : false;
  const executionConfigValid = !executionCostEnabled || (
    Number.isFinite(orderNotional)
    && orderNotional > 0
    && Number.isFinite(maxParticipationPercent)
    && maxParticipationPercent > 0
    && maxParticipationPercent <= 100
    && Number.isFinite(impactCoefficient)
    && impactCoefficient >= 0
    && impactCoefficient <= 10
    && Number.isInteger(impactVolatilityLookback)
    && impactVolatilityLookback >= 2
    && impactVolatilityLookback <= 252
  );
  const exclusionLabels: Record<string, string> = {
    missing_turnover: '缺少成交额',
    insufficient_volatility_history: '波动率历史不足',
    invalid_participation_rate: '参与率无效',
    participation_rate_exceeded: '参与率超限',
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4">
      <div className="max-h-[calc(100vh-2rem)] w-full max-w-4xl overflow-y-auto rounded-xl bg-white p-6 shadow-2xl dark:bg-slate-800">
        <div className="mb-5 flex items-start justify-between gap-4">
          <div>
            <h3 className="text-xl font-bold text-slate-900 dark:text-white">
              智能选股回测评估
            </h3>
            <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">
              固定当前评分版本，按时间切分评估未来 5/10/20 个交易日方向收益。
            </p>
          </div>
          <button
            aria-label="关闭回测评估弹窗"
            onClick={onClose}
            className="rounded p-1 hover:bg-slate-100 dark:hover:bg-slate-700"
          >
            <Close className="h-5 w-5 text-slate-500" />
          </button>
        </div>

        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          <div>
            <span className="mb-1 block text-sm font-medium text-slate-700 dark:text-slate-300">
              评估方向
            </span>
            <div className="grid grid-cols-2 gap-2">
              {(['LONG', 'SHORT'] as const).map((value) => (
                <button
                  key={value}
                  type="button"
                  disabled={running}
                  onClick={() => setPoolType(value)}
                  className={`rounded-lg border px-3 py-2 text-sm font-medium ${
                    poolType === value
                      ? value === 'LONG'
                        ? 'border-emerald-500 bg-emerald-50 text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-300'
                        : 'border-red-500 bg-red-50 text-red-700 dark:bg-red-900/30 dark:text-red-300'
                      : 'border-slate-200 text-slate-600 dark:border-slate-600 dark:text-slate-300'
                  }`}
                >
                  {value}
                </button>
              ))}
            </div>
          </div>
          {([
            ['Top N', topN, setTopN, 1, 100],
            ['交易成本（bps）', transactionCostBps, setTransactionCostBps, 0, 1000],
            ['信号步长（日）', step, setStep, 1, 60],
          ] as Array<
            [string, number, React.Dispatch<React.SetStateAction<number>>, number, number]
          >).map(([label, value, setter, min, max]) => (
            <label key={label} className="text-sm font-medium text-slate-700 dark:text-slate-300">
              <span className="mb-1 block">{label}</span>
              <input
                type="number"
                min={min}
                max={max}
                step="1"
                value={value}
                disabled={running}
                onChange={(event) => setter(Number(event.target.value))}
                className="w-full rounded-lg border border-slate-300 bg-white px-3 py-2
                  text-slate-900 focus:border-cyan-500 focus:outline-none focus:ring-2
                  focus:ring-cyan-500/30 disabled:opacity-60 dark:border-slate-600
                  dark:bg-slate-900 dark:text-white"
              />
            </label>
          ))}
        </div>

        <div className="mt-4 rounded-lg border border-slate-200 p-4 dark:border-slate-700">
          <label className="flex cursor-pointer items-start gap-3">
            <input
              type="checkbox"
              checked={executionCostEnabled}
              disabled={running}
              onChange={(event) => setExecutionCostEnabled(event.target.checked)}
              className="mt-1 h-4 w-4 rounded border-slate-300 text-cyan-600"
            />
            <span>
              <span className="block text-sm font-semibold text-slate-800 dark:text-slate-100">
                启用订单规模与冲击成本代理
              </span>
              <span className="mt-1 block text-xs text-slate-500 dark:text-slate-400">
                使用信号日成交额和历史日波动率估算参与率与动态成本；默认关闭。
              </span>
            </span>
          </label>

          {executionCostEnabled && (
            <div className="mt-4 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
              <label className="text-sm font-medium text-slate-700 dark:text-slate-300">
                <span className="mb-1 block">每笔名义金额（市场本币）</span>
                <input
                  type="number"
                  min="0.01"
                  step="1000"
                  value={orderNotional}
                  disabled={running}
                  onChange={(event) => setOrderNotional(Number(event.target.value))}
                  className="w-full rounded-lg border border-slate-300 bg-white px-3 py-2
                    text-slate-900 focus:border-cyan-500 focus:outline-none focus:ring-2
                    focus:ring-cyan-500/30 disabled:opacity-60 dark:border-slate-600
                    dark:bg-slate-900 dark:text-white"
                />
              </label>
              <label className="text-sm font-medium text-slate-700 dark:text-slate-300">
                <span className="mb-1 block">最大参与率（%）</span>
                <input
                  type="number"
                  min="0.01"
                  max="100"
                  step="0.1"
                  value={maxParticipationPercent}
                  disabled={running}
                  onChange={(event) => setMaxParticipationPercent(Number(event.target.value))}
                  className="w-full rounded-lg border border-slate-300 bg-white px-3 py-2
                    text-slate-900 focus:border-cyan-500 focus:outline-none focus:ring-2
                    focus:ring-cyan-500/30 disabled:opacity-60 dark:border-slate-600
                    dark:bg-slate-900 dark:text-white"
                />
              </label>
              <label className="text-sm font-medium text-slate-700 dark:text-slate-300">
                <span className="mb-1 block">冲击系数</span>
                <input
                  type="number"
                  min="0"
                  max="10"
                  step="0.1"
                  value={impactCoefficient}
                  disabled={running}
                  onChange={(event) => setImpactCoefficient(Number(event.target.value))}
                  className="w-full rounded-lg border border-slate-300 bg-white px-3 py-2
                    text-slate-900 focus:border-cyan-500 focus:outline-none focus:ring-2
                    focus:ring-cyan-500/30 disabled:opacity-60 dark:border-slate-600
                    dark:bg-slate-900 dark:text-white"
                />
              </label>
              <label className="text-sm font-medium text-slate-700 dark:text-slate-300">
                <span className="mb-1 block">波动率窗口（日）</span>
                <input
                  type="number"
                  min="2"
                  max="252"
                  step="1"
                  value={impactVolatilityLookback}
                  disabled={running}
                  onChange={(event) => setImpactVolatilityLookback(Number(event.target.value))}
                  className="w-full rounded-lg border border-slate-300 bg-white px-3 py-2
                    text-slate-900 focus:border-cyan-500 focus:outline-none focus:ring-2
                    focus:ring-cyan-500/30 disabled:opacity-60 dark:border-slate-600
                    dark:bg-slate-900 dark:text-white"
                />
              </label>
            </div>
          )}
        </div>

        <div className="mt-4 flex items-center justify-between gap-3">
          <p className="text-xs text-slate-500 dark:text-slate-400">
            默认使用当前方向池中的启用股票；固定成本每条信号扣除一次，动态模型不代表真实成交滑点。
          </p>
          <Button
            type="button"
            onClick={runBacktest}
            loading={running}
            disabled={
              topN < 1
              || step < 1
              || transactionCostBps < 0
              || !executionConfigValid
            }
          >
            运行评估
          </Button>
        </div>

        {error && (
          <div className="mt-4">
            <Alert type="error">{error}</Alert>
          </div>
        )}

        {report && validation && (
          <div className="mt-5 space-y-4">
            <div className="rounded-lg border border-slate-200 p-4 dark:border-slate-700">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div>
                  <p className="font-semibold text-slate-900 dark:text-white">
                    {report.metadata?.market
                      ? `${report.metadata.market} · `
                      : ''}
                    {report.pool_type} · {report.score_version} · Top {report.parameters.top_n}
                  </p>
                  <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">
                    训练 {report.periods.train.signal_start || '-'} ～ {report.periods.train.signal_end || '-'}；
                    验证 {validation.signal_start || '-'} ～ {validation.signal_end || '-'}
                  </p>
                </div>
                <div className="text-right text-xs text-slate-500 dark:text-slate-400">
                  <p>评估股票 {report.data.symbols_evaluated}/{report.data.symbols_requested}</p>
                  <p>基准总体覆盖 {formatPercent(report.data.benchmark_coverage)}</p>
                </div>
              </div>
            </div>

            {overlap && (
              <Alert type="warning">
                信号步长小于最长持有期，样本存在重叠；最大回撤是信号日组合近似值，并非真实资金曲线。
              </Alert>
            )}

            {report.metadata?.limitations?.length ? (
              <Alert type="warning">
                {report.metadata.limitations.join('；')}
              </Alert>
            ) : null}

            {report.execution?.enabled && (
              <div className="rounded-lg border border-cyan-200 bg-cyan-50/40 p-4 dark:border-cyan-900 dark:bg-cyan-950/20">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div>
                    <p className="text-sm font-semibold text-slate-900 dark:text-white">
                      订单规模与执行成本代理
                    </p>
                    <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">
                      每笔 {report.execution.order_notional?.toLocaleString() ?? '-'}；
                      币种按市场本币（
                      {Object.entries(report.execution.order_currency_by_market)
                        .map(([market, currency]) => `${market} ${currency}`)
                        .join('、') || '-'}
                      ）
                    </p>
                  </div>
                  <p className="text-xs text-slate-500 dark:text-slate-400">
                    {report.execution.model_version}
                  </p>
                </div>
                <div className="mt-3 grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
                  <SnapshotMetric
                    label="可执行样本"
                    value={`${report.execution.executable_sample_count}/${report.execution.sample_count}`}
                  />
                  <SnapshotMetric
                    label="成交额覆盖"
                    value={formatPercent(report.execution.turnover_coverage)}
                  />
                  <SnapshotMetric
                    label="波动率覆盖"
                    value={formatPercent(report.execution.volatility_coverage)}
                  />
                  <SnapshotMetric
                    label="参与率均值 / P95"
                    value={`${formatPrecisePercent(report.execution.participation_rate.average)} / ${formatPrecisePercent(report.execution.participation_rate.p95)}`}
                  />
                  <SnapshotMetric
                    label="冲击成本均值 / P95"
                    value={`${formatBps(report.execution.dynamic_cost_rate.average)} / ${formatBps(report.execution.dynamic_cost_rate.p95)}`}
                  />
                </div>
                <div className="mt-3 text-xs text-slate-600 dark:text-slate-300">
                  <span className="font-medium">排除原因：</span>
                  {Object.keys(report.execution.exclusion_counts).length === 0
                    ? '无'
                    : Object.entries(report.execution.exclusion_counts)
                      .map(([reason, count]) => (
                        `${exclusionLabels[reason] || reason} ${count}`
                      ))
                      .join('；')}
                  {report.selection?.validation ? (
                    <>
                      ；验证期可选集合 Top N 欠配
                      {' '}
                      {report.selection.validation.underfilled_signal_dates}
                      /
                      {report.selection.validation.signal_dates}
                      {' '}
                      个信号日
                    </>
                  ) : null}
                </div>
                <div className="mt-3">
                  <Alert type="info">
                    该结果只使用日 K 成交额和历史波动率估算成本，不包含历史盘口价差、盘中成交分布、借券费或真实成交概率。
                  </Alert>
                </div>
              </div>
            )}

            <div className="overflow-x-auto rounded-lg border border-slate-200 dark:border-slate-700">
              <table className="min-w-full text-sm">
                <thead className="bg-slate-50 text-left text-xs uppercase text-slate-500 dark:bg-slate-900/50 dark:text-slate-400">
                  <tr>
                    <th className="px-3 py-2">持有期</th>
                    <th className="px-3 py-2">样本数</th>
                    <th className="px-3 py-2">平均净收益</th>
                    <th className="px-3 py-2">固定成本（bps）</th>
                    <th className="px-3 py-2">冲击成本（bps）</th>
                    <th className="px-3 py-2">总成本（bps）</th>
                    <th className="px-3 py-2">命中率</th>
                    <th className="px-3 py-2">平均超额</th>
                    <th className="px-3 py-2">基准覆盖</th>
                    <th className="px-3 py-2">近似最大回撤</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-200 dark:divide-slate-700">
                  {report.parameters.horizons.map((horizon) => {
                    const metrics = validation.top_n.horizons[String(horizon)];
                    const fixedCost = metrics?.avg_fixed_cost_rate
                      ?? report.parameters.transaction_cost_bps / 10000;
                    const dynamicCost = metrics?.avg_dynamic_cost_rate ?? 0;
                    const totalCost = metrics?.avg_total_cost_rate
                      ?? fixedCost + dynamicCost;
                    return (
                      <tr key={horizon} className="text-slate-700 dark:text-slate-300">
                        <td className="px-3 py-2 font-medium">{horizon} 日</td>
                        <td className="px-3 py-2">{metrics?.sample_count ?? 0}</td>
                        <td className="px-3 py-2">{formatPercent(metrics?.avg_net_return)}</td>
                        <td className="px-3 py-2">{formatBps(fixedCost)}</td>
                        <td className="px-3 py-2">{formatBps(dynamicCost)}</td>
                        <td className="px-3 py-2">{formatBps(totalCost)}</td>
                        <td className="px-3 py-2">{formatPercent(metrics?.hit_rate)}</td>
                        <td className="px-3 py-2">{formatPercent(metrics?.avg_excess_return)}</td>
                        <td className="px-3 py-2">{formatPercent(metrics?.excess_coverage)}</td>
                        <td className="px-3 py-2">{formatPercent(metrics?.max_drawdown)}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>

            <div className="rounded-lg border border-slate-200 p-4 dark:border-slate-700">
              <p className="text-sm font-semibold text-slate-900 dark:text-white">
                Walk-forward 时间边界
              </p>
              <div className="mt-2 grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
                {report.walk_forward.map((fold) => (
                  <div key={fold.fold} className="rounded bg-slate-50 p-2 text-xs text-slate-600 dark:bg-slate-900/50 dark:text-slate-300">
                    <p className="font-medium">Fold {fold.fold}</p>
                    <p>训练至 {fold.train_end || '-'}</p>
                    <p>验证 {fold.validation_start} ～ {fold.validation_end}</p>
                  </div>
                ))}
              </div>
            </div>
          </div>
        )}

        <div className="mt-5 border-t border-slate-200 pt-4 dark:border-slate-700">
          <p className="mb-2 text-sm font-medium text-slate-700 dark:text-slate-300">
            最近评估
          </p>
          {loadingHistory ? (
            <p className="text-xs text-slate-500">正在加载历史…</p>
          ) : history.length === 0 ? (
            <p className="text-xs text-slate-500">暂无历史记录</p>
          ) : (
            <div className="flex flex-wrap gap-2">
              {history.map((item) => (
                <button
                  key={`${item.id}-${item.created_at}`}
                  type="button"
                  onClick={() => openHistory(item)}
                  className="rounded-md border border-slate-200 px-3 py-2 text-left text-xs
                    text-slate-600 hover:border-cyan-400 dark:border-slate-600 dark:text-slate-300"
                >
                  <span className="font-medium">
                    {item.result.metadata?.market
                      ? `${item.result.metadata.market} · `
                      : ''}
                    {item.pool_type} · Top {item.parameters.top_n}
                  </span>
                  <span className="ml-2 text-slate-400">{item.created_at.slice(0, 16)}</span>
                </button>
              ))}
            </div>
          )}
        </div>
      </div>
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
  const [includeFundamentalDetails, setIncludeFundamentalDetails] = useState(false);
  const [includeMarginDetails, setIncludeMarginDetails] = useState(false);
  const [includeShortCapacity, setIncludeShortCapacity] = useState(false);
  const [scanPages, setScanPages] = useState(1);
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
    max_spread_bps: '',
    min_top_of_book_notional: '',
    min_revenue_yoy: '',
    max_revenue_yoy: '',
    min_net_profit_yoy: '',
    max_net_profit_yoy: '',
    min_operating_cash_flow_yoy: '',
    min_analyst_alignment: '',
    min_eps_revision_alignment: '',
    min_days_to_financial_event: '',
    min_days_to_corporate_action: '',
    max_initial_margin_ratio: '',
    min_short_selling_quantity: '',
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
        scanPages,
        filters,
        targetDirection: poolType,
        includeTradeability: true,
        requireNormalTradeStatus: true,
        includeFundamentals: includeFundamentalDetails,
        includeMarginRequirements: poolType === 'SHORT' && includeMarginDetails,
        includeShortCapacity: (
          market === 'US' && poolType === 'SHORT' && includeShortCapacity
        ),
        includeCorporateActions: includeFundamentalDetails,
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
  const formatRatio = (value: number | null | undefined) => (
    value == null ? '-' : `${(value * 100).toFixed(1)}%`
  );
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
    ['max_spread_bps', '最高买卖点差', 'bps，例如 50'],
    ['min_top_of_book_notional', '最低一档盘口金额', '市场币种'],
    ['min_revenue_yoy', '最低收入同比', '小数，例如 0.1'],
    ['max_revenue_yoy', '最高收入同比', '小数，例如 0.5'],
    ['min_net_profit_yoy', '最低净利润同比', '小数，例如 0.1'],
    ['max_net_profit_yoy', '最高净利润同比', '小数，例如 0.8'],
    ['min_operating_cash_flow_yoy', '最低经营现金流同比', '小数'],
    ['min_analyst_alignment', '最低分析师方向一致性', '-1 到 1'],
    ['min_eps_revision_alignment', '最低 EPS 修正一致性', '-1 到 1'],
    ['min_days_to_financial_event', '距财报至少天数', '0 到 365'],
    ['min_days_to_corporate_action', '距公司行动至少天数', '0 到 365'],
    ...(poolType === 'SHORT'
      ? [
          ['max_days_to_cover', '最高回补天数', '例如 5'],
          ['max_short_ratio', '最高做空比例', 'SDK 数值'],
          ['max_short_ratio_change', '最高做空比例增量', '可输入负数'],
          ['max_initial_margin_ratio', '最高初始保证金比例', '小数，例如 0.6'],
          ...(market === 'US'
            ? [[
                'min_short_selling_quantity',
                '最低账户预估可卖空股数',
                '例如 100',
              ]] as Array<[keyof typeof filterInputs, string, string]>
            : []),
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
                  onClick={() => {
                    setMarket(value);
                    setResult(null);
                    setSelectedSymbols(new Set());
                    if (value !== 'US') {
                      setIncludeShortCapacity(false);
                      setFilterInputs((current) => ({
                        ...current,
                        min_short_selling_quantity: '',
                      }));
                    }
                  }}
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
                      setIncludeMarginDetails(false);
                      setIncludeShortCapacity(false);
                      setFilterInputs((current) => ({
                        ...current,
                        max_days_to_cover: '',
                        max_short_ratio: '',
                        max_short_ratio_change: '',
                        max_initial_margin_ratio: '',
                        min_short_selling_quantity: '',
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

        <div className="mt-3 grid gap-2 sm:grid-cols-2">
          <label className="flex cursor-pointer items-start gap-2 rounded-lg border border-slate-200 p-3 dark:border-slate-700">
            <input
              type="checkbox"
              checked={scanPages > 1}
              onChange={(event) => {
                setScanPages(event.target.checked ? 3 : 1);
                setResult(null);
                setSelectedSymbols(new Set());
              }}
              className="mt-0.5 h-4 w-4 rounded border-slate-300 text-cyan-600 focus:ring-cyan-500"
            />
            <span className="min-w-0 flex-1">
              <span className="block text-sm text-slate-700 dark:text-slate-300">
                跨页扫描
              </span>
              <span className="block text-xs text-slate-500">
                按上游顺序逐页过滤，单次最多处理 100 只
              </span>
            </span>
            {scanPages > 1 && (
              <select
                aria-label="跨页扫描页数"
                value={scanPages}
                onClick={(event) => event.stopPropagation()}
                onChange={(event) => {
                  setScanPages(Number(event.target.value));
                  setResult(null);
                  setSelectedSymbols(new Set());
                }}
                className="rounded-md border border-slate-300 bg-white px-2 py-1 text-xs text-slate-700 dark:border-slate-600 dark:bg-slate-900 dark:text-slate-200"
              >
                <option value={2}>2 页</option>
                <option value={3}>3 页</option>
                <option value={5}>5 页</option>
              </select>
            )}
          </label>
          <label className="flex cursor-pointer items-start gap-2 rounded-lg border border-slate-200 p-3 dark:border-slate-700">
            <input
              type="checkbox"
              checked={includeFundamentalDetails}
              onChange={(event) => {
                setIncludeFundamentalDetails(event.target.checked);
                setResult(null);
                setSelectedSymbols(new Set());
              }}
              className="mt-0.5 h-4 w-4 rounded border-slate-300 text-cyan-600 focus:ring-cyan-500"
            />
            <span>
              <span className="block text-sm text-slate-700 dark:text-slate-300">
                补充财务与事件
              </span>
              <span className="block text-xs text-slate-500">
                可选，可能显著增加等待时间；设置相关过滤时会自动加载
              </span>
            </span>
          </label>
          {poolType === 'SHORT' && (
            <label className="flex cursor-pointer items-start gap-2 rounded-lg border border-slate-200 p-3 dark:border-slate-700">
              <input
                type="checkbox"
                checked={includeMarginDetails}
                onChange={(event) => {
                  setIncludeMarginDetails(event.target.checked);
                  setResult(null);
                  setSelectedSymbols(new Set());
                }}
                className="mt-0.5 h-4 w-4 rounded border-slate-300 text-cyan-600 focus:ring-cyan-500"
              />
              <span>
                <span className="block text-sm text-slate-700 dark:text-slate-300">
                  补充保证金比例
                </span>
                <span className="block text-xs text-slate-500">
                  可选；不代表实时券源或融券费，设置保证金过滤时会自动加载
                </span>
              </span>
            </label>
          )}
          {market === 'US' && poolType === 'SHORT' && (
            <label className="flex cursor-pointer items-start gap-2 rounded-lg border border-slate-200 p-3 dark:border-slate-700">
              <input
                type="checkbox"
                checked={includeShortCapacity}
                onChange={(event) => {
                  setIncludeShortCapacity(event.target.checked);
                  setResult(null);
                  setSelectedSymbols(new Set());
                }}
                className="mt-0.5 h-4 w-4 rounded border-slate-300 text-cyan-600 focus:ring-cyan-500"
              />
              <span>
                <span className="block text-sm text-slate-700 dark:text-slate-300">
                  补充账户预估卖空能力
                </span>
                <span className="block text-xs text-slate-500">
                  美股账户点时预估；不提供融券费率或召回风险
                </span>
              </span>
            </label>
          )}
        </div>

        <div className="mt-3 rounded-lg border border-slate-200 dark:border-slate-700">
          <button
            type="button"
            onClick={() => setShowFilters((value) => !value)}
            className="flex w-full items-center justify-between px-3 py-2 text-left text-sm font-medium text-slate-700 dark:text-slate-300"
          >
            <span>流动性、财务、事件与风险过滤（可选）</span>
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
                    max_spread_bps: '',
                    min_top_of_book_notional: '',
                    min_revenue_yoy: '',
                    max_revenue_yoy: '',
                    min_net_profit_yoy: '',
                    max_net_profit_yoy: '',
                    min_operating_cash_flow_yoy: '',
                    min_analyst_alignment: '',
                    min_eps_revision_alignment: '',
                    min_days_to_financial_event: '',
                    min_days_to_corporate_action: '',
                    max_initial_margin_ratio: '',
                    min_short_selling_quantity: '',
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
                  {result.scan.mode === 'single_page'
                    ? `第 ${result.page + 1} 页`
                    : `扫描第 ${result.scan.first_page + 1}–${result.scan.last_page + 1} 页`}
                  ，共 {result.total} 个上游候选；已选择 {selectedCount} 个
                </p>
                <p className="text-xs text-slate-500">
                  市场基准 {result.relative_strength.benchmark_symbol}；
                  行业 RS 为{result.scan.pages_scanned === 1 ? '本页' : '各来源页'}同行中位数差
                </p>
                <p className="text-xs text-slate-500">
                  已排除非正常交易标的；点差与盘口仅在设置对应阈值时请求，
                  冲击成本仍需订单规模才能评估
                </p>
                {result.filters.excluded > 0 && (
                  <p className="text-xs text-amber-600 dark:text-amber-400">
                    {result.scan.mode === 'single_page' ? '本页' : '扫描范围'}按候选约束过滤 {result.filters.excluded} 只，
                    保留 {result.filters.after}/{result.filters.before} 只
                  </p>
                )}
                {result.scan.mode === 'bounded' && (
                  <p className="text-xs text-slate-500">
                    实际扫描 {result.scan.candidates_scanned} 只，返回 {result.scan.candidates_returned} 只；
                    {result.scan.stopped_reason === 'source_exhausted'
                      ? '已到达上游末页'
                      : '已达到本次页数上限'}
                  </p>
                )}
              </div>
              <button
                type="button"
                onClick={() => {
                  if (
                    result.items.length > 0
                    && selectedCount === result.items.length
                  ) {
                    setSelectedSymbols(new Set());
                  } else {
                    setSelectedSymbols(new Set(result.items.map((item) => item.symbol)));
                  }
                }}
                className="text-sm text-cyan-600 hover:text-cyan-700 dark:text-cyan-400"
              >
                {
                  result.items.length > 0
                  && selectedCount === result.items.length
                    ? '取消全选'
                    : result.scan.mode === 'single_page' ? '全选本页' : '全选结果'
                }
              </button>
            </div>

            {result.enrichment.status === 'fallback' && (
              <div className="mb-2">
                <Alert type="warning">
                  实时指标暂不可用，当前仅展示 Screener 原始结果。
                </Alert>
              </div>
            )}
            {result.fundamentals.status === 'fallback' && (
              <div className="mb-2">
                <Alert type="warning">
                  财务与事件数据暂不可用，当前候选未应用相关补充信息。
                </Alert>
              </div>
            )}
            {result.margin_requirements.status === 'fallback' && (
              <div className="mb-2">
                <Alert type="warning">
                  保证金比例暂不可用；券源和融券费仍保持未知。
                </Alert>
              </div>
            )}
            {result.short_capacity.status === 'fallback' && (
              <div className="mb-2">
                <Alert type="warning">
                  账户预估可卖空数量暂不可用；无法判断账户权限、风险控制或临时服务错误。
                </Alert>
              </div>
            )}
            {result.short_capacity.status === 'unsupported' && (
              <div className="mb-2">
                <Alert type="warning">
                  账户预估可卖空数量目前仅支持美股做空候选。
                </Alert>
              </div>
            )}
            {poolType === 'SHORT' && (
              <p className="mb-2 text-xs text-amber-600 dark:text-amber-400">
                做空拥挤度、保证金比例和账户预估数量都不是实时券源清单；
                当前融券费率与召回风险明确标记为未知。
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
                        <span>
                          交易状态 {
                            candidate.tradeability.trade_status === 'normal'
                              ? '正常'
                              : candidate.tradeability.trade_status || '-'
                          }
                        </span>
                        <span>
                          点差 {candidate.tradeability.spread_bps == null
                            ? '-'
                            : `${candidate.tradeability.spread_bps.toFixed(1)} bps`}
                        </span>
                        <span>
                          一档盘口 {formatIndex(
                            candidate.tradeability.top_of_book_notional,
                          )}
                        </span>
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
                        {candidate.fundamentals.status !== 'disabled' && (
                          <>
                            <span>
                              收入同比 {formatRatio(
                                candidate.fundamentals.revenue_yoy,
                              )}
                            </span>
                            <span>
                              净利润同比 {formatRatio(
                                candidate.fundamentals.net_profit_yoy,
                              )}
                            </span>
                            <span>
                              经营现金流同比 {formatRatio(
                                candidate.fundamentals.operating_cash_flow_yoy,
                              )}
                            </span>
                            <span>
                              分析师一致性 {formatRatio(
                                candidate.fundamentals.analyst_alignment,
                              )}
                            </span>
                            <span>
                              EPS 修正一致性 {formatRatio(
                                candidate.fundamentals.eps_revision_alignment,
                              )}
                            </span>
                            {candidate.fundamentals.financial_event ? (
                              <span>
                                财报 {candidate.fundamentals.days_to_financial_event} 天后
                                （{candidate.fundamentals.financial_event.content
                                  || '财报事件'}）
                              </span>
                            ) : candidate.fundamentals.days_to_financial_event != null ? (
                              <span>
                                未来 {result.fundamentals.event_window_days} 天无财报事件
                              </span>
                            ) : (
                              <span>财报事件 -</span>
                            )}
                            {candidate.fundamentals.corporate_action ? (
                              <span>
                                公司行动 {candidate.fundamentals.days_to_corporate_action} 天后
                                （{candidate.fundamentals.corporate_action.description
                                  || candidate.fundamentals.corporate_action.type}）
                              </span>
                            ) : candidate.fundamentals.days_to_corporate_action != null ? (
                              <span>
                                未来 {result.fundamentals.event_window_days} 天无公司行动
                              </span>
                            ) : (
                              <span>公司行动 -</span>
                            )}
                            {candidate.fundamentals.status === 'partial' && (
                              <span className="text-amber-600 dark:text-amber-400">
                                部分财务数据缺失
                              </span>
                            )}
                          </>
                        )}
                        {poolType === 'SHORT' && (
                          <>
                            <span>
                              做空比例 {formatIndex(candidate.short_risk.short_ratio)}
                            </span>
                            <span>
                              回补天数 {formatIndex(candidate.short_risk.days_to_cover)}
                            </span>
                            {candidate.margin_requirements.status !== 'disabled' && (
                              <>
                                <span>
                                  初始保证金 {formatRatio(
                                    candidate.margin_requirements.initial_margin_ratio,
                                  )}
                                </span>
                                <span>券源/融券费 未知</span>
                              </>
                            )}
                            {!['disabled', 'not_applicable'].includes(
                              candidate.short_capacity.status,
                            ) && (
                              <>
                                <span>
                                  账户预估可卖空 {
                                    candidate.short_capacity.short_selling_max_qty == null
                                      ? '-'
                                      : `${formatIndex(
                                          candidate.short_capacity.short_selling_max_qty,
                                        )} 股`
                                  }
                                </span>
                                <span>融券费率/召回风险 未知</span>
                              </>
                            )}
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
                  onClick={() => runSearch(Math.max(0, result.page - result.scan.requested_pages))}
                  disabled={searching || result.page === 0}
                >
                  上一页
                </Button>
                <Button
                  type="button"
                  size="sm"
                  variant="secondary"
                  onClick={() => {
                    if (result.scan.next_page != null) {
                      runSearch(result.scan.next_page);
                    }
                  }}
                  disabled={searching || result.scan.next_page == null}
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
