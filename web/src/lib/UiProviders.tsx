/** Providers for workspace state and toasts. See `uiContext.ts` for the split. */
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { useQuery } from '@tanstack/react-query';

import { listProjects } from './api';
import { useAuth } from './authContext';
import {
  ToastContext,
  WorkspaceContext,
  type Toast,
  type TimeRange,
  type ToastState,
} from './uiContext';

const PROJECT_STORAGE_KEY = 'apicost.project_id';
const RANGE_STORAGE_KEY = 'apicost.range';

function readStored(key: string): string | null {
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
}

export function WorkspaceProvider({ children }: { children: ReactNode }) {
  const { accessToken } = useAuth();
  const [selected, setSelected] = useState<string | null>(() => readStored(PROJECT_STORAGE_KEY));
  const [range, setRangeState] = useState<TimeRange>(
    () => (readStored(RANGE_STORAGE_KEY) as TimeRange) ?? '30d',
  );

  const projectsQuery = useQuery({
    queryKey: ['projects'],
    queryFn: () => listProjects(accessToken ?? ''),
    enabled: Boolean(accessToken),
  });

  const projects = useMemo(() => projectsQuery.data ?? [], [projectsQuery.data]);

  // A stored id can outlive the project it names — a deleted project, or a
  // different account on the same browser. Falling back to the first project
  // beats every screen 404ing with no way to recover from the UI.
  const projectId = useMemo(() => {
    if (projects.length === 0) return null;
    if (selected && projects.some((p) => p.id === selected)) return selected;
    return projects[0]?.id ?? null;
  }, [projects, selected]);

  const setProjectId = useCallback((id: string) => {
    setSelected(id);
    try {
      window.localStorage.setItem(PROJECT_STORAGE_KEY, id);
    } catch {
      /* storage unavailable — selection still works for this session */
    }
  }, []);

  const setRange = useCallback((next: TimeRange) => {
    setRangeState(next);
    try {
      window.localStorage.setItem(RANGE_STORAGE_KEY, next);
    } catch {
      /* as above */
    }
  }, []);

  const value = useMemo(
    () => ({
      projects,
      project: projects.find((p) => p.id === projectId) ?? null,
      projectId,
      setProjectId,
      range,
      setRange,
      isLoading: projectsQuery.isLoading,
      refetchProjects: () => void projectsQuery.refetch(),
    }),
    [projects, projectId, setProjectId, range, setRange, projectsQuery],
  );

  return <WorkspaceContext.Provider value={value}>{children}</WorkspaceContext.Provider>;
}

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const nextId = useRef(1);
  const timers = useRef(new Map<number, number>());

  const dismiss = useCallback((id: number) => {
    setToasts((current) => current.filter((toast) => toast.id !== id));
    const timer = timers.current.get(id);
    if (timer !== undefined) {
      window.clearTimeout(timer);
      timers.current.delete(id);
    }
  }, []);

  const push = useCallback<ToastState['push']>(
    ({ durationMs = 4000, ...toast }) => {
      const id = nextId.current++;
      setToasts((current) => [...current, { ...toast, id }]);
      timers.current.set(
        id,
        window.setTimeout(() => dismiss(id), durationMs),
      );
    },
    [dismiss],
  );

  // Clearing on unmount stops a timer from calling setState after teardown,
  // which React logs as an update on an unmounted component.
  useEffect(() => {
    const pending = timers.current;
    return () => {
      pending.forEach((timer) => window.clearTimeout(timer));
      pending.clear();
    };
  }, []);

  const value = useMemo(() => ({ toasts, push, dismiss }), [toasts, push, dismiss]);

  return <ToastContext.Provider value={value}>{children}</ToastContext.Provider>;
}
