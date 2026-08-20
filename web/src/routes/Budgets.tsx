/**
 * Budgets — APICOST_FRONTEND_SPEC §5.5.
 *
 * All three periods are always visible, set or not: this is a screen about
 * spend safety, and "what have I not protected" is as useful as "what have I".
 *
 * `hard_stop` gets an explanatory confirmation because it returns HTTP 402 and
 * stops the user's application. It is deliberately *lighter* than the kill
 * switch's type-to-confirm: a budget action can be changed back instantly,
 * whereas revoked keys have to be reissued and redeployed.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';

import { RequireProject, ScreenHeader } from '../components/Screen';
import { Badge, Button, Card, ErrorBanner, Meter, Modal, Select, Spinner } from '../components/ui';
import { describeError, useAuth } from '../lib/authContext';
import { money, num, percent } from '../lib/format';
import {
  createBudget,
  deleteBudget,
  keys,
  listBudgets,
  updateBudget,
  type BudgetResponse,
} from '../lib/queries';
import { useToast } from '../lib/uiContext';

type Period = 'daily' | 'weekly' | 'monthly';
type Action = 'alert_only' | 'soft_throttle' | 'hard_stop';

const PERIODS: Period[] = ['daily', 'weekly', 'monthly'];

const ACTION_LABELS: Record<Action, string> = {
  alert_only: 'Alert me only',
  soft_throttle: 'Throttle to the cheapest model',
  hard_stop: 'Stop all requests',
};

const ACTION_HELP: Record<Action, string> = {
  alert_only: 'Traffic continues. You get an alert when the limit is crossed.',
  soft_throttle:
    'Traffic continues on the cheapest equivalent model, overriding both your choice and the router.',
  hard_stop: 'Requests are refused with HTTP 402 until the period resets or you raise the limit.',
};

export function Budgets() {
  return (
    <>
      <ScreenHeader
        title="Budgets"
        description="Cap spend per project before it happens, not after."
      />
      <RequireProject>{(projectId) => <BudgetsBody projectId={projectId} />}</RequireProject>
    </>
  );
}

function BudgetsBody({ projectId }: { projectId: string }) {
  const { accessToken } = useAuth();
  const budgets = useQuery({
    queryKey: keys.budgets(projectId),
    queryFn: () => listBudgets(accessToken ?? '', projectId),
    enabled: Boolean(accessToken),
  });

  if (budgets.isLoading) return <Spinner />;
  if (budgets.isError)
    return (
      <ErrorBanner message={describeError(budgets.error)} onRetry={() => void budgets.refetch()} />
    );

  const byPeriod = new Map((budgets.data ?? []).map((budget) => [budget.period, budget]));

  return (
    <div className="grid gap-4 lg:grid-cols-3">
      {PERIODS.map((period) => (
        <BudgetCard
          key={period}
          projectId={projectId}
          period={period}
          budget={byPeriod.get(period) ?? null}
        />
      ))}
    </div>
  );
}

function BudgetCard({
  projectId,
  period,
  budget,
}: {
  projectId: string;
  period: Period;
  budget: BudgetResponse | null;
}) {
  const { accessToken } = useAuth();
  const client = useQueryClient();
  const { push } = useToast();

  const [limit, setLimit] = useState(budget ? String(num(budget.limit_usd)) : '');
  const [pendingAction, setPendingAction] = useState<Action | null>(null);

  const invalidate = () => {
    void client.invalidateQueries({ queryKey: keys.budgets(projectId) });
  };

  const save = useMutation({
    mutationFn: (action: Action) =>
      createBudget(accessToken ?? '', {
        project_id: projectId,
        period,
        limit_usd: limit,
        action,
      }),
    onSuccess: () => {
      push({ tone: 'success', message: `${label(period)} budget saved.` });
      invalidate();
    },
    onError: (error) => push({ tone: 'error', message: describeError(error) }),
  });

  const changeAction = useMutation({
    mutationFn: (action: Action) => updateBudget(accessToken ?? '', budget!.id, { action }),
    onSuccess: () => {
      push({ tone: 'success', message: 'Budget action updated.' });
      invalidate();
    },
    onError: (error) => push({ tone: 'error', message: describeError(error) }),
  });

  const remove = useMutation({
    mutationFn: () => deleteBudget(accessToken ?? '', budget!.id),
    onSuccess: () => {
      setLimit('');
      push({ tone: 'success', message: 'Budget removed.' });
      invalidate();
    },
    onError: (error) => push({ tone: 'error', message: describeError(error) }),
  });

  const applyAction = (action: Action) => {
    // The consequence modal fires before the change, not after — a warning
    // shown once the traffic is already blockable is an explanation, not a
    // choice.
    if (action === 'hard_stop') {
      setPendingAction(action);
      return;
    }
    commit(action);
  };

  const commit = (action: Action) => {
    if (budget) changeAction.mutate(action);
    else save.mutate(action);
  };

  const spent = budget ? num(budget.spent_usd) : 0;
  const cap = budget ? num(budget.limit_usd) : 0;
  const fraction = budget ? budget.fraction_used : 0;
  const over = budget !== null && fraction >= 1;

  return (
    <Card
      title={label(period)}
      tone={over && budget?.action === 'hard_stop' ? 'critical' : undefined}
      action={
        budget && (
          <Button variant="ghost" onClick={() => remove.mutate()}>
            Remove
          </Button>
        )
      }
    >
      {budget ? (
        <>
          <p className="tnum font-mono text-2xl leading-tight font-semibold text-ink">
            {money(spent)}
          </p>
          <p className="mt-1 text-xs text-muted">
            of {money(cap)} · {percent(fraction)} used
          </p>
          <div className="mt-3">
            <Meter fraction={fraction} />
          </div>
          {over && (
            <p className="mt-3">
              <Badge tone={budget.action === 'hard_stop' ? 'critical' : 'warning'}>
                {budget.action === 'hard_stop' ? 'Traffic stopped' : 'Over budget'}
              </Badge>
            </p>
          )}

          <div className="mt-4 border-t border-edge pt-4">
            <Select
              label="When exceeded"
              value={budget.action}
              onChange={(value) => applyAction(value as Action)}
              disabled={changeAction.isPending}
            >
              {(Object.keys(ACTION_LABELS) as Action[]).map((action) => (
                <option key={action} value={action}>
                  {ACTION_LABELS[action]}
                </option>
              ))}
            </Select>
            <p className="mt-1.5 text-[11px] text-muted">{ACTION_HELP[budget.action as Action]}</p>
          </div>

          <form
            className="mt-4 flex items-end gap-2"
            onSubmit={(event) => {
              event.preventDefault();
              commit(budget.action as Action);
            }}
          >
            <div className="flex-1">
              <label
                htmlFor={`limit-${period}`}
                className="mb-1 block text-xs font-medium text-muted"
              >
                Limit (USD)
              </label>
              <input
                id={`limit-${period}`}
                type="number"
                min="0.000001"
                step="0.01"
                value={limit}
                onChange={(event) => setLimit(event.target.value)}
                className="tnum w-full rounded-md border border-edge bg-page px-3 py-1.5
                           font-mono text-sm text-ink focus:border-info focus:outline-none"
              />
            </div>
            <Button type="submit" variant="secondary" disabled={save.isPending || !limit}>
              Update
            </Button>
          </form>
        </>
      ) : (
        <>
          <p className="text-xs text-muted">
            No {period} budget set. Add one to cap spend before it happens, not after.
          </p>
          <form
            className="mt-4 flex items-end gap-2"
            onSubmit={(event) => {
              event.preventDefault();
              commit('alert_only');
            }}
          >
            <div className="flex-1">
              <label
                htmlFor={`new-limit-${period}`}
                className="mb-1 block text-xs font-medium text-muted"
              >
                Limit (USD)
              </label>
              <input
                id={`new-limit-${period}`}
                type="number"
                min="0.000001"
                step="0.01"
                placeholder="25.00"
                value={limit}
                onChange={(event) => setLimit(event.target.value)}
                className="tnum w-full rounded-md border border-edge bg-page px-3 py-1.5
                           font-mono text-sm text-ink focus:border-info focus:outline-none"
              />
            </div>
            <Button type="submit" disabled={!limit || save.isPending}>
              Add budget
            </Button>
          </form>
        </>
      )}

      {pendingAction === 'hard_stop' && (
        <Modal
          title="Stop all requests when this budget is exceeded?"
          tone="critical"
          onClose={() => setPendingAction(null)}
          footer={
            <>
              <Button variant="secondary" onClick={() => setPendingAction(null)}>
                Cancel
              </Button>
              <Button
                variant="danger"
                onClick={() => {
                  commit('hard_stop');
                  setPendingAction(null);
                }}
              >
                Enable hard stop
              </Button>
            </>
          }
        >
          <p>
            Once this {period} budget is exceeded, requests will be refused with{' '}
            <span className="font-mono text-xs">HTTP 402</span> until the period resets or you raise
            the limit. Your application will see those failures.
          </p>
          <p className="mt-2 text-xs text-muted">
            This is the one place APICost fails closed rather than passing traffic through. You can
            change it back at any time and traffic resumes immediately.
          </p>
        </Modal>
      )}
    </Card>
  );
}

function label(period: Period): string {
  return period.charAt(0).toUpperCase() + period.slice(1);
}
