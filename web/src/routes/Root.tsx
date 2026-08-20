/**
 * Application root.
 *
 * Signed out: the auth form on a bare page. Signed in: the full shell (§3.1),
 * unless the account has no project yet — a new user is sent to onboarding,
 * because a dashboard with nothing in it teaches them nothing about what this
 * product does.
 */
import { Navigate, useLocation } from 'react-router-dom';

import { Shell } from '../components/Shell';
import { Spinner } from '../components/ui';
import { useAuth } from '../lib/authContext';
import { useWorkspace } from '../lib/uiContext';
import { AuthForm } from './AuthForm';

export function Root() {
  const { user, isLoading } = useAuth();
  const workspace = useWorkspace();
  const location = useLocation();

  if (isLoading) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-page">
        <Spinner />
      </div>
    );
  }

  if (!user) {
    return (
      <div className="min-h-screen bg-page">
        <main className="mx-auto max-w-md px-6 py-24">
          <div className="mb-8">
            <h1 className="text-lg font-semibold tracking-tight text-ink">APICost</h1>
            <p className="mt-1 text-xs text-muted">
              See exactly where your API money goes, then spend less of it.
            </p>
          </div>
          <AuthForm />
        </main>
      </div>
    );
  }

  const needsSetup =
    !workspace.isLoading && workspace.projects.length === 0 && location.pathname !== '/setup';

  if (needsSetup) return <Navigate to="/setup" replace />;

  return <Shell />;
}
