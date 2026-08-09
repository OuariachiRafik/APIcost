/** Usage and request-log API types and calls — UC-08 through UC-13. */
import { apiFetch } from './api';

export type TimeRange = 'today' | '7d' | '30d' | '90d';
export type BreakdownDimension = 'model' | 'project' | 'endpoint' | 'provider';
export type Decision = 'cache_hit' | 'routed' | 'passthrough' | 'escalated' | 'error';

import type { components } from './schema';

type Schemas = components['schemas'];

// Aliases into the generated schema (`make api-types`). See lib/api.ts for why.
export type UsagePoint = Schemas['UsagePoint'];
export type UsageSummary = Schemas['UsageSummary'];
export type UsageResponse = Schemas['UsageResponse'];
export type BreakdownRow = Schemas['BreakdownRow'];
export type BreakdownResponse = Schemas['BreakdownResponse'];
export type HistogramBucket = Schemas['HistogramBucket'];
export type TokenDistributionResponse = Schemas['TokenDistributionResponse'];
export type RequestRow = Schemas['RequestRow'];
export type RequestPage = Schemas['RequestPage'];

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
