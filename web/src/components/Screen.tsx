/**
 * Page scaffolding shared by every screen.
 *
 * Almost every endpoint is project-scoped, so "no project selected" is a real
 * state on eight screens rather than an edge case on one. Handling it here
 * keeps each screen about its own subject.
 */
import type { ReactNode } from 'react';

import { useWorkspace } from '../lib/uiContext';
import { Button, Card, EmptyState, Spinner } from './ui';

export function ScreenHeader({
  title,
  description,
  action,
}: {
  title: string;
  description?: string;
  action?: ReactNode;
}) {
  return (
    <header className="mb-5 flex items-end justify-between gap-4">
      <div>
        <h1 className="text-lg font-semibold text-ink">{title}</h1>
        {description && <p className="mt-0.5 text-xs text-muted">{description}</p>}
      </div>
      {action}
    </header>
  );
}

/** Renders `children` only once a project is selected. */
export function RequireProject({ children }: { children: (projectId: string) => ReactNode }) {
  const { projectId, isLoading } = useWorkspace();

  if (isLoading) return <Spinner />;

  if (!projectId) {
    return (
      <Card>
        <EmptyState
          title="No project yet"
          action={
            <Button onClick={() => (window.location.href = '/settings')}>Create a project</Button>
          }
        >
          Everything in APICost is scoped to a project. Create one in Settings and issue a proxy key
          to start seeing data here.
        </EmptyState>
      </Card>
    );
  }

  return <>{children(projectId)}</>;
}

export function SectionTitle({ children, hint }: { children: ReactNode; hint?: string }) {
  return (
    <div className="mb-2">
      <h2 className="text-sm font-semibold text-ink">{children}</h2>
      {hint && <p className="mt-0.5 text-[11px] text-muted">{hint}</p>}
    </div>
  );
}
