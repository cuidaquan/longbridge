/**
 * 智能选股 API 客户端
 */

import { API_BASE } from './client';

export interface Stock {
  id: number;
  pool_type: string;
  symbol: string;
  name?: string;
  added_at: string;
  added_reason?: string;
  is_active: boolean;
  priority: number;
}

export interface ScoreBreakdown {
  trend: number;
  momentum: number;
  support_resistance?: number;
  volume: number;
  volatility: number;
  pattern: number;
}

export interface Score {
  total: number;
  grade: string;
  breakdown: ScoreBreakdown;
}

export interface AIDecision {
  action: string;
  confidence: number;
  reasoning: string[];
  status?: 'available' | 'disabled' | 'skipped' | 'fallback' | 'error';
  error?: string;
}

export interface AnalysisMetadata {
  data_as_of?: string;
  score_version?: string;
  prompt_version?: string;
  ai_model?: string;
  analysis_mode?: 'ai' | 'quant';
  job_id?: string;
  ai_snapshot_version?: string;
  ai_request_status?: string;
  ai_input_hash?: string;
  ai_snapshot_available?: boolean;
}

export interface Analysis {
  id: number;
  pool_id: number;
  symbol: string;
  pool_type: string;
  analysis_time: string;
  current_price: number;
  price_change_1d: number;
  price_change_5d: number;
  score: Score;
  ai_decision: AIDecision;
  signals: string[];
  recommendation_score: number;
  recommendation_reason: string;
  indicators?: Record<string, number | string | null>;
  metadata?: AnalysisMetadata;
  name?: string;
  added_reason?: string;
}

export interface StockPickerAIInputSnapshot {
  version: string;
  captured_at?: string;
  request_status: string;
  request_reason?: string | null;
  symbol: string;
  pool_type: 'LONG' | 'SHORT';
  scenario?: string | null;
  analysis_mode?: 'ai' | 'quant';
  data_as_of?: string | null;
  data_definition_version?: string;
  score_version?: string;
  prompt_version?: string;
  ai_model?: string | null;
  temperature?: number | null;
  style?: string | null;
  system_prompt?: string | null;
  user_prompt?: string | null;
  news_enabled?: boolean;
  news_snapshot?: Record<string, unknown> | null;
  klines_hash?: string;
  config_version?: string;
  universe_version?: string;
  selection_version?: string;
  selection_context?: string;
  selection?: {
    quant_rank?: number;
    ai_top_n_per_pool?: number;
    ai_selected?: boolean;
    ranking?: Array<Record<string, unknown>>;
  };
}

export interface StockPickerAIOutputSnapshot {
  version: string;
  captured_at?: string;
  status: string;
  raw_response?: string | null;
  parsed_response?: Record<string, unknown> | null;
  error_type?: string | null;
  error?: string | null;
}

export interface StockPickerAnalysisSnapshot {
  analysis_id: number;
  pool_id: number;
  symbol: string;
  pool_type: 'LONG' | 'SHORT';
  analysis_time: string;
  legacy_record: boolean;
  ai_input_hash?: string | null;
  hash_valid?: boolean | null;
  integrity: {
    recomputed_input_hash?: string | null;
    input_hash_valid?: boolean | null;
    expected_klines_hash?: string | null;
    actual_klines_hash?: string | null;
    klines_hash_valid?: boolean | null;
    indicators_match?: boolean | null;
    parse_errors: Record<string, string | null>;
  };
  ai_input_snapshot?: StockPickerAIInputSnapshot | null;
  ai_output_snapshot?: StockPickerAIOutputSnapshot | null;
  indicators_snapshot?: Record<string, unknown> | null;
  klines_snapshot?: Array<Record<string, unknown>> | null;
}

export interface PoolsResponse {
  long_pool: Stock[];
  short_pool: Stock[];
}

export type SecurityMarket = 'US' | 'HK' | 'CN';

export interface SecuritySearchItem {
  symbol: string;
  name: string;
  name_en: string;
  name_hk: string;
  market: SecurityMarket;
}

export interface SecuritySearchResponse {
  market: SecurityMarket;
  query: string;
  source: 'longbridge';
  items: SecuritySearchItem[];
}

export type ScreenerMarket = 'US' | 'HK' | 'CN' | 'SG';

export interface ScreenerStrategy {
  id: number;
  name: string;
  description?: string;
  market: ScreenerMarket;
  source: 'recommended' | 'user';
}

export interface ScreenerStrategiesResponse {
  market: ScreenerMarket;
  source: 'longbridge-screener';
  items: ScreenerStrategy[];
}

export interface ScreenerCandidate {
  rank: number;
  symbol: string;
  name: string;
  market: ScreenerMarket;
  indicators: Record<string, string | number | boolean | null>;
  indexes: Record<string, number | null>;
  relative_strength: {
    benchmark_symbol: string;
    target_direction: 'LONG' | 'SHORT';
    industry?: string | null;
    industry_peer_count: number;
    market_rs_10d?: number | null;
    market_rs_half_year?: number | null;
    industry_rs_10d?: number | null;
    industry_rs_half_year?: number | null;
  };
  short_risk: {
    status: 'available' | 'no_data' | 'unsupported' | 'error' | 'fallback' | 'not_applicable';
    error?: string | null;
    data_as_of?: string;
    short_ratio?: number | null;
    short_ratio_change?: number | null;
    days_to_cover?: number | null;
    shares_short?: number | null;
    avg_daily_volume?: number | null;
    short_amount?: number | null;
    short_balance?: number | null;
    short_cost?: number | null;
  };
  tradeability: {
    status: 'available' | 'no_data' | 'error' | 'fallback' | 'disabled';
    error?: string | null;
    trade_status?: string | null;
    is_tradable?: boolean | null;
    last_done?: number | null;
    volume?: number | null;
    turnover?: number | null;
    data_as_of?: string;
    best_bid?: number | null;
    best_ask?: number | null;
    spread_bps?: number | null;
    top_of_book_notional?: number | null;
    depth_error?: string;
    impact_cost_status: 'requires_order_size';
  };
  fundamentals: {
    status: 'available' | 'partial' | 'no_data' | 'error' | 'fallback' | 'disabled';
    errors: string[];
    revenue_yoy?: number | null;
    net_profit_yoy?: number | null;
    operating_cash_flow_yoy?: number | null;
    analyst_alignment?: number | null;
    analyst_total?: number | null;
    target_price?: number | null;
    eps_revision_alignment?: number | null;
    days_to_financial_event?: number | null;
    financial_event?: {
      date: string;
      content: string;
      market_time: string;
      star: number;
    } | null;
    days_to_corporate_action?: number | null;
    corporate_action?: {
      date: string;
      type: string;
      description: string;
    } | null;
  };
  margin_requirements: {
    status: 'available' | 'no_data' | 'error' | 'fallback' | 'disabled';
    error?: string | null;
    initial_margin_ratio?: number | null;
    maintenance_margin_ratio?: number | null;
    forced_close_margin_ratio?: number | null;
    borrow_availability: 'unknown';
    borrow_fee_rate?: number | null;
    note?: string;
  };
}

export interface ScreenerIndexFilters {
  min_turnover?: number;
  min_market_value?: number;
  min_turnover_rate?: number;
  min_pe_ttm?: number;
  max_pe_ttm?: number;
  max_pb?: number;
  min_capital_flow?: number;
  min_volume_ratio?: number;
  min_market_rs_10d?: number;
  min_market_rs_half_year?: number;
  min_industry_rs_10d?: number;
  min_industry_rs_half_year?: number;
  max_days_to_cover?: number;
  max_short_ratio?: number;
  max_short_ratio_change?: number;
  max_spread_bps?: number;
  min_top_of_book_notional?: number;
  min_revenue_yoy?: number;
  max_revenue_yoy?: number;
  min_net_profit_yoy?: number;
  max_net_profit_yoy?: number;
  min_operating_cash_flow_yoy?: number;
  min_analyst_alignment?: number;
  min_eps_revision_alignment?: number;
  min_days_to_financial_event?: number;
  min_days_to_corporate_action?: number;
  max_initial_margin_ratio?: number;
}

export interface ScreenerSearchResponse {
  market: ScreenerMarket;
  strategy_id: number;
  source: 'longbridge-screener';
  page: number;
  size: number;
  total: number;
  has_more: boolean;
  enrichment: {
    status: 'available' | 'fallback' | 'disabled';
    error?: string;
  };
  relative_strength: {
    benchmark_symbol: string;
    target_direction: 'LONG' | 'SHORT';
    industry_basis: 'current_page_industry_median';
  };
  short_risk: {
    status: 'available' | 'fallback' | 'disabled' | 'not_applicable';
    error?: string;
  };
  tradeability: {
    status: 'available' | 'fallback' | 'disabled';
    error?: string | null;
    require_normal_trade_status: boolean;
    depth_included: boolean;
  };
  fundamentals: {
    status: 'available' | 'fallback' | 'disabled';
    error?: string | null;
    event_window_days: number;
    corporate_actions_included: boolean;
  };
  margin_requirements: {
    status: 'available' | 'fallback' | 'disabled';
    error?: string | null;
    borrow_availability: 'unknown';
  };
  filters: {
    applied: ScreenerIndexFilters & {
      require_normal_trade_status?: boolean;
    };
    before: number;
    after: number;
    excluded: number;
    reasons: Record<string, number>;
  };
  items: ScreenerCandidate[];
}

export interface AnalysisResponse {
  long_analysis: Analysis[];
  short_analysis: Analysis[];
  stats: {
    long_count: number;
    short_count: number;
    long_avg_score: number;
    short_avg_score: number;
  };
}

export interface StockPickerBacktestHorizonMetrics {
  sample_count: number;
  avg_gross_return: number | null;
  avg_net_return: number | null;
  median_net_return: number | null;
  hit_rate: number | null;
  avg_excess_return: number | null;
  excess_coverage: number;
  avg_fixed_cost_rate?: number | null;
  avg_dynamic_cost_rate?: number | null;
  avg_total_cost_rate?: number | null;
  estimated_fixed_cost_sum?: number;
  estimated_dynamic_cost_sum?: number;
  estimated_cost_sum: number;
  max_drawdown: number | null;
}

export interface StockPickerBacktestSampleMetrics {
  sample_count: number;
  avg_score: number | null;
  horizons: Record<string, StockPickerBacktestHorizonMetrics>;
}

export interface StockPickerBacktestPeriod {
  signal_start: string | null;
  signal_end: string | null;
  signal_dates: number;
  all: StockPickerBacktestSampleMetrics;
  top_n: StockPickerBacktestSampleMetrics;
}

export interface StockPickerBacktestMetadata {
  baseline_version?: string;
  market?: string;
  universe_name?: string;
  universe_selection?: string;
  limitations?: string[];
}

export interface StockPickerBacktestDistribution {
  average: number | null;
  median: number | null;
  p95: number | null;
  maximum: number | null;
}

export interface StockPickerBacktestExecution {
  enabled: boolean;
  model_version: string | null;
  order_notional: number | null;
  order_currency_by_market: Record<string, string>;
  sample_count: number;
  executable_sample_count: number;
  executable_coverage: number;
  turnover_coverage: number | null;
  volatility_coverage: number | null;
  turnover_source_counts: Record<string, number>;
  exclusion_counts: Record<string, number>;
  participation_rate: StockPickerBacktestDistribution;
  dynamic_cost_rate: StockPickerBacktestDistribution;
}

export interface StockPickerBacktestSelectionSummary {
  sample_count: number;
  eligible_sample_count: number;
  eligible_coverage: number;
  signal_dates: number;
  eligible_signal_dates: number;
  underfilled_signal_dates: number;
}

export interface StockPickerBacktestReport {
  id?: number;
  score_version: string;
  pool_type: 'LONG' | 'SHORT';
  metadata?: StockPickerBacktestMetadata;
  parameters: {
    symbols: string[];
    horizons: number[];
    lookback: number;
    max_bars: number;
    min_history: number;
    step: number;
    top_n: number;
    train_ratio: number;
    walk_forward_folds: number;
    transaction_cost_bps: number;
    order_notional?: number | null;
    max_participation_rate?: number;
    impact_coefficient?: number;
    impact_volatility_lookback?: number;
    data_as_of?: string | null;
    market_benchmarks: Record<string, string>;
  };
  data: {
    signal_start: string;
    signal_end: string;
    data_as_of: string;
    symbols_requested: number;
    symbols_evaluated: number;
    skipped_symbols: Record<string, string>;
    sample_count: number;
    top_n_sample_count: number;
    benchmark_coverage: number;
  };
  execution?: StockPickerBacktestExecution;
  selection?: {
    all: StockPickerBacktestSelectionSummary;
    validation: StockPickerBacktestSelectionSummary;
  };
  periods: {
    train: StockPickerBacktestPeriod;
    validation: StockPickerBacktestPeriod;
    all: StockPickerBacktestPeriod;
  };
  walk_forward: Array<{
    fold: number;
    train_start: string | null;
    train_end: string | null;
    validation_start: string;
    validation_end: string;
    train_metrics: StockPickerBacktestPeriod;
    validation_metrics: StockPickerBacktestPeriod;
  }>;
  methodology: {
    no_lookahead: string;
    directional_return: string;
    top_n: string;
    transaction_cost: string;
    execution_cost?: string;
    industry_or_market_calibration: string;
    overlap_warning: string;
  };
}

export interface StockPickerBacktestHistoryItem {
  id: number;
  created_at: string;
  pool_type: 'LONG' | 'SHORT';
  score_version: string;
  parameters: StockPickerBacktestReport['parameters'];
  result: Omit<StockPickerBacktestReport, 'id' | 'parameters'>;
  data_as_of: string | null;
}

export interface StockPickerAIReturnSummary {
  sample_count: number;
  average: number | null;
  median: number | null;
  positive_rate: number | null;
  p05: number | null;
  p95: number | null;
}

export interface StockPickerAIIncrementMetrics {
  paired_batches: number;
  quant_top_k: StockPickerAIReturnSummary;
  ai_top_k: StockPickerAIReturnSummary;
  paired_delta: StockPickerAIReturnSummary;
  paired_delta_inference?: {
    ready: boolean;
    reason: string | null;
    method: string;
    cluster_unit: string;
    estimate: number | null;
    confidence_level: number;
    lower: number | null;
    upper: number | null;
    standard_error: number | null;
    interval_direction: 'positive' | 'negative' | 'inconclusive' | null;
    bootstrap_samples: number;
    requested_block_size: number | null;
    effective_block_size: number;
    distinct_observation_dates: number;
    seed: number;
  };
  selection_changed_batches: number;
  selection_change_rate: number | null;
  average_selection_overlap: number | null;
}

export interface StockPickerAIIncrementEvaluationReport {
  id?: number;
  evaluation_version: string;
  pool_type: 'LONG' | 'SHORT';
  ready: boolean;
  parameters: {
    horizons: number[];
    top_k: number;
    lookback_days: number;
    max_bars: number;
    minimum_complete_batches: number;
    minimum_labeled_records: number;
    minimum_ai_completion_rate: number;
    bootstrap_samples?: number;
    bootstrap_confidence_level?: number;
    bootstrap_block_size?: number | null;
    bootstrap_seed?: number;
    snapshot_version: string;
    score_version: string;
    prompt_version: string;
    ai_model: string;
    news_mode: 'all' | 'enabled' | 'disabled';
  };
  gate: {
    ready: boolean;
    reasons: string[];
  };
  coverage: {
    raw_analysis_records: number;
    raw_batches: number;
    structurally_complete_batches: number;
    ai_complete_batches: number;
    selected_records: number;
    ai_completed_records: number;
    ai_completion_rate: number | null;
    labeled_records_by_horizon: Record<string, number>;
    paired_batches_by_horizon: Record<string, number>;
    excluded_batches: Record<string, number>;
    latest_analysis_time: string | null;
    latest_label_date: string | null;
  };
  metrics: Record<string, StockPickerAIIncrementMetrics> | null;
  methodology: {
    comparison: string;
    no_lookahead: string;
    batch_integrity: string;
    gate: string;
    inference?: string;
    inference_limit?: string;
    causal_limit: string;
    costs: string;
  };
}

export interface StockPickerAIIncrementEvaluationHistoryItem {
  id: number;
  created_at: string;
  pool_type: 'LONG' | 'SHORT';
  evaluation_version: string;
  parameters: StockPickerAIIncrementEvaluationReport['parameters'];
  result: Omit<
    StockPickerAIIncrementEvaluationReport,
    'id' | 'parameters'
  >;
  ready: boolean;
  data_as_of: string | null;
}

export interface StockPickerConfig {
  auto_refresh_enabled: boolean;
  auto_refresh_interval: number;
  max_pool_size: number;
  cache_duration: number;
  min_score_to_recommend: number;
  analysis_lookback: number;
  ai_top_n_per_pool: number;
  history_retention_days: number;
  max_history_per_stock: number;
  factor_snapshot_enabled: boolean;
  factor_snapshot_poll_interval: number;
  updated_at?: string;
}

export interface StockPickerFactorCoverageMetric {
  available_count: number;
  total_count: number;
  coverage: number;
  missing_reasons: Record<string, number>;
  coverage_ready: boolean;
}

export interface StockPickerFactorCoverageGroup {
  market: 'US' | 'HK';
  target_direction: 'LONG' | 'SHORT';
  captured_daily_snapshot_count: number;
  session_phase_counts: Record<string, number>;
  snapshot_count: number;
  observation_dates: number;
  distinct_symbols: number;
  latest_observed_at: string | null;
  snapshot_age_hours: number | null;
  factors: Record<string, StockPickerFactorCoverageMetric>;
  ready_for_return_evaluation: boolean;
}

export interface StockPickerFactorCaptureRun {
  market: 'US' | 'HK';
  target_direction: 'LONG' | 'SHORT';
  observation_date: string;
  status: 'running' | 'completed' | 'failed';
  claim_id: string;
  started_at: string;
  completed_at: string | null;
  request_id: string | null;
  row_count: number;
  error: string | null;
  lease_expires_at: string;
  lease_expired: boolean;
}

export interface StockPickerFactorCoverage {
  snapshot_version: string;
  window_days: number;
  raw_snapshot_count: number;
  daily_snapshot_count: number;
  evaluation_snapshot_count: number;
  capture_runs: {
    total_count: number;
    status_counts: Record<string, number>;
    active: StockPickerFactorCaptureRun[];
    recent_failures: StockPickerFactorCaptureRun[];
  };
  minimums: {
    observation_dates: number;
    factor_coverage: number;
    distinct_symbols: number;
    max_snapshot_age_hours: number;
  };
  groups: StockPickerFactorCoverageGroup[];
  ready_for_return_evaluation: boolean;
}

export async function getStockPickerConfig(): Promise<StockPickerConfig> {
  const response = await fetch(`${API_BASE}/api/stock-picker/config`);
  if (!response.ok) {
    throw new Error('获取选股配置失败');
  }
  return response.json();
}

export async function updateStockPickerConfig(
  updates: Partial<Omit<StockPickerConfig, 'updated_at'>>,
): Promise<StockPickerConfig> {
  const response = await fetch(`${API_BASE}/api/stock-picker/config`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(updates),
  });
  if (!response.ok) {
    const error = await response.json().catch(() => null);
    throw new Error(error?.detail || '更新选股配置失败');
  }
  return response.json();
}

export async function getStockPickerFactorCoverage(
  days = 365,
): Promise<StockPickerFactorCoverage> {
  const query = new URLSearchParams({ days: String(days) });
  const response = await fetch(
    `${API_BASE}/api/stock-picker/factor-snapshots/coverage?${query.toString()}`,
  );
  if (!response.ok) {
    const error = await response.json().catch(() => null);
    throw new Error(error?.detail || '获取因子快照覆盖率失败');
  }
  return response.json();
}

/**
 * 搜索 Longbridge 官方证券列表
 */
export async function searchSecurities(params: {
  market: SecurityMarket;
  query: string;
  limit?: number;
  signal?: AbortSignal;
}): Promise<SecuritySearchResponse> {
  const queryParams = new URLSearchParams({
    market: params.market,
    q: params.query,
    limit: String(params.limit || 20),
  });
  const response = await fetch(
    `${API_BASE}/api/stock-picker/securities?${queryParams.toString()}`,
    { signal: params.signal },
  );

  if (!response.ok) {
    const error = await response.json().catch(() => null);
    throw new Error(error?.detail || '搜索股票失败');
  }
  return response.json();
}

export async function getScreenerStrategies(params: {
  market: ScreenerMarket;
  includeUser?: boolean;
}): Promise<ScreenerStrategiesResponse> {
  const queryParams = new URLSearchParams({
    market: params.market,
    include_user: String(params.includeUser ?? true),
  });
  const response = await fetch(
    `${API_BASE}/api/stock-picker/screener/strategies?${queryParams.toString()}`,
  );
  if (!response.ok) {
    const error = await response.json().catch(() => null);
    throw new Error(error?.detail || '获取 Longbridge 选股策略失败');
  }
  return response.json();
}

export async function searchScreenerCandidates(params: {
  market: ScreenerMarket;
  strategyId: number;
  page?: number;
  size?: number;
  includeIndexes?: boolean;
  filters?: ScreenerIndexFilters;
  targetDirection?: 'LONG' | 'SHORT';
  benchmarkSymbol?: string;
  includeShortRisk?: boolean;
  includeTradeability?: boolean;
  requireNormalTradeStatus?: boolean;
  includeFundamentals?: boolean;
  includeMarginRequirements?: boolean;
  fundamentalEventWindowDays?: number;
  includeCorporateActions?: boolean;
}): Promise<ScreenerSearchResponse> {
  const response = await fetch(`${API_BASE}/api/stock-picker/screener/search`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      market: params.market,
      strategy_id: params.strategyId,
      page: params.page ?? 0,
      size: params.size ?? 20,
      include_indexes: params.includeIndexes ?? true,
      filters: params.filters,
      target_direction: params.targetDirection ?? 'LONG',
      benchmark_symbol: params.benchmarkSymbol,
      include_short_risk: params.includeShortRisk ?? true,
      include_tradeability: params.includeTradeability ?? true,
      require_normal_trade_status: params.requireNormalTradeStatus ?? true,
      include_fundamentals: params.includeFundamentals ?? false,
      include_margin_requirements: params.includeMarginRequirements ?? false,
      fundamental_event_window_days: params.fundamentalEventWindowDays ?? 30,
      include_corporate_actions: params.includeCorporateActions ?? false,
    }),
  });
  if (!response.ok) {
    const error = await response.json().catch(() => null);
    throw new Error(error?.detail || 'Longbridge 主动选股失败');
  }
  return response.json();
}

export async function importScreenerCandidates(params: {
  poolType: 'LONG' | 'SHORT';
  strategy: ScreenerStrategy;
  items: ScreenerCandidate[];
}): Promise<{
  success: string[];
  failed: Array<{ symbol: string; error: string }>;
  total: number;
  success_count: number;
}> {
  const response = await fetch(`${API_BASE}/api/stock-picker/screener/import`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      pool_type: params.poolType,
      strategy_id: params.strategy.id,
      strategy_name: params.strategy.name,
      items: params.items.map((item) => ({
        symbol: item.symbol,
        name: item.name,
      })),
    }),
  });
  if (!response.ok) {
    const error = await response.json().catch(() => null);
    throw new Error(error?.detail || '导入候选股票失败');
  }
  return response.json();
}

export async function runStockPickerBacktest(params: {
  poolType: 'LONG' | 'SHORT';
  symbols?: string[];
  horizons?: number[];
  lookback?: number;
  maxBars?: number;
  minHistory?: number;
  step?: number;
  topN?: number;
  trainRatio?: number;
  walkForwardFolds?: number;
  transactionCostBps?: number;
  orderNotional?: number | null;
  maxParticipationRate?: number;
  impactCoefficient?: number;
  impactVolatilityLookback?: number;
}): Promise<StockPickerBacktestReport> {
  const response = await fetch(`${API_BASE}/api/stock-picker/backtest`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      pool_type: params.poolType,
      symbols: params.symbols,
      horizons: params.horizons ?? [5, 10, 20],
      lookback: params.lookback ?? 250,
      max_bars: params.maxBars ?? 1000,
      min_history: params.minHistory ?? 60,
      step: params.step ?? 5,
      top_n: params.topN ?? 5,
      train_ratio: params.trainRatio ?? 0.7,
      walk_forward_folds: params.walkForwardFolds ?? 3,
      transaction_cost_bps: params.transactionCostBps ?? 10,
      order_notional: params.orderNotional ?? null,
      max_participation_rate: params.maxParticipationRate ?? 0.1,
      impact_coefficient: params.impactCoefficient ?? 0.5,
      impact_volatility_lookback: params.impactVolatilityLookback ?? 20,
    }),
  });
  if (!response.ok) {
    const error = await response.json().catch(() => null);
    throw new Error(error?.detail || '运行智能选股回测失败');
  }
  return response.json();
}

export async function getStockPickerBacktests(
  limit = 20,
): Promise<{ items: StockPickerBacktestHistoryItem[] }> {
  const queryParams = new URLSearchParams({ limit: String(limit) });
  const response = await fetch(
    `${API_BASE}/api/stock-picker/backtests?${queryParams.toString()}`,
  );
  if (!response.ok) {
    const error = await response.json().catch(() => null);
    throw new Error(error?.detail || '获取智能选股回测历史失败');
  }
  return response.json();
}

export async function runStockPickerAIIncrementEvaluation(params: {
  poolType: 'LONG' | 'SHORT';
  horizons?: number[];
  topK?: number;
  lookbackDays?: number;
  maxBars?: number;
  minimumCompleteBatches?: number;
  minimumLabeledRecords?: number;
  minimumAICompletionRate?: number;
  bootstrapSamples?: number;
  bootstrapConfidenceLevel?: number;
  bootstrapBlockSize?: number | null;
  bootstrapSeed?: number;
  scoreVersion?: string;
  promptVersion?: string;
  aiModel?: string;
  newsMode?: 'all' | 'enabled' | 'disabled';
}): Promise<StockPickerAIIncrementEvaluationReport> {
  const response = await fetch(
    `${API_BASE}/api/stock-picker/ai-evaluation`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        pool_type: params.poolType,
        horizons: params.horizons ?? [5, 10, 20],
        top_k: params.topK ?? 3,
        lookback_days: params.lookbackDays ?? 365,
        max_bars: params.maxBars ?? 5000,
        minimum_complete_batches:
          params.minimumCompleteBatches ?? 20,
        minimum_labeled_records:
          params.minimumLabeledRecords ?? 60,
        minimum_ai_completion_rate:
          params.minimumAICompletionRate ?? 0.9,
        bootstrap_samples: params.bootstrapSamples ?? 2000,
        bootstrap_confidence_level:
          params.bootstrapConfidenceLevel ?? 0.95,
        bootstrap_block_size: params.bootstrapBlockSize ?? null,
        bootstrap_seed: params.bootstrapSeed ?? 20260724,
        score_version: params.scoreVersion,
        prompt_version: params.promptVersion,
        ai_model: params.aiModel,
        news_mode: params.newsMode ?? 'all',
      }),
    },
  );
  if (!response.ok) {
    const error = await response.json().catch(() => null);
    throw new Error(error?.detail || '运行 AI 增量评估失败');
  }
  return response.json();
}

export async function getStockPickerAIIncrementEvaluations(
  limit = 20,
): Promise<{
  items: StockPickerAIIncrementEvaluationHistoryItem[];
}> {
  const queryParams = new URLSearchParams({ limit: String(limit) });
  const response = await fetch(
    `${API_BASE}/api/stock-picker/ai-evaluations?${queryParams.toString()}`,
  );
  if (!response.ok) {
    const error = await response.json().catch(() => null);
    throw new Error(error?.detail || '获取 AI 增量评估历史失败');
  }
  return response.json();
}

/**
 * 获取股票池
 */
export async function getPools(
  poolType?: 'LONG' | 'SHORT',
  includeInactive = true,
): Promise<PoolsResponse> {
  const queryParams = new URLSearchParams({
    include_inactive: String(includeInactive),
  });
  if (poolType) queryParams.set('pool_type', poolType);
  const url = `${API_BASE}/api/stock-picker/pools?${queryParams.toString()}`;
  
  const response = await fetch(url);
  if (!response.ok) {
    throw new Error('获取股票池失败');
  }
  return response.json();
}

/**
 * 添加股票
 */
export async function addStock(data: {
  pool_type: 'LONG' | 'SHORT';
  symbol: string;
  name?: string;
  added_reason?: string;
  priority?: number;
}): Promise<{ success: boolean; id: number; message: string }> {
  const response = await fetch(`${API_BASE}/api/stock-picker/pools`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });
  
  if (!response.ok) {
    const error = await response.json();
    throw new Error(error.detail || '添加失败');
  }
  
  return response.json();
}

/**
 * 批量添加股票
 */
export async function batchAddStocks(data: {
  pool_type: 'LONG' | 'SHORT';
  symbols: string[];
}): Promise<{
  success: string[];
  failed: Array<{ symbol: string; error: string }>;
  total: number;
  success_count: number;
}> {
  const response = await fetch(`${API_BASE}/api/stock-picker/pools/batch`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });
  
  if (!response.ok) {
    const error = await response.json();
    throw new Error(error.detail || '批量添加失败');
  }
  
  return response.json();
}

/**
 * 删除股票
 */
export async function removeStock(poolId: number): Promise<void> {
  const response = await fetch(`${API_BASE}/api/stock-picker/pools/${poolId}`, {
    method: 'DELETE',
  });
  
  if (!response.ok) {
    throw new Error('删除失败');
  }
}

/**
 * 清空股票池
 */
export async function clearPool(poolType: 'LONG' | 'SHORT'): Promise<{
  success: boolean;
  message: string;
  count: number;
}> {
  const response = await fetch(`${API_BASE}/api/stock-picker/pools/clear/${poolType}`, {
    method: 'DELETE',
  });
  
  if (!response.ok) {
    const error = await response.json();
    throw new Error(error.detail || '清空失败');
  }
  
  return response.json();
}

/**
 * 切换激活状态
 */
export async function toggleStock(poolId: number): Promise<void> {
  const response = await fetch(`${API_BASE}/api/stock-picker/pools/${poolId}/toggle`, {
    method: 'PATCH',
  });
  
  if (!response.ok) {
    throw new Error('切换状态失败');
  }
}

/**
 * 触发分析
 */
export async function analyzeStocks(data?: {
  pool_type?: 'LONG' | 'SHORT';
  force_refresh?: boolean;
}): Promise<{
  success: boolean;
  job_id: string;
  status: 'queued';
  message: string;
}> {
  const response = await fetch(`${API_BASE}/api/stock-picker/analyze`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data || { force_refresh: false }),
  });
  
  if (!response.ok) {
    const error = await response.json();
    throw new Error(error.detail || '分析失败');
  }
  
  return response.json();
}

/**
 * 获取分析结果
 */
export async function getAnalysisResults(params?: {
  pool_type?: 'LONG' | 'SHORT';
  sort_by?: 'recommendation' | 'score' | 'confidence';
  limit?: number;
}): Promise<AnalysisResponse> {
  const queryParams = new URLSearchParams();
  if (params?.pool_type) queryParams.append('pool_type', params.pool_type);
  if (params?.sort_by) queryParams.append('sort_by', params.sort_by);
  if (params?.limit) queryParams.append('limit', params.limit.toString());
  
  const url = `${API_BASE}/api/stock-picker/analysis${queryParams.toString() ? '?' + queryParams.toString() : ''}`;
  
  const response = await fetch(url);
  if (!response.ok) {
    throw new Error('获取分析结果失败');
  }
  
  return response.json();
}

/**
 * 按需获取单条分析的不可变 AI/新闻输入输出快照。
 */
export async function getAnalysisSnapshot(
  analysisId: number,
): Promise<StockPickerAnalysisSnapshot> {
  const response = await fetch(
    `${API_BASE}/api/stock-picker/analysis-snapshots/${analysisId}`,
  );
  if (!response.ok) {
    const error = await response.json().catch(() => null);
    throw new Error(error?.detail || '获取分析快照失败');
  }
  return response.json();
}

/**
 * 获取统计信息
 */
export async function getStats(): Promise<{
  pools: {
    long_count: number;
    short_count: number;
  };
  analysis: {
    long_count: number;
    short_count: number;
    long_avg_score: number;
    short_avg_score: number;
  };
}> {
  const response = await fetch(`${API_BASE}/api/stock-picker/stats`);
  if (!response.ok) {
    throw new Error('获取统计信息失败');
  }
  
  return response.json();
}
