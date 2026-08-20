/**
 * Cache — APICOST_FRONTEND_SPEC §5.3.
 *
 * The similarity threshold is the one control in this product that can cause
 * silently *wrong* behaviour rather than merely expensive behaviour: set it
 * too low and a cached answer to a different question gets served as if it
 * were the answer to this one. So it is a slider (invalid values impossible)
 * with a standing warning below 0.90, and it is the most visually cautious
 * element on the page.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useState } from 'react';

import { RequireProject, ScreenHeader } from '../components/Screen';
import { SpendChart } from '../components/SpendChart';
import {
  Button,
  Card,
  CodeBlock,
  EmptyState,
  ErrorBanner,
  Modal,
  Select,
  Spinner,
  StatCard,
  Toggle,
} from '../components/ui';
import { describeError, useAuth } from '../lib/authContext';
import { compact, money, num, percent } from '../lib/format';
import { getCacheStats, getProject, invalidateCache, keys, updateSettings } from '../lib/queries';
import { useToast, useWorkspace } from '../lib/uiContext';

const THRESHOLD_FLOOR = 0.9;

export function Cache() {
  return (
    <>
      <ScreenHeader
        title="Cache"
        description="Serve a repeated question from the last answer instead of paying for it again."
      />
      <RequireProject>{(projectId) => <CacheBody projectId={projectId} />}</RequireProject>
    </>
  );
}

function CacheBody({ projectId }: { projectId: string }) {
  const { accessToken } = useAuth();
  const { range } = useWorkspace();
  const client = useQueryClient();
  const { push } = useToast();

  const [confirming, setConfirming] = useState(false);

  const stats = useQuery({
    queryKey: keys.cacheStats(projectId, range),
    queryFn: () => getCacheStats(accessToken ?? '', projectId, range),
    enabled: Boolean(accessToken),
  });

  const project = useQuery({
    queryKey: ['project', projectId],
    queryFn: () => getProject(accessToken ?? '', projectId),
    enabled: Boolean(accessToken),
  });

  const save = useMutation({
    mutationFn: (body: Parameters<typeof updateSettings>[2]) =>
      updateSettings(accessToken ?? '', projectId, body),
    onSuccess: () => {
      push({ tone: 'success', message: 'Cache settings saved.' });
      void client.invalidateQueries({ queryKey: ['project', projectId] });
    },
    onError: (error) => push({ tone: 'error', message: describeError(error) }),
  });

  const invalidate = useMutation({
    mutationFn: () => invalidateCache(accessToken ?? '', projectId),
    onSuccess: (result) => {
      setConfirming(false);
      push({
        tone: 'success',
        message: `Cleared ${compact(result.entries_removed)} cache entries.`,
      });
      void client.invalidateQueries({ queryKey: keys.cacheStats(projectId, range) });
    },
    onError: (error) => {
      setConfirming(false);
      push({ tone: 'error', message: describeError(error) });
    },
  });

  if (stats.isLoading || project.isLoading) return <Spinner />;
  if (stats.isError)
    return (
      <ErrorBanner message={describeError(stats.error)} onRetry={() => void stats.refetch()} />
    );

  const data = stats.data;
  const settings = project.data;
  if (!data || !settings) return null;

  const series = data.series.map((point) => ({
    label: point.day.slice(5),
    value: num(point.savings_usd),
  }));

  return (
    <div className="flex flex-col gap-4">
      <Card
        title="Caching"
        action={
          <Button
            variant="secondary"
            onClick={() => setConfirming(true)}
            disabled={invalidate.isPending}
          >
            Invalidate cache
          </Button>
        }
      >
        <Toggle
          size="lg"
          label="Cache enabled"
          description="When on, a request close enough to one we've already answered is served from the stored response — no provider call, no cost."
          checked={settings.cache_enabled}
          onChange={(next) => save.mutate({ cache_enabled: next })}
          disabled={save.isPending}
        />
      </Card>

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <StatCard label="Hit rate" value={percent(data.hit_rate)} />
        <StatCard
          label="Saved by caching"
          value={money(data.savings_usd)}
          tone={num(data.savings_usd) > 0 ? 'positive' : undefined}
        />
        <StatCard
          label="Cache hits"
          value={compact(data.hits)}
          hint={`of ${compact(data.requests)} requests`}
        />
        <StatCard
          label="Live entries"
          value={compact(data.live_entries)}
          hint={`${data.avg_hits_per_entry.toFixed(1)} hits per entry`}
        />
      </div>

      {data.requests > 0 ? (
        <Card title="Savings from caching">
          <SpendChart data={series} valueLabel="Saved" />
        </Card>
      ) : (
        <Card>
          <EmptyState title="No cache activity yet">
            Once your proxied requests start repeating, cache hits and savings will show up here.
          </EmptyState>
        </Card>
      )}

      <div className="grid gap-4 lg:grid-cols-2">
        <ThresholdControl
          value={settings.similarity_threshold}
          onCommit={(value) => save.mutate({ similarity_threshold: value })}
          disabled={save.isPending}
        />
        <TtlControl
          seconds={settings.cache_ttl_seconds}
          onCommit={(value) => save.mutate({ cache_ttl_seconds: value })}
          disabled={save.isPending}
        />
      </div>

      <NonCacheable />

      {confirming && (
        <Modal
          title="Invalidate cache?"
          onClose={() => setConfirming(false)}
          footer={
            <>
              <Button variant="secondary" onClick={() => setConfirming(false)}>
                Cancel
              </Button>
              <Button onClick={() => invalidate.mutate()} disabled={invalidate.isPending}>
                {invalidate.isPending ? 'Clearing…' : 'Invalidate'}
              </Button>
            </>
          }
        >
          Every stored entry for this project is dropped. Nothing breaks — the next requests go to
          the provider as normal and the cache refills from live traffic. You lose the savings until
          it does.
        </Modal>
      )}
    </div>
  );
}

function ThresholdControl({
  value,
  onCommit,
  disabled,
}: {
  value: number;
  onCommit: (value: number) => void;
  disabled?: boolean;
}) {
  const [draft, setDraft] = useState(value);
  useEffect(() => setDraft(value), [value]);

  const risky = draft < THRESHOLD_FLOOR;

  return (
    <Card title="Similarity threshold" tone={risky ? 'critical' : undefined}>
      <p className="mb-3 text-xs text-muted">
        How alike a new prompt must be to a stored one before we reuse the answer. Higher is safer
        and caches less; lower saves more and risks serving an answer to a different question.
      </p>

      <div className="flex items-center gap-3">
        <input
          type="range"
          min={0.8}
          max={0.99}
          step={0.005}
          value={draft}
          disabled={disabled}
          aria-label="Similarity threshold"
          onChange={(event) => setDraft(Number(event.target.value))}
          onMouseUp={() => draft !== value && onCommit(draft)}
          onKeyUp={() => draft !== value && onCommit(draft)}
          className="h-1 flex-1 accent-[var(--accent-info)]"
        />
        <span className="tnum w-16 text-right font-mono text-sm text-ink">{draft.toFixed(3)}</span>
      </div>

      {risky && (
        <p className="mt-3 rounded-md border border-warning/40 bg-warning/10 px-3 py-2 text-xs text-warning">
          Below 0.90, cache hits may return answers that don&rsquo;t actually match the new request.
        </p>
      )}

      {draft !== value && (
        <div className="mt-3 flex gap-2">
          <Button onClick={() => onCommit(draft)} disabled={disabled}>
            Save threshold
          </Button>
          <Button variant="ghost" onClick={() => setDraft(value)}>
            Reset
          </Button>
        </div>
      )}
    </Card>
  );
}

const UNITS: Record<string, number> = { minutes: 60, hours: 3600, days: 86_400 };

function TtlControl({
  seconds,
  onCommit,
  disabled,
}: {
  seconds: number;
  onCommit: (seconds: number) => void;
  disabled?: boolean;
}) {
  // Pick the largest unit the stored value divides into cleanly, so 86400
  // comes back as "1 day" rather than "1440 minutes".
  const initialUnit = seconds % 86_400 === 0 ? 'days' : seconds % 3600 === 0 ? 'hours' : 'minutes';
  const [unit, setUnit] = useState(initialUnit);
  const [amount, setAmount] = useState(String(seconds / UNITS[initialUnit]!));

  useEffect(() => {
    const next = seconds % 86_400 === 0 ? 'days' : seconds % 3600 === 0 ? 'hours' : 'minutes';
    setUnit(next);
    setAmount(String(seconds / UNITS[next]!));
  }, [seconds]);

  const parsed = Number(amount);
  const nextSeconds = Number.isFinite(parsed) ? Math.round(parsed * UNITS[unit]!) : seconds;
  const changed = nextSeconds !== seconds && nextSeconds > 0;

  return (
    <Card title="Time to live">
      <p className="mb-3 text-xs text-muted">
        How long a cached answer stays usable. Shorter means fresher answers and fewer hits; longer
        means more savings on content that doesn&rsquo;t change.
      </p>
      <div className="flex items-end gap-2">
        <div className="w-28">
          <label className="mb-1 block text-xs font-medium text-muted" htmlFor="ttl-amount">
            Duration
          </label>
          <input
            id="ttl-amount"
            type="number"
            min={1}
            value={amount}
            disabled={disabled}
            onChange={(event) => setAmount(event.target.value)}
            className="tnum w-full rounded-md border border-edge bg-page px-3 py-1.5 font-mono
                       text-sm text-ink focus:border-info focus:outline-none"
          />
        </div>
        <div className="w-32">
          <Select label="Unit" value={unit} onChange={setUnit} disabled={disabled}>
            <option value="minutes">Minutes</option>
            <option value="hours">Hours</option>
            <option value="days">Days</option>
          </Select>
        </div>
        <Button onClick={() => onCommit(nextSeconds)} disabled={!changed || disabled}>
          Save
        </Button>
      </div>
    </Card>
  );
}

/**
 * UC-24 is a request header the developer sets in their own code, not a
 * setting — there is no API to configure it per endpoint. Documenting it is
 * the honest UI; a rule list here would imply a control that does not exist.
 */
function NonCacheable() {
  return (
    <Card title="Marking a request as non-cacheable">
      <p className="mb-3 text-xs text-muted">
        Some requests should never be served from cache — anything with a timestamp in the answer,
        anything deliberately random. Send this header and APICost forwards the request untouched.
      </p>
      <CodeBlock
        code={`curl https://proxy.apicost.dev/v1/chat/completions \\
  -H "Authorization: Bearer $APICOST_PROXY_KEY" \\
  -H "X-APICost-No-Cache: 1" \\
  -d '{"model": "gpt-4o", "messages": [...]}'`}
      />
    </Card>
  );
}
