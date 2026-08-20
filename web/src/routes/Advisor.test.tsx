/**
 * Two properties the spec calls out explicitly, both about not misleading:
 *
 * - break-even caveats render inline and always, never behind a click;
 * - dismissing a recommendation is permanent, with a short undo for misclicks.
 */
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { json, renderScreen, stubApi, TEST_PROJECT } from '../lib/testing';
import { Advisor } from './Advisor';

const CAVEATS = [
  'This compares infrastructure cost only. It does not price your time.',
  'A dedicated GPU bills continuously, including while idle.',
  'An open-weights model you host is not the model you are calling today.',
  'You lose the provider availability guarantees.',
  'Cold starts, model loading, and failover capacity are not in this number.',
  'Throughput is assumed at 50% of peak.',
];

function breakeven(recommendation = 'gpu') {
  return json({
    recommendation,
    monthly_tokens: 40_000_000,
    api_monthly_cost_usd: 1200,
    gpu_monthly_cost_usd: 730,
    n_gpus: 1,
    gpu_option: 'A10G (24GB)',
    break_even_tokens: 24_000_000,
    capacity_tokens_per_gpu: 1_314_000_000,
    monthly_saving_usd: 470,
    caveats: CAVEATS,
    options: [],
  });
}

function recommendation(overrides: Record<string, unknown> = {}) {
  return {
    id: '01JREC',
    kind: 'downgrade',
    title: 'Move /v1/chat to gpt-4o-mini',
    detail: { rationale: '300 requests already ran on gpt-4o-mini with no escalations.' },
    projected_savings_usd: 6.3,
    confidence: 'high',
    sample_size: 300,
    status: 'open',
    generated_at: '2026-08-17T03:20:00Z',
    ...overrides,
  };
}

function baseRoutes(extra: Record<string, (body: unknown) => Response> = {}) {
  return {
    'GET /advisor/recommendations': () => json([recommendation()]),
    'GET /advisor/breakeven': () => breakeven(),
    'GET /advisor/prompt-optimizations': () =>
      json({
        warned_requests: 0,
        total_requests: 0,
        warned_fraction: 0,
        estimated_wasted_usd: '0',
        by_endpoint: [],
        token_heavy: [],
      }),
    ...extra,
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
  window.localStorage.clear();
});

describe('break-even advisor', () => {
  it('shows every caveat inline, without a click', async () => {
    stubApi(baseRoutes());
    renderScreen(<Advisor />);

    // No disclosure to open, no accordion to expand — they are simply present.
    for (const caveat of CAVEATS) {
      expect(await screen.findByText(caveat)).toBeInTheDocument();
    }
  });

  it('keeps the caveats when the API is the cheaper option', async () => {
    stubApi(baseRoutes({ 'GET /advisor/breakeven': () => breakeven('api') }));
    renderScreen(<Advisor />);

    expect(await screen.findByText(CAVEATS[0]!)).toBeInTheDocument();
    expect(screen.getByText(/the API is cheaper at your volume/i)).toBeInTheDocument();
  });
});

describe('recommendations', () => {
  it('offers an undo after dismissing, and restores on undo', async () => {
    const user = userEvent.setup();
    const calls = stubApi(
      baseRoutes({ 'POST /advisor/recommendations/01JREC/status': () => json(recommendation()) }),
    );
    renderScreen(<Advisor />);

    await user.click(await screen.findByRole('button', { name: 'Dismiss' }));

    await waitFor(() => {
      const post = calls.find((call) => call.key.includes('/status'));
      expect(post?.body).toMatchObject({ status: 'dismissed' });
    });

    await user.click(await screen.findByRole('button', { name: 'Undo' }));

    await waitFor(() => {
      const restored = calls.filter((call) => call.key.includes('/status'));
      expect(restored.at(-1)?.body).toMatchObject({ status: 'open' });
    });
  });

  it('quotes the evidence behind a recommendation', async () => {
    stubApi(baseRoutes());
    renderScreen(<Advisor />);

    expect(await screen.findByText(/300 requests already ran/)).toBeInTheDocument();
    expect(screen.getByText('high confidence')).toBeInTheDocument();
  });
});

describe('prompt compression', () => {
  it('rejects input that is not JSON without calling the API', async () => {
    const user = userEvent.setup();
    const calls = stubApi(baseRoutes());
    renderScreen(<Advisor />);

    await user.type(await screen.findByLabelText('Request body'), 'not json');
    await user.click(screen.getByRole('button', { name: 'Analyse prompt' }));

    expect(await screen.findByText(/not valid JSON/i)).toBeInTheDocument();
    expect(calls.some((call) => call.key === 'POST /advisor/compress')).toBe(false);
  });

  it('reports the token difference for a compressible prompt', async () => {
    const user = userEvent.setup();
    stubApi(
      baseRoutes({
        'POST /advisor/compress': () =>
          json({
            warn: true,
            reason: 'STALE_HISTORY',
            total_tokens: 2400,
            message_count: 6,
            stale: [{ index: 1, role: 'user', tokens: 600, relevance: 0.01 }],
            reclaimable_tokens: 600,
            reclaimable_fraction: 0.25,
            suggestion: {
              messages: [],
              tokens_before: 2400,
              tokens_after: 1800,
              tokens_saved: 600,
              fraction_saved: 0.25,
              removed_indices: [1],
              strategy: 'drop_stale_messages',
              applied: false,
            },
          }),
      }),
    );
    renderScreen(<Advisor />);

    // `user.type` reads `{` as a key descriptor; paste puts the literal JSON in.
    await user.click(await screen.findByLabelText('Request body'));
    await user.paste('{"messages":[]}');
    await user.click(screen.getByRole('button', { name: 'Analyse prompt' }));

    expect(await screen.findByText('Tokens now')).toBeInTheDocument();
    expect(screen.getByText('After trimming')).toBeInTheDocument();
    expect(screen.getByText(/1 message looked unrelated/)).toBeInTheDocument();
  });
});

describe('project scoping', () => {
  it('asks the API for the selected project', async () => {
    const calls = stubApi(baseRoutes());
    renderScreen(<Advisor />);

    await screen.findByText(/Move \/v1\/chat/);
    expect(calls.some((call) => call.key === 'GET /advisor/recommendations')).toBe(true);
    expect(TEST_PROJECT.id).toBe('01JPROJ');
  });
});
