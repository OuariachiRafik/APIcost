import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import type { ReactNode } from 'react';
import { afterEach, expect, it, vi } from 'vitest';

import { Root } from './Root';

afterEach(() => {
  vi.unstubAllGlobals();
});

function withQueryClient(ui: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={client}>{ui}</QueryClientProvider>;
}

it('renders the dependency checks reported by /readyz', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          status: 'ready',
          service: 'api',
          checks: { postgres: true, redis: true },
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    ),
  );

  render(withQueryClient(<Root />));

  expect(await screen.findByText(/postgres:/)).toBeInTheDocument();
  expect(await screen.findByText(/redis:/)).toBeInTheDocument();
});

it('shows an actionable message when the API is unreachable', async () => {
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')));

  render(withQueryClient(<Root />));

  expect(await screen.findByRole('alert')).toHaveTextContent(/unreachable/i);
});
