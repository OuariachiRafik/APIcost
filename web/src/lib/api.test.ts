import { afterEach, describe, expect, it, vi } from 'vitest';

import { ApiError, apiFetch, apiUrl, getReadiness } from './api';

afterEach(() => {
  vi.unstubAllGlobals();
});

/**
 * Stub fetch with a *factory*, not a fixed Response: a Response body can only
 * be read once, so a test that calls the client twice needs a fresh one each
 * time.
 */
function stubFetch(makeResponse: () => Response) {
  const fetchMock = vi.fn().mockImplementation(async () => makeResponse());
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

describe('apiUrl', () => {
  it('joins paths with and without a leading slash', () => {
    expect(apiUrl('/usage')).toMatch(/\/usage$/);
    expect(apiUrl('usage')).toMatch(/\/usage$/);
  });
});

describe('apiFetch', () => {
  it('returns the parsed body on success', async () => {
    stubFetch(
      () =>
        new Response(JSON.stringify({ status: 'ready', service: 'api', checks: {} }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
    );

    await expect(getReadiness()).resolves.toEqual({
      status: 'ready',
      service: 'api',
      checks: {},
    });
  });

  it('throws ApiError carrying the problem document and request id', async () => {
    stubFetch(
      () =>
        new Response(
          JSON.stringify({
            type: 'about:blank',
            title: 'Not Found',
            status: 404,
            detail: 'project not found',
          }),
          {
            status: 404,
            headers: {
              'Content-Type': 'application/problem+json',
              'X-Request-Id': '01JREQUESTID0000000000000',
            },
          },
        ),
    );

    await expect(apiFetch('/projects/9')).rejects.toThrowError(ApiError);

    try {
      await apiFetch('/projects/9');
    } catch (error) {
      const apiError = error as ApiError;
      expect(apiError.problem.status).toBe(404);
      expect(apiError.requestId).toBe('01JREQUESTID0000000000000');
      expect(apiError.message).toBe('project not found');
    }
  });

  it('synthesizes a problem document when the body is not JSON', async () => {
    stubFetch(() => new Response('<html>502</html>', { status: 502, statusText: 'Bad Gateway' }));

    await expect(apiFetch('/usage')).rejects.toMatchObject({
      problem: { status: 502, title: 'Bad Gateway' },
    });
  });
});
