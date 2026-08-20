/** Transient action-level feedback — §4.3. Page-level failures use ErrorBanner. */
import { useToast } from '../lib/uiContext';

const TONE_BORDER = {
  success: 'border-positive/40',
  error: 'border-critical/40',
  info: 'border-info/40',
} as const;

export function Toaster() {
  const { toasts, dismiss } = useToast();
  if (toasts.length === 0) return null;

  return (
    <div
      className="fixed right-4 bottom-4 z-50 flex w-80 flex-col gap-2"
      role="status"
      aria-live="polite"
    >
      {toasts.map((toast) => (
        <div
          key={toast.id}
          className={`rounded-md border bg-surface px-3 py-2 shadow-lg ${TONE_BORDER[toast.tone]}`}
        >
          <div className="flex items-start justify-between gap-3">
            <p className="text-xs text-ink">{toast.message}</p>
            <button
              type="button"
              aria-label="Dismiss"
              onClick={() => dismiss(toast.id)}
              className="text-muted hover:text-ink"
            >
              ×
            </button>
          </div>
          {toast.action && (
            <button
              type="button"
              onClick={() => {
                toast.action?.onAction();
                dismiss(toast.id);
              }}
              className="mt-1 text-xs font-medium text-info hover:underline"
            >
              {toast.action.label}
            </button>
          )}
        </div>
      ))}
    </div>
  );
}
