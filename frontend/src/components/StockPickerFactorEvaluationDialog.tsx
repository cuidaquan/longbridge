import React, { useEffect, useMemo, useState } from 'react';
import { Close } from '@mui/icons-material';
import {
  getStockPickerFactorIncrementEvaluations,
  runStockPickerFactorIncrementEvaluation,
  type StockPickerFactorIncrementEvaluationHistoryItem,
  type StockPickerFactorIncrementEvaluationReport,
} from '../api/stockPicker';
import { Alert, Button, LoadingSpinner } from './ui';

const VARIANT_LABELS: Record<string, string> = {
  fundamental_quality: '财务质量',
  event_safety: '事件安全',
  execution_risk: '执行风险',
  fundamental_and_execution: '财务 + 执行',
};

const FACTOR_LABELS: Record<string, string> = {
  market_rs: '市场相对强度',
  fundamental_quality: '财务质量',
  expectations: '分析师与 EPS 预期',
  financial_event: '财报窗口',
  corporate_action: '公司行动窗口',
  trade_status: '交易状态',
  depth: '点差与一档金额',
  margin: '保证金',
  short_risk: 'SHORT 拥挤风险',
  short_capacity: '账户卖空能力',
};

const inputClass = `w-full rounded-lg border border-slate-300 bg-white px-3 py-2
  text-slate-900 focus:border-cyan-500 focus:outline-none focus:ring-2
  focus:ring-cyan-500/30 disabled:opacity-60 dark:border-slate-600
  dark:bg-slate-900 dark:text-white`;

const formatPercent = (value: number | null | undefined) => (
  value == null ? '-' : `${(value * 100).toFixed(2)}%`
);

const inferenceDirectionLabel = (
  direction: 'positive' | 'negative' | 'inconclusive' | null,
) => {
  if (direction === 'positive') return '正向';
  if (direction === 'negative') return '负向';
  return '不确定';
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

const gateReasonLabel = (reason: string) => {
  const labels: Record<string, string> = {
    insufficient_observation_dates: '观测日期不足',
    insufficient_distinct_symbols: '不同股票数不足',
    missing_latest_snapshot: '没有有效快照',
    stale_latest_snapshot: '最新快照已过期',
  };
  if (labels[reason]) return labels[reason];
  const factor = reason.match(/^insufficient_factor_coverage:(.+)$/);
  if (factor) {
    return `${FACTOR_LABELS[factor[1]] || factor[1]}覆盖率不足`;
  }
  const label = reason.match(/^insufficient_label_coverage:(\d+)$/);
  if (label) return `${label[1]} 日标签覆盖率不足`;
  const paired = reason.match(/^insufficient_paired_dates:([^:]+):(\d+)$/);
  if (paired) {
    return `${VARIANT_LABELS[paired[1]] || paired[1]} ${paired[2]} 日配对日期不足`;
  }
  return reason;
};

const exclusionReasonLabel = (reason: string) => {
  const labels: Record<string, string> = {
    unsupported_snapshot_version: '快照版本不支持',
    invalid_payload: '快照载荷无效',
    not_post_close: '不是收盘后快照',
    future_observed_at: '观测时间位于未来',
    invalid_observation_date: '观测日期无效',
    invalid_market: '市场无效',
    observation_date_mismatch: '市场日期与观测时间不一致',
    invalid_symbol: '股票代码无效',
    duplicate_same_day_replaced: '同日旧快照被替换',
    duplicate_same_day_ignored: '同日较旧快照被忽略',
    missing_signal_bar: '缺少信号日 K 线',
    invalid_signal_close: '信号日收盘价无效',
  };
  const future = reason.match(/^missing_future_bar_(\d+)$/);
  if (future) return `缺少未来第 ${future[1]} 根 K 线`;
  const invalidFuture = reason.match(/^invalid_future_bar_(\d+)$/);
  if (invalidFuture) return `未来第 ${invalidFuture[1]} 根 K 线无效`;
  return labels[reason] || reason;
};

function MetricTile({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="rounded-lg bg-slate-50 px-3 py-2 dark:bg-slate-900/50">
      <p className="text-xs text-slate-500 dark:text-slate-400">{label}</p>
      <p className="mt-1 text-lg font-semibold text-slate-900 dark:text-white">
        {value}
      </p>
    </div>
  );
}

export default function StockPickerFactorEvaluationDialog({
  onClose,
}: {
  onClose: () => void;
}) {
  const [market, setMarket] = useState<'US' | 'HK'>('US');
  const [poolType, setPoolType] = useState<'LONG' | 'SHORT'>('LONG');
  const [horizonInput, setHorizonInput] = useState('5, 10, 20');
  const [lookbackDays, setLookbackDays] = useState(730);
  const [maxBars, setMaxBars] = useState(5000);
  const [minimumDates, setMinimumDates] = useState(60);
  const [minimumSymbols, setMinimumSymbols] = useState(10);
  const [factorCoveragePercent, setFactorCoveragePercent] = useState(90);
  const [maximumAgeHours, setMaximumAgeHours] = useState(48);
  const [labelCoveragePercent, setLabelCoveragePercent] = useState(90);
  const [minimumPairedDates, setMinimumPairedDates] = useState(40);
  const [minimumSelectedPerDate, setMinimumSelectedPerDate] = useState(3);
  const [bootstrapSamples, setBootstrapSamples] = useState(2000);
  const [confidencePercent, setConfidencePercent] = useState(95);
  const [bootstrapBlockSize, setBootstrapBlockSize] = useState('');
  const [bootstrapSeed, setBootstrapSeed] = useState(20260724);
  const [running, setRunning] = useState(false);
  const [loadingHistory, setLoadingHistory] = useState(true);
  const [history, setHistory] = useState<
    StockPickerFactorIncrementEvaluationHistoryItem[]
  >([]);
  const [report, setReport] = useState<
    StockPickerFactorIncrementEvaluationReport | null
  >(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    getStockPickerFactorIncrementEvaluations(8)
      .then((response) => {
        if (!cancelled) setHistory(response.items);
      })
      .catch((err) => {
        if (!cancelled) {
          setError(
            err instanceof Error ? err.message : '获取因子增量评估历史失败',
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

  const horizonTokens = useMemo(
    () => horizonInput.split(',').map((value) => value.trim()),
    [horizonInput],
  );
  const horizons = useMemo(() => {
    const values = horizonTokens.map((value) => Number(value));
    return [...new Set(values)].sort((left, right) => left - right);
  }, [horizonTokens]);
  const parsedBlockSize = bootstrapBlockSize === ''
    ? null
    : Number(bootstrapBlockSize);
  const parametersValid = (
    horizonTokens.length >= 1
    && horizonTokens.every((value) => value !== '' && Number.isInteger(Number(value)))
    && horizons.length >= 1
    && horizons.length <= 10
    && horizons.every((value) => value >= 1 && value <= 252)
    && Number.isInteger(lookbackDays) && lookbackDays >= 1 && lookbackDays <= 3650
    && Number.isInteger(maxBars) && maxBars >= 2 && maxBars <= 10000
    && Number.isInteger(minimumDates) && minimumDates >= 1 && minimumDates <= 3650
    && Number.isInteger(minimumSymbols) && minimumSymbols >= 1 && minimumSymbols <= 10000
    && Number.isFinite(factorCoveragePercent)
    && factorCoveragePercent >= 0 && factorCoveragePercent <= 100
    && Number.isFinite(maximumAgeHours)
    && maximumAgeHours >= 1 && maximumAgeHours <= 87600
    && Number.isFinite(labelCoveragePercent)
    && labelCoveragePercent >= 0 && labelCoveragePercent <= 100
    && Number.isInteger(minimumPairedDates)
    && minimumPairedDates >= 1 && minimumPairedDates <= 3650
    && Number.isInteger(minimumSelectedPerDate)
    && minimumSelectedPerDate >= 1 && minimumSelectedPerDate <= 10000
    && Number.isInteger(bootstrapSamples)
    && bootstrapSamples >= 200 && bootstrapSamples <= 100000
    && Number.isFinite(confidencePercent)
    && confidencePercent >= 80 && confidencePercent <= 99
    && (
      parsedBlockSize == null
      || (Number.isInteger(parsedBlockSize) && parsedBlockSize >= 2 && parsedBlockSize <= 3650)
    )
    && Number.isInteger(bootstrapSeed)
    && bootstrapSeed >= 0 && bootstrapSeed <= 4294967295
  );

  const runEvaluation = async () => {
    setRunning(true);
    setError(null);
    try {
      const result = await runStockPickerFactorIncrementEvaluation({
        market,
        poolType,
        horizons,
        lookbackDays,
        maxBars,
        minimumObservationDates: minimumDates,
        minimumDistinctSymbols: minimumSymbols,
        minimumFactorCoverage: factorCoveragePercent / 100,
        maximumSnapshotAgeHours: maximumAgeHours,
        minimumLabelCoverage: labelCoveragePercent / 100,
        minimumPairedDates,
        minimumSelectedPerDate,
        bootstrapSamples,
        bootstrapConfidenceLevel: confidencePercent / 100,
        bootstrapBlockSize: parsedBlockSize,
        bootstrapSeed,
      });
      setReport(result);
      setHistory((current) => [
        {
          id: result.id || 0,
          created_at: new Date().toISOString(),
          market: result.market,
          pool_type: result.pool_type,
          evaluation_version: result.evaluation_version,
          parameters: result.parameters,
          result: {
            evaluation_version: result.evaluation_version,
            market: result.market,
            pool_type: result.pool_type,
            ready: result.ready,
            coverage: result.coverage,
            metrics: result.metrics,
            methodology: result.methodology,
          },
          ready: result.ready,
          data_as_of: result.coverage.latest_label_date,
        },
        ...current.filter((item) => item.id !== result.id),
      ].slice(0, 8));
    } catch (err) {
      setError(err instanceof Error ? err.message : '因子增量评估失败');
    } finally {
      setRunning(false);
    }
  };

  const openHistory = (
    item: StockPickerFactorIncrementEvaluationHistoryItem,
  ) => {
    setMarket(item.market);
    setPoolType(item.pool_type);
    setHorizonInput(item.parameters.horizons.join(', '));
    setLookbackDays(item.parameters.lookback_days);
    setMaxBars(item.parameters.max_bars);
    setMinimumDates(item.parameters.minimum_observation_dates);
    setMinimumSymbols(item.parameters.minimum_distinct_symbols);
    setFactorCoveragePercent(item.parameters.minimum_factor_coverage * 100);
    setMaximumAgeHours(item.parameters.maximum_snapshot_age_hours);
    setLabelCoveragePercent(item.parameters.minimum_label_coverage * 100);
    setMinimumPairedDates(item.parameters.minimum_paired_dates);
    setMinimumSelectedPerDate(item.parameters.minimum_selected_per_date);
    setBootstrapSamples(item.parameters.bootstrap_samples);
    setConfidencePercent(item.parameters.bootstrap_confidence_level * 100);
    setBootstrapBlockSize(
      item.parameters.bootstrap_block_size == null
        ? ''
        : String(item.parameters.bootstrap_block_size),
    );
    setBootstrapSeed(item.parameters.bootstrap_seed);
    setReport({ ...item.result, id: item.id, parameters: item.parameters });
    setError(null);
  };

  const reportInferenceReady = Boolean(
    report?.metrics
    && Object.values(report.metrics).every((variant) => (
      report.parameters.horizons.every((horizon) => (
        variant[String(horizon)]?.paired_delta_inference.ready === true
      ))
    )),
  );

  const numberInputs: Array<[
    string,
    number,
    React.Dispatch<React.SetStateAction<number>>,
    number,
    number,
  ]> = [
    ['回看天数', lookbackDays, setLookbackDays, 1, 3650],
    ['最大日 K 数', maxBars, setMaxBars, 2, 10000],
    ['最少观测日期', minimumDates, setMinimumDates, 1, 3650],
    ['最少不同股票', minimumSymbols, setMinimumSymbols, 1, 10000],
    ['最低因子覆盖（%）', factorCoveragePercent, setFactorCoveragePercent, 0, 100],
    ['快照最长年龄（小时）', maximumAgeHours, setMaximumAgeHours, 1, 87600],
    ['最低标签覆盖（%）', labelCoveragePercent, setLabelCoveragePercent, 0, 100],
    ['最少配对日期', minimumPairedDates, setMinimumPairedDates, 1, 3650],
    ['每日最少入选', minimumSelectedPerDate, setMinimumSelectedPerDate, 1, 10000],
    ['Bootstrap 次数', bootstrapSamples, setBootstrapSamples, 200, 100000],
    ['置信水平（%）', confidencePercent, setConfidencePercent, 80, 99],
    ['Bootstrap 种子', bootstrapSeed, setBootstrapSeed, 0, 4294967295],
  ];

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-3 sm:p-4">
      <div className="max-h-[calc(100vh-1.5rem)] w-full max-w-6xl overflow-y-auto rounded-lg bg-white p-4 shadow-2xl dark:bg-slate-800 sm:max-h-[calc(100vh-2rem)] sm:p-6">
        <div className="mb-5 flex items-start justify-between gap-4">
          <div className="min-w-0">
            <h3 className="text-xl font-bold text-slate-900 dark:text-white">
              因子增量评估
            </h3>
            <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">
              按市场日期比较全股票基线与冻结的财务、事件和执行风险过滤变体。
            </p>
          </div>
          <button
            type="button"
            aria-label="关闭因子增量评估弹窗"
            onClick={onClose}
            className="shrink-0 rounded p-1 hover:bg-slate-100 dark:hover:bg-slate-700"
          >
            <Close className="h-5 w-5 text-slate-500" />
          </button>
        </div>

        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          <div>
            <span className="mb-1 block text-sm font-medium text-slate-700 dark:text-slate-300">
              市场
            </span>
            <div className="grid grid-cols-2 gap-2">
              {(['US', 'HK'] as const).map((value) => (
                <button
                  key={value}
                  type="button"
                  disabled={running}
                  onClick={() => setMarket(value)}
                  className={`rounded-lg border px-3 py-2 text-sm font-medium ${
                    market === value
                      ? 'border-cyan-500 bg-cyan-50 text-cyan-700 dark:bg-cyan-900/30 dark:text-cyan-300'
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
              方向
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
          <label className="text-sm font-medium text-slate-700 dark:text-slate-300">
            <span className="mb-1 block">持有期（交易日，逗号分隔）</span>
            <input
              type="text"
              value={horizonInput}
              disabled={running}
              onChange={(event) => setHorizonInput(event.target.value)}
              className={inputClass}
            />
          </label>
          {numberInputs.map(([label, value, setter, min, max]) => (
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
                className={inputClass}
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
              className={inputClass}
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
          <div className="mt-4"><Alert type="error">{error}</Alert></div>
        )}

        {report && (
          <div className="mt-5 space-y-4">
            <Alert type={report.ready ? 'success' : 'warning'}>
              {report.ready
                ? reportInferenceReady
                  ? '样本门禁与时间聚类推断均已通过。'
                  : '样本门禁已通过；描述性指标可用，部分置信区间尚未就绪。'
                : `样本门禁未通过：${report.coverage.gate_reasons.map(gateReasonLabel).join('；')}`}
            </Alert>

            <p className="break-words text-xs text-slate-500 dark:text-slate-400">
              {report.evaluation_version}
              {' · '}{report.parameters.snapshot_version}
              {' · '}Bootstrap {report.parameters.bootstrap_samples}
              {' · '}置信水平 {formatPercent(report.parameters.bootstrap_confidence_level)}
            </p>

            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
              <MetricTile
                label="有效 / 原始快照"
                value={`${report.coverage.eligible_snapshot_rows}/${report.coverage.raw_snapshot_rows}`}
              />
              <MetricTile label="观测日期" value={report.coverage.observation_dates} />
              <MetricTile label="不同股票" value={report.coverage.distinct_symbols} />
              <MetricTile
                label="最新快照年龄"
                value={report.coverage.snapshot_age_hours == null
                  ? '-'
                  : `${report.coverage.snapshot_age_hours.toFixed(1)} 小时`}
              />
              <MetricTile
                label="最新后验标签"
                value={report.coverage.latest_label_date || '-'}
              />
            </div>

            <div className="overflow-x-auto rounded-lg border border-slate-200 dark:border-slate-700">
              <table className="min-w-full text-sm">
                <thead className="bg-slate-50 text-left text-xs uppercase text-slate-500 dark:bg-slate-900/50 dark:text-slate-400">
                  <tr>
                    <th className="px-3 py-2">适用因子</th>
                    <th className="px-3 py-2">可用 / 总数</th>
                    <th className="px-3 py-2">覆盖率</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-200 dark:divide-slate-700">
                  {report.coverage.applicable_factors.map((factor) => {
                    const item = report.coverage.factor_coverage[factor];
                    return (
                      <tr key={factor} className="text-slate-700 dark:text-slate-300">
                        <td className="px-3 py-2 font-medium">
                          {FACTOR_LABELS[factor] || factor}
                        </td>
                        <td className="px-3 py-2">
                          {item ? `${item.available_count}/${item.total_count}` : '-'}
                        </td>
                        <td className="px-3 py-2">
                          {formatPercent(item?.coverage)}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>

            <div className="overflow-x-auto rounded-lg border border-slate-200 dark:border-slate-700">
              <table className="min-w-[980px] text-sm">
                <thead className="bg-slate-50 text-left text-xs uppercase text-slate-500 dark:bg-slate-900/50 dark:text-slate-400">
                  <tr>
                    <th className="px-3 py-2">过滤变体</th>
                    <th className="px-3 py-2">持有期</th>
                    <th className="px-3 py-2">标签覆盖</th>
                    <th className="px-3 py-2">配对日期</th>
                    <th className="px-3 py-2">基线收益</th>
                    <th className="px-3 py-2">变体收益</th>
                    <th className="px-3 py-2">配对增量</th>
                    <th className="px-3 py-2">增量置信区间</th>
                    <th className="px-3 py-2">日均入选</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-200 dark:divide-slate-700">
                  {Object.keys(report.parameters.variants).flatMap((variant) => (
                    report.parameters.horizons.map((horizon) => {
                      const key = String(horizon);
                      const metric = report.metrics?.[variant]?.[key];
                      const inference = metric?.paired_delta_inference;
                      return (
                        <tr key={`${variant}-${horizon}`} className="text-slate-700 dark:text-slate-300">
                          <td className="px-3 py-2 font-medium">
                            {VARIANT_LABELS[variant] || variant}
                          </td>
                          <td className="px-3 py-2">{horizon} 日</td>
                          <td className="px-3 py-2">
                            {formatPercent(report.coverage.label_coverage_by_horizon[key])}
                          </td>
                          <td className="px-3 py-2">
                            {report.coverage.paired_dates_by_variant_horizon[variant]?.[key] || 0}
                          </td>
                          <td className="px-3 py-2">
                            {formatPercent(metric?.baseline.average)}
                          </td>
                          <td className="px-3 py-2">
                            {formatPercent(metric?.variant.average)}
                          </td>
                          <td className="px-3 py-2">
                            {formatPercent(metric?.paired_delta.average)}
                          </td>
                          <td className="whitespace-nowrap px-3 py-2">
                            {!metric || !inference
                              ? '-'
                              : inference.ready
                                ? `${formatPercent(inference.lower)} ～ ${formatPercent(inference.upper)}（${inferenceDirectionLabel(inference.interval_direction)}）`
                                : inferenceReasonLabel(inference.reason)}
                          </td>
                          <td className="px-3 py-2">
                            {metric ? metric.average_selected_count.toFixed(1) : '-'}
                          </td>
                        </tr>
                      );
                    })
                  ))}
                </tbody>
              </table>
            </div>

            {Object.keys(report.coverage.excluded_rows).length > 0 && (
              <p className="text-xs text-slate-500 dark:text-slate-400">
                排除记录：{' '}
                {Object.entries(report.coverage.excluded_rows)
                  .map(([reason, count]) => `${exclusionReasonLabel(reason)} ${count}`)
                  .join('；')}
              </p>
            )}

            <Alert type="info">{report.methodology.causal_limit}</Alert>
            <Alert type="info">
              {report.methodology.costs}；{report.methodology.price_limit}
            </Alert>
          </div>
        )}

        <div className="mt-6 border-t border-slate-200 pt-4 dark:border-slate-700">
          <p className="mb-2 text-sm font-semibold text-slate-900 dark:text-white">
            最近评估
          </p>
          {loadingHistory ? (
            <LoadingSpinner size="sm" text="加载评估历史..." />
          ) : history.length === 0 ? (
            <p className="text-xs text-slate-500 dark:text-slate-400">暂无评估记录</p>
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
                    {item.market} · {item.pool_type}
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
