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
