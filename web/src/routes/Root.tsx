/**
 * Application shell.
 *
 * Signed out: the auth form. Signed in: a nav shell wrapping whichever route
 * is active. Onboarding stays the landing page until there is traffic, since
 * a dashboard with no data teaches a new user nothing.
 */
import { NavLink, Outlet } from 'react-router-dom';

import { Button } from '../components/ui';
import { useAuth } from '../lib/authContext';
import { AuthForm } from './AuthForm';

const NAV = [
  { to: '/', label: 'Overview', end: true },
  { to: '/requests', label: 'Requests' },
  { to: '/setup', label: 'Setup' },
];

export function Root() {
  const { user, isLoading, logout } = useAuth();

  if (isLoading) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-slate-50">
        <p className="text-sm text-slate-500">Loading…</p>
      </div>
    );
  }

  if (!user) {
    return (
      <div className="min-h-screen bg-slate-50">
        <main className="mx-auto max-w-lg px-6 py-16">
          <div className="mb-8 text-center">
            <h1 className="text-xl font-semibold text-slate-900">APICost</h1>
            <p className="text-sm text-slate-500">Spend less on the same LLM calls.</p>
          </div>
          <AuthForm />
        </main>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-slate-50">
      <header className="border-b border-slate-200 bg-white">
        <div className="mx-auto flex max-w-6xl items-center justify-between px-6 py-3">
          <div className="flex items-center gap-8">
            <h1 className="text-base font-semibold text-slate-900">APICost</h1>
            <nav className="flex gap-1">
              {NAV.map((item) => (
                <NavLink
                  key={item.to}
                  to={item.to}
                  end={item.end}
                  className={({ isActive }) =>
                    `rounded px-3 py-1.5 text-sm ${
                      isActive
                        ? 'bg-slate-100 font-medium text-slate-900'
                        : 'text-slate-600 hover:bg-slate-50'
                    }`
                  }
                >
                  {item.label}
                </NavLink>
              ))}
            </nav>
          </div>

          <div className="flex items-center gap-3">
            <span className="hidden text-sm text-slate-600 sm:inline">{user.email}</span>
            <Button variant="secondary" onClick={() => void logout()}>
              Sign out
            </Button>
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-6xl px-6 py-8">
        <Outlet />
      </main>
    </div>
  );
}
