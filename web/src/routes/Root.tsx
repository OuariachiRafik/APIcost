/**
 * Application shell.
 *
 * P1 has exactly two states: signed out (auth form) and signed in (onboarding).
 * That does not warrant a router, and BUILD_SPEC §2 does not list one in the
 * stack, so there is none. P3 introduces real navigation across the dashboard
 * routes and is the right moment to decide on one.
 */
import { AuthForm } from './AuthForm';
import { Onboarding } from './Onboarding';
import { Button } from '../components/ui';
import { useAuth } from '../lib/authContext';

export function Root() {
  const { user, isLoading, logout } = useAuth();

  return (
    <div className="min-h-screen bg-slate-50">
      <header className="border-b border-slate-200 bg-white">
        <div className="mx-auto flex max-w-3xl items-center justify-between px-6 py-4">
          <div>
            <h1 className="text-base font-semibold text-slate-900">APICost</h1>
            <p className="text-xs text-slate-500">Spend less on the same LLM calls.</p>
          </div>
          {user && (
            <div className="flex items-center gap-3">
              <span className="text-sm text-slate-600">{user.email}</span>
              <Button variant="secondary" onClick={() => void logout()}>
                Sign out
              </Button>
            </div>
          )}
        </div>
      </header>

      <main className="mx-auto max-w-3xl px-6 py-10">
        {isLoading ? (
          <p className="text-sm text-slate-500">Loading…</p>
        ) : user ? (
          <Onboarding />
        ) : (
          <AuthForm />
        )}
      </main>
    </div>
  );
}
