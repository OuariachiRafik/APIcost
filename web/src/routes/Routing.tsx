/**
 * Routing — APICOST_FRONTEND_SPEC §5.4.
 *
 * Two decisions from the spec shape this screen:
 *
 * The rules editor is a **constrained builder**, not a JSON textarea. The
 * underlying `match_condition` is a JSONB blob, but handing users raw JSON
 * makes malformed rules easy and silent — a rule that matches nothing looks
 * identical to a rule that is working.
 *
 * Savings are shown in **amber when negative**, not red. Negative savings mean
 * escalations to a stronger model cost more than routing saved. That is a real
 * and expected outcome with a conservative escalation threshold, not an error,
 * and red would send the user hunting for a fault that isn't there.
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
  Select,
  Spinner,
  StatCard,
  Table,
  Toggle,
} from '../components/ui';
import { describeError, useAuth } from '../lib/authContext';
import { compact, money, num, percent } from '../lib/format';
import {
  createRoutingRule,
  deleteRoutingRule,
  getProject,
  getRoutingStats,
  keys,
  listRoutingRules,
  updateSettings,
} from '../lib/queries';
import { useToast, useWorkspace } from '../lib/uiContext';

const ENDPOINTS = ['/v1/chat/completions', '/v1/completions', '/v1/embeddings', '/v1/messages'];

const MODELS = [
  'gpt-4o-mini',
  'gpt-4o',
  'gpt-3.5-turbo',
  'claude-3-5-haiku-20241022',
  'claude-3-5-sonnet-20241022',
  'gemini-1.5-flash',
  'gemini-1.5-pro',
];

export function Routing() {
  return (
    <>
      <ScreenHeader
        title="Routing"
        description="Send each request to the cheapest model that can handle it — and escalate when it can't."
      />
      <RequireProject>{(projectId) => <RoutingBody projectId={projectId} />}</RequireProject>
    </>
  );
}

function RoutingBody({ projectId }: { projectId: string }) {
  const { accessToken } = useAuth();
  const { range } = useWorkspace();
  const client = useQueryClient();
  const { push } = useToast();

  const stats = useQuery({
    queryKey: keys.routingStats(projectId, range),
    queryFn: () => getRoutingStats(accessToken ?? '', projectId, range),
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
      push({ tone: 'success', message: 'Routing settings saved.' });
      void client.invalidateQueries({ queryKey: ['project', projectId] });
    },
    onError: (error) => push({ tone: 'error', message: describeError(error) }),
  });

  if (stats.isLoading || project.isLoading) return <Spinner />;
  if (stats.isError)
    return (
      <ErrorBanner message={describeError(stats.error)} onRetry={() => void stats.refetch()} />
    );

  const data = stats.data;
  const settings = project.data;
  if (!data || !settings) return null;

  const savings = num(data.savings_usd);
  const negative = savings < 0;

  return (
    <div className="flex flex-col gap-4">
      <Card>
        <div className="flex items-center justify-between gap-6">
          <div>
            <h2 className="text-sm font-semibold text-ink">
              Routing is {settings.routing_enabled ? 'on' : 'off'}
            </h2>
            <p className="mt-0.5 max-w-xl text-xs text-muted">
              Caching returns the same answer more cheaply. Routing returns a{' '}
              <em className="text-ink not-italic">different</em> answer from a cheaper model, so it
              is off until you turn it on.
            </p>
          </div>
          <Button
            variant={settings.routing_enabled ? 'secondary' : 'primary'}
            onClick={() => save.mutate({ routing_enabled: !settings.routing_enabled })}
            disabled={save.isPending}
          >
            {settings.routing_enabled ? 'Turn routing off' : 'Turn routing on'}
          </Button>
        </div>
      </Card>

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <StatCard
          label="Net savings"
          value={money(savings)}
          tone={negative ? 'warning' : savings > 0 ? 'positive' : undefined}
          hint={negative ? 'Escalations cost more than routing saved' : undefined}
        />
        <StatCard label="Routed requests" value={compact(data.routed_requests)} />
        <StatCard
          label="Escalations"
          value={compact(data.escalations)}
          hint={`${percent(data.escalation_rate)} of routed`}
        />
        <StatCard label="Passthrough" value={compact(data.passthrough_requests)} />
      </div>

      {negative && (
        <div className="rounded-md border border-warning/40 bg-warning/10 px-4 py-3">
          <p className="text-xs text-ink">
            <span className="font-medium text-warning">Routing is costing more than it saves.</span>{' '}
            Escalations to a stronger model cost {money(data.escalation_cost_usd)} against{' '}
            {money(data.gross_savings_usd)} saved. This is expected when the cheap tier is a poor
            fit for these prompts — exclude the endpoint below, or turn routing off for this
            project.
          </p>
        </div>
      )}

      <Card title="Escalation">
        <Toggle
          size="lg"
          label="Retry on a stronger model when the cheap answer looks wrong"
          description="Catches empty, truncated, refused or hedging answers and re-runs them on the originally requested model. Costs two calls when it fires, which is why the net figure above can go negative."
          checked={settings.escalation_enabled}
          onChange={(next) => save.mutate({ escalation_enabled: next })}
          disabled={save.isPending}
        />
      </Card>

      {data.tier_distribution.length > 0 && (
        <Card title="Where requests went">
          <Table head={['Model', 'Requests', 'Share']}>
            {data.tier_distribution.map((tier) => (
              <Row key={tier.model}>
                <Cell>
                  <span className="font-mono text-xs">{tier.model}</span>
                </Cell>
                <Cell numeric>{compact(tier.requests)}</Cell>
                <Cell numeric>{percent(tier.share)}</Cell>
              </Row>
            ))}
          </Table>
        </Card>
      )}

      <Rules projectId={projectId} />
    </div>
  );
}

function Rules({ projectId }: { projectId: string }) {
  const { accessToken } = useAuth();
  const client = useQueryClient();
  const { push } = useToast();

  const [ruleType, setRuleType] = useState<'override' | 'exclude'>('override');
  const [endpoint, setEndpoint] = useState(ENDPOINTS[0]!);
  const [targetModel, setTargetModel] = useState(MODELS[0]!);
  const [priority, setPriority] = useState('10');

  const rules = useQuery({
    queryKey: keys.routingRules(projectId),
    queryFn: () => listRoutingRules(accessToken ?? '', projectId),
    enabled: Boolean(accessToken),
  });

  const create = useMutation({
    mutationFn: () =>
      createRoutingRule(accessToken ?? '', {
        project_id: projectId,
        rule_type: ruleType,
        match_condition: { endpoint },
        target_model: ruleType === 'override' ? targetModel : null,
        priority: Number(priority) || 0,
      }),
    onSuccess: () => {
      push({ tone: 'success', message: 'Rule created.' });
      void client.invalidateQueries({ queryKey: keys.routingRules(projectId) });
    },
    onError: (error) => push({ tone: 'error', message: describeError(error) }),
  });

  const remove = useMutation({
    mutationFn: (id: string) => deleteRoutingRule(accessToken ?? '', id),
    onSuccess: () => {
      push({ tone: 'success', message: 'Rule deleted.' });
      void client.invalidateQueries({ queryKey: keys.routingRules(projectId) });
    },
    onError: (error) => push({ tone: 'error', message: describeError(error) }),
  });

  return (
    <Card title="Rules">
      <p className="mb-3 text-xs text-muted">
        Rules are absolute — they run before the classifier and win. An exclusion beats an override
        at the same priority, because &ldquo;never touch this&rdquo; is a stronger instruction than
        &ldquo;use model X&rdquo;. Rules apply even when routing is off.
      </p>

      {rules.isLoading ? (
        <Spinner />
      ) : rules.data && rules.data.length > 0 ? (
        <Table head={['Type', 'When', 'Then', 'Priority', '']}>
          {rules.data.map((rule) => (
            <Row key={rule.id}>
              <Cell>
                <Badge tone={rule.rule_type === 'exclude' ? 'warning' : 'info'}>
                  {rule.rule_type}
                </Badge>
              </Cell>
              <Cell>
                <span className="font-mono text-xs text-muted">
                  {String((rule.match_condition as Record<string, unknown>).endpoint ?? 'any')}
                </span>
              </Cell>
              <Cell>
                <span className="font-mono text-xs">
                  {rule.rule_type === 'exclude' ? 'never route' : rule.target_model}
                </span>
              </Cell>
              <Cell numeric>{rule.priority}</Cell>
              <Cell className="text-right">
                <Button variant="ghost" onClick={() => remove.mutate(rule.id)}>
                  Delete
                </Button>
              </Cell>
            </Row>
          ))}
        </Table>
      ) : (
        <EmptyState title="No rules yet">
          Without rules, every routable request is decided by the classifier.
        </EmptyState>
      )}

      <form
        className="mt-4 grid items-end gap-2 border-t border-edge pt-4 sm:grid-cols-5"
        onSubmit={(event) => {
          event.preventDefault();
          create.mutate();
        }}
      >
        <Select
          label="Rule"
          value={ruleType}
          onChange={(value) => setRuleType(value as 'override' | 'exclude')}
        >
          <option value="override">Always use</option>
          <option value="exclude">Never route</option>
        </Select>
        <Select label="On endpoint" value={endpoint} onChange={setEndpoint}>
          {ENDPOINTS.map((option) => (
            <option key={option} value={option}>
              {option}
            </option>
          ))}
        </Select>
        <Select
          label="Model"
          value={targetModel}
          onChange={setTargetModel}
          disabled={ruleType === 'exclude'}
        >
          {MODELS.map((option) => (
            <option key={option} value={option}>
              {option}
            </option>
          ))}
        </Select>
        <div>
          <label htmlFor="rule-priority" className="mb-1 block text-xs font-medium text-muted">
            Priority
          </label>
          <input
            id="rule-priority"
            type="number"
            value={priority}
            onChange={(event) => setPriority(event.target.value)}
            className="tnum w-full rounded-md border border-edge bg-page px-3 py-1.5 font-mono
                       text-sm text-ink focus:border-info focus:outline-none"
          />
        </div>
        <Button type="submit" disabled={create.isPending}>
          Add rule
        </Button>
      </form>
    </Card>
  );
}
