/**
 * Advisor — APICOST_FRONTEND_SPEC §5.7.
 *
 * The break-even caveats render inline and always, never behind a click. The
 * API returns them in the payload for exactly this reason: a bare
 * "self-hosting is cheaper" number is a misleading recommendation, and a
 * caveat the frontend can forget to render is a caveat that will be forgotten.
 *
 * Dismissing a recommendation is permanent — the nightly job never re-suggests
 * a dismissed title — so the dismiss is optimistic with a short undo window.
 * The undo covers the misclick, not a standing re-offer.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';

import { RequireProject, ScreenHeader } from '../components/Screen';
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
  type Tone,
} from '../components/ui';
import { describeError, useAuth } from '../lib/authContext';
import { compact, money, num, percent } from '../lib/format';
import {
  compressPrompt,
  getBreakeven,
  getPromptOptimizations,
  keys,
  listRecommendations,
  setRecommendationStatus,
  type CompressResponse,
} from '../lib/queries';
import { useToast, useWorkspace } from '../lib/uiContext';

const CONFIDENCE_TONE: Record<string, Tone> = {
  high: 'positive',
  medium: 'info',
  low: 'warning',
};

export function Advisor() {
  return (
    <>
      <ScreenHeader title="Advisor" description="What your own usage says you could change." />
      <RequireProject>{(projectId) => <AdvisorBody projectId={projectId} />}</RequireProject>
    </>
  );
}

function AdvisorBody({ projectId }: { projectId: string }) {
  return (
    <div className="flex flex-col gap-4">
      <Recommendations projectId={projectId} />
      <div className="grid gap-4 lg:grid-cols-2">
        <BreakEven projectId={projectId} />
        <PromptOptimizations projectId={projectId} />
      </div>
      <Compressor />
    </div>
  );
}

function Recommendations({ projectId }: { projectId: string }) {
  const { accessToken } = useAuth();
  const client = useQueryClient();
  const { push } = useToast();

  const list = useQuery({
    queryKey: keys.recommendations(projectId),
    queryFn: () => listRecommendations(accessToken ?? '', projectId),
    enabled: Boolean(accessToken),
  });

  const setStatus = useMutation({
    mutationFn: ({ id, status }: { id: string; status: string }) =>
      setRecommendationStatus(accessToken ?? '', id, status),
    onSuccess: () => void client.invalidateQueries({ queryKey: keys.recommendations(projectId) }),
    onError: (error) => push({ tone: 'error', message: describeError(error) }),
  });

  const dismiss = (id: string, title: string) => {
    setStatus.mutate({ id, status: 'dismissed' });
    push({
      tone: 'info',
      message: `Dismissed “${title}”. It won't be suggested again.`,
      durationMs: 5000,
      action: {
        label: 'Undo',
        onAction: () => setStatus.mutate({ id, status: 'open' }),
      },
    });
  };

  if (list.isLoading) return <Spinner />;
  if (list.isError)
    return <ErrorBanner message={describeError(list.error)} onRetry={() => void list.refetch()} />;

  const open = (list.data ?? []).filter((item) => item.status !== 'dismissed');

  return (
    <Card title="Recommendations">
      {open.length === 0 ? (
        <EmptyState title="Nothing to recommend yet">
          Not enough usage history yet to generate recommendations. Check back once you&rsquo;ve
          made some requests.
        </EmptyState>
      ) : (
        <ul className="flex flex-col gap-3">
          {open.map((item) => {
            const detail = (item.detail ?? {}) as Record<string, unknown>;
            return (
              <li
                key={item.id}
                className="flex items-start justify-between gap-4 rounded-md border
                           border-edge bg-page px-4 py-3"
              >
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-2">
                    <p className="text-sm font-medium text-ink">{item.title}</p>
                    <Badge tone={CONFIDENCE_TONE[item.confidence] ?? 'neutral'}>
                      {item.confidence} confidence
                    </Badge>
                    {item.status === 'adopted' && <Badge tone="positive">Adopted</Badge>}
                  </div>
                  {typeof detail.rationale === 'string' && (
                    <p className="mt-1 text-xs text-muted">{detail.rationale}</p>
                  )}
                  <p className="mt-1 text-[11px] text-muted">
                    Based on {compact(item.sample_size)} observations
                  </p>
                </div>
                <div className="flex shrink-0 flex-col items-end gap-2">
                  <span className="tnum font-mono text-sm text-positive">
                    {money(item.projected_savings_usd)}
                    <span className="text-[11px] text-muted"> /mo</span>
                  </span>
                  <div className="flex gap-1">
                    {item.status !== 'adopted' && (
                      <Button
                        variant="secondary"
                        onClick={() => setStatus.mutate({ id: item.id, status: 'adopted' })}
                      >
                        Adopt
                      </Button>
                    )}
                    <Button variant="ghost" onClick={() => dismiss(item.id, item.title)}>
                      Dismiss
                    </Button>
                  </div>
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </Card>
  );
}

function BreakEven({ projectId }: { projectId: string }) {
  const { accessToken } = useAuth();
  const query = useQuery({
    queryKey: keys.breakeven(projectId),
    queryFn: () => getBreakeven(accessToken ?? '', projectId),
    enabled: Boolean(accessToken),
  });

  if (query.isLoading)
    return (
      <Card title="Self-hosting">
        <Spinner />
      </Card>
    );
  if (query.isError)
    return (
      <Card title="Self-hosting">
        <ErrorBanner message={describeError(query.error)} onRetry={() => void query.refetch()} />
      </Card>
    );

  const data = query.data;
  if (!data) return null;

  const favourable = data.recommendation === 'gpu';
  const insufficient = data.recommendation === 'insufficient_data';

  return (
    <Card title="Self-hosting vs the API">
      {insufficient ? (
        <p className="text-xs text-muted">
          Not enough volume yet for this comparison to mean anything.
        </p>
      ) : (
        <>
          <p
            className={`tnum font-mono text-2xl leading-tight font-semibold ${
              favourable ? 'text-positive' : 'text-ink'
            }`}
          >
            {favourable
              ? money(data.monthly_saving_usd)
              : money(Math.abs(num(data.monthly_saving_usd)))}
          </p>
          <p className="mt-1 text-xs text-muted">
            {favourable
              ? `cheaper per month on ${data.n_gpus}× ${data.gpu_option}`
              : `more expensive per month on ${data.gpu_option} — the API is cheaper at your volume`}
          </p>

          <dl className="mt-3 grid grid-cols-2 gap-x-4 gap-y-1 border-t border-edge pt-3 text-xs">
            <dt className="text-muted">API cost</dt>
            <dd className="tnum text-right font-mono text-ink">
              {money(data.api_monthly_cost_usd)}
            </dd>
            <dt className="text-muted">GPU cost</dt>
            <dd className="tnum text-right font-mono text-ink">
              {money(data.gpu_monthly_cost_usd)}
            </dd>
            <dt className="text-muted">Monthly tokens</dt>
            <dd className="tnum text-right font-mono text-ink">{compact(data.monthly_tokens)}</dd>
            <dt className="text-muted">Break-even at</dt>
            <dd className="tnum text-right font-mono text-ink">
              {data.break_even_tokens === null
                ? 'never'
                : `${compact(data.break_even_tokens)} tokens`}
            </dd>
          </dl>
        </>
      )}

      {/* Always visible, never behind a click — the number is misleading alone. */}
      <ul className="mt-4 flex list-disc flex-col gap-1 border-t border-edge pt-3 pl-4">
        {data.caveats.map((caveat) => (
          <li key={caveat} className="text-[11px] leading-relaxed text-muted">
            {caveat}
          </li>
        ))}
      </ul>
    </Card>
  );
}

function PromptOptimizations({ projectId }: { projectId: string }) {
  const { accessToken } = useAuth();
  const { range } = useWorkspace();

  const query = useQuery({
    queryKey: keys.promptOptimizations(projectId, range),
    queryFn: () => getPromptOptimizations(accessToken ?? '', projectId, range),
    enabled: Boolean(accessToken),
  });

  if (query.isLoading)
    return (
      <Card title="Prompt size">
        <Spinner />
      </Card>
    );
  const data = query.data;
  if (!data) return null;

  return (
    <Card title="Prompt size">
      {data.warned_requests > 0 ? (
        <>
          <div className="grid grid-cols-2 gap-3">
            <StatCard
              label="Requests resending stale history"
              value={percent(data.warned_fraction)}
              hint={`${compact(data.warned_requests)} of ${compact(data.total_requests)}`}
            />
            <StatCard
              label="Estimated waste"
              value={money(data.estimated_wasted_usd)}
              tone="warning"
            />
          </div>
          <p className="mt-3 text-[11px] text-muted">
            These requests carried earlier conversation turns that had little to do with the
            question being asked. Trimming them is the single cheapest saving available.
          </p>
        </>
      ) : (
        <p className="text-xs text-muted">
          No requests are carrying obviously stale conversation history.
        </p>
      )}

      {data.token_heavy.length > 0 && (
        <div className="mt-4 border-t border-edge pt-3">
          <p className="mb-2 text-xs font-medium text-ink">Heaviest endpoints by average tokens</p>
          <Table head={['Endpoint', 'Requests', 'Avg tokens', 'Cost']}>
            {data.token_heavy.slice(0, 6).map((row) => (
              <Row key={row.endpoint}>
                <Cell>
                  <span className="font-mono text-xs">{row.endpoint}</span>
                </Cell>
                <Cell numeric>{compact(row.requests)}</Cell>
                <Cell numeric>{compact(Math.round(row.avg_tokens_total))}</Cell>
                <Cell numeric>{money(row.total_cost_usd)}</Cell>
              </Row>
            ))}
          </Table>
        </div>
      )}
    </Card>
  );
}

const SAMPLE = `{
  "model": "gpt-4o",
  "messages": [
    { "role": "system", "content": "You are a helpful assistant." },
    { "role": "user", "content": "…" }
  ]
}`;

function Compressor() {
  const { accessToken } = useAuth();
  const { push } = useToast();
  const [text, setText] = useState('');
  const [result, setResult] = useState<CompressResponse | null>(null);

  const run = useMutation({
    mutationFn: () => {
      let parsed: unknown;
      try {
        parsed = JSON.parse(text);
      } catch {
        throw new Error('That is not valid JSON. Paste the request body you would send.');
      }
      return compressPrompt(accessToken ?? '', parsed);
    },
    onSuccess: setResult,
    onError: (error) => push({ tone: 'error', message: describeError(error) }),
  });

  const suggestion = result?.suggestion as
    | { tokens_before: number; tokens_after: number; tokens_saved: number; messages: unknown[] }
    | null
    | undefined;

  return (
    <Card title="Prompt compression">
      <p className="mb-3 text-xs text-muted">
        Paste a request body and we&rsquo;ll show which earlier messages look unrelated to the
        current question, and what dropping them would save. We never store prompts, so this cannot
        be pre-filled from your history — and nothing here is applied to live traffic.
      </p>

      <label htmlFor="compress-input" className="mb-1 block text-xs font-medium text-muted">
        Request body
      </label>
      <textarea
        id="compress-input"
        rows={10}
        spellCheck={false}
        value={text}
        onChange={(event) => setText(event.target.value)}
        placeholder={SAMPLE}
        className="w-full rounded-md border border-edge bg-sunken px-3 py-2 font-mono text-xs
                   leading-relaxed text-ink placeholder:text-muted/50 focus:border-info
                   focus:outline-none"
      />

      <div className="mt-3 flex items-center gap-2">
        <Button onClick={() => run.mutate()} disabled={!text.trim() || run.isPending}>
          {run.isPending ? 'Analysing…' : 'Analyse prompt'}
        </Button>
        {result && (
          <Button
            variant="ghost"
            onClick={() => {
              setResult(null);
              setText('');
            }}
          >
            Clear
          </Button>
        )}
      </div>

      {result && (
        <div className="mt-4 border-t border-edge pt-4">
          {result.warn && suggestion ? (
            <>
              <div className="grid grid-cols-3 gap-3">
                <StatCard label="Tokens now" value={compact(suggestion.tokens_before)} />
                <StatCard label="After trimming" value={compact(suggestion.tokens_after)} />
                <StatCard
                  label="Saved"
                  value={compact(suggestion.tokens_saved)}
                  tone="positive"
                  hint={percent(result.reclaimable_fraction)}
                />
              </div>
              <p className="mt-3 text-xs text-muted">
                {result.stale.length} message{result.stale.length === 1 ? '' : 's'} looked unrelated
                to the latest question. The suggestion drops them and keeps everything else exactly
                as written — nothing is summarised or reworded.
              </p>
            </>
          ) : (
            <p className="text-xs text-muted">
              Nothing worth trimming here
              {result.reason === 'BELOW_THRESHOLD' && ' — this conversation is already short'}
              {result.reason === 'ALL_RELEVANT' &&
                ' — every earlier message relates to the question'}
              {result.reason === 'FEW_MESSAGES' &&
                ' — this is one long message, not a conversation with history to trim'}
              .
            </p>
          )}
        </div>
      )}
    </Card>
  );
}
