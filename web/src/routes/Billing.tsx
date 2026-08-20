/**
 * Billing — APICOST_FRONTEND_SPEC §5.8. Read-only in v1.
 *
 * No checkout flow: the Stripe integration exists on the backend but has never
 * run against Stripe, and the price ids are placeholders. Shipping an upgrade
 * button that fails at the last step is worse than not offering one.
 */
import { useQuery } from '@tanstack/react-query';

import { ScreenHeader } from '../components/Screen';
import {
  Badge,
  Card,
  Cell,
  ErrorBanner,
  Meter,
  Row,
  Spinner,
  StatCard,
  Table,
} from '../components/ui';
import { describeError, useAuth } from '../lib/authContext';
import { compact, date, money, percent } from '../lib/format';
import { getPeerBenchmark, getPlan, keys } from '../lib/queries';

export function Billing() {
  const { accessToken } = useAuth();

  const plan = useQuery({
    queryKey: keys.plan(),
    queryFn: () => getPlan(accessToken ?? ''),
    enabled: Boolean(accessToken),
  });

  return (
    <>
      <ScreenHeader title="Billing" description="Your plan and how much of it you've used." />

      {plan.isLoading ? (
        <Spinner />
      ) : plan.isError ? (
        <ErrorBanner message={describeError(plan.error)} onRetry={() => void plan.refetch()} />
      ) : (
        plan.data && (
          <div className="flex flex-col gap-4">
            <Card title={`You're on the ${plan.data.plan_name} plan`}>
              <div className="grid gap-3 sm:grid-cols-3">
                <StatCard
                  label="Requests this month"
                  value={compact(plan.data.requests_this_month)}
                  hint={
                    plan.data.monthly_request_limit > 0
                      ? `of ${compact(plan.data.monthly_request_limit)}`
                      : 'unlimited'
                  }
                />
                <StatCard
                  label="Remaining"
                  value={plan.data.remaining < 0 ? 'Unlimited' : compact(plan.data.remaining)}
                />
                <StatCard
                  label="Renews"
                  value={plan.data.renews_at ? date(plan.data.renews_at) : '—'}
                />
              </div>

              {plan.data.monthly_request_limit > 0 && (
                <div className="mt-4">
                  <Meter fraction={plan.data.fraction_used} />
                  <p className="mt-1.5 text-xs text-muted">
                    {percent(plan.data.fraction_used)} of your monthly requests used
                  </p>
                </div>
              )}

              {plan.data.action !== 'allow' && (
                <p className="mt-3 rounded-md border border-warning/40 bg-warning/10 px-3 py-2 text-xs text-ink">
                  You&rsquo;re over your plan&rsquo;s request allowance. Nothing is blocked — your
                  traffic keeps flowing — but this is the point where a paid plan starts to make
                  sense.
                </p>
              )}
            </Card>

            <Card title="Plans">
              <Table head={['Plan', 'Requests / month', 'Price', '']}>
                {plan.data.available_plans.map((option) => (
                  <Row key={option.id}>
                    <Cell>
                      <span className="flex items-center gap-2">
                        {option.name}
                        {option.id === plan.data?.plan_id && <Badge tone="info">Current</Badge>}
                      </span>
                    </Cell>
                    <Cell numeric>
                      {option.monthly_request_limit === 0
                        ? 'Unlimited'
                        : compact(option.monthly_request_limit)}
                    </Cell>
                    <Cell numeric>{option.price_usd === 0 ? 'Free' : money(option.price_usd)}</Cell>
                    <Cell className="text-right">
                      <span className="text-xs text-muted">
                        {option.id === plan.data?.plan_id ? '' : 'Contact us to upgrade'}
                      </span>
                    </Cell>
                  </Row>
                ))}
              </Table>
            </Card>

            <PeerBenchmark />
          </div>
        )
      )}
    </>
  );
}

/**
 * UC-39. Publishes nothing below a cohort of 50 accounts — not a rounded
 * number, not a wider band, nothing. The refusal is worth showing plainly
 * rather than hiding the section, so the guarantee is visible to the user it
 * protects.
 */
function PeerBenchmark() {
  const { accessToken } = useAuth();
  const query = useQuery({
    queryKey: keys.benchmark(),
    queryFn: () => getPeerBenchmark(accessToken ?? ''),
    enabled: Boolean(accessToken),
  });

  const data = query.data;
  if (query.isLoading || !data) return null;

  return (
    <Card title="How you compare">
      {data.available ? (
        <>
          <p className="tnum font-mono text-2xl leading-tight font-semibold text-ink">
            {money(data.your_cost_per_request)}
            <span className="text-xs font-normal text-muted"> / request</span>
          </p>
          <p className="mt-1 text-xs text-muted">{data.verdict}</p>
          <dl className="mt-3 grid grid-cols-3 gap-2 border-t border-edge pt-3 text-xs">
            {(
              [
                ['25th percentile', data.cohort_p25],
                ['Median', data.cohort_p50],
                ['75th percentile', data.cohort_p75],
              ] as const
            ).map(([label, value]) => (
              <div key={label}>
                <dt className="text-muted">{label}</dt>
                <dd className="tnum font-mono text-ink">{money(value)}</dd>
              </div>
            ))}
          </dl>
          <p className="mt-2 text-[11px] text-muted">
            Across {compact(data.cohort_size)} other accounts. Only aggregates are ever computed —
            no individual account&rsquo;s figures are readable from this, including yours.
          </p>
        </>
      ) : (
        <p className="text-xs text-muted">
          {data.reason === 'NO_TRAFFIC'
            ? 'No requests yet, so there is nothing to compare.'
            : `Not enough accounts to compare against yet. We publish a cohort statistic only at ${data.minimum_cohort_size} or more, so no single account can be inferred from it.`}
        </p>
      )}
    </Card>
  );
}
