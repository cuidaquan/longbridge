import { useCallback, useEffect, useRef, useState } from "react";
import {
  AutoGraph,
  CheckCircle,
  Close,
  ErrorOutline,
  History,
  PlayArrow,
  QueryStats,
  Refresh,
} from "@mui/icons-material";
import { Tooltip } from "@mui/material";
import {
  Alert,
  Badge,
  Button,
  Card,
  EmptyState,
  LoadingSpinner,
  PageHeader,
  ProgressBar,
  Tabs,
} from "../components/ui";
import {
  createQuantSelectionRun,
  getLatestQuantSelection,
  getQuantSelectionResults,
  getQuantSelectionRun,
  listQuantSelectionRuns,
  quantSelectionEventsUrl,
  type QuantCandidate,
  type QuantFinalResult,
  type QuantRunStatus,
  type QuantSelectionResults,
  type QuantSelectionRun,
} from "../api/quantStockSelector";

const ACTIVE_RUN_KEY = "quantSelectorActiveRunId";
const HARD_FILTER_COUNT = 8;
const TERMINAL_STATUSES = new Set<QuantRunStatus>([
  "completed",
  "partial",
  "failed",
  "cancelled",
]);
const SSE_STATUSES: QuantRunStatus[] = [
  "queued",
  "loading_universe",
  "scoring_quant",
  "analyzing_ai",
  "completed",
  "partial",
  "failed",
  "cancelled",
];

const STATUS_META: Record<QuantRunStatus, { label: string; progress: number }> = {
  queued: { label: "等待运行", progress: 5 },
  loading_universe: { label: "加载候选与快照", progress: 25 },
  scoring_quant: { label: "计算量化评分", progress: 55 },
  analyzing_ai: { label: "AI 最终确认", progress: 82 },
  completed: { label: "完整运行", progress: 100 },
  partial: { label: "部分完成", progress: 100 },
  failed: { label: "运行失败", progress: 100 },
  cancelled: { label: "已取消", progress: 100 },
};

function statusVariant(status: QuantRunStatus) {
  if (status === "completed") return "success" as const;
  if (status === "partial") return "warning" as const;
  if (status === "failed" || status === "cancelled") return "danger" as const;
  return "info" as const;
}

function formatDate(value: string | null) {
  if (!value) return "--";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}

function formatScore(value: number | null | undefined) {
  return value == null || !Number.isFinite(value) ? "--" : value.toFixed(1);
}

function formatPercent(value: number | string | null | undefined) {
  const number = Number(value);
  return Number.isFinite(number) ? `${(number * 100).toFixed(1)}%` : "--";
}

function candidateDataTime(candidate: QuantCandidate | undefined) {
  if (!candidate) return "--";
  return candidate.bar_data_as_of || candidate.price_data_as_of || "--";
}

export default function QuantStockSelector() {
  const [run, setRun] = useState<QuantSelectionRun | null>(null);
  const [data, setData] = useState<QuantSelectionResults | null>(null);
  const [history, setHistory] = useState<QuantSelectionRun[]>([]);
  const [view, setView] = useState<"results" | "diagnostics">("results");
  const [selected, setSelected] = useState<QuantCandidate | null>(null);
  const [loading, setLoading] = useState(true);
  const [starting, setStarting] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const eventSourceRef = useRef<EventSource | null>(null);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const mountedRef = useRef(true);
  const connectRef = useRef<(runId: string) => void>(() => undefined);

  const closeConnection = useCallback(() => {
    eventSourceRef.current?.close();
    eventSourceRef.current = null;
    if (reconnectTimerRef.current) {
      clearTimeout(reconnectTimerRef.current);
      reconnectTimerRef.current = null;
    }
  }, []);

  const loadHistory = useCallback(async () => {
    const response = await listQuantSelectionRuns(20);
    if (mountedRef.current) setHistory(response.items);
  }, []);

  const loadResults = useCallback(async (runId: string) => {
    const response = await getQuantSelectionResults(runId);
    if (!mountedRef.current) return;
    setRun(response.run);
    setData(response);
    setStarting(false);
    setView(response.run.status === "partial" ? "diagnostics" : "results");
  }, []);

  const finishRun = useCallback(async (snapshot: QuantSelectionRun) => {
    closeConnection();
    localStorage.removeItem(ACTIVE_RUN_KEY);
    setRun(snapshot);
    setStarting(false);
    await Promise.allSettled([
      loadResults(snapshot.run_id),
      loadHistory(),
    ]);
  }, [closeConnection, loadHistory, loadResults]);

  const connectToRun = useCallback((runId: string) => {
    closeConnection();
    const source = new EventSource(quantSelectionEventsUrl(runId));
    eventSourceRef.current = source;

    const handleEvent = (event: Event) => {
      if (!(event instanceof MessageEvent)) return;
      try {
        const snapshot = JSON.parse(event.data) as QuantSelectionRun;
        if (!mountedRef.current || snapshot.run_id !== runId) return;
        setRun(snapshot);
        if (TERMINAL_STATUSES.has(snapshot.status)) {
          void finishRun(snapshot);
        }
      } catch {
        setError("运行进度响应无效");
      }
    };
    SSE_STATUSES.forEach((status) => source.addEventListener(status, handleEvent));

    source.onerror = () => {
      if (eventSourceRef.current !== source) return;
      source.close();
      eventSourceRef.current = null;
      void getQuantSelectionRun(runId)
        .then((snapshot) => {
          if (!mountedRef.current) return;
          setRun(snapshot);
          if (TERMINAL_STATUSES.has(snapshot.status)) {
            void finishRun(snapshot);
            return;
          }
          reconnectTimerRef.current = setTimeout(
            () => connectRef.current(runId),
            1500,
          );
        })
        .catch((reason: unknown) => {
          if (!mountedRef.current) return;
          setError(reason instanceof Error ? reason.message : "运行状态恢复失败");
          setStarting(false);
        });
    };
  }, [closeConnection, finishRun]);

  useEffect(() => {
    connectRef.current = connectToRun;
  }, [connectToRun]);

  useEffect(() => {
    mountedRef.current = true;
    const initialize = async () => {
      setLoading(true);
      setError(null);
      const activeRunId = localStorage.getItem(ACTIVE_RUN_KEY);
      try {
        const [latestResult, historyResult] = await Promise.allSettled([
          getLatestQuantSelection(),
          listQuantSelectionRuns(20),
        ]);
        if (!mountedRef.current) return;
        if (historyResult.status === "fulfilled") {
          setHistory(historyResult.value.items);
        }
        if (latestResult.status === "fulfilled" && latestResult.value) {
          setData(latestResult.value);
          setRun(latestResult.value.run);
        }
        const rejected = [latestResult, historyResult].find(
          (item) => item.status === "rejected",
        );
        if (rejected?.status === "rejected") {
          throw rejected.reason;
        }
        if (activeRunId) {
          const snapshot = await getQuantSelectionRun(activeRunId);
          if (!mountedRef.current) return;
          setRun(snapshot);
          if (TERMINAL_STATUSES.has(snapshot.status)) {
            localStorage.removeItem(ACTIVE_RUN_KEY);
            await Promise.all([
              loadResults(snapshot.run_id),
              loadHistory(),
            ]);
          } else {
            setStarting(true);
            connectToRun(snapshot.run_id);
          }
        }
      } catch (reason) {
        if (mountedRef.current) {
          setError(reason instanceof Error ? reason.message : "量化优选加载失败");
        }
      } finally {
        if (mountedRef.current) setLoading(false);
      }
    };
    void initialize();
    return () => {
      mountedRef.current = false;
      closeConnection();
    };
  }, [closeConnection, connectToRun, loadHistory, loadResults]);

  const startRun = async () => {
    setStarting(true);
    setError(null);
    setSelected(null);
    try {
      const created = await createQuantSelectionRun(false);
      if (!mountedRef.current) return;
      setRun(created);
      setData(null);
      localStorage.setItem(ACTIVE_RUN_KEY, created.run_id);
      connectToRun(created.run_id);
    } catch (reason) {
      setStarting(false);
      setError(reason instanceof Error ? reason.message : "无法创建量化优选运行");
    }
  };

  const refreshStatus = async () => {
    setRefreshing(true);
    setError(null);
    try {
      if (run) {
        const snapshot = await getQuantSelectionRun(run.run_id);
        if (!mountedRef.current) return;
        setRun(snapshot);
        if (TERMINAL_STATUSES.has(snapshot.status)) {
          await Promise.all([loadResults(snapshot.run_id), loadHistory()]);
        } else {
          connectToRun(snapshot.run_id);
        }
      } else {
        const [latest, runs] = await Promise.all([
          getLatestQuantSelection(),
          listQuantSelectionRuns(20),
        ]);
        if (!mountedRef.current) return;
        setHistory(runs.items);
        if (latest) {
          setData(latest);
          setRun(latest.run);
        }
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "运行状态刷新失败");
    } finally {
      if (mountedRef.current) setRefreshing(false);
    }
  };

  const openRun = async (item: QuantSelectionRun) => {
    if (!TERMINAL_STATUSES.has(item.status)) return;
    setError(null);
    try {
      await loadResults(item.run_id);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "运行结果加载失败");
    }
  };

  const openResult = (result: QuantFinalResult) => {
    const candidate = data?.candidates.find((item) => item.symbol === result.symbol);
    if (candidate) setSelected(candidate);
  };

  if (loading) return <LoadingSpinner size="lg" text="加载量化优选..." />;

  const running = Boolean(run && !TERMINAL_STATUSES.has(run.status));
  const status = run?.status;
  const completedProgress = status ? STATUS_META[status].progress : 0;
  const candidates = data?.candidates || [];
  const hardFilteredCount = candidates.filter((candidate) => (
    Object.keys(candidate.hard_filters || {}).length === HARD_FILTER_COUNT
    && Object.values(candidate.hard_filters).every((item) => item.status === "pass")
  )).length;
  const quantCandidateCount = candidates.filter((candidate) => (
    candidate.quant_score != null
    && candidate.quant_score.total >= 65
    && Object.values(candidate.hard_filters || {}).every(
      (item) => item.status === "pass",
    )
  )).length;

  return (
    <div className="space-y-5 animate-fade-in">
      <PageHeader
        title="量化优选"
        description="美国主板统一候选 · 5–20 个交易日"
        icon={<QueryStats />}
        actions={(
          <div className="flex items-center gap-2">
            <Tooltip title="刷新运行状态" arrow>
              <span>
                <button
                  type="button"
                  aria-label="刷新运行状态"
                  onClick={() => void refreshStatus()}
                  disabled={refreshing}
                  className="flex h-9 w-9 items-center justify-center rounded-lg border border-slate-300 bg-white text-slate-600 transition-colors hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-50 dark:border-slate-600 dark:bg-slate-800 dark:text-slate-300 dark:hover:bg-slate-700"
                >
                  <Refresh className={`h-5 w-5 ${refreshing ? "animate-spin" : ""}`} />
                </button>
              </span>
            </Tooltip>
            <Button
              onClick={() => void startRun()}
              loading={starting || running}
              icon={<PlayArrow className="h-4 w-4" />}
            >
              {running ? "筛选运行中" : "运行量化优选"}
            </Button>
          </div>
        )}
      />

      {error && (
        <Alert type="error" onClose={() => setError(null)}>{error}</Alert>
      )}
      {run?.status === "partial" && (
        <Alert type="warning" title="本次运行不完整">
          最终榜单未发布。{run.error_summary.join("；")}
        </Alert>
      )}
      {run?.status === "failed" && (
        <Alert type="error" title="本次运行失败">
          {run.error_summary.join("；") || "无法形成可用的量化诊断"}
        </Alert>
      )}

      {run && (
        <Card padding="sm">
          <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_auto] lg:items-center">
            <div>
              <div className="mb-2 flex flex-wrap items-center gap-2">
                <Badge variant={statusVariant(run.status)} dot>
                  {STATUS_META[run.status].label}
                </Badge>
                <span className="text-xs text-slate-500 dark:text-slate-400">
                  {run.data_as_of || "数据时点待确定"}
                </span>
                {run.reused_from_run_id && (
                  <Badge variant="default">缓存复用</Badge>
                )}
                <span className="font-mono text-[11px] text-slate-400">
                  {run.score_version}
                </span>
                <span className="text-xs text-slate-500 dark:text-slate-400">
                  {run.resolved_model_id || run.model_alias}
                </span>
              </div>
              <ProgressBar
                value={completedProgress}
                variant={run.status === "partial" ? "warning" : run.status === "failed" ? "danger" : run.status === "completed" ? "success" : "default"}
              />
            </div>
            <div className="grid grid-cols-2 gap-4 text-right sm:grid-cols-5">
              <RunMetric label="证券总数" value={run.candidate_count} />
              <RunMetric label="硬过滤后" value={hardFilteredCount} />
              <RunMetric label="量化候选" value={quantCandidateCount} />
              <RunMetric label="AI 完成" value={run.ai_completed_count} />
              <RunMetric label="最终入选" value={run.final_count} />
            </div>
          </div>
        </Card>
      )}

      <div className="grid gap-5 xl:grid-cols-[minmax(0,1fr)_280px]">
        <section className="min-w-0">
          <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
            <Tabs
              tabs={[
                { id: "results", label: `最终榜单 ${data?.results.length || 0}` },
                { id: "diagnostics", label: `候选诊断 ${data?.candidates.length || 0}` },
              ]}
              activeTab={view}
              onChange={(value) => setView(value as "results" | "diagnostics")}
            />
            {data?.run.resolved_model_id && (
              <span className="text-xs text-slate-500 dark:text-slate-400">
                {data.run.resolved_model_id}
              </span>
            )}
          </div>

          <Card padding="none" className="overflow-hidden">
            {view === "results" ? (
              data?.results.length ? (
                <ResultsTable
                  results={data.results}
                  candidates={data.candidates}
                  onOpen={openResult}
                />
              ) : (
                <EmptyState
                  icon={<AutoGraph />}
                  title={run?.status === "partial" ? "最终榜单未发布" : "暂无入选标的"}
                />
              )
            ) : data?.candidates.length ? (
              <DiagnosticsTable
                candidates={data.candidates}
                onOpen={setSelected}
              />
            ) : (
              <EmptyState icon={<QueryStats />} title="暂无候选诊断" />
            )}
          </Card>
        </section>

        <aside className="min-w-0">
          <div className="mb-3 flex h-10 items-center gap-2">
            <History className="h-5 w-5 text-slate-500" />
            <h2 className="text-sm font-semibold text-slate-900 dark:text-white">
              历史运行
            </h2>
          </div>
          <Card padding="none" className="max-h-[620px] overflow-y-auto">
            {history.length ? history.map((item) => (
              <button
                key={item.run_id}
                type="button"
                onClick={() => void openRun(item)}
                disabled={!TERMINAL_STATUSES.has(item.status)}
                className={`w-full border-b border-slate-100 px-4 py-3 text-left transition-colors last:border-b-0 hover:bg-slate-50 disabled:cursor-default dark:border-slate-700 dark:hover:bg-slate-700/40 ${run?.run_id === item.run_id ? "bg-cyan-50 dark:bg-cyan-900/20" : ""}`}
              >
                <div className="flex items-center justify-between gap-2">
                  <Badge variant={statusVariant(item.status)}>{STATUS_META[item.status].label}</Badge>
                  <span className="text-xs text-slate-400">{formatDate(item.created_at)}</span>
                </div>
                <div className="mt-2 flex items-center justify-between text-xs text-slate-500 dark:text-slate-400">
                  <span>{item.data_as_of || "--"}</span>
                  <span>{item.final_count} 入选</span>
                </div>
                <div className="mt-1 truncate font-mono text-[11px] text-slate-400">
                  {item.score_version}
                </div>
              </button>
            )) : (
              <EmptyState icon={<History />} title="暂无历史运行" />
            )}
          </Card>
        </aside>
      </div>

      {selected && (
        <CandidateDrawer
          candidate={selected}
          run={data?.run || run}
          onClose={() => setSelected(null)}
        />
      )}
    </div>
  );
}

function RunMetric({ label, value }: { label: string; value: number }) {
  return (
    <div className="min-w-[52px]">
      <div className="text-lg font-semibold text-slate-900 dark:text-white">{value}</div>
      <div className="text-xs text-slate-500 dark:text-slate-400">{label}</div>
    </div>
  );
}

function ResultsTable({
  results,
  candidates,
  onOpen,
}: {
  results: QuantFinalResult[];
  candidates: QuantCandidate[];
  onOpen: (result: QuantFinalResult) => void;
}) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[1040px] text-sm">
        <thead className="bg-slate-50 text-xs uppercase text-slate-500 dark:bg-slate-900/50 dark:text-slate-400">
          <tr>
            <th className="w-14 px-4 py-3 text-left">排名</th>
            <th className="px-4 py-3 text-left">标的</th>
            <th className="px-4 py-3 text-left">交易所</th>
            <th className="px-4 py-3 text-right">Q</th>
            <th className="px-4 py-3 text-right">AI</th>
            <th className="px-4 py-3 text-right">F</th>
            <th className="px-4 py-3 text-right">置信度</th>
            <th className="px-4 py-3 text-right">趋势</th>
            <th className="px-4 py-3 text-right">相对强度</th>
            <th className="px-4 py-3 text-right">流动性</th>
            <th className="px-4 py-3 text-right">风险</th>
            <th className="px-4 py-3 text-right">数据时点</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100 dark:divide-slate-700">
          {results.map((item, index) => {
            const candidate = candidates.find((value) => value.symbol === item.symbol);
            return (
              <tr
                key={item.symbol}
                onClick={() => onOpen(item)}
                className="cursor-pointer bg-white transition-colors hover:bg-cyan-50/60 dark:bg-slate-800 dark:hover:bg-cyan-900/10"
              >
                <td className="px-4 py-3 font-mono text-slate-500">{index + 1}</td>
                <td className="px-4 py-3">
                  <div className="font-semibold text-slate-900 dark:text-white">{item.symbol}</div>
                  <div className="max-w-48 truncate text-xs text-slate-500">{item.name}</div>
                </td>
                <td className="px-4 py-3">
                  <Badge>{candidate?.catalog_evidence.exchange || "--"}</Badge>
                </td>
                <td className="px-4 py-3 text-right tabular-nums">{formatScore(item.quant_score)}</td>
                <td className="px-4 py-3 text-right tabular-nums">{formatScore(item.ai_score)}</td>
                <td className="px-4 py-3 text-right font-semibold tabular-nums text-cyan-700 dark:text-cyan-400">{formatScore(item.final_score)}</td>
                <td className="px-4 py-3 text-right tabular-nums">{(item.ai_decision.confidence * 100).toFixed(0)}%</td>
                <td className="px-4 py-3 text-right tabular-nums">{formatScore(Number(candidate?.quant_score?.trend))}</td>
                <td className="px-4 py-3 text-right tabular-nums">{formatScore(Number(candidate?.quant_score?.relative_strength))}</td>
                <td className="px-4 py-3 text-right tabular-nums">{formatScore(Number(candidate?.quant_score?.liquidity))}</td>
                <td className="px-4 py-3 text-right tabular-nums">{formatScore(Number(candidate?.quant_score?.risk))}</td>
                <td className="whitespace-nowrap px-4 py-3 text-right text-xs text-slate-500">{candidateDataTime(candidate)}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function DiagnosticsTable({
  candidates,
  onOpen,
}: {
  candidates: QuantCandidate[];
  onOpen: (candidate: QuantCandidate) => void;
}) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[720px] text-sm">
        <thead className="bg-slate-50 text-xs uppercase text-slate-500 dark:bg-slate-900/50 dark:text-slate-400">
          <tr>
            <th className="px-4 py-3 text-left">标的</th>
            <th className="px-4 py-3 text-left">目录</th>
            <th className="px-4 py-3 text-left">状态</th>
            <th className="px-4 py-3 text-right">Q</th>
            <th className="px-4 py-3 text-right">硬过滤</th>
            <th className="px-4 py-3 text-left">排除原因</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100 dark:divide-slate-700">
          {candidates.map((item) => (
            <tr
              key={item.symbol}
              onClick={() => onOpen(item)}
              className="cursor-pointer bg-white transition-colors hover:bg-slate-50 dark:bg-slate-800 dark:hover:bg-slate-700/40"
            >
              <td className="px-4 py-3 font-semibold text-slate-900 dark:text-white">{item.symbol}</td>
              <td className="px-4 py-3">
                <div className="font-medium text-slate-700 dark:text-slate-200">
                  {item.catalog_evidence.exchange || "--"}
                </div>
                <div className="text-xs text-slate-400">
                  {item.catalog_evidence.board || "--"}
                </div>
              </td>
              <td className="px-4 py-3">
                <Badge variant={item.selected_for_ai ? "info" : item.exclusion_reasons.length ? "danger" : "default"}>
                  {item.selected_for_ai ? "AI 候选" : item.selection_status}
                </Badge>
              </td>
              <td className="px-4 py-3 text-right tabular-nums">{formatScore(item.quant_score?.total)}</td>
              <td className="px-4 py-3 text-right tabular-nums">
                {Object.values(item.hard_filters || {}).filter(
                  (filter) => filter.status === "pass",
                ).length} / {HARD_FILTER_COUNT}
              </td>
              <td className="max-w-64 truncate px-4 py-3 text-slate-500 dark:text-slate-400">
                {item.exclusion_reasons.join("、") || "--"}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function CandidateDrawer({
  candidate,
  run,
  onClose,
}: {
  candidate: QuantCandidate;
  run: QuantSelectionRun | null;
  onClose: () => void;
}) {
  const decision = candidate.ai?.decision;
  const passed = Object.values(candidate.hard_filters || {}).filter(
    (item) => item.status === "pass",
  ).length;
  return (
    <div className="fixed inset-0 z-50 bg-slate-950/35" onMouseDown={onClose}>
      <aside
        role="dialog"
        aria-modal="true"
        aria-label={`${candidate.symbol} 量化优选详情`}
        onMouseDown={(event) => event.stopPropagation()}
        className="absolute right-0 top-0 h-full w-full max-w-xl overflow-y-auto border-l border-slate-200 bg-white shadow-2xl dark:border-slate-700 dark:bg-slate-900"
      >
        <div className="sticky top-0 z-10 flex items-center justify-between border-b border-slate-200 bg-white px-5 py-4 dark:border-slate-700 dark:bg-slate-900">
          <div>
            <div className="flex items-center gap-2">
              <h2 className="text-xl font-semibold text-slate-900 dark:text-white">{candidate.symbol}</h2>
              <Badge>{candidate.catalog_evidence.exchange || "USMain"}</Badge>
            </div>
            <p className="mt-0.5 text-sm text-slate-500">{candidate.name}</p>
          </div>
          <Tooltip title="关闭" arrow>
            <button
              type="button"
              aria-label="关闭详情"
              onClick={onClose}
              className="flex h-9 w-9 items-center justify-center rounded-lg text-slate-500 hover:bg-slate-100 dark:hover:bg-slate-800"
            >
              <Close />
            </button>
          </Tooltip>
        </div>

        <div className="space-y-6 p-5">
          <div className="grid grid-cols-3 gap-3">
            <DetailMetric label="Q 分" value={formatScore(candidate.quant_score?.total)} />
            <DetailMetric label="AI 分" value={formatScore(candidate.result?.ai_score)} />
            <DetailMetric label="F 分" value={formatScore(candidate.result?.final_score)} emphasis />
          </div>

          <DetailSection title="目录与流动性">
            <DetailRows rows={[
              ["市场", candidate.catalog_evidence.market || "--"],
              ["目录板块", candidate.catalog_evidence.board || "--"],
              ["交易所", candidate.catalog_evidence.exchange || "--"],
              ["20 日成交额中位数", candidate.indicators?.median_turnover_20d == null ? "--" : `$${(Number(candidate.indicators.median_turnover_20d) / 1_000_000).toFixed(1)}M`],
            ]} />
          </DetailSection>

          <DetailSection title="量化分项">
            <DetailRows rows={[
              ["趋势", formatScore(Number(candidate.quant_score?.trend))],
              ["相对强度", formatScore(Number(candidate.quant_score?.relative_strength))],
              ["流动性", formatScore(Number(candidate.quant_score?.liquidity))],
              ["动量确认", formatScore(Number(candidate.quant_score?.momentum))],
              ["风险适配", formatScore(Number(candidate.quant_score?.risk))],
              ["RS20 / RS60", `${formatPercent(candidate.indicators?.rs20)} / ${formatPercent(candidate.indicators?.rs60)}`],
              ["RSI14", formatScore(Number(candidate.indicators?.rsi14))],
              ["ATR14 / Close", formatPercent(candidate.indicators?.atr14_close)],
              ["20 日波动率", formatPercent(candidate.indicators?.volatility_20d)],
              ["60 日最大回撤", formatPercent(candidate.indicators?.max_drawdown_60d)],
            ]} />
          </DetailSection>

          <DetailSection title="硬过滤">
            <div className="mb-3 text-sm text-slate-500 dark:text-slate-400">{passed} / {HARD_FILTER_COUNT} 通过</div>
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
              {Object.entries(candidate.hard_filters || {}).sort().map(([code, item]) => (
                <div key={code} className="flex items-center gap-2 border-b border-slate-100 py-2 dark:border-slate-800">
                  {item.status === "pass" ? (
                    <CheckCircle className="h-4 w-4 text-emerald-500" />
                  ) : (
                    <ErrorOutline className="h-4 w-4 text-red-500" />
                  )}
                  <span className="text-sm font-medium text-slate-700 dark:text-slate-200">{code}</span>
                  <span className="truncate text-xs text-slate-400">{item.reason || item.status}</span>
                </div>
              ))}
            </div>
          </DetailSection>

          {decision && (
            <DetailSection title="AI 决策">
              <div className="mb-4 flex flex-wrap items-center gap-2">
                <Badge variant={decision.decision === "SELECT" ? "success" : "danger"}>{decision.decision}</Badge>
                <Badge variant={decision.risk_level === "HIGH" ? "danger" : decision.risk_level === "MEDIUM" ? "warning" : "success"}>{decision.risk_level}</Badge>
                <span className="text-sm text-slate-500">置信度 {(decision.confidence * 100).toFixed(0)}%</span>
              </div>
              <TextList title="依据" items={decision.reasons} />
              <TextList title="风险" items={decision.risks} />
              <DetailRows rows={[
                ["入场条件", decision.entry_condition],
                ["失效条件", decision.invalidation_condition],
                ["持有期", `${decision.time_horizon_days} 个交易日`],
              ]} />
            </DetailSection>
          )}

          {candidate.exclusion_reasons.length > 0 && (
            <DetailSection title="排除原因">
              <TextList items={candidate.exclusion_reasons} />
            </DetailSection>
          )}

          <DetailSection title="数据完整性与哈希">
            <DetailRows rows={[
              ["数据时点", candidateDataTime(candidate)],
              ["日 K 截止", candidate.bar_data_as_of || "--"],
              ["目录来源", candidate.catalog_evidence.source || "--"],
              ["目录版本", candidate.catalog_evidence.source_version || "--"],
              ["目录捕获时间", formatDate(candidate.catalog_evidence.captured_at || null)],
              ["硬过滤", `${passed} / ${HARD_FILTER_COUNT} 通过`],
              ["AI 输入", candidate.ai?.request_status || "未计划"],
              ["候选输入哈希", candidate.candidate_quant_input_hash || "--"],
              ["运行输入哈希", run?.run_input_hash || "--"],
              ["AI 输入哈希", candidate.ai?.ai_input_hash || "--"],
            ]} />
          </DetailSection>
        </div>
      </aside>
    </div>
  );
}

function DetailMetric({ label, value, emphasis = false }: { label: string; value: string; emphasis?: boolean }) {
  return (
    <div className="border-b border-slate-200 pb-3 dark:border-slate-700">
      <div className={`text-2xl font-semibold tabular-nums ${emphasis ? "text-cyan-700 dark:text-cyan-400" : "text-slate-900 dark:text-white"}`}>{value}</div>
      <div className="mt-1 text-xs text-slate-500">{label}</div>
    </div>
  );
}

function DetailSection({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section>
      <h3 className="mb-3 text-sm font-semibold text-slate-900 dark:text-white">{title}</h3>
      {children}
    </section>
  );
}

function DetailRows({ rows }: { rows: Array<[string, string]> }) {
  return (
    <dl className="divide-y divide-slate-100 text-sm dark:divide-slate-800">
      {rows.map(([label, value]) => (
        <div key={label} className="grid grid-cols-[120px_minmax(0,1fr)] gap-3 py-2.5">
          <dt className="text-slate-500 dark:text-slate-400">{label}</dt>
          <dd className="break-words text-slate-800 dark:text-slate-200">{value}</dd>
        </div>
      ))}
    </dl>
  );
}

function TextList({ title, items }: { title?: string; items: string[] }) {
  if (!items.length) return null;
  return (
    <div className="mb-4">
      {title && <div className="mb-1.5 text-xs font-medium text-slate-500">{title}</div>}
      <ul className="space-y-1.5 text-sm text-slate-700 dark:text-slate-300">
        {items.map((item, index) => <li key={`${item}-${index}`}>· {item}</li>)}
      </ul>
    </div>
  );
}
