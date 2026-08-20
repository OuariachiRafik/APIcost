/**
 * The kill switch. It revokes every proxy key for a project in under a second
 * and they cannot be un-revoked — so the only thing worth testing here is that
 * it is hard to fire by accident.
 */
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { json, renderScreen, stubApi, TEST_PROJECT } from '../lib/testing';
import { Alerts } from './Alerts';

function alertsResponse(alerts: unknown[] = []) {
  return json({ alerts, next_cursor: null });
}

afterEach(() => {
  vi.unstubAllGlobals();
  window.localStorage.clear();
});

describe('kill switch', () => {
  it('stays disabled until the project name is typed exactly', async () => {
    const user = userEvent.setup();
    const calls = stubApi({ 'GET /alert-events': () => alertsResponse() });
    renderScreen(<Alerts />);

    await user.click(await screen.findByRole('button', { name: 'Kill access' }));

    const confirm = await screen.findByRole('button', { name: 'Revoke all keys' });
    expect(confirm).toBeDisabled();

    await user.type(screen.getByLabelText(/Type/), 'wrong-name');
    expect(confirm).toBeDisabled();

    await user.clear(screen.getByLabelText(/Type/));
    await user.type(screen.getByLabelText(/Type/), TEST_PROJECT.name);
    expect(confirm).toBeEnabled();

    // Nothing has been sent while the user was typing.
    expect(calls.some((call) => call.key.includes('/kill'))).toBe(false);
  });

  it('revokes only after an exact confirmation', async () => {
    const user = userEvent.setup();
    const calls = stubApi({
      'GET /alert-events': () => alertsResponse(),
      [`POST /projects/${TEST_PROJECT.id}/kill`]: () =>
        json({ project_id: TEST_PROJECT.id, keys_revoked: 2, took_ms: 41, alert_id: '01JA' }),
    });
    renderScreen(<Alerts />);

    await user.click(await screen.findByRole('button', { name: 'Kill access' }));
    await user.type(screen.getByLabelText(/Type/), TEST_PROJECT.name);
    await user.click(screen.getByRole('button', { name: 'Revoke all keys' }));

    await waitFor(() => {
      expect(calls.some((call) => call.key === `POST /projects/${TEST_PROJECT.id}/kill`)).toBe(
        true,
      );
    });
    expect(await screen.findByText(/Revoked 2 proxy keys/)).toBeInTheDocument();
  });

  it('says plainly that provider keys and history survive', async () => {
    stubApi({ 'GET /alert-events': () => alertsResponse() });
    renderScreen(<Alerts />);

    expect(
      await screen.findByText(/provider keys, projects and history are untouched/i),
    ).toBeInTheDocument();
  });
});

describe('alert history', () => {
  it('shows severity and lets an alert be resolved with a note', async () => {
    const user = userEvent.setup();
    const calls = stubApi({
      'GET /alert-events': () =>
        alertsResponse([
          {
            id: '01JALERT',
            project_id: TEST_PROJECT.id,
            alert_type: 'spend_spike',
            severity: 'critical',
            title: 'Spend spike on production',
            detail: { times_normal: '34.3x' },
            status: 'open',
            notified_at: null,
            resolved_at: null,
            resolution: null,
            created_at: '2026-08-17T10:00:00Z',
          },
        ]),
      'POST /alert-events/01JALERT/resolve': () => json({}),
    });
    renderScreen(<Alerts />);

    expect(await screen.findByText('Spend spike on production')).toBeInTheDocument();
    expect(screen.getByText('critical')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Resolve' }));
    await user.type(await screen.findByLabelText(/What did you do about it/), 'Our own load test.');
    await user.click(screen.getByRole('button', { name: 'Mark resolved' }));

    await waitFor(() => {
      const resolve = calls.find((call) => call.key.includes('/resolve'));
      expect(resolve?.body).toMatchObject({
        status: 'resolved',
        resolution: 'Our own load test.',
      });
    });
  });
});
