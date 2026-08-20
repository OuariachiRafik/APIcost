/**
 * The authenticated shell — APICOST_FRONTEND_SPEC §3.1.
 *
 * Left sidebar with the wordmark, a global project switcher, nav, and the
 * account menu; a header carrying the shared time-range control (§3.2).
 * Billing sits below a divider at the bottom, de-emphasised, because it is
 * read-only in v1.
 */
import { NavLink, Outlet } from 'react-router-dom';

import { useAuth } from '../lib/authContext';
import { RANGE_LABELS, useWorkspace, type TimeRange } from '../lib/uiContext';
import { Toaster } from './Toaster';

const NAV = [
  { to: '/', label: 'Overview', end: true },
  { to: '/cache', label: 'Cache' },
  { to: '/routing', label: 'Routing' },
  { to: '/budgets', label: 'Budgets' },
  { to: '/alerts', label: 'Alerts' },
  { to: '/advisor', label: 'Advisor' },
  { to: '/settings', label: 'Settings' },
];

const RANGES: TimeRange[] = ['today', '7d', '30d', '90d'];

export function Shell() {
  const { user, logout } = useAuth();
  const { projects, projectId, setProjectId, range, setRange } = useWorkspace();

  return (
    <div className="flex min-h-screen bg-page">
      {/* Desktop-only (§4.5): below tablet width the app shows a notice rather
          than a broken layout, and no responsive work is spent here. */}
      <aside className="hidden w-56 shrink-0 flex-col border-r border-edge bg-surface md:flex">
        <div className="px-4 py-4">
          <span className="font-semibold tracking-tight text-ink">APICost</span>
        </div>

        <div className="px-3 pb-3">
          <label htmlFor="project-switcher" className="sr-only">
            Project
          </label>
          <select
            id="project-switcher"
            value={projectId ?? ''}
            onChange={(event) => setProjectId(event.target.value)}
            disabled={projects.length === 0}
            className="w-full rounded-md border border-edge bg-page px-2 py-1.5 text-xs
                       text-ink focus:border-info focus:outline-none disabled:opacity-40"
          >
            {projects.length === 0 && <option value="">No projects</option>}
            {projects.map((project) => (
              <option key={project.id} value={project.id}>
                {project.name}
              </option>
            ))}
          </select>
        </div>

        <nav className="flex flex-1 flex-col gap-0.5 px-2">
          {NAV.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.end}
              className={({ isActive }) =>
                `rounded px-2 py-1.5 text-sm transition-colors ${
                  isActive ? 'bg-page font-medium text-ink' : 'text-muted hover:text-ink'
                }`
              }
            >
              {item.label}
            </NavLink>
          ))}

          <div className="mt-auto border-t border-edge pt-2 pb-2">
            <NavLink
              to="/billing"
              className={({ isActive }) =>
                `block rounded px-2 py-1.5 text-xs transition-colors ${
                  isActive ? 'bg-page text-ink' : 'text-muted hover:text-ink'
                }`
              }
            >
              Billing
            </NavLink>
          </div>
        </nav>

        <div className="border-t border-edge px-3 py-3">
          <p className="truncate text-[11px] text-muted" title={user?.email}>
            {user?.email}
          </p>
          <button
            type="button"
            onClick={() => void logout()}
            className="mt-1 text-xs text-muted hover:text-ink"
          >
            Sign out
          </button>
        </div>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex items-center justify-between gap-4 border-b border-edge px-6 py-3">
          <div className="md:hidden">
            <span className="font-semibold text-ink">APICost</span>
          </div>
          <div className="ml-auto flex items-center gap-1 rounded-md border border-edge p-0.5">
            {RANGES.map((option) => (
              <button
                key={option}
                type="button"
                onClick={() => setRange(option)}
                className={`rounded px-2 py-1 text-xs transition-colors ${
                  range === option ? 'bg-surface text-ink' : 'text-muted hover:text-ink'
                }`}
              >
                {RANGE_LABELS[option]}
              </button>
            ))}
          </div>
        </header>

        <p className="border-b border-warning/40 bg-warning/10 px-6 py-2 text-xs text-ink md:hidden">
          APICost is built for a desktop screen. This dashboard is data-dense by design and is not
          laid out for phones.
        </p>

        <main className="min-w-0 flex-1 px-6 py-6">
          <Outlet />
        </main>
      </div>

      <Toaster />
    </div>
  );
}
