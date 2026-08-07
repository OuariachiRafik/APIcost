/**
 * Per-request decision log — UC-12.
 *
 * BUILD_SPEC calls this the most important trust-building screen in the
 * product. That drives two choices:
 *
 * - The decision (`cache_hit | routed | passthrough | escalated | error`) is
 *   the first thing on every row, not buried in a detail view. A user checking
 *   whether we did something surprising should not have to click.
 * - Filters and the selected row live in the URL, so a row can be linked to.
 */
import { useQuery } from '@tanstack/react-query';
import { useState } from 'react';
import { useSearchParams } from 'react-router-dom';

import { Button, Card } from '../components/ui';
import { useAuth } from '../lib/authContext';
import { formatUsd, getRequests, type Decision, type RequestRow } from '../lib/usage';

const DECISION_STYLES: Record<Decision, { label: string; className: string }> = {
  cache_hit: { label: 'cache hit', className: 'bg-emerald-100 text-emerald-800' },
  routed: { label: 'routed', className: 'bg-blue-100 text-blue-800' },
  escalated: { label: 'escalated', className: 'bg-amber-100 text-amber-900' },
  passthrough: { label: 'passthrough', className: 'bg-slate-100 text-slate-700' },
  error: { label: 'error', className: 'bg-red-100 text-red-800' },
};

const FILTERS: { value: Decision | ''; label: string }[] = [
  { value: '', label: 'All' },
  { value: 'cache_hit', label: 'Cache hits' },
  { value: 'routed', label: 'Routed' },
  { value: 'escalated', label: 'Escalated' },
  { value: 'passthrough', label: 'Passthrough' },
  { value: 'error', label: 'Errors' },
];

function DecisionBadge({ decision }: { decision: Decision }) {
  const style = DECISION_STYLES[decision];
  return (
    <span className={`rounded-full px-2 py-0.5 text-xs font-medium ${style.className}`}>
      {style.label}
    </span>
  );
}

function DetailDrawer({ row, onClose }: { row: RequestRow; onClose: () => void }) {
  const fields: [string, string][] = [
    ['Request ID', row.request_id],
    ['When', new Date(row.timestamp).toLocaleString()],
    ['Decision', DECISION_STYLES[row.decision].label],
    ['Model requested', row.model_requested],
    ['Model used', row.model_used],
    ['Reason code', row.routing_reason_code ?? '—'],
    ['Provider', row.provider],
    ['Endpoint', row.endpoint],
    ['Tokens in', `${row.tokens_in.toLocaleString()}${row.tokens_estimated ? ' (estimated)' : ''}`],
    ['Tokens out', row.tokens_out.toLocaleString()],
    ['Cost', formatUsd(row.cost_usd, { precise: true })],
    [
      'Would have cost',
      row.cost_would_have_been_usd
        ? formatUsd(row.cost_would_have_been_usd, { precise: true })
        : '—',
    ],
    ['Saved', formatUsd(row.saved_usd, { precise: true })],
    ['Latency', `${row.latency_ms.toFixed(0)} ms`],
    ['Time to first token', row.ttft_ms ? `${row.ttft_ms.toFixed(0)} ms` : '—'],
    ['Cache similarity', row.cache_similarity ? row.cache_similarity.toFixed(4) : '—'],
    ['Streamed', row.streamed ? 'yes' : 'no'],
    ['Status', String(row.status)],
    ['Error', row.error_code ?? '—'],
  ];

  return (
    <aside
      role="dialog"
      aria-label="Request detail"
      className="fixed right-0 top-0 z-20 h-full w-full max-w-md overflow-y-auto border-l
                 border-slate-200 bg-white p-6 shadow-xl"
    >
      <div className="mb-4 flex items-start justify-between">
        <h2 className="text-sm font-semibold text-slate-900">Request detail</h2>
        <Button variant="secondary" onClick={onClose}>
          Close
        </Button>
      </div>

      <dl className="space-y-2 text-sm">
        {fields.map(([label, value]) => (
          <div key={label} className="flex justify-between gap-4 border-b border-slate-100 py-1">
            <dt className="text-slate-500">{label}</dt>
            <dd className="text-right font-mono text-xs text-slate-900">{value}</dd>
          </div>
        ))}
      </dl>

      {row.tokens_estimated && (
        <p className="mt-4 rounded-md bg-amber-50 px-3 py-2 text-xs text-amber-900">
          The provider did not report usage for this request, so token counts are estimated and the
          cost is approximate.
        </p>
      )}
    </aside>
  );
}

export function Requests() {
  const { accessToken } = useAuth();
  const [searchParams, setSearchParams] = useSearchParams();
  const [cursors, setCursors] = useState<(string | null)[]>([null]);
  const [pageIndex, setPageIndex] = useState(0);

  const token = accessToken ?? '';
  const decision = (searchParams.get('decision') ?? '') as Decision | '';
  const selectedId = searchParams.get('request');

  const page = useQuery({
    queryKey: ['requests', decision, cursors[pageIndex]],
    queryFn: () =>
      getRequests(token, {
        cursor: cursors[pageIndex] ?? undefined,
        decision: decision || undefined,
      }),
    enabled: Boolean(token),
  });

  const rows = page.data?.rows ?? [];
  const selected = rows.find((row) => row.request_id === selectedId) ?? null;

  const setDecision = (value: Decision | '') => {
    const next = new URLSearchParams(searchParams);
    if (value) next.set('decision', value);
    else next.delete('decision');
    next.delete('request');
    setSearchParams(next);
    setCursors([null]);
    setPageIndex(0);
  };

  const select = (requestId: string | null) => {
    const next = new URLSearchParams(searchParams);
    if (requestId) next.set('request', requestId);
    else next.delete('request');
    setSearchParams(next);
  };

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap gap-1 rounded-lg border border-slate-200 bg-white p-1">
        {FILTERS.map((filter) => (
          <button
            key={filter.label}
            type="button"
            onClick={() => setDecision(filter.value)}
            className={`rounded px-3 py-1 text-sm ${
              decision === filter.value
                ? 'bg-slate-900 text-white'
                : 'text-slate-600 hover:bg-slate-100'
            }`}
          >
            {filter.label}
          </button>
        ))}
      </div>

      <Card>
        {page.isPending && <p className="py-8 text-center text-sm text-slate-500">Loading…</p>}

        {page.isError && (
          <p role="alert" className="py-8 text-center text-sm text-red-700">
            Could not load the request log.
          </p>
        )}

        {!page.isPending && rows.length === 0 && (
          <p className="py-8 text-center text-sm text-slate-500">
            No requests yet. Point your app at the proxy and they will appear here.
          </p>
        )}

        {rows.length > 0 && (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-slate-200 text-left text-xs uppercase text-slate-500">
                  <th className="pb-2 font-medium">When</th>
                  <th className="pb-2 font-medium">Decision</th>
                  <th className="pb-2 font-medium">Model</th>
                  <th className="pb-2 text-right font-medium">Tokens</th>
                  <th className="pb-2 text-right font-medium">Cost</th>
                  <th className="pb-2 text-right font-medium">Saved</th>
                  <th className="pb-2 text-right font-medium">Latency</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <tr
                    key={row.id}
                    onClick={() => select(row.request_id)}
                    className="cursor-pointer border-b border-slate-100 hover:bg-slate-50"
                  >
                    <td className="py-2 whitespace-nowrap text-xs text-slate-600">
                      {new Date(row.timestamp).toLocaleTimeString()}
                    </td>
                    <td className="py-2">
                      <DecisionBadge decision={row.decision} />
                    </td>
                    <td className="py-2 font-mono text-xs">
                      {row.model_requested === row.model_used ? (
                        row.model_used
                      ) : (
                        <span>
                          <span className="text-slate-400 line-through">{row.model_requested}</span>{' '}
                          <span className="text-slate-900">{row.model_used}</span>
                        </span>
                      )}
                    </td>
                    <td className="py-2 text-right tabular-nums text-xs">
                      {(row.tokens_in + row.tokens_out).toLocaleString()}
                      {row.tokens_estimated && <span className="text-amber-600">*</span>}
                    </td>
                    <td className="py-2 text-right tabular-nums text-xs">
                      {formatUsd(row.cost_usd, { precise: true })}
                    </td>
                    <td className="py-2 text-right tabular-nums text-xs text-emerald-700">
                      {Number(row.saved_usd) > 0
                        ? formatUsd(row.saved_usd, { precise: true })
                        : '—'}
                    </td>
                    <td className="py-2 text-right tabular-nums text-xs">
                      {row.latency_ms.toFixed(0)} ms
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        <div className="mt-4 flex items-center justify-between">
          <p className="text-xs text-slate-500">
            <span className="text-amber-600">*</span> token counts estimated — the provider did not
            report usage
          </p>
          <div className="flex gap-2">
            <Button
              variant="secondary"
              disabled={pageIndex === 0}
              onClick={() => setPageIndex((index) => Math.max(0, index - 1))}
            >
              Previous
            </Button>
            <Button
              variant="secondary"
              disabled={!page.data?.has_more}
              onClick={() => {
                const next = page.data?.next_cursor ?? null;
                setCursors((existing) => [...existing.slice(0, pageIndex + 1), next]);
                setPageIndex((index) => index + 1);
              }}
            >
              Next
            </Button>
          </div>
        </div>
      </Card>

      {selected && <DetailDrawer row={selected} onClose={() => select(null)} />}
    </div>
  );
}
