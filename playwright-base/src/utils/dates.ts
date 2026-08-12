/**
 * Date helpers (doc §9 `utils/`).
 *
 * Deterministic, dependency-free, UTC-based. Anything locale-specific belongs in the
 * application automation project, which knows its own formats.
 */

/** `YYYY-MM-DD` for `date` (UTC). Defaults to now. */
export function isoDate(date: Date = new Date()): string {
  return date.toISOString().slice(0, 10);
}

/** Full ISO-8601 timestamp for `date` (UTC). Defaults to now. */
export function isoDateTime(date: Date = new Date()): string {
  return date.toISOString();
}

/** Today as `YYYY-MM-DD` (UTC). */
export function today(): string {
  return isoDate();
}

/** `date` shifted by `days` (negative to go back). Does not mutate the input. */
export function addDays(date: Date, days: number): Date {
  const copy = new Date(date.getTime());
  copy.setUTCDate(copy.getUTCDate() + days);
  return copy;
}

/** `date` shifted by `months`. Does not mutate the input. */
export function addMonths(date: Date, months: number): Date {
  const copy = new Date(date.getTime());
  copy.setUTCMonth(copy.getUTCMonth() + months);
  return copy;
}

/** `days` from now as `YYYY-MM-DD` (UTC). */
export function daysFromNow(days: number): string {
  return isoDate(addDays(new Date(), days));
}

/**
 * Format `date` (UTC) with a small token set: `YYYY MM DD HH mm ss`.
 *
 * @example formatDate(new Date(), 'DD/MM/YYYY')
 */
export function formatDate(date: Date, pattern: string): string {
  const pad = (n: number, width = 2) => String(n).padStart(width, '0');
  const tokens: Record<string, string> = {
    YYYY: String(date.getUTCFullYear()),
    MM: pad(date.getUTCMonth() + 1),
    DD: pad(date.getUTCDate()),
    HH: pad(date.getUTCHours()),
    mm: pad(date.getUTCMinutes()),
    ss: pad(date.getUTCSeconds()),
  };
  return pattern.replace(/YYYY|MM|DD|HH|mm|ss/g, (token) => tokens[token] ?? token);
}

/** A filesystem-safe timestamp, e.g. `20260812-134501`. Useful in artefact names. */
export function timestampSlug(date: Date = new Date()): string {
  return formatDate(date, 'YYYYMMDD-HHmmss');
}
