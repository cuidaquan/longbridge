import { API_BASE } from "./client";

export type QuantRunStatus =
  | "queued"
  | "loading_universe"
  | "scoring_quant"
  | "analyzing_ai"
  | "completed"
  | "partial"
  | "failed"
  | "cancelled";

export interface QuantSelectionRun {
  run_id: string;
  runtime_id: string;
  status: QuantRunStatus;
  force_refresh: boolean;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
  data_as_of: string | null;
  universe_version: string;
  filter_version: string;
  score_version: string;
  prompt_version: string;
  model_policy_version: string;
  model_alias: string;
  resolved_model_id: string | null;
  candidate_count: number;
  ai_planned_count: number;
  ai_completed_count: number;
  final_count: number;
  error_summary: string[];
  quant_input_hash: string | null;
  run_input_hash: string | null;
  cache_key: string | null;
  reused_from_run_id: string | null;
}

export interface QuantAIResult {
  decision: "SELECT" | "REJECT" | "INSUFFICIENT_DATA";
  confidence: number;
  suitability_score: number;
  risk_level: "LOW" | "MEDIUM" | "HIGH";
  time_horizon_days: number;
  reasons: string[];
  risks: string[];
  entry_condition: string;
  invalidation_condition: string;
  data_conflicts: string[];
}

export interface QuantFinalResult {
  symbol: string;
  name: string;
  quant_score: number;
  ai_score: number;
  final_score: number;
  ai_decision: QuantAIResult;
  effective_decision: string;
  rejection_reason: string | null;
  median_turnover_20d: number;
  eligible: boolean;
  ai_input_hash: string;
}

export interface QuantCandidate {
  symbol: string;
  name: string;
  catalog_evidence: {
    board: string;
    exchange: string;
    market: string;
    source: string;
    source_version: string;
    captured_at: string;
  };
  indicators: Record<string, number | string> | null;
  hard_filters: Record<string, { status: string; reason?: string | null }>;
  exclusion_reasons: string[];
  selection_status: string;
  selected_for_ai: boolean;
  candidate_quant_input_hash: string;
  price_data_as_of: string | null;
  bar_data_as_of: string | null;
  quant_score: { total: number; [key: string]: number | string } | null;
  ai?: {
    request_status: string;
    attempts: number;
    ai_input_hash: string;
    decision: QuantAIResult | null;
    error: string | null;
  };
  result?: QuantFinalResult;
}

export interface QuantSelectionResults {
  run: QuantSelectionRun;
  results: QuantFinalResult[];
  candidates: QuantCandidate[];
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, init);
  if (!response.ok) {
    let message = `请求失败 (${response.status})`;
    try {
      const body = await response.json();
      if (typeof body.detail === "string") message = body.detail;
    } catch {
      // Keep the status-based fallback for non-JSON responses.
    }
    throw new Error(message);
  }
  return response.json() as Promise<T>;
}

export function createQuantSelectionRun(forceRefresh = false) {
  return request<QuantSelectionRun>("/api/quant-stock-selector/runs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ force_refresh: forceRefresh }),
  });
}

export function getQuantSelectionRun(runId: string) {
  return request<QuantSelectionRun>(
    `/api/quant-stock-selector/runs/${encodeURIComponent(runId)}`,
  );
}

export function getQuantSelectionResults(runId: string) {
  return request<QuantSelectionResults>(
    `/api/quant-stock-selector/runs/${encodeURIComponent(runId)}/results`,
  );
}

export function listQuantSelectionRuns(limit = 20) {
  return request<{ items: QuantSelectionRun[] }>(
    `/api/quant-stock-selector/runs?limit=${limit}`,
  );
}

export async function getLatestQuantSelection() {
  const response = await fetch(`${API_BASE}/api/quant-stock-selector/latest`);
  if (response.status === 404) return null;
  if (!response.ok) {
    let message = `请求失败 (${response.status})`;
    try {
      const body = await response.json();
      if (typeof body.detail === "string") message = body.detail;
    } catch {
      // Keep the status-based fallback for non-JSON responses.
    }
    throw new Error(message);
  }
  return response.json() as Promise<QuantSelectionResults>;
}

export function quantSelectionEventsUrl(runId: string) {
  return `${API_BASE}/api/quant-stock-selector/runs/${encodeURIComponent(runId)}/events`;
}
