/**
 * Shared primitives — APICOST_FRONTEND_SPEC §2.6.
 *
 * Dark-only (§4.4), data-dense (§2.5): small type, tight rows, compact padding,
 * 1px borders instead of drop shadows. Every numeric value renders in the
 * monospace font with tabular figures (§2.3).
 */
import type { ButtonHTMLAttributes, InputHTMLAttributes, ReactNode } from 'react';
import { useEffect, useId, useRef, useState } from 'react';

export type Tone = 'neutral' | 'positive' | 'warning' | 'critical' | 'info';

const TONE_DOT: Record<Tone, string> = {
  neutral: 'bg-ink',
  positive: 'bg-positive',
  warning: 'bg-warning',
  critical: 'bg-critical',
  info: 'bg-info',
};

const TONE_TEXT: Record<Tone, string> = {
  neutral: 'text-ink',
  positive: 'text-positive',
  warning: 'text-warning',
  critical: 'text-critical',
  info: 'text-info',
};

export function Button({
  children,
  variant = 'primary',
  className = '',
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: 'primary' | 'secondary' | 'danger' | 'ghost';
}) {
  const base =
    'inline-flex items-center justify-center gap-1.5 rounded-md px-3 py-1.5 text-sm font-medium ' +
    'transition-colors disabled:cursor-not-allowed disabled:opacity-40';

  // Primary is inverted — light fill, dark label — per the reference swatch.
  const styles = {
    primary: 'bg-ink text-page hover:bg-ink/85',
    secondary: 'border border-edge bg-transparent text-ink hover:bg-surface',
    danger: 'bg-critical text-white hover:bg-critical/85',
    ghost: 'text-muted hover:bg-surface hover:text-ink',
  }[variant];

  return (
    <button className={`${base} ${styles} ${className}`} {...props}>
      {children}
    </button>
  );
}

/** Navigational text link with the trailing arrow from the reference. */
export function TextLink({
  children,
  onClick,
  href,
}: {
  children: ReactNode;
  onClick?: () => void;
  href?: string;
}) {
  const cls = 'inline-flex items-center gap-1 text-sm text-info hover:underline';
  if (href) {
    return (
      <a className={cls} href={href}>
        {children} <span aria-hidden>→</span>
      </a>
    );
  }
  return (
    <button type="button" className={cls} onClick={onClick}>
      {children} <span aria-hidden>→</span>
    </button>
  );
}

export function Badge({ tone = 'neutral', children }: { tone?: Tone; children: ReactNode }) {
  return (
    <span
      className="inline-flex items-center gap-1.5 rounded-full border border-edge
                 bg-surface px-2 py-0.5 text-xs whitespace-nowrap text-ink"
    >
      <span className={`h-1.5 w-1.5 rounded-full ${TONE_DOT[tone]}`} aria-hidden />
      {children}
    </span>
  );
}

export function Card({
  children,
  title,
  action,
  tone,
  className = '',
}: {
  children: ReactNode;
  title?: ReactNode;
  action?: ReactNode;
  tone?: Tone;
  className?: string;
}) {
  const border = tone === 'critical' ? 'border-critical/40' : 'border-edge';
  return (
    <section className={`rounded-lg border ${border} bg-surface ${className}`}>
      {(title || action) && (
        <header className="flex items-center justify-between gap-3 border-b border-edge px-4 py-3">
          {typeof title === 'string' ? (
            <h2 className="text-sm font-semibold text-ink">{title}</h2>
          ) : (
            title
          )}
          {action}
        </header>
      )}
      <div className="p-4">{children}</div>
    </section>
  );
}

/**
 * Headline monospace number with a label and optional delta pill.
 *
 * `goodDirection` exists because "down" is not universally good: down in spend
 * is a win, down in savings is a loss. Colouring a delta by sign alone would
 * tell the user the opposite of the truth on half the cards in this product.
 */
export function StatCard({
  label,
  value,
  delta,
  goodDirection = 'up',
  hint,
  tone,
}: {
  label: string;
  value: string;
  delta?: number | null;
  goodDirection?: 'up' | 'down';
  hint?: string;
  tone?: Tone;
}) {
  const showDelta = delta !== null && delta !== undefined && Number.isFinite(delta);
  const rising = showDelta && delta > 0;
  const good = showDelta && (goodDirection === 'up' ? delta > 0 : delta < 0);

  return (
    <div className="rounded-lg border border-edge bg-surface p-4">
      <p
        className={`tnum font-mono text-2xl leading-tight font-semibold ${
          tone ? TONE_TEXT[tone] : 'text-ink'
        }`}
      >
        {value}
      </p>
      <div className="mt-1.5 flex items-center gap-2">
        <span className="text-xs text-muted">{label}</span>
        {showDelta && delta !== 0 && (
          <span
            className={`tnum inline-flex items-center gap-0.5 rounded-full px-1.5 py-0.5
                        font-mono text-[11px] ${
                          good ? 'bg-positive/10 text-positive' : 'bg-critical/10 text-critical'
                        }`}
          >
            <span aria-hidden>{rising ? '↑' : '↓'}</span>
            {Math.abs(delta * 100).toFixed(1)}%
          </span>
        )}
      </div>
      {hint && <p className="mt-1 text-[11px] text-muted">{hint}</p>}
    </div>
  );
}

export function Field({
  label,
  hint,
  suffix,
  className = '',
  ...props
}: InputHTMLAttributes<HTMLInputElement> & {
  label: string;
  hint?: string;
  suffix?: ReactNode;
}) {
  const id = useId();
  const hintId = `${id}-hint`;

  // The hint sits outside the <label> and is wired up with aria-describedby.
  // Nesting it would fold it into the input's accessible name, so a screen
  // reader would announce "Password At least 12 characters" as the field name.
  return (
    <div className={className}>
      <label htmlFor={id} className="mb-1 block text-xs font-medium text-muted">
        {label}
      </label>
      <div className="flex items-center gap-2">
        <input
          id={id}
          aria-describedby={hint ? hintId : undefined}
          className="tnum w-full rounded-md border border-edge bg-page px-3 py-1.5 font-mono
                     text-sm text-ink placeholder:text-muted/60 focus:border-info focus:outline-none"
          {...props}
        />
        {suffix}
      </div>
      {hint && (
        <span id={hintId} className="mt-1 block text-[11px] text-muted">
          {hint}
        </span>
      )}
    </div>
  );
}

export function Select({
  label,
  hint,
  children,
  value,
  onChange,
  disabled,
}: {
  label?: string;
  hint?: string;
  children: ReactNode;
  value: string;
  onChange: (value: string) => void;
  disabled?: boolean;
}) {
  const id = useId();
  return (
    <div>
      {label && (
        <label htmlFor={id} className="mb-1 block text-xs font-medium text-muted">
          {label}
        </label>
      )}
      <select
        id={id}
        value={value}
        disabled={disabled}
        onChange={(event) => onChange(event.target.value)}
        className="w-full rounded-md border border-edge bg-page px-3 py-1.5 text-sm text-ink
                   focus:border-info focus:outline-none disabled:opacity-40"
      >
        {children}
      </select>
      {hint && <span className="mt-1 block text-[11px] text-muted">{hint}</span>}
    </div>
  );
}

export function Toggle({
  checked,
  onChange,
  label,
  description,
  disabled,
  size = 'md',
}: {
  checked: boolean;
  onChange: (next: boolean) => void;
  label: string;
  description?: string;
  disabled?: boolean;
  size?: 'md' | 'lg';
}) {
  return (
    <div className="flex items-start justify-between gap-4">
      <div>
        <p className={`font-medium text-ink ${size === 'lg' ? 'text-sm' : 'text-xs'}`}>{label}</p>
        {description && <p className="mt-0.5 text-[11px] text-muted">{description}</p>}
      </div>
      <button
        type="button"
        role="switch"
        aria-checked={checked}
        aria-label={label}
        disabled={disabled}
        onClick={() => onChange(!checked)}
        className={`relative shrink-0 rounded-full transition-colors disabled:opacity-40 ${
          size === 'lg' ? 'h-6 w-11' : 'h-5 w-9'
        } ${checked ? 'bg-positive' : 'bg-edge'}`}
      >
        <span
          className={`absolute top-0.5 rounded-full bg-page transition-transform ${
            size === 'lg' ? 'h-5 w-5' : 'h-4 w-4'
          } ${checked ? (size === 'lg' ? 'translate-x-5.5' : 'translate-x-4.5') : 'translate-x-0.5'}`}
        />
      </button>
    </div>
  );
}

export function Table({ head, children }: { head: ReactNode[]; children: ReactNode }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full border-collapse text-sm">
        <thead>
          <tr className="border-b border-edge">
            {head.map((cell, index) => (
              <th
                key={index}
                className={`px-3 py-2 text-[11px] font-medium tracking-wide text-muted uppercase ${
                  index === 0 ? 'text-left' : 'text-left last:text-right'
                }`}
              >
                {cell}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>{children}</tbody>
      </table>
    </div>
  );
}

export function Row({ children, onClick }: { children: ReactNode; onClick?: () => void }) {
  return (
    <tr
      onClick={onClick}
      className={`border-b border-edge/60 last:border-0 ${
        onClick ? 'cursor-pointer hover:bg-page/60' : ''
      }`}
    >
      {children}
    </tr>
  );
}

export function Cell({
  children,
  numeric,
  tone,
  className = '',
}: {
  children: ReactNode;
  numeric?: boolean;
  tone?: Tone;
  className?: string;
}) {
  return (
    <td
      className={`px-3 py-2 align-middle ${
        numeric ? 'tnum text-right font-mono' : ''
      } ${tone ? TONE_TEXT[tone] : 'text-ink'} ${className}`}
    >
      {children}
    </td>
  );
}

export function EmptyState({
  title,
  children,
  action,
}: {
  title: string;
  children?: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className="flex flex-col items-center gap-3 px-6 py-14 text-center">
      <p className="text-sm font-medium text-ink">{title}</p>
      {children && <p className="max-w-md text-xs text-muted">{children}</p>}
      {action}
    </div>
  );
}

/** Page-level failure: persistent, with a retry (§4.3). */
export function ErrorBanner({
  message,
  onRetry,
}: {
  message: string | null;
  onRetry?: () => void;
}) {
  if (!message) return null;
  return (
    <div
      role="alert"
      className="mb-4 flex items-start justify-between gap-4 rounded-md border
                 border-critical/40 bg-critical/10 px-4 py-3"
    >
      <p className="text-sm text-ink">{message}</p>
      {onRetry && (
        <Button variant="secondary" onClick={onRetry}>
          Retry
        </Button>
      )}
    </div>
  );
}

/** Kept for the auth form, which renders outside the app shell. */
export function ErrorNotice({ message }: { message: string | null }) {
  if (!message) return null;
  return (
    <p
      role="alert"
      className="rounded-md border border-critical/40 bg-critical/10 px-3 py-2 text-sm text-ink"
    >
      {message}
    </p>
  );
}

export function CodeBlock({
  code,
  label,
  lineNumbers = false,
}: {
  code: string;
  label?: string;
  lineNumbers?: boolean;
}) {
  const [copied, setCopied] = useState(false);
  const lines = code.split('\n');

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(code);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      /* clipboard unavailable — the text is selectable regardless */
    }
  };

  return (
    <div className="relative">
      {label && <p className="mb-1 text-xs font-medium text-muted">{label}</p>}
      <pre
        className="overflow-x-auto rounded-md border border-edge bg-sunken p-3
                   font-mono text-xs leading-relaxed text-ink"
      >
        <code>
          {lineNumbers
            ? lines.map((line, index) => (
                <span key={index} className="block">
                  <span className="mr-3 inline-block w-6 text-right text-muted select-none">
                    {index + 1}
                  </span>
                  {line}
                </span>
              ))
            : code}
        </code>
      </pre>
      <button
        type="button"
        onClick={copy}
        className="absolute top-2 right-2 rounded border border-edge bg-surface px-2 py-0.5
                   text-[11px] text-muted hover:text-ink"
      >
        {copied ? 'Copied' : 'Copy'}
      </button>
    </div>
  );
}

export function Modal({
  title,
  onClose,
  children,
  footer,
  tone,
}: {
  title: string;
  onClose: () => void;
  children: ReactNode;
  footer?: ReactNode;
  tone?: Tone;
}) {
  const ref = useRef<HTMLDivElement>(null);

  // `onClose` is almost always an inline arrow, so its identity changes on
  // every render. Depending on it here re-ran this effect on every keystroke
  // and re-focused the dialog container — which silently ate every character
  // after the first in any modal containing a text input. That is exactly the
  // kill switch's type-to-confirm field, so the safest control in the product
  // was the one it broke. Read it through a ref and mount the effect once.
  const closeRef = useRef(onClose);
  useEffect(() => {
    closeRef.current = onClose;
  }, [onClose]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') closeRef.current();
    };
    window.addEventListener('keydown', onKey);
    ref.current?.focus();
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4">
      <div
        ref={ref}
        tabIndex={-1}
        role="dialog"
        aria-modal="true"
        aria-label={title}
        className={`w-full max-w-lg rounded-lg border bg-surface outline-none ${
          tone === 'critical' ? 'border-critical/50' : 'border-edge'
        }`}
      >
        <header className="border-b border-edge px-4 py-3">
          <h2 className="text-sm font-semibold text-ink">{title}</h2>
        </header>
        <div className="px-4 py-4 text-sm text-ink">{children}</div>
        {footer && (
          <footer className="flex justify-end gap-2 border-t border-edge px-4 py-3">
            {footer}
          </footer>
        )}
      </div>
    </div>
  );
}

export function Spinner({ label = 'Loading…' }: { label?: string }) {
  return <p className="px-6 py-10 text-center text-xs text-muted">{label}</p>;
}

/** Horizontal progress, used for budgets and plan usage. */
export function Meter({ fraction, tone }: { fraction: number; tone?: Tone }) {
  const clamped = Math.max(0, Math.min(1, Number.isFinite(fraction) ? fraction : 0));
  const auto: Tone = clamped >= 1 ? 'critical' : clamped >= 0.8 ? 'warning' : 'positive';
  const fill = {
    neutral: 'bg-ink',
    positive: 'bg-positive',
    warning: 'bg-warning',
    critical: 'bg-critical',
    info: 'bg-info',
  }[tone ?? auto];
  return (
    <div className="h-1.5 w-full overflow-hidden rounded-full bg-edge">
      <div className={`h-full rounded-full ${fill}`} style={{ width: `${clamped * 100}%` }} />
    </div>
  );
}
