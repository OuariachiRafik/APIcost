/**
 * Typed client for the dashboard API.
 *
 * From P3 this file is generated from the API's OpenAPI schema, which
 * BUILD_SPEC §8 makes the source of truth. Until the surface stabilises it is
 * maintained by hand, mirroring the response models in `api/routers/`.
 */

const API_BASE_URL: string = import.meta.env.VITE_API_BASE_URL ?? 'http://localhost:8001';

// ---------------------------------------------------------------------------
// Types — these mirror the Pydantic response models exactly.
// ---------------------------------------------------------------------------

export interface HealthResponse {
  status: string;
  service: string;
}

export interface ReadinessResponse {
  status: 'ready' | 'not_ready';
  service: string;
  checks: Record<string, boolean>;
}

export interface TokenPair {
  access_token: string;
  refresh_token: string;
  token_type: string;
  expires_in: number;
}

export interface User {
  id: string;
  email: string;
  timezone: string;
  plan_id: string;
  created_at: string;
}

export type Provider = 'openai' | 'anthropic' | 'gemini';

/** Note what is absent: there is no field here that can carry key material. */
export interface ProviderKey {
  id: string;
  provider: string;
  last4: string;
  is_active: boolean;
  added_at: string;
  last_used_at: string | null;
}

export interface Project {
  id: string;
  name: string;
  created_at: string;
  archived_at: string | null;
  cache_enabled: boolean;
  similarity_threshold: number;
  cache_ttl_seconds: number;
  routing_enabled: boolean;
  escalation_enabled: boolean;
  store_raw_content: boolean;
}

export interface ProxyKey {
  id: string;
  project_id: string;
  name: string | null;
  last4: string;
  created_at: string;
  revoked_at: string | null;
  last_used_at: string | null;
}

/** Only the creation response carries `key`, and only once (UC-05). */
export interface CreatedProxyKey extends ProxyKey {
  key: string;
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

  get status(): number {
    return this.problem.status;
  }
}

// ---------------------------------------------------------------------------
// Transport
// ---------------------------------------------------------------------------

export function apiUrl(path: string): string {
  return `${API_BASE_URL}${path.startsWith('/') ? path : `/${path}`}`;
}

export interface RequestOptions {
  method?: string;
  body?: unknown;
  token?: string;
}

export async function apiFetch<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = 'GET', body, token } = options;

  const headers: Record<string, string> = { Accept: 'application/json' };
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  if (token) headers['Authorization'] = `Bearer ${token}`;

  const response = await fetch(apiUrl(path), {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });

  if (!response.ok) {
    const requestId = response.headers.get('X-Request-Id') ?? undefined;
    let problem: ProblemDetail;
    try {
      problem = (await response.json()) as ProblemDetail;
    } catch {
      problem = {
        type: 'about:blank',
        title: response.statusText || 'Request failed',
        status: response.status,
        detail: response.statusText || 'Request failed',
      };
    }
    throw new ApiError(problem, requestId);
  }

  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

// ---------------------------------------------------------------------------
// Endpoints
// ---------------------------------------------------------------------------

export const getHealth = (): Promise<HealthResponse> => apiFetch<HealthResponse>('/healthz');

export const getReadiness = (): Promise<ReadinessResponse> =>
  apiFetch<ReadinessResponse>('/readyz');

export const signup = (email: string, password: string): Promise<TokenPair> =>
  apiFetch<TokenPair>('/auth/signup', { method: 'POST', body: { email, password } });

export const login = (email: string, password: string): Promise<TokenPair> =>
  apiFetch<TokenPair>('/auth/login', { method: 'POST', body: { email, password } });

export const logout = (refreshToken: string): Promise<void> =>
  apiFetch<void>('/auth/logout', { method: 'POST', body: { refresh_token: refreshToken } });

export const refreshSession = (refreshToken: string): Promise<TokenPair> =>
  apiFetch<TokenPair>('/auth/refresh-token', {
    method: 'POST',
    body: { refresh_token: refreshToken },
  });

export const getMe = (token: string): Promise<User> => apiFetch<User>('/auth/me', { token });

export const addProviderKey = (
  token: string,
  provider: Provider,
  apiKey: string,
): Promise<ProviderKey> =>
  apiFetch<ProviderKey>('/keys', {
    method: 'POST',
    token,
    body: { provider, api_key: apiKey },
  });

export const listProviderKeys = (token: string): Promise<ProviderKey[]> =>
  apiFetch<ProviderKey[]>('/keys', { token });

export const deleteProviderKey = (token: string, keyId: string): Promise<void> =>
  apiFetch<void>(`/keys/${keyId}`, { method: 'DELETE', token });

export const createProject = (token: string, name: string): Promise<Project> =>
  apiFetch<Project>('/projects', { method: 'POST', token, body: { name } });

export const listProjects = (token: string): Promise<Project[]> =>
  apiFetch<Project[]>('/projects', { token });

export const createProxyKey = (
  token: string,
  projectId: string,
  name?: string,
): Promise<CreatedProxyKey> =>
  apiFetch<CreatedProxyKey>(`/projects/${projectId}/proxy-keys`, {
    method: 'POST',
    token,
    body: { name: name ?? null },
  });

export const listProxyKeys = (token: string, projectId: string): Promise<ProxyKey[]> =>
  apiFetch<ProxyKey[]>(`/projects/${projectId}/proxy-keys`, { token });

export const revokeProxyKey = (token: string, keyId: string): Promise<void> =>
  apiFetch<void>(`/proxy-keys/${keyId}`, { method: 'DELETE', token });
