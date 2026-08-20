/**
 * Overview — APICOST_FRONTEND_SPEC §5.2. The post-login landing screen.
 *
 * This is the screen that makes the product legible before any optimization
 * feature is switched on: what you spent, what we saved you, and one row per
 * request showing what actually happened to it. Density is highest here by
 * design (§2.5).
 *
 * Savings are reported the way the API reports them — cache and routing kept
 * separate, routing net of escalation cost. A combined figure that quietly
 * double-counted would undermine the one number this product exists to prove.
 */
import { useQuery } from '@tanstack/react-query';
import { useState } from 'react';

import { RequireProject, ScreenHeader } from '../components/Screen';
import { SpendChart } from '../components/SpendChart';
import {
  Badge,
  Button,
  Card,
  Cell,
  EmptyState,
  ErrorBanner,
  Row,
  Spinner,
  StatCard,
  Table,
  TextLink,
  type Tone,
} from '../components/ui';
import { apiUrl } from '../lib/api';
import { describeError, useAuth } from '../lib/authContext';
import { compact, dateTime, money, moneyPrecise, ms, num, percent } from '../lib/format';
import {
  exportCsvUrl,
  getBreakdown,
  getRequests,
  getUsage,
  keys,
  type RequestRow,
} from '../lib/queries';
import { useWorkspace, type TimeRange } from '../lib/uiContext';

export function Overview() {
  return (
    <>
      <ScreenHeader title="Overview" description="Where the money went, and what we saved you." />
      <RequireProject>{(projectId) => <OverviewBody projectId={projectId} />}</RequireProject>
    </>
  );
}

function OverviewBody({ projectId }: { projectId: string }) {
  const { accessToken } = useAuth();
  const { range } = useWorkspace();

  const usage = useQuery({
    queryKey: keys.usage(projectId, range),
    queryFn: () => getUsage(accessToken ?? '', projectId, range),
    enabled: Boolean(accessToken),
  });

  if (usage.isLoading) return <Spinner />;
  if (usage.isError)
    return (
      <ErrorBanner message={describeError(usage.error)} onRetry={() => void usage.refetch()} />
    );

  const data = usage.data;
  if (!data) return null;

  const { summary } = data;
  const totalSaved = num(summary.cache_savings_usd) + num(summary.routing_savings_usd);
  const wouldHaveBeen = num(summary.total_would_have_been_usd);
  const savedFraction = wouldHaveBeen > 0 ? totalSaved / wouldHaveBeen : 0;

  if (summary.total_requests === 0) {
    return (
      <Card>
        <EmptyState
          title="No requests yet"
          action={<TextLink href="/settings">Get your proxy key</TextLink>}
        >
          Point your app at your proxy key and this dashboard fills in automatically.
        </EmptyState>
      </Card>
    );
  }

  const series = data.series.map((point) => ({
    label: point.bucket.slice(5, 16).replace('T', ' '),
    value: num(point.cost_usd),
    secondary: Math.max(0, num(point.cost_would_have_been_usd) - num(point.cost_usd)),
  }));

  return (
    <div className="flex flex-col gap-4">
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <StatCard label="Total spend" value={money(summary.total_cost_usd)} />
        <StatCard
          label="Savings"
          value={money(totalSaved)}
          tone={totalSaved > 0 ? 'positive' : undefined}
          hint={`${percent(savedFraction)} of what these calls would have cost`}
        />
        <StatCard label="Requests" value={compact(summary.total_requests)} />
        <StatCard
          label="Cache hit rate"
          value={percent(summary.cache_hit_rate)}
          hint={`${compact(summary.cache_hits)} hits`}
        />
      </div>

      <Card title="Spend over time">
        <SpendChart data={series} valueLabel="Spend" secondaryLabel="Saved" />
        <div className="mt-3 flex gap-4 border-t border-edge pt-3 text-xs text-muted">
          <span>
            Saved by caching:{' '}
            <span className="tnum font-mono text-positive">{money(summary.cache_savings_usd)}</span>
          </span>
          <span>
            Saved by routing:{' '}
            <span
              className={`tnum font-mono ${
                num(summary.routing_savings_usd) < 0 ? 'text-warning' : 'text-positive'
              }`}
            >
              {money(summary.routing_savings_usd)}
            </span>
          </span>
        </div>
      </Card>

      <div className="grid gap-4 lg:grid-cols-2">
        <Breakdown projectId={projectId} range={range} by="model" title="By model" />
        <Breakdown projectId={projectId} range={range} by="endpoint" title="By endpoint" />
      </div>

      <RequestLog projectId={projectId} range={range} />
    </div>
  );
}

function Breakdown({
  projectId,
  range,
  by,
  title,
}: {
  projectId: string;
  range: TimeRange;
  by: string;
  title: string;
}) {
  const { accessToken } = useAuth();
  const breakdown = useQuery({
    queryKey: keys.breakdown(projectId, range, by),
    queryFn: () => getBreakdown(accessToken ?? '', projectId, range, by),
    enabled: Boolean(accessToken),
  });

  return (
    <Card title={title}>
      {breakdown.isLoading ? (
        <Spinner />
      ) : breakdown.data && breakdown.data.rows.length > 0 ? (
        <Table head={[by === 'model' ? 'Model' : 'Endpoint', 'Requests', 'Avg tokens', 'Cost']}>
          {breakdown.data.rows.map((row) => (
            <Row key={row.key}>
              <Cell>
                <span className="font-mono text-xs">{row.key}</span>
              </Cell>
              <Cell numeric>{compact(row.requests)}</Cell>
              <Cell numeric>{compact(Math.round(row.avg_tokens))}</Cell>
              <Cell numeric>{money(row.cost_usd)}</Cell>
            </Row>
          ))}
        </Table>
      ) : (
        <EmptyState title="Nothing in this range" />
      )}
    </Card>
  );
}

const DECISIONS = ['', 'cache_hit', 'routed', 'passthrough', 'escalated', 'error'] as const;

const DECISION_TONE: Record<string, Tone> = {
  cache_hit: 'positive',
  routed: 'info',
  escalated: 'warning',
  error: 'critical',
  passthrough: 'neutral',
};

const DECISION_LABEL: Record<string, string> = {
  cache_hit: 'Cached',
  routed: 'Routed',
  passthrough: 'Passthrough',
  escalated: 'Escalated',
  error: 'Error',
};

function RequestLog({ projectId, range }: { projectId: string; range: TimeRange }) {
  const { accessToken } = useAuth();
  const [decision, setDecision] = useState('');
  const [selected, setSelected] = useState<RequestRow | null>(null);

  const requests = useQuery({
    queryKey: keys.requests(projectId, range, decision),
    queryFn: () => getRequests(accessToken ?? '', projectId, range, decision || undefined),
    enabled: Boolean(accessToken),
  });

  return (
    <Card
      title="Requests"
      action={
        <div className="flex items-center gap-2">
          <select
            value={decision}
            onChange={(event) => setDecision(event.target.value)}
            aria-label="Filter by decision"
            className="rounded-md border border-edge bg-page px-2 py-1 text-xs text-ink
                       focus:border-info focus:outline-none"
          >
            {DECISIONS.map((option) => (
              <option key={option} value={option}>
                {option === '' ? 'All decisions' : (DECISION_LABEL[option] ?? option)}
              </option>
            ))}
          </select>
          <a
            href={apiUrl(exportCsvUrl(projectId, range))}
            className="inline-flex items-center rounded-md border border-edge px-3 py-1.5
                       text-sm text-ink hover:bg-page"
          >
            Export CSV
          </a>
        </div>
      }
    >
      {requests.isLoading ? (
        <Spinner />
      ) : requests.data && requests.data.rows.length > 0 ? (
        <Table
          head={['Time', 'Requested', 'Used', 'Decision', 'Tokens', 'Latency', 'Saved', 'Cost']}
        >
          {requests.data.rows.map((row) => (
            <Row key={row.id} onClick={() => setSelected(row)}>
              <Cell>
                <span className="text-xs text-muted">{dateTime(row.timestamp)}</span>
              </Cell>
              <Cell>
                <span className="font-mono text-xs">{row.model_requested}</span>
              </Cell>
              <Cell>
                <span className="font-mono text-xs">
                  {row.model_used !== row.model_requested ? (
                    <span className="text-info">{row.model_used}</span>
                  ) : (
                    <span className="text-muted">—</span>
                  )}
                </span>
              </Cell>
              <Cell>
                <Badge tone={DECISION_TONE[row.decision] ?? 'neutral'}>
                  {DECISION_LABEL[row.decision] ?? row.decision}
                </Badge>
              </Cell>
              <Cell numeric>{compact(row.tokens_in + row.tokens_out)}</Cell>
              <Cell numeric>{ms(row.latency_ms)}</Cell>
              <Cell numeric tone={num(row.saved_usd) > 0 ? 'positive' : undefined}>
                {num(row.saved_usd) > 0 ? moneyPrecise(row.saved_usd) : '—'}
              </Cell>
              <Cell numeric>{moneyPrecise(row.cost_usd)}</Cell>
            </Row>
          ))}
        </Table>
      ) : (
        <EmptyState title="No requests match this filter" />
      )}

      {selected && <RequestDetail row={selected} onClose={() => setSelected(null)} />}
    </Card>
  );
}

function RequestDetail({ row, onClose }: { row: RequestRow; onClose: () => void }) {
  const facts: [string, string][] = [
    ['Request ID', row.request_id],
    ['Endpoint', row.endpoint],
    ['Provider', row.provider],
    ['Model requested', row.model_requested],
    ['Model used', row.model_used],
    ['Decision', DECISION_LABEL[row.decision] ?? row.decision],
    ['Reason', row.routing_reason_code ?? '—'],
    ['Cache similarity', row.cache_similarity === null ? '—' : row.cache_similarity.toFixed(4)],
    ['Tokens in / out', `${compact(row.tokens_in)} / ${compact(row.tokens_out)}`],
    ['Estimated tokens', row.tokens_estimated ? 'yes' : 'no'],
    ['Latency', ms(row.latency_ms)],
    ['Time to first token', ms(row.ttft_ms)],
    ['Streamed', row.streamed ? 'yes' : 'no'],
    ['Status', String(row.status)],
    ['Error', row.error_code ?? '—'],
    ['Cost', moneyPrecise(row.cost_usd)],
    ['Would have cost', moneyPrecise(row.cost_would_have_been_usd)],
    ['Saved', moneyPrecise(row.saved_usd)],
  ];

  return (
    <div className="fixed inset-0 z-50 flex justify-end bg-black/60" onClick={onClose}>
      <aside
        className="h-full w-full max-w-md overflow-y-auto border-l border-edge bg-surface"
        onClick={(event) => event.stopPropagation()}
      >
        <header className="flex items-center justify-between border-b border-edge px-4 py-3">
          <h2 className="text-sm font-semibold text-ink">Request detail</h2>
          <Button variant="ghost" onClick={onClose}>
            Close
          </Button>
        </header>
        <dl className="divide-y divide-edge/60">
          {facts.map(([label, value]) => (
            <div key={label} className="flex justify-between gap-4 px-4 py-2">
              <dt className="text-xs text-muted">{label}</dt>
              <dd className="tnum text-right font-mono text-xs break-all text-ink">{value}</dd>
            </div>
          ))}
        </dl>
      </aside>
    </div>
  );
}
