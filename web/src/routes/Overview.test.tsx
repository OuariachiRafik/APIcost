/**
 * Overview is where the product's central claim is displayed, so the tests
 * here are about the money being right rather than the layout being present.
 *
 * The specific risk: `cost_usd` and every savings field arrive as JSON
 * **strings** (Pydantic Decimal). `"12.40" + "6.10"` is `"12.406.10"` in
 * JavaScript, and it would render without complaint.
 */
import { screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { json, renderScreen, stubApi } from '../lib/testing';
import { Overview } from './Overview';

function usage(overrides: Record<string, unknown> = {}) {
  return json({
    range: '30d',
    start: '2026-07-18T00:00:00Z',
    end: '2026-08-17T00:00:00Z',
    bucket: 'day',
    summary: {
      total_cost_usd: '31.550000',
      total_would_have_been_usd: '50.050000',
      total_requests: 4200,
      total_tokens_in: 1_000_000,
      total_tokens_out: 400_000,
      cache_hits: 1300,
      cache_hit_rate: 0.31,
      cache_savings_usd: '12.400000',
      routing_savings_usd: '6.100000',
      ...(overrides.summary as object),
    },
    series: [
      {
        bucket: '2026-08-16T00:00:00Z',
        cost_usd: '1.500000',
        cost_would_have_been_usd: '2.500000',
        requests: 100,
        cache_hits: 30,
        tokens_in: 1000,
        tokens_out: 400,
      },
    ],
  });
}

function baseRoutes(extra: Record<string, (body: unknown) => Response> = {}) {
  return {
    'GET /usage': () => usage(),
    'GET /usage/breakdown': () => json({ by: 'model', start: '', end: '', rows: [] }),
    'GET /requests': () => json({ rows: [], next_cursor: null, has_more: false }),
    ...extra,
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
  window.localStorage.clear();
});

describe('overview', () => {
  it('adds savings numerically, not by string concatenation', async () => {
    stubApi(baseRoutes());
    renderScreen(<Overview />);

    // 12.40 + 6.10 = 18.50. String concatenation would give "12.4000006.100000".
    expect(await screen.findByText('$18.50')).toBeInTheDocument();
    expect(screen.queryByText(/12\.4000006/)).not.toBeInTheDocument();
  });

  it('reports cache and routing savings separately', async () => {
    stubApi(baseRoutes());
    renderScreen(<Overview />);

    expect(await screen.findByText('$12.40')).toBeInTheDocument();
    expect(screen.getByText('$6.10')).toBeInTheDocument();
  });

  it('shows a negative routing week as negative rather than hiding it', async () => {
    stubApi(
      baseRoutes({
        'GET /usage': () => usage({ summary: { routing_savings_usd: '-3.200000' } }),
      }),
    );
    renderScreen(<Overview />);

    expect(await screen.findByText('-$3.20')).toBeInTheDocument();
  });

  it('tells a new user what to do instead of showing an empty chart', async () => {
    stubApi(
      baseRoutes({
        'GET /usage': () =>
          usage({
            summary: {
              total_requests: 0,
              total_cost_usd: '0',
              cache_savings_usd: '0',
              routing_savings_usd: '0',
              total_would_have_been_usd: '0',
              cache_hits: 0,
              cache_hit_rate: 0,
            },
          }),
      }),
    );
    renderScreen(<Overview />);

    expect(await screen.findByText('No requests yet')).toBeInTheDocument();
    expect(screen.getByText(/Point your app at your proxy key/)).toBeInTheDocument();
  });
});
