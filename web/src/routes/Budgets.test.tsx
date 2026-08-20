/**
 * The hard-stop confirmation, and the guarantee that it fires *before* the
 * change reaches the API rather than after.
 *
 * `hard_stop` is the one setting in this product that can stop a user's
 * application. A confirmation shown after the fact is an explanation, not a
 * choice, so the ordering is the thing worth pinning.
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { AuthProvider } from '../lib/auth';
import { ToastProvider, WorkspaceProvider } from '../lib/UiProviders';
import { Budgets } from './Budgets';

const PROJECT = { id: '01JPROJ', name: 'production', created_at: '2026-08-05T00:00:00Z' };

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function budget(overrides: Record<string, unknown> = {}) {
  return {
    id: '01JBUDGET',
    project_id: PROJECT.id,
    period: 'daily',
    limit_usd: '25.000000',
    action: 'alert_only',
    is_active: true,
    spent_usd: 5,
    fraction_used: 0.2,
    created_at: '2026-08-05T00:00:00Z',
    ...overrides,
  };
}

let calls: { key: string; body: unknown }[] = [];

function stubApi(budgets: unknown[]) {
  calls = [];
  const fetchMock = vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
    const url = new URL(String(input));
    const key = `${init?.method ?? 'GET'} ${url.pathname}`;
    calls.push({ key, body: init?.body ? JSON.parse(String(init.body)) : null });

    // AuthProvider trades the stored refresh token for a session on mount;
    // without this every query stays disabled and the screen never renders.
    if (key === 'POST /auth/refresh-token')
      return json({
        access_token: 'access',
        refresh_token: 'refresh',
        token_type: 'bearer',
        expires_in: 900,
      });
    if (key === 'GET /auth/me')
      return json({
        id: '01JUSER',
        email: 'dev@example.com',
        timezone: 'UTC',
        plan_id: 'free',
        created_at: '2026-08-05T00:00:00Z',
      });
    if (key === 'GET /projects') return json([PROJECT]);
    if (key === 'GET /budgets') return json(budgets);
    if (key.startsWith('PATCH /budgets') || key.startsWith('POST /budgets'))
      return json(budget({ action: 'hard_stop' }));
    throw new Error(`unstubbed request: ${key}`);
  });
  vi.stubGlobal('fetch', fetchMock);
}

function renderBudgets() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <AuthProvider>
        <WorkspaceProvider>
          <ToastProvider>
            <Budgets />
          </ToastProvider>
        </WorkspaceProvider>
      </AuthProvider>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  window.localStorage.setItem('apicost.refresh_token', 'refresh');
});

afterEach(() => {
  vi.unstubAllGlobals();
  window.localStorage.clear();
});

describe('budgets', () => {
  it('shows all three periods even when none are set', async () => {
    stubApi([]);
    renderBudgets();

    expect(await screen.findByText('Daily')).toBeInTheDocument();
    expect(screen.getByText('Weekly')).toBeInTheDocument();
    expect(screen.getByText('Monthly')).toBeInTheDocument();
  });

  it('explains what hard_stop does before applying it', async () => {
    const user = userEvent.setup();
    stubApi([budget()]);
    renderBudgets();

    await screen.findByText('Daily');
    const [select] = await screen.findAllByLabelText('When exceeded');
    await user.selectOptions(select!, 'hard_stop');

    // The modal is up and nothing has been sent yet.
    expect(await screen.findByText(/refused with/i)).toBeInTheDocument();
    expect(calls.some((call) => call.key.startsWith('PATCH'))).toBe(false);

    await user.click(screen.getByRole('button', { name: 'Enable hard stop' }));

    await waitFor(() => {
      const patch = calls.find((call) => call.key.startsWith('PATCH /budgets'));
      expect(patch?.body).toMatchObject({ action: 'hard_stop' });
    });
  });

  it('does not apply hard_stop when the confirmation is cancelled', async () => {
    const user = userEvent.setup();
    stubApi([budget()]);
    renderBudgets();

    await screen.findByText('Daily');
    const [select] = await screen.findAllByLabelText('When exceeded');
    await user.selectOptions(select!, 'hard_stop');

    await screen.findByText(/refused with/i);
    await user.click(screen.getByRole('button', { name: 'Cancel' }));

    expect(calls.some((call) => call.key.startsWith('PATCH'))).toBe(false);
  });

  it('applies the harmless actions without a confirmation', async () => {
    const user = userEvent.setup();
    stubApi([budget({ action: 'alert_only' })]);
    renderBudgets();

    await screen.findByText('Daily');
    const [select] = await screen.findAllByLabelText('When exceeded');
    await user.selectOptions(select!, 'soft_throttle');

    await waitFor(() => {
      const patch = calls.find((call) => call.key.startsWith('PATCH /budgets'));
      expect(patch?.body).toMatchObject({ action: 'soft_throttle' });
    });
  });
});
