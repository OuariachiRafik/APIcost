/**
 * Session state.
 *
 * Where the tokens live is a deliberate split:
 *
 * - The **access token** stays in memory only. It is short-lived (15 min) and
 *   never written anywhere a stray script could read it.
 * - The **refresh token** goes to localStorage, so a page reload does not log
 *   the user out.
 *
 * localStorage is readable by any script running on the page, so this trades
 * some XSS exposure for not having to re-authenticate on every reload. The
 * mitigation that actually closes it is httpOnly, SameSite cookies, which needs
 * the API to set cookies and a CSRF strategy to go with them. That is a
 * deliberate follow-up, not an oversight — recorded in
 * docs/adr/0004-spa-token-storage.md.
 */
import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react';

import {
  login as apiLogin,
  logout as apiLogout,
  signup as apiSignup,
  getMe,
  refreshSession,
  type TokenPair,
  type User,
} from './api';
import { AuthContext, type AuthState } from './authContext';

const REFRESH_TOKEN_STORAGE_KEY = 'apicost.refresh_token';

function readStoredRefreshToken(): string | null {
  try {
    return window.localStorage.getItem(REFRESH_TOKEN_STORAGE_KEY);
  } catch {
    return null; // private browsing, storage disabled
  }
}

function storeRefreshToken(token: string | null): void {
  try {
    if (token === null) window.localStorage.removeItem(REFRESH_TOKEN_STORAGE_KEY);
    else window.localStorage.setItem(REFRESH_TOKEN_STORAGE_KEY, token);
  } catch {
    /* non-fatal: the session simply will not survive a reload */
  }
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [accessToken, setAccessToken] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(true);

  const adopt = useCallback(async (tokens: TokenPair) => {
    setAccessToken(tokens.access_token);
    storeRefreshToken(tokens.refresh_token);
    setUser(await getMe(tokens.access_token));
  }, []);

  // On mount, try to trade a stored refresh token for a live session.
  useEffect(() => {
    let cancelled = false;

    (async () => {
      const stored = readStoredRefreshToken();
      if (!stored) {
        setIsLoading(false);
        return;
      }
      try {
        const tokens = await refreshSession(stored);
        if (!cancelled) await adopt(tokens);
      } catch {
        // Expired, revoked, or part of a family we revoked after a replay.
        storeRefreshToken(null);
      } finally {
        if (!cancelled) setIsLoading(false);
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [adopt]);

  const value = useMemo<AuthState>(
    () => ({
      user,
      accessToken,
      isLoading,
      signup: async (email, password) => adopt(await apiSignup(email, password)),
      login: async (email, password) => adopt(await apiLogin(email, password)),
      logout: async () => {
        const stored = readStoredRefreshToken();
        if (stored) {
          // Best effort: the local session ends either way.
          await apiLogout(stored).catch(() => undefined);
        }
        storeRefreshToken(null);
        setAccessToken(null);
        setUser(null);
      },
    }),
    [user, accessToken, isLoading, adopt],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}
