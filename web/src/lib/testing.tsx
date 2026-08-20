/**
 * Shared test harness for screen tests.
 *
 * Every screen needs the same four providers and the same two auth calls
 * stubbed before it renders anything at all. Repeating that per file is how a
 * test ends up asserting against an empty document and passing for the wrong
 * reason.
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render } from '@testing-library/react';
import type { ReactNode } from 'react';
import { vi } from 'vitest';

import { Toaster } from '../components/Toaster';
import { AuthProvider } from './auth';
import { ToastProvider, WorkspaceProvider } from './UiProviders';

export const TEST_PROJECT = {
  id: '01JPROJ',
  name: 'production',
  created_at: '2026-08-05T00:00:00Z',
};

export function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

export interface RecordedCall {
  key: string;
  body: unknown;
}

/**
 * Stub the API. `routes` is keyed by `"METHOD /path"`; the auth handshake and
 * `GET /projects` are provided by default and can be overridden.
 */
export function stubApi(routes: Record<string, (body: unknown) => Response>) {
  const calls: RecordedCall[] = [];

  const defaults: Record<string, (body: unknown) => Response> = {
    'POST /auth/refresh-token': () =>
      json({
        access_token: 'access',
        refresh_token: 'refresh',
        token_type: 'bearer',
        expires_in: 900,
      }),
    'GET /auth/me': () =>
      json({
        id: '01JUSER',
        email: 'dev@example.com',
        timezone: 'UTC',
        plan_id: 'free',
        created_at: '2026-08-05T00:00:00Z',
      }),
    'GET /projects': () => json([TEST_PROJECT]),
  };

  const table = { ...defaults, ...routes };

  const fetchMock = vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
    const url = new URL(String(input));
    const key = `${init?.method ?? 'GET'} ${url.pathname}`;
    const body = init?.body ? JSON.parse(String(init.body)) : null;
    calls.push({ key, body });

    const handler = table[key];
    if (!handler) throw new Error(`unstubbed request: ${key}`);
    return handler(body);
  });

  vi.stubGlobal('fetch', fetchMock);
  window.localStorage.setItem('apicost.refresh_token', 'refresh');
  return calls;
}

export function renderScreen(ui: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <AuthProvider>
        <WorkspaceProvider>
          <ToastProvider>
            {ui}
            {/* Lives in Shell in the real app, so a screen rendered on its own
                would push toasts nobody renders. */}
            <Toaster />
          </ToastProvider>
        </WorkspaceProvider>
      </AuthProvider>
    </QueryClientProvider>,
  );
}
