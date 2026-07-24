import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  CompareArrows,
  DatasetOutlined,
  FactCheckOutlined,
  Refresh,
  Search,
} from '@mui/icons-material';
import {
  compareSecurityUniverseSnapshots,
  getSecurityUniverseClassification,
  getSecurityUniverseClassificationCoverage,
  getSecurityUniverseCoverage,
  getSecurityUniverseSnapshot,
  getSecurityUniverseSnapshots,
  type SecurityMarket,
  type SecurityUniverseClassificationCoverage,
  type SecurityUniverseClassificationDetail,
  type SecurityUniverseClassificationReadinessReason,
  type SecurityUniverseCaptureRun,
  type SecurityUniverseComparisonReason,
  type SecurityUniverseCoverage,
  type SecurityUniverseSnapshotComparison,
  type SecurityUniverseSnapshotDetail,
  type SecurityUniverseSnapshotHistory,
} from '../api/stockPicker';
import {
  Alert,
  Badge,
  Button,
  EmptyState,
  Input,
  LoadingSpinner,
  Select,
} from './ui';

const MARKET_OPTIONS = [
  { value: 'US', label: '美国市场' },
  { value: 'HK', label: '香港市场' },
  { value: 'CN', label: '沪深市场' },
];

const COMPARISON_REASON_LABELS: Record<SecurityUniverseComparisonReason, string> = {
  base_snapshot_integrity_invalid: '基准快照完整性未通过',
  target_snapshot_integrity_invalid: '目标快照完整性未通过',
  market_mismatch: '两个快照属于不同市场',
  snapshot_version_mismatch: '两个快照版本不一致',
};

const INTEGRITY_ERROR_LABELS: Record<string, string> = {
  invalid_payload_json: '载荷不是有效 JSON',
  payload_not_object: '载荷不是对象',
  unsupported_snapshot_version: '快照版本不支持',
  unexpected_source: '数据来源不一致',
  payload_hash_mismatch: '载荷哈希不一致',
  payload_version_mismatch: '载荷版本不一致',
  payload_source_mismatch: '载荷来源不一致',
  payload_market_mismatch: '载荷市场不一致',
  payload_date_mismatch: '载荷观测日不一致',
  payload_captured_at_mismatch: '载荷捕获时间不一致',
  source_request_mismatch: '来源请求不一致',
  security_count_mismatch: '证券数量不一致',
  non_canonical_items: '证券条目未规范化',
  source_snapshot_integrity_invalid: '源目录快照完整性未通过',
  source_snapshot_hash_mismatch: '源目录快照哈希不一致',
  source_snapshot_reference_mismatch: '源目录快照引用不一致',
  source_snapshot_metadata_mismatch: '源目录快照元数据不一致',
  classification_policy_mismatch: '分类策略不一致',
  classification_count_mismatch: '分类数量不一致',
  classification_metadata_count_mismatch: '分类摘要数量不一致',
  classification_readiness_mismatch: '研究候选门禁不一致',
  non_canonical_classification_items: '分类条目未规范化',
};

const READINESS_REASON_LABELS: Record<
  SecurityUniverseClassificationReadinessReason,
  string
> = {
  incomplete_static_info: '静态信息覆盖不完整',
  unknown_board: '存在未知板块',
  board_market_mismatch: '存在跨市场板块',
};

const EXCLUSION_REASON_LABELS: Record<string, string> = {
  missing_static_info: '缺少静态信息',
  unknown_board: '未知板块',
  board_market_mismatch: '跨市场板块',
  otc_board: '场外市场',
  derivative_board: '衍生品',
  index_board: '指数',
  sector_board: '行业板块',
  pre_ipo_board: '上市前证券',
};

const METADATA_FIELD_LABELS: Record<string, string> = {
  name: '中文名',
  name_en: '英文名',
  name_hk: '繁体名',
};

function formatDateTime(value: string | null) {
  return value
    ? new Date(value).toLocaleString('zh-CN', { hour12: false })
    : '-';
}

function formatRunStatus(run: SecurityUniverseCaptureRun) {
  if (run.status === 'completed') return '已完成';
  if (run.status === 'failed') return '失败';
  return '运行中';
}

function runBadgeVariant(status: SecurityUniverseCaptureRun['status']) {
  if (status === 'completed') return 'success' as const;
  if (status === 'failed') return 'danger' as const;
  return 'info' as const;
}

export default function SecurityUniverseSnapshotPanel() {
  const [market, setMarket] = useState<SecurityMarket>('HK');
  const [coverage, setCoverage] = useState<SecurityUniverseCoverage | null>(null);
  const [classificationCoverage, setClassificationCoverage] = useState<
    SecurityUniverseClassificationCoverage | null
  >(null);
  const [history, setHistory] = useState<SecurityUniverseSnapshotHistory | null>(null);
  const [detail, setDetail] = useState<SecurityUniverseSnapshotDetail | null>(null);
  const [classificationDetail, setClassificationDetail] = useState<
    SecurityUniverseClassificationDetail | null
  >(null);
  const [comparison, setComparison] = useState<SecurityUniverseSnapshotComparison | null>(null);
  const [baseSnapshotId, setBaseSnapshotId] = useState('');
  const [targetSnapshotId, setTargetSnapshotId] = useState('');
  const [detailQuery, setDetailQuery] = useState('');
  const [loading, setLoading] = useState(true);
  const [detailLoading, setDetailLoading] = useState(false);
  const [comparisonLoading, setComparisonLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const loadAuditData = useCallback(async () => {
    setLoading(true);
    setError(null);
    setDetail(null);
    setClassificationDetail(null);
    setDetailQuery('');
    setComparison(null);
    try {
      const [coverageResult, classificationCoverageResult, historyResult] = await Promise.all([
        getSecurityUniverseCoverage(),
        getSecurityUniverseClassificationCoverage(),
        getSecurityUniverseSnapshots({ market, limit: 50 }),
      ]);
      setCoverage(coverageResult);
      setClassificationCoverage(classificationCoverageResult);
      setHistory(historyResult);
      const latest = historyResult.items[0];
      const previous = historyResult.items[1] || latest;
      setBaseSnapshotId(previous?.snapshot_id || '');
      setTargetSnapshotId(latest?.snapshot_id || '');
    } catch (loadError) {
      setCoverage(null);
      setClassificationCoverage(null);
      setHistory(null);
      setBaseSnapshotId('');
      setTargetSnapshotId('');
      setError(
        loadError instanceof Error
          ? loadError.message
          : '加载证券目录审计数据失败',
      );
    } finally {
      setLoading(false);
    }
  }, [market]);

  useEffect(() => {
    void loadAuditData();
  }, [loadAuditData]);

  const loadDetail = async (snapshotId: string) => {
    setDetailLoading(true);
    setError(null);
    setDetailQuery('');
    try {
      const [snapshotResult, classificationResult] = await Promise.all([
        getSecurityUniverseSnapshot(snapshotId),
        getSecurityUniverseClassification(snapshotId),
      ]);
      setDetail(snapshotResult);
      setClassificationDetail(classificationResult);
    } catch (loadError) {
      setDetail(null);
      setClassificationDetail(null);
      setError(
        loadError instanceof Error
          ? loadError.message
          : '加载证券目录快照详情失败',
      );
    } finally {
      setDetailLoading(false);
    }
  };

  const runComparison = async () => {
    if (!baseSnapshotId || !targetSnapshotId) {
      setError('请选择基准快照和目标快照');
      return;
    }
    setComparisonLoading(true);
    setError(null);
    try {
      setComparison(await compareSecurityUniverseSnapshots({
        baseSnapshotId,
        targetSnapshotId,
        detailLimit: 100,
      }));
    } catch (compareError) {
      setComparison(null);
      setError(
        compareError instanceof Error
          ? compareError.message
          : '比较证券目录快照失败',
      );
    } finally {
      setComparisonLoading(false);
    }
  };

  const visibleDetailItems = useMemo(() => {
    const items = detail?.payload?.items || [];
    const query = detailQuery.trim().toLocaleLowerCase();
    if (!query) return items.slice(0, 100);
    return items
      .filter((item) => (
        item.symbol.toLocaleLowerCase().includes(query)
        || item.name.toLocaleLowerCase().includes(query)
        || item.name_en.toLocaleLowerCase().includes(query)
        || item.name_hk.toLocaleLowerCase().includes(query)
      ))
      .slice(0, 100);
  }, [detail, detailQuery]);

  const matchingDetailCount = useMemo(() => {
    const items = detail?.payload?.items || [];
    const query = detailQuery.trim().toLocaleLowerCase();
    if (!query) return items.length;
    return items.filter((item) => (
      item.symbol.toLocaleLowerCase().includes(query)
      || item.name.toLocaleLowerCase().includes(query)
      || item.name_en.toLocaleLowerCase().includes(query)
      || item.name_hk.toLocaleLowerCase().includes(query)
    )).length;
  }, [detail, detailQuery]);

  const classificationBySymbol = useMemo(() => new Map(
    (classificationDetail?.payload?.items || []).map((item) => [item.symbol, item]),
  ), [classificationDetail]);

  if (loading && !coverage && !history) {
    return <LoadingSpinner size="md" text="加载证券目录审计数据..." />;
  }

  return (
    <div className="space-y-5">
      {error && (
        <Alert type="error" onClose={() => setError(null)}>
          {error}
        </Alert>
      )}

      <div className="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
        <div className="w-full sm:max-w-[220px]">
          <Select
            label="历史市场"
            value={market}
            options={MARKET_OPTIONS}
            onChange={(event) => setMarket(event.target.value as SecurityMarket)}
          />
        </div>
        <Button
          type="button"
          variant="secondary"
          icon={<Refresh className="h-4 w-4" />}
          loading={loading}
          onClick={loadAuditData}
        >
          刷新目录审计
        </Button>
      </div>

      {coverage && (
        <section aria-labelledby="security-universe-coverage-title">
          <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
            <div>
              <h4
                id="security-universe-coverage-title"
                className="font-semibold text-slate-900 dark:text-white"
              >
                市场覆盖
              </h4>
              <p className="text-xs text-slate-500 dark:text-slate-400">
                本地元数据统计 · 载荷在详情与比较时校验
              </p>
            </div>
            <Badge variant="info">{coverage.coverage_version}</Badge>
          </div>
          <div className="grid gap-3 md:grid-cols-3">
            {coverage.markets.map((item) => {
              const classification = classificationCoverage?.markets.find(
                (candidate) => candidate.market === item.market,
              );
              return (
                <button
                  type="button"
                  key={item.market}
                  onClick={() => setMarket(item.market)}
                  className={`min-w-0 rounded-lg border p-4 text-left transition-colors ${
                    market === item.market
                      ? 'border-cyan-500 bg-cyan-50/70 dark:bg-cyan-950/20'
                      : 'border-slate-200 hover:border-slate-300 dark:border-slate-700 dark:hover:border-slate-600'
                  }`}
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="font-semibold text-slate-900 dark:text-white">
                      {item.market}
                    </span>
                    <Badge variant={item.snapshot_count > 0 ? 'success' : 'default'}>
                      {item.snapshot_count > 0 ? '已采集' : '无快照'}
                    </Badge>
                  </div>
                  <div className="mt-3 grid grid-cols-3 gap-2 text-center">
                    <AuditMetric label="观测日" value={item.observation_dates} />
                    <AuditMetric label="目录快照" value={item.snapshot_count} />
                    <AuditMetric
                      label="分类快照"
                      value={classification?.classification_snapshot_count ?? 0}
                    />
                  </div>
                  <div className="mt-3 space-y-1 text-xs text-slate-500 dark:text-slate-400">
                    <p>最近：{item.latest_observation_date || '-'}</p>
                    <p>证券记录：{item.latest_security_count.toLocaleString('zh-CN')}</p>
                    <p>
                      分类缺口：{classification?.unclassified_snapshot_count ?? item.snapshot_count}
                    </p>
                    <p>
                      最近分类：{
                        classification?.latest?.ready_for_research_universe
                          ? '研究候选门禁通过'
                          : classification?.latest
                            ? '研究候选门禁未通过'
                            : '-'
                      }
                    </p>
                  </div>
                </button>
              );
            })}
          </div>
        </section>
      )}

      <section
        aria-labelledby="security-universe-history-title"
        className="border-t border-slate-200 pt-5 dark:border-slate-700"
      >
        <div className="mb-3 flex items-center justify-between gap-2">
          <div>
            <h4
              id="security-universe-history-title"
              className="font-semibold text-slate-900 dark:text-white"
            >
              {market} 快照历史
            </h4>
            <p className="text-xs text-slate-500 dark:text-slate-400">
              {history?.items.length || 0} 条快照 · {history?.capture_runs.length || 0} 条运行记录
            </p>
          </div>
          <Badge variant="default">{history?.snapshot_version || '-'}</Badge>
        </div>

        {!history || history.items.length === 0 ? (
          <EmptyState
            icon={<DatasetOutlined />}
            title="当前市场没有目录快照"
            description="自动采集保持关闭时不会产生外部调用或历史记录。"
          />
        ) : (
          <div className="grid min-w-0 gap-4 lg:grid-cols-[minmax(260px,0.8fr)_minmax(0,1.2fr)]">
            <div className="max-h-[430px] space-y-2 overflow-y-auto pr-1">
              {history.items.map((snapshot) => (
                <button
                  type="button"
                  key={snapshot.snapshot_id}
                  onClick={() => loadDetail(snapshot.snapshot_id)}
                  className={`w-full min-w-0 rounded-lg border p-3 text-left transition-colors ${
                    detail?.snapshot_id === snapshot.snapshot_id
                      ? 'border-cyan-500 bg-cyan-50/70 dark:bg-cyan-950/20'
                      : 'border-slate-200 hover:border-slate-300 dark:border-slate-700 dark:hover:border-slate-600'
                  }`}
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="font-medium text-slate-900 dark:text-white">
                      {snapshot.observation_date}
                    </span>
                    <span className="text-sm text-slate-600 dark:text-slate-300">
                      {snapshot.security_count.toLocaleString('zh-CN')} 条
                    </span>
                  </div>
                  <p className="mt-1 truncate font-mono text-[11px] text-slate-400">
                    {snapshot.payload_hash}
                  </p>
                  <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">
                    {formatDateTime(snapshot.captured_at)}
                  </p>
                </button>
              ))}
            </div>

            <div className="min-w-0 rounded-lg border border-slate-200 p-4 dark:border-slate-700">
              {detailLoading ? (
                <LoadingSpinner size="sm" text="校验并加载目录载荷..." />
              ) : !detail ? (
                <EmptyState
                  icon={<Search />}
                  title="选择快照查看详情"
                />
              ) : (
                <div className="min-w-0 space-y-3">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <div>
                      <p className="font-semibold text-slate-900 dark:text-white">
                        {detail.market} · {detail.observation_date}
                      </p>
                      <p className="text-xs text-slate-500 dark:text-slate-400">
                        {detail.security_count.toLocaleString('zh-CN')} 条证券记录
                      </p>
                    </div>
                    <Badge variant={detail.integrity_valid ? 'success' : 'danger'} dot>
                      {detail.integrity_valid ? '完整性通过' : '完整性失败'}
                    </Badge>
                  </div>

                  {!detail.integrity_valid && (
                    <Alert type="error">
                      {detail.integrity_errors
                        .map((reason) => INTEGRITY_ERROR_LABELS[reason] || reason)
                        .join('；')}
                    </Alert>
                  )}

                  <div className="grid gap-2 text-xs text-slate-500 dark:text-slate-400 sm:grid-cols-2">
                    <p className="break-all">保存哈希：{detail.payload_hash}</p>
                    <p className="break-all">重算哈希：{detail.computed_payload_hash || '-'}</p>
                  </div>

                  {!classificationDetail ? (
                    <div className="flex items-start gap-2 rounded-lg border border-slate-200 p-3 text-xs text-slate-500 dark:border-slate-700 dark:text-slate-400">
                      <FactCheckOutlined className="mt-0.5 h-4 w-4 shrink-0" />
                      <p>此目录快照尚无版本化静态分类。</p>
                    </div>
                  ) : (
                    <div className="space-y-3 border-t border-slate-200 pt-3 dark:border-slate-700">
                      <div className="flex flex-wrap items-center justify-between gap-2">
                        <div>
                          <p className="text-sm font-semibold text-slate-900 dark:text-white">
                            静态分类覆盖
                          </p>
                          <p className="text-xs text-slate-500 dark:text-slate-400">
                            {classificationDetail.classification_version}
                          </p>
                        </div>
                        <div className="flex flex-wrap gap-2">
                          <Badge
                            variant={classificationDetail.integrity_valid ? 'success' : 'danger'}
                          >
                            {classificationDetail.integrity_valid ? '分类完整' : '分类损坏'}
                          </Badge>
                          <Badge
                            variant={
                              classificationDetail.ready_for_research_universe
                                ? 'success'
                                : 'danger'
                            }
                          >
                            {classificationDetail.ready_for_research_universe
                              ? '研究候选就绪'
                              : '研究候选未就绪'}
                          </Badge>
                        </div>
                      </div>

                      {classificationDetail.payload && (
                        <>
                          <div className="grid grid-cols-2 gap-2 text-center sm:grid-cols-4">
                            <AuditMetric
                              label="静态信息"
                              value={`${classificationDetail.classified_count}/${classificationDetail.security_count}`}
                            />
                            <AuditMetric
                              label="板块已解析"
                              value={`${classificationDetail.resolved_board_count}/${classificationDetail.security_count}`}
                            />
                            <AuditMetric
                              label="研究候选"
                              value={classificationDetail.eligible_count}
                              tone="success"
                            />
                            <AuditMetric
                              label="排除"
                              value={classificationDetail.payload.counts.excluded_count}
                              tone="danger"
                            />
                          </div>

                          {classificationDetail.payload.readiness_reasons.length > 0 && (
                            <Alert type="error" title="分类门禁未通过">
                              {classificationDetail.payload.readiness_reasons
                                .map((reason) => READINESS_REASON_LABELS[reason] || reason)
                                .join('；')}
                            </Alert>
                          )}

                          {!classificationDetail.integrity_valid && (
                            <Alert type="error" title="分类完整性失败">
                              {classificationDetail.integrity_errors
                                .map((reason) => INTEGRITY_ERROR_LABELS[reason] || reason)
                                .join('；')}
                            </Alert>
                          )}

                          <div className="text-xs text-slate-500 dark:text-slate-400">
                            <p>
                              板块分布：{
                                Object.entries(classificationDetail.payload.board_counts)
                                  .map(([board, count]) => `${board} ${count}`)
                                  .join(' · ') || '-'
                              }
                            </p>
                            <p className="mt-1">
                              板块分类不区分普通股与 ETF，也不证明实时交易状态、账户权限或流动性。
                            </p>
                          </div>
                        </>
                      )}
                    </div>
                  )}

                  {detail.payload && (
                    <>
                      <Input
                        label="代码或名称"
                        value={detailQuery}
                        onChange={(event) => setDetailQuery(event.target.value)}
                        placeholder="例如 700.HK 或 腾讯"
                      />
                      <div className="flex items-center justify-between gap-2 text-xs text-slate-500 dark:text-slate-400">
                        <span>匹配 {matchingDetailCount.toLocaleString('zh-CN')} 条</span>
                        <span>显示前 {Math.min(100, matchingDetailCount)} 条</span>
                      </div>
                      <div className="max-h-[300px] overflow-auto rounded-lg border border-slate-200 dark:border-slate-700">
                        {visibleDetailItems.length === 0 ? (
                          <p className="p-4 text-center text-sm text-slate-500">
                            没有匹配记录
                          </p>
                        ) : (
                          <div className="divide-y divide-slate-100 dark:divide-slate-700">
                            {visibleDetailItems.map((item) => {
                              const classification = classificationBySymbol.get(
                                item.symbol,
                              );
                              const classificationLabel = !classification
                                ? '未分类'
                                : `${classification.board_raw || '未知板块'} · ${
                                  classification.research_eligible
                                    ? '研究候选'
                                    : EXCLUSION_REASON_LABELS[
                                      classification.exclusion_reason || ''
                                    ] || classification.exclusion_reason || '排除'
                                }`;
                              return (
                                <div
                                  key={item.symbol}
                                  className="grid min-w-0 gap-1 px-3 py-2 sm:grid-cols-[120px_minmax(0,1fr)_minmax(120px,0.65fr)]"
                                >
                                  <span className="font-mono text-xs font-semibold text-slate-800 dark:text-slate-200">
                                    {item.symbol}
                                  </span>
                                  <span className="min-w-0 break-words text-xs text-slate-600 dark:text-slate-300">
                                    {item.name || item.name_hk || item.name_en || '-'}
                                  </span>
                                  <span className="min-w-0 break-words text-[11px] text-slate-500 dark:text-slate-400">
                                    {classificationLabel}
                                  </span>
                                </div>
                              );
                            })}
                          </div>
                        )}
                      </div>
                    </>
                  )}
                </div>
              )}
            </div>
          </div>
        )}
      </section>

      {history && history.items.length > 0 && (
        <section
          aria-labelledby="security-universe-comparison-title"
          className="border-t border-slate-200 pt-5 dark:border-slate-700"
        >
          <div className="mb-3">
            <h4
              id="security-universe-comparison-title"
              className="font-semibold text-slate-900 dark:text-white"
            >
              快照比较
            </h4>
            <p className="text-xs text-slate-500 dark:text-slate-400">
              基准到目标 · 完整计数 · 每类最多显示 100 条
            </p>
          </div>
          <div className="grid gap-3 md:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_auto] md:items-end">
            <Select
              label="基准快照"
              value={baseSnapshotId}
              options={history.items.map((item) => ({
                value: item.snapshot_id,
                label: `${item.observation_date} · ${item.security_count.toLocaleString('zh-CN')} 条`,
              }))}
              onChange={(event) => setBaseSnapshotId(event.target.value)}
            />
            <Select
              label="目标快照"
              value={targetSnapshotId}
              options={history.items.map((item) => ({
                value: item.snapshot_id,
                label: `${item.observation_date} · ${item.security_count.toLocaleString('zh-CN')} 条`,
              }))}
              onChange={(event) => setTargetSnapshotId(event.target.value)}
            />
            <Button
              type="button"
              icon={<CompareArrows className="h-4 w-4" />}
              loading={comparisonLoading}
              onClick={runComparison}
            >
              比较
            </Button>
          </div>

          {comparison && (
            <div className="mt-4 space-y-3">
              {!comparison.ready ? (
                <Alert type="error" title="无法比较">
                  {comparison.reasons
                    .map((reason) => COMPARISON_REASON_LABELS[reason] || reason)
                    .join('；')}
                </Alert>
              ) : (
                <>
                  <div className="grid grid-cols-3 gap-2 text-center">
                    <AuditMetric label="新增" value={comparison.added_count ?? '-'} tone="success" />
                    <AuditMetric label="移除" value={comparison.removed_count ?? '-'} tone="danger" />
                    <AuditMetric label="名称变化" value={comparison.metadata_changed_count ?? '-'} tone="info" />
                  </div>
                  <div className="grid min-w-0 gap-3 lg:grid-cols-3">
                    <ComparisonList
                      title="新增记录"
                      count={comparison.added_count || 0}
                      truncated={comparison.added_truncated}
                      items={comparison.added.map((item) => ({
                        key: item.symbol,
                        title: item.symbol,
                        detail: item.name || item.name_hk || item.name_en || '-',
                      }))}
                    />
                    <ComparisonList
                      title="移除记录"
                      count={comparison.removed_count || 0}
                      truncated={comparison.removed_truncated}
                      items={comparison.removed.map((item) => ({
                        key: item.symbol,
                        title: item.symbol,
                        detail: item.name || item.name_hk || item.name_en || '-',
                      }))}
                    />
                    <ComparisonList
                      title="名称变化"
                      count={comparison.metadata_changed_count || 0}
                      truncated={comparison.metadata_changed_truncated}
                      items={comparison.metadata_changed.map((item) => ({
                        key: item.symbol,
                        title: item.symbol,
                        detail: Object.entries(item.changes)
                          .map(([field, change]) => (
                            `${METADATA_FIELD_LABELS[field] || field}: ${change.before || '-'} -> ${change.after || '-'}`
                          ))
                          .join('；'),
                      }))}
                    />
                  </div>
                </>
              )}
            </div>
          )}
        </section>
      )}

      {history && history.capture_runs.length > 0 && (
        <section
          aria-labelledby="security-universe-runs-title"
          className="border-t border-slate-200 pt-5 dark:border-slate-700"
        >
          <h4
            id="security-universe-runs-title"
            className="mb-3 font-semibold text-slate-900 dark:text-white"
          >
            最近采集运行
          </h4>
          <div className="space-y-2">
            {history.capture_runs.map((run) => (
              <div
                key={`${run.market}-${run.observation_date}-${run.claim_id}`}
                className="flex min-w-0 flex-col gap-2 rounded-lg border border-slate-200 px-3 py-2 text-xs dark:border-slate-700 sm:flex-row sm:items-center sm:justify-between"
              >
                <div className="min-w-0">
                  <p className="font-medium text-slate-800 dark:text-slate-200">
                    {run.market} · {run.observation_date} · {run.security_count.toLocaleString('zh-CN')} 条
                  </p>
                  <p className="mt-0.5 break-words text-slate-500 dark:text-slate-400">
                    {run.error || `开始于 ${formatDateTime(run.started_at)}`}
                  </p>
                </div>
                <span className="shrink-0">
                  <Badge variant={runBadgeVariant(run.status)} dot>
                    {formatRunStatus(run)}
                  </Badge>
                </span>
              </div>
            ))}
          </div>
        </section>
      )}
    </div>
  );
}

function AuditMetric({
  label,
  value,
  tone = 'default',
}: {
  label: string;
  value: string | number;
  tone?: 'default' | 'success' | 'danger' | 'info';
}) {
  const toneClass = {
    default: 'text-slate-900 dark:text-white',
    success: 'text-emerald-700 dark:text-emerald-300',
    danger: 'text-red-700 dark:text-red-300',
    info: 'text-cyan-700 dark:text-cyan-300',
  }[tone];
  return (
    <div className="min-w-0 rounded-lg bg-slate-50 px-2 py-2 dark:bg-slate-900/50">
      <p className="text-[11px] text-slate-500 dark:text-slate-400">{label}</p>
      <p className={`mt-1 text-base font-semibold ${toneClass}`}>{value}</p>
    </div>
  );
}

function ComparisonList({
  title,
  count,
  truncated,
  items,
}: {
  title: string;
  count: number;
  truncated: boolean;
  items: Array<{ key: string; title: string; detail: string }>;
}) {
  return (
    <div className="min-w-0 rounded-lg border border-slate-200 dark:border-slate-700">
      <div className="flex items-center justify-between gap-2 border-b border-slate-200 px-3 py-2 dark:border-slate-700">
        <p className="text-sm font-medium text-slate-800 dark:text-slate-200">{title}</p>
        <span className="text-xs text-slate-500">{count}</span>
      </div>
      {items.length === 0 ? (
        <p className="px-3 py-4 text-center text-xs text-slate-500">无变化</p>
      ) : (
        <div className="max-h-[250px] divide-y divide-slate-100 overflow-y-auto dark:divide-slate-700">
          {items.map((item) => (
            <div key={item.key} className="min-w-0 px-3 py-2">
              <p className="font-mono text-xs font-semibold text-slate-800 dark:text-slate-200">
                {item.title}
              </p>
              <p className="mt-0.5 break-words text-[11px] text-slate-500 dark:text-slate-400">
                {item.detail}
              </p>
            </div>
          ))}
        </div>
      )}
      {truncated && (
        <p className="border-t border-slate-200 px-3 py-2 text-[11px] text-amber-600 dark:border-slate-700 dark:text-amber-400">
          明细已截断，顶部计数为完整结果
        </p>
      )}
    </div>
  );
}
