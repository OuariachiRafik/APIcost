/** Small shared primitives. Deliberately plain — the dashboard proper is P3. */
import type { ButtonHTMLAttributes, InputHTMLAttributes, ReactNode } from 'react';
import { useId, useState } from 'react';

export function Button({
  children,
  variant = 'primary',
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: 'primary' | 'secondary' }) {
  const base =
    'inline-flex items-center justify-center rounded-md px-4 py-2 text-sm font-medium ' +
    'transition-colors disabled:cursor-not-allowed disabled:opacity-50';
  const styles =
    variant === 'primary'
      ? 'bg-slate-900 text-white hover:bg-slate-700'
      : 'border border-slate-300 bg-white text-slate-700 hover:bg-slate-50';
  return (
    <button className={`${base} ${styles}`} {...props}>
      {children}
    </button>
  );
}

export function Field({
  label,
  hint,
  ...props
}: InputHTMLAttributes<HTMLInputElement> & { label: string; hint?: string }) {
  const id = useId();
  const hintId = `${id}-hint`;

  // The hint sits outside the <label> and is wired up with aria-describedby.
  // Nesting it would fold it into the input's accessible name, so a screen
  // reader would announce "Password At least 12 characters" as the field name.
  return (
    <div className="block">
      <label htmlFor={id} className="mb-1 block text-sm font-medium text-slate-700">
        {label}
      </label>
      <input
        id={id}
        aria-describedby={hint ? hintId : undefined}
        className="w-full rounded-md border border-slate-300 px-3 py-2 text-sm
                   focus:border-slate-500 focus:outline-none"
        {...props}
      />
      {hint && (
        <span id={hintId} className="mt-1 block text-xs text-slate-500">
          {hint}
        </span>
      )}
    </div>
  );
}

export function ErrorNotice({ message }: { message: string | null }) {
  if (!message) return null;
  return (
    <p role="alert" className="rounded-md bg-red-50 px-3 py-2 text-sm text-red-700">
      {message}
    </p>
  );
}

export function Card({ children }: { children: ReactNode }) {
  return <section className="rounded-lg border border-slate-200 bg-white p-6">{children}</section>;
}

/** A copyable code block. Used for the integration snippets in onboarding. */
export function CodeBlock({ code, label }: { code: string; label?: string }) {
  const [copied, setCopied] = useState(false);

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
      {label && <p className="mb-1 text-xs font-medium text-slate-500">{label}</p>}
      <pre className="overflow-x-auto rounded-md bg-slate-900 p-4 text-xs leading-relaxed text-slate-100">
        <code>{code}</code>
      </pre>
      <button
        type="button"
        onClick={copy}
        className="absolute right-2 top-2 rounded border border-slate-600 bg-slate-800 px-2 py-1
                   text-xs text-slate-200 hover:bg-slate-700"
      >
        {copied ? 'Copied' : 'Copy'}
      </button>
    </div>
  );
}
