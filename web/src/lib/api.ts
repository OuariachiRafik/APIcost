/**
 * Typed client for the dashboard API.
 *
 * From P1 this file is generated from the API's OpenAPI schema, which
 * BUILD_SPEC §8 makes the source of truth. Until there are endpoints to
 * generate from, it holds the fetch wrapper and the health types by hand.
 */

const API_BASE_URL: string = import.meta.env.VITE_API_BASE_URL ?? 'http://localhost:8001';

export interface HealthResponse {
  status: string;
  service: string;
}

export interface ReadinessResponse {
  status: 'ready' | 'not_ready';
  service: string;
  checks: Record<string, boolean>;
}

/** RFC 7807 problem document — the error shape every endpoint returns (§8). */
export interface ProblemDetail {
  type: string;
  title: string;
  status: number;
  detail: string;
  instance?: string;
  request_id?: string;
}

export class ApiError extends Error {
  constructor(
    readonly problem: ProblemDetail,
    readonly requestId?: string,
  ) {
    super(problem.detail || problem.title);
    this.name = 'ApiError';
  }
}

export function apiUrl(path: string): string {
  return `${API_BASE_URL}${path.startsWith('/') ? path : `/${path}`}`;
}

export async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(apiUrl(path), {
    ...init,
    headers: { Accept: 'application/json', ...init?.headers },
  });

  if (!response.ok) {
    const requestId = response.headers.get('X-Request-Id') ?? undefined;
    let problem: ProblemDetail;
    try {
      problem = (await response.json()) as ProblemDetail;
    } catch {
      problem = {
        type: 'about:blank',
        title: response.statusText,
        status: response.status,
        detail: response.statusText,
      };
    }
    throw new ApiError(problem, requestId);
  }

  return (await response.json()) as T;
}

export const getHealth = (): Promise<HealthResponse> => apiFetch<HealthResponse>('/healthz');

export const getReadiness = (): Promise<ReadinessResponse> =>
  apiFetch<ReadinessResponse>('/readyz');
