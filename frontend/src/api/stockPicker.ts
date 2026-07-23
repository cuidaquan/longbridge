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
  filters: {
    applied: ScreenerIndexFilters;
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

export interface StockPickerBacktestReport {
  id?: number;
  score_version: string;
  pool_type: 'LONG' | 'SHORT';
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
  updated_at?: string;
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
