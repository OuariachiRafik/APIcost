/**
 * Spend overview — UC-08, UC-09, UC-10, UC-11.
 *
 * The headline is savings, not spend: a user installs this to find out whether
 * it is working. Caching and routing are shown separately because they are
 * separate mechanisms and conflating them is how savings numbers become a lie
 * (CODEBASE_GUIDE §6).
 */
import { useQuery } from '@tanstack/react-query';
import { useState } from 'react';
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';

import { Card } from '../components/ui';
import { useAuth } from '../lib/authContext';
import {
  exportCsvUrl,
  formatCount,
  formatTokens,
  formatUsd,
  getBreakdown,
  getTokenDistribution,
  getUsage,
  type BreakdownDimension,
  type TimeRange,
} from '../lib/usage';

const RANGES: { value: TimeRange; label: string }[] = [
  { value: 'today', label: 'Today' },
  { value: '7d', label: '7 days' },
  { value: '30d', label: '30 days' },
  { value: '90d', label: '90 days' },
];

// Categorical palette, chosen to stay distinguishable in grayscale and for the
// most common colour-vision deficiencies.
const SERIES_COLORS = ['#0f172a', '#2563eb', '#0891b2', '#7c3aed', '#c2410c', '#65a30d'];

function Stat({
  label,
  value,
  hint,
  emphasis = false,
}: {
  label: string;
  value: string;
  hint?: string;
  emphasis?: boolean;
}) {
  return (
    <div className="rounded-lg border border-slate-200 bg-white p-4">
      <p className="text-xs font-medium uppercase tracking-wide text-slate-500">{label}</p>
      <p
        className={`mt-1 text-2xl font-semibold ${emphasis ? 'text-emerald-700' : 'text-slate-900'}`}
      >
        {value}
      </p>
      {hint && <p className="mt-1 text-xs text-slate-500">{hint}</p>}
    </div>
  );
}

export function Dashboard() {
  const { accessToken } = useAuth();
  const [range, setRange] = useState<TimeRange>('30d');
  const [dimension, setDimension] = useState<BreakdownDimension>('model');

  const token = accessToken ?? '';

  const usage = useQuery({
    queryKey: ['usage', range],
    queryFn: () => getUsage(token, range),
    enabled: Boolean(token),
  });

  const breakdown = useQuery({
    queryKey: ['breakdown', dimension, range],
    queryFn: () => getBreakdown(token, dimension, range),
    enabled: Boolean(token),
  });

  const distribution = useQuery({
    queryKey: ['token-distribution', range],
    queryFn: () => getTokenDistribution(token, range),
    enabled: Boolean(token),
  });

  const summary = usage.data?.summary;
  const totalSaved =
    Number(summary?.cache_savings_usd ?? 0) + Number(summary?.routing_savings_usd ?? 0);

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex gap-1 rounded-lg border border-slate-200 bg-white p-1">
          {RANGES.map((option) => (
            <button
              key={option.value}
              type="button"
              onClick={() => setRange(option.value)}
              className={`rounded px-3 py-1 text-sm ${
                range === option.value
                  ? 'bg-slate-900 text-white'
                  : 'text-slate-600 hover:bg-slate-100'
              }`}
            >
              {option.label}
            </button>
          ))}
        </div>

        <a
          href={exportCsvUrl(range)}
          className="rounded-md border border-slate-300 bg-white px-4 py-2 text-sm
                     font-medium text-slate-700 hover:bg-slate-50"
        >
          Export CSV
        </a>
      </div>

      {usage.isError && (
        <p role="alert" className="rounded-md bg-red-50 px-3 py-2 text-sm text-red-700">
          Could not load usage. Is the API running?
        </p>
      )}

      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <Stat label="Spend" value={formatUsd(summary?.total_cost_usd ?? 0)} />
        <Stat
          label="Saved"
          value={formatUsd(totalSaved)}
          hint={`${formatUsd(summary?.cache_savings_usd ?? 0)} cache · ${formatUsd(
            summary?.routing_savings_usd ?? 0,
          )} routing`}
          emphasis
        />
        <Stat
          label="Requests"
          value={formatCount(summary?.total_requests ?? 0)}
          hint={`${formatCount(summary?.cache_hits ?? 0)} served from cache`}
        />
        <Stat
          label="Cache hit rate"
          value={`${((summary?.cache_hit_rate ?? 0) * 100).toFixed(1)}%`}
          hint={`${formatTokens(summary?.total_tokens_in ?? 0)} in · ${formatTokens(
            summary?.total_tokens_out ?? 0,
          )} out`}
        />
      </div>

      <Card>
        <h2 className="mb-4 text-sm font-semibold text-slate-900">Spend over time</h2>
        <div className="h-64">
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={usage.data?.series ?? []}>
              <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" vertical={false} />
              <XAxis
                dataKey="bucket"
                tickFormatter={(value: string) => value.slice(5, 10)}
                stroke="#94a3b8"
                fontSize={12}
              />
              <YAxis
                tickFormatter={(value: number) => `$${value.toFixed(2)}`}
                stroke="#94a3b8"
                fontSize={12}
              />
              <Tooltip
                formatter={(value: number | string) => formatUsd(value)}
                labelFormatter={(label: string) => new Date(label).toLocaleDateString()}
              />
              <Line
                type="monotone"
                dataKey="cost_would_have_been_usd"
                name="Without APICost"
                stroke="#cbd5e1"
                strokeWidth={2}
                dot={false}
              />
              <Line
                type="monotone"
                dataKey="cost_usd"
                name="Actual spend"
                stroke="#0f172a"
                strokeWidth={2}
                dot={false}
              />
            </LineChart>
          </ResponsiveContainer>
        </div>
        <p className="mt-2 text-xs text-slate-500">
          The grey line is what these requests would have cost at the model you asked for. The gap
          is the saving.
        </p>
      </Card>

      <div className="grid gap-6 lg:grid-cols-2">
        <Card>
          <div className="mb-4 flex items-center justify-between">
            <h2 className="text-sm font-semibold text-slate-900">Breakdown</h2>
            <select
              value={dimension}
              onChange={(event) => setDimension(event.target.value as BreakdownDimension)}
              className="rounded border border-slate-300 px-2 py-1 text-sm"
              aria-label="Break down by"
            >
              <option value="model">By model</option>
              <option value="project">By project</option>
              <option value="endpoint">By endpoint</option>
              <option value="provider">By provider</option>
            </select>
          </div>

          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-slate-200 text-left text-xs uppercase text-slate-500">
                <th className="pb-2 font-medium">{dimension}</th>
                <th className="pb-2 text-right font-medium">Requests</th>
                <th className="pb-2 text-right font-medium">Avg tokens</th>
                <th className="pb-2 text-right font-medium">Spend</th>
              </tr>
            </thead>
            <tbody>
              {(breakdown.data?.rows ?? []).slice(0, 8).map((row, index) => (
                <tr key={row.key} className="border-b border-slate-100">
                  <td className="py-2">
                    <span className="inline-flex items-center gap-2">
                      <span
                        className="inline-block h-2 w-2 rounded-full"
                        style={{ background: SERIES_COLORS[index % SERIES_COLORS.length] }}
                      />
                      <span className="font-mono text-xs">{row.key}</span>
                    </span>
                  </td>
                  <td className="py-2 text-right tabular-nums">{formatCount(row.requests)}</td>
                  <td className="py-2 text-right tabular-nums">
                    {formatTokens(Math.round(row.avg_tokens))}
                  </td>
                  <td className="py-2 text-right tabular-nums">{formatUsd(row.cost_usd)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {breakdown.data?.rows.length === 0 && (
            <p className="py-6 text-center text-sm text-slate-500">No traffic in this range.</p>
          )}
        </Card>

        <Card>
          <h2 className="mb-4 text-sm font-semibold text-slate-900">Request sizes</h2>
          <div className="h-56">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={distribution.data?.buckets ?? []}>
                <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" vertical={false} />
                <XAxis
                  dataKey="label"
                  stroke="#94a3b8"
                  fontSize={10}
                  interval={0}
                  angle={-35}
                  textAnchor="end"
                  height={60}
                />
                <YAxis stroke="#94a3b8" fontSize={12} />
                <Tooltip formatter={(value: number) => formatCount(value)} />
                <Bar dataKey="requests" name="Requests">
                  {(distribution.data?.buckets ?? []).map((bucket, index) => (
                    <Cell key={bucket.label} fill={SERIES_COLORS[index % SERIES_COLORS.length]} />
                  ))}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          </div>
          <p className="mt-2 text-xs text-slate-500">
            Median request falls in the {formatTokens(distribution.data?.median_tokens_bucket ?? 0)}
            + token bucket. Long requests are where prompt optimization pays.
          </p>
        </Card>
      </div>
    </div>
  );
}
