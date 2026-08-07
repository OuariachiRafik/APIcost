/** Usage and request-log API types and calls — UC-08 through UC-13. */
import { apiFetch } from './api';

export type TimeRange = 'today' | '7d' | '30d' | '90d';
export type BreakdownDimension = 'model' | 'project' | 'endpoint' | 'provider';
export type Decision = 'cache_hit' | 'routed' | 'passthrough' | 'escalated' | 'error';

export interface UsagePoint {
  bucket: string;
  cost_usd: string;
  cost_would_have_been_usd: string;
  requests: number;
  tokens_in: number;
  tokens_out: number;
  cache_hits: number;
}

export interface UsageSummary {
  total_cost_usd: string;
  total_would_have_been_usd: string;
  cache_savings_usd: string;
  routing_savings_usd: string;
  total_requests: number;
  cache_hits: number;
  cache_hit_rate: number;
  total_tokens_in: number;
  total_tokens_out: number;
}

export interface UsageResponse {
  range: string;
  start: string;
  end: string;
  bucket: string;
  summary: UsageSummary;
  series: UsagePoint[];
}

export interface BreakdownRow {
  key: string;
  cost_usd: string;
  requests: number;
  tokens_in: number;
  tokens_out: number;
  avg_tokens: number;
  share: number;
}

export interface BreakdownResponse {
  by: string;
  rows: BreakdownRow[];
}

export interface HistogramBucket {
  label: string;
  lower: number;
  upper: number | null;
  requests: number;
  cost_usd: string;
}

export interface TokenDistributionResponse {
  buckets: HistogramBucket[];
  /** Bucket floor, not an exact percentile — see ADR 0006. */
  median_tokens_bucket: number;
  p95_tokens_bucket: number;
}

export interface RequestRow {
  id: string;
  request_id: string;
  timestamp: string;
  project_id: string;
  endpoint: string;
  provider: string;
  decision: Decision;
  model_requested: string;
  model_used: string;
  tokens_in: number;
  tokens_out: number;
  tokens_estimated: boolean;
  cost_usd: string;
  cost_would_have_been_usd: string | null;
  saved_usd: string;
  latency_ms: number;
  ttft_ms: number | null;
  cache_hit: boolean;
  cache_similarity: number | null;
  routed: boolean;
  routing_reason_code: string | null;
  escalation_triggered: boolean;
  status: number;
  error_code: string | null;
  streamed: boolean;
}

export interface RequestPage {
  rows: RequestRow[];
  next_cursor: string | null;
  has_more: boolean;
}

function query(params: Record<string, string | number | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== '') search.set(key, String(value));
  }
  const rendered = search.toString();
  return rendered ? `?${rendered}` : '';
}

export const getUsage = (token: string, range: TimeRange, projectId?: string) =>
  apiFetch<UsageResponse>(`/usage${query({ range, project_id: projectId })}`, { token });

export const getBreakdown = (
  token: string,
  by: BreakdownDimension,
  range: TimeRange,
  projectId?: string,
) =>
  apiFetch<BreakdownResponse>(`/usage/breakdown${query({ by, range, project_id: projectId })}`, {
    token,
  });

export const getTokenDistribution = (token: string, range: TimeRange, projectId?: string) =>
  apiFetch<TokenDistributionResponse>(
    `/usage/token-distribution${query({ range, project_id: projectId })}`,
    { token },
  );

export const getRequests = (
  token: string,
  options: { cursor?: string; limit?: number; model?: string; decision?: Decision } = {},
) =>
  apiFetch<RequestPage>(
    `/requests${query({
      cursor: options.cursor,
      limit: options.limit ?? 50,
      model: options.model,
      decision: options.decision,
    })}`,
    { token },
  );

export const getRequestDetail = (token: string, requestId: string) =>
  apiFetch<RequestRow>(`/requests/${requestId}`, { token });

/** The CSV export is a browser download, so it needs a URL rather than a fetch. */
export function exportCsvUrl(range: TimeRange, projectId?: string): string {
  const base = import.meta.env.VITE_API_BASE_URL ?? 'http://localhost:8001';
  return `${base}/usage/export.csv${query({ range, project_id: projectId })}`;
}

// ---------------------------------------------------------------------------
// Formatting
// ---------------------------------------------------------------------------

export function formatUsd(value: string | number, { precise = false } = {}): string {
  const amount = typeof value === 'string' ? Number(value) : value;
  if (!Number.isFinite(amount)) return '$0.00';
  // Sub-cent amounts are normal here — a gpt-4o-mini call costs fractions of a
  // cent, and rounding to $0.00 would report most requests as free.
  if (precise && amount > 0 && amount < 0.01) return `$${amount.toFixed(6)}`;
  return `$${amount.toLocaleString(undefined, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}

export function formatCount(value: number): string {
  return value.toLocaleString();
}

export function formatTokens(value: number): string {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
  if (value >= 1_000) return `${(value / 1_000).toFixed(1)}k`;
  return String(value);
}
