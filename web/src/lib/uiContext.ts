/**
 * Cross-screen UI state: selected project, selected time range, toasts.
 *
 * Contexts and hooks live here, providers in `UiProviders.tsx`. That split is
 * the same one `auth.tsx` / `authContext.ts` uses and it exists for
 * react-refresh: a module exporting both a component and a hook loses fast
 * refresh for everything that imports it.
 */
import { createContext, useContext } from 'react';

import type { Project } from './api';

/** §3.2 — one shared control for every time series on a screen. */
export type TimeRange = 'today' | '7d' | '30d' | '90d';

export const RANGE_LABELS: Record<TimeRange, string> = {
  today: 'Today',
  '7d': '7 days',
  '30d': '30 days',
  '90d': '90 days',
};

export interface WorkspaceState {
  projects: Project[];
  project: Project | null;
  projectId: string | null;
  setProjectId: (id: string) => void;
  range: TimeRange;
  setRange: (range: TimeRange) => void;
  isLoading: boolean;
  refetchProjects: () => void;
}

export const WorkspaceContext = createContext<WorkspaceState | null>(null);

export function useWorkspace(): WorkspaceState {
  const context = useContext(WorkspaceContext);
  if (context === null) throw new Error('useWorkspace must be used inside a WorkspaceProvider');
  return context;
}

export type ToastTone = 'success' | 'error' | 'info';

export interface Toast {
  id: number;
  tone: ToastTone;
  message: string;
  /** Renders an action button inside the toast — used for undo (§5.7). */
  action?: { label: string; onAction: () => void };
}

export interface ToastState {
  toasts: Toast[];
  push: (toast: Omit<Toast, 'id'> & { durationMs?: number }) => void;
  dismiss: (id: number) => void;
}

export const ToastContext = createContext<ToastState | null>(null);

export function useToast(): ToastState {
  const context = useContext(ToastContext);
  if (context === null) throw new Error('useToast must be used inside a ToastProvider');
  return context;
}
