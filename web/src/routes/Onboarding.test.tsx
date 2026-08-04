/**
 * The P1 acceptance criterion, as a test: signup through to integration
 * instructions without leaving the wizard.
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { ReactNode } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { AuthProvider } from '../lib/auth';
import { Root } from './Root';

const PROVIDER_KEY = 'sk-proj-TestProviderKey0123456789';
const PROXY_KEY = 'apc_live_TestProxyKey9876543210ABCD';

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

/** Minimal stand-in for the API, keyed by method and path. */
function stubApi(overrides: Record<string, () => Response> = {}) {
  const routes: Record<string, () => Response> = {
    'POST /auth/signup': () =>
      json(
        { access_token: 'access', refresh_token: 'refresh', token_type: 'bearer', expires_in: 900 },
        201,
      ),
    'POST /auth/login': () =>
      json({
        access_token: 'access',
        refresh_token: 'refresh',
        token_type: 'bearer',
        expires_in: 900,
      }),
    'GET /auth/me': () =>
      json({
        id: '01JUSER',
        email: 'new@example.com',
        timezone: 'UTC',
        plan_id: 'free',
        created_at: '2026-08-05T00:00:00Z',
      }),
    'POST /keys': () =>
      json(
        {
          id: '01JKEY',
          provider: 'openai',
          last4: PROVIDER_KEY.slice(-4),
          is_active: true,
          added_at: '2026-08-05T00:00:00Z',
          last_used_at: null,
        },
        201,
      ),
    'POST /projects': () =>
      json({ id: '01JPROJ', name: 'production', created_at: '2026-08-05T00:00:00Z' }, 201),
    'POST /projects/01JPROJ/proxy-keys': () =>
      json(
        { id: '01JPX', project_id: '01JPROJ', name: 'default', last4: 'ABCD', key: PROXY_KEY },
        201,
      ),
    ...overrides,
  };

  const fetchMock = vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
    const url = new URL(String(input));
    const key = `${init?.method ?? 'GET'} ${url.pathname}`;
    const handler = routes[key];
    if (!handler) throw new Error(`unstubbed request: ${key}`);
    return handler();
  });

  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

function renderApp(ui: ReactNode = <Root />) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <AuthProvider>{ui}</AuthProvider>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  window.localStorage.clear();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('onboarding', () => {
  it('takes a new user from signup to integration instructions in one flow', async () => {
    stubApi();
    const user = userEvent.setup();
    renderApp();

    // Step 0 — sign up.
    await user.type(await screen.findByLabelText('Email'), 'new@example.com');
    await user.type(screen.getByLabelText('Password'), 'a-very-long-password');
    await user.click(screen.getByRole('button', { name: 'Create account' }));

    // Step 1 — provider key.
    await user.type(await screen.findByLabelText('API key'), PROVIDER_KEY);
    await user.click(screen.getByRole('button', { name: 'Add key' }));

    // Step 2 — project.
    await screen.findByLabelText('Project name');
    await user.click(screen.getByRole('button', { name: 'Create project' }));

    // Step 3 — proxy key.
    await user.click(await screen.findByRole('button', { name: 'Issue proxy key' }));

    // Step 4 — the payoff: a copyable key and a base-URL swap.
    await waitFor(() => {
      expect(screen.getByText(PROXY_KEY)).toBeInTheDocument();
    });
    expect(screen.getByText(/Python \(openai SDK\)/)).toBeInTheDocument();
    expect(screen.getByText(/Node \(openai SDK\)/)).toBeInTheDocument();
    expect(screen.getByText(/cURL/)).toBeInTheDocument();
    expect(screen.getByText(/shown once/i)).toBeInTheDocument();
  });

  it('never renders the provider key back to the user', async () => {
    stubApi();
    const user = userEvent.setup();
    const { container } = renderApp();

    await user.type(await screen.findByLabelText('Email'), 'new@example.com');
    await user.type(screen.getByLabelText('Password'), 'a-very-long-password');
    await user.click(screen.getByRole('button', { name: 'Create account' }));

    await user.type(await screen.findByLabelText('API key'), PROVIDER_KEY);
    await user.click(screen.getByRole('button', { name: 'Add key' }));

    await screen.findByLabelText('Project name');
    expect(container.textContent).not.toContain(PROVIDER_KEY);
  });

  it('surfaces an API failure without advancing the wizard', async () => {
    stubApi({
      'POST /keys': () =>
        json(
          {
            type: 'about:blank',
            title: 'Conflict',
            status: 409,
            detail: 'An active openai key already exists.',
          },
          409,
        ),
    });
    const user = userEvent.setup();
    renderApp();

    await user.type(await screen.findByLabelText('Email'), 'new@example.com');
    await user.type(screen.getByLabelText('Password'), 'a-very-long-password');
    await user.click(screen.getByRole('button', { name: 'Create account' }));

    await user.type(await screen.findByLabelText('API key'), PROVIDER_KEY);
    await user.click(screen.getByRole('button', { name: 'Add key' }));

    expect(await screen.findByRole('alert')).toHaveTextContent(/already exists/i);
    expect(screen.getByLabelText('API key')).toBeInTheDocument();
  });
});

describe('authentication', () => {
  it('shows the signup form when signed out', async () => {
    stubApi();
    renderApp();
    expect(await screen.findByRole('button', { name: 'Create account' })).toBeInTheDocument();
  });

  it('blocks submission until the password is long enough', async () => {
    stubApi();
    const user = userEvent.setup();
    renderApp();

    await user.type(await screen.findByLabelText('Email'), 'new@example.com');
    await user.type(screen.getByLabelText('Password'), 'short');

    expect(screen.getByRole('button', { name: 'Create account' })).toBeDisabled();
    expect(screen.getByText(/more characters needed/)).toBeInTheDocument();
  });

  it('restores a session from a stored refresh token', async () => {
    window.localStorage.setItem('apicost.refresh_token', 'stored-refresh');
    stubApi({
      'POST /auth/refresh-token': () =>
        json({
          access_token: 'access',
          refresh_token: 'rotated',
          token_type: 'bearer',
          expires_in: 900,
        }),
    });

    renderApp();

    expect(await screen.findByText('new@example.com')).toBeInTheDocument();
    // The rotated token replaces the one we started with.
    await waitFor(() => {
      expect(window.localStorage.getItem('apicost.refresh_token')).toBe('rotated');
    });
  });

  it('discards a refresh token the server rejects', async () => {
    window.localStorage.setItem('apicost.refresh_token', 'revoked-token');
    stubApi({
      'POST /auth/refresh-token': () =>
        json(
          {
            type: 'about:blank',
            title: 'Unauthorized',
            status: 401,
            detail: 'Invalid refresh token',
          },
          401,
        ),
    });

    renderApp();

    expect(await screen.findByRole('button', { name: 'Create account' })).toBeInTheDocument();
    expect(window.localStorage.getItem('apicost.refresh_token')).toBeNull();
  });

  it('keeps the access token out of localStorage', async () => {
    stubApi();
    const user = userEvent.setup();
    renderApp();

    await user.type(await screen.findByLabelText('Email'), 'new@example.com');
    await user.type(screen.getByLabelText('Password'), 'a-very-long-password');
    await user.click(screen.getByRole('button', { name: 'Create account' }));

    await screen.findByLabelText('API key');
    expect(JSON.stringify(window.localStorage)).not.toContain('access');
  });
});
