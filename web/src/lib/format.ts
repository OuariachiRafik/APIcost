/**
 * Number and date formatting.
 *
 * Centralised because the tone-of-voice rule in APICOST_FRONTEND_SPEC §2.4 is
 * "back every claim with a real number" — which only works if the same number
 * is rendered the same way everywhere. Money that appears as `$1,284.75` on
 * one screen and `$1284.8` on another reads as two different figures.
 */

const USD = new Intl.NumberFormat('en-US', {
  style: 'currency',
  currency: 'USD',
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

/** Sub-cent amounts are common on a per-request basis and round to $0.00. */
const USD_PRECISE = new Intl.NumberFormat('en-US', {
  style: 'currency',
  currency: 'USD',
  minimumFractionDigits: 2,
  maximumFractionDigits: 6,
});

const COMPACT = new Intl.NumberFormat('en-US', { notation: 'compact', maximumFractionDigits: 1 });
const PLAIN = new Intl.NumberFormat('en-US');

export function money(value: number | string | null | undefined): string {
  const n = toNumber(value);
  if (n === null) return '—';
  // Anything under a cent would render as "$0.00", which reads as free.
  if (n !== 0 && Math.abs(n) < 0.01) return USD_PRECISE.format(n);
  return USD.format(n);
}

/** Full precision — for per-request costs, where fractions of a cent matter. */
export function moneyPrecise(value: number | string | null | undefined): string {
  const n = toNumber(value);
  return n === null ? '—' : USD_PRECISE.format(n);
}

export function count(value: number | null | undefined): string {
  return value === null || value === undefined ? '—' : PLAIN.format(value);
}

export function compact(value: number | null | undefined): string {
  return value === null || value === undefined ? '—' : COMPACT.format(value);
}

/** `fraction` is 0..1, as every rate field in the API returns it. */
export function percent(fraction: number | null | undefined, digits = 1): string {
  if (fraction === null || fraction === undefined) return '—';
  return `${(fraction * 100).toFixed(digits)}%`;
}

export function ms(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—';
  if (value >= 1000) return `${(value / 1000).toFixed(2)} s`;
  return `${value.toFixed(0)} ms`;
}

export function tokens(value: number | null | undefined): string {
  return value === null || value === undefined ? '—' : COMPACT.format(value);
}

const DATE_TIME = new Intl.DateTimeFormat(undefined, {
  month: 'short',
  day: 'numeric',
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  hour12: false,
});

const DATE_ONLY = new Intl.DateTimeFormat(undefined, {
  year: 'numeric',
  month: 'short',
  day: 'numeric',
});

export function dateTime(value: string | null | undefined): string {
  if (!value) return '—';
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? '—' : DATE_TIME.format(parsed);
}

export function date(value: string | null | undefined): string {
  if (!value) return '—';
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? '—' : DATE_ONLY.format(parsed);
}

/** "3 days ago" / "in 12 days". For renewal dates and alert timestamps. */
const RELATIVE = new Intl.RelativeTimeFormat(undefined, { numeric: 'auto' });

export function relative(value: string | null | undefined): string {
  if (!value) return '—';
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return '—';

  const seconds = (parsed.getTime() - Date.now()) / 1000;
  const steps: [Intl.RelativeTimeFormatUnit, number][] = [
    ['second', 60],
    ['minute', 60],
    ['hour', 24],
    ['day', 30],
    ['month', 12],
  ];

  let amount = seconds;
  for (const [unit, size] of steps) {
    if (Math.abs(amount) < size) return RELATIVE.format(Math.round(amount), unit);
    amount /= size;
  }
  return RELATIVE.format(Math.round(amount), 'year');
}

/**
 * Coerce an API money field to a number.
 *
 * Every `cost_usd` / `savings_usd` field is a Pydantic `Decimal`, which
 * serialises to a JSON **string** — deliberately, so no precision is lost in
 * transit. Anything doing arithmetic on them has to convert first; doing it
 * through one helper keeps `"0.0012" + "0.0034" === "0.00120.0034"` from ever
 * reaching a total.
 */
export function num(value: number | string | null | undefined): number {
  return toNumber(value) ?? 0;
}

function toNumber(value: number | string | null | undefined): number | null {
  if (value === null || value === undefined) return null;
  const n = typeof value === 'string' ? Number(value) : value;
  return Number.isFinite(n) ? n : null;
}
