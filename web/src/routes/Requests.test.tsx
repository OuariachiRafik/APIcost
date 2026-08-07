/**
 * The decision log — UC-12.
 *
 * The assertions are about whether a user can tell what happened to their
 * request, which is the whole purpose of this screen.
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { createMemoryRouter, RouterProvider } from 'react-router-dom';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';

import { AuthContext, type AuthState } from '../lib/authContext';
import { Requests } from './Requests';

const AUTH: AuthState = {
  user: {
    id: '01JUSER',
    email: 'user@example.com',
    timezone: 'UTC',
    plan_id: 'free',
    created_at: '2026-08-01T00:00:00Z',
  },
  accessToken: 'token',
  isLoading: false,
  signup: async () => undefined,
  login: async () => undefined,
  logout: async () => undefined,
};

function row(overrides: Record<string, unknown> = {}) {
  return {
    id: '01JROW',
    request_id: '01JREQ',
    timestamp: '2026-08-05T10:00:00Z',
    project_id: '01JPROJ',
    endpoint: 'chat/completions',
    provider: 'openai',
    decision: 'passthrough',
    model_requested: 'gpt-4o',
    model_used: 'gpt-4o',
    tokens_in: 100,
    tokens_out: 50,
    tokens_estimated: false,
    cost_usd: '0.001',
    cost_would_have_been_usd: '0.001',
    saved_usd: '0',
    latency_ms: 250,
    ttft_ms: null,
    cache_hit: false,
    cache_similarity: null,
    routed: false,
    routing_reason_code: 'PASSTHROUGH',
    escalation_triggered: false,
    status: 200,
    error_code: null,
    streamed: false,
    ...overrides,
  };
}

function stub(rows: unknown[], hasMore = false) {
  vi.stubGlobal(
    'fetch',
    vi.fn(
      async () =>
        new Response(
          JSON.stringify({ rows, next_cursor: hasMore ? 'abc' : null, has_more: hasMore }),
          {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          },
        ),
    ),
  );
}

function renderRequests() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const router = createMemoryRouter([{ path: '/', element: <Requests /> }], {
    initialEntries: ['/'],
  });
  return render(
    <QueryClientProvider client={client}>
      <AuthContext.Provider value={AUTH}>
        <RouterProvider router={router} />
      </AuthContext.Provider>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  window.localStorage.clear();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

it('labels every decision so a user can see what we did', async () => {
  stub([
    row({ id: '1', request_id: 'r1', decision: 'cache_hit', cache_hit: true }),
    row({ id: '2', request_id: 'r2', decision: 'routed', routed: true }),
    row({ id: '3', request_id: 'r3', decision: 'passthrough' }),
    row({ id: '4', request_id: 'r4', decision: 'error', status: 429 }),
  ]);

  renderRequests();

  expect(await screen.findByText('cache hit')).toBeInTheDocument();
  expect(screen.getByText('routed')).toBeInTheDocument();
  expect(screen.getByText('passthrough')).toBeInTheDocument();
  expect(screen.getByText('error')).toBeInTheDocument();
});

it('shows the requested model struck through when it was routed elsewhere', async () => {
  stub([
    row({ decision: 'routed', routed: true, model_requested: 'gpt-4o', model_used: 'gpt-4o-mini' }),
  ]);

  renderRequests();

  const struck = await screen.findByText('gpt-4o');
  expect(struck).toHaveClass('line-through');
  expect(screen.getByText('gpt-4o-mini')).toBeInTheDocument();
});

it('opens a detail drawer with the reason code', async () => {
  stub([row({ decision: 'routed', routed: true, routing_reason_code: 'CLASSIFIER_CHEAP_TIER' })]);
  const user = userEvent.setup();

  renderRequests();

  await user.click(await screen.findByText('routed'));

  const drawer = await screen.findByRole('dialog', { name: 'Request detail' });
  expect(drawer).toHaveTextContent('CLASSIFIER_CHEAP_TIER');
  expect(drawer).toHaveTextContent('01JREQ');
});

it('flags estimated token counts rather than presenting them as exact', async () => {
  stub([row({ tokens_estimated: true })]);
  const user = userEvent.setup();

  renderRequests();

  await user.click(await screen.findByText('passthrough'));

  // Scoped to the drawer: the table footnote carries similar wording.
  const drawer = await screen.findByRole('dialog', { name: 'Request detail' });
  expect(drawer).toHaveTextContent(/did not report usage/i);
  expect(drawer).toHaveTextContent(/estimated/i);
});

it('shows an empty state that says what to do next', async () => {
  stub([]);
  renderRequests();
  expect(await screen.findByText(/Point your app at the proxy/i)).toBeInTheDocument();
});

it('disables Next when there is no further page', async () => {
  stub([row()], false);
  renderRequests();
  expect(await screen.findByRole('button', { name: 'Next' })).toBeDisabled();
});
