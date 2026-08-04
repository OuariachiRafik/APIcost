/** Auth context and its consumer hook, split from the provider component. */
import { createContext, useContext } from 'react';

import { ApiError, type User } from './api';

export interface AuthState {
  user: User | null;
  accessToken: string | null;
  isLoading: boolean;
  signup: (email: string, password: string) => Promise<void>;
  login: (email: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
}

export const AuthContext = createContext<AuthState | null>(null);

export function useAuth(): AuthState {
  const context = useContext(AuthContext);
  if (context === null) throw new Error('useAuth must be used inside an AuthProvider');
  return context;
}

/** Turn an API failure into something a person can act on. */
export function describeError(error: unknown): string {
  if (error instanceof ApiError) return error.problem.detail || error.problem.title;
  if (error instanceof TypeError) return 'Could not reach the API. Is `make dev` running?';
  if (error instanceof Error) return error.message;
  return 'Something went wrong.';
}
