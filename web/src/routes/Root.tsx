/**
 * Placeholder shell.
 *
 * The real routes — onboarding, dashboard, requests, cache, routing, budgets,
 * alerts, advisor, settings — arrive from P1 onward (BUILD_SPEC §3).
 */
import { useQuery } from '@tanstack/react-query';

import { getReadiness } from '../lib/api';

export function Root() {
  const { data, isPending, isError } = useQuery({
    queryKey: ['readiness'],
    queryFn: getReadiness,
    retry: false,
  });

  return (
    <main className="mx-auto max-w-2xl p-8 font-sans">
      <h1 className="text-2xl font-semibold">APICost</h1>
      <p className="mt-2 text-sm text-gray-600">Scaffolding only. The dashboard is built in P3.</p>

      <section className="mt-6 rounded-lg border border-gray-200 p-4">
        <h2 className="text-sm font-medium uppercase tracking-wide text-gray-500">Dashboard API</h2>
        {isPending && <p className="mt-2 text-sm">Checking…</p>}
        {isError && (
          <p className="mt-2 text-sm text-red-600" role="alert">
            Unreachable — is <code>make dev</code> running?
          </p>
        )}
        {data && (
          <ul className="mt-2 space-y-1 text-sm">
            {Object.entries(data.checks).map(([name, ok]) => (
              <li key={name}>
                {name}: <span className={ok ? 'text-green-600' : 'text-red-600'}>{String(ok)}</span>
              </li>
            ))}
          </ul>
        )}
      </section>
    </main>
  );
}
