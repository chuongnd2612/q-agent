/**
 * Environment helpers (doc §9 `config/`).
 *
 * Generic accessors over `process.env` plus a small resolved-environment record.
 * Deliberately knows no application URLs, routes or credentials — an automation
 * project supplies those.
 */

/** Read an environment variable, falling back to `fallback` when unset/empty. */
export function env(name: string, fallback = ''): string {
  const raw = typeof process !== 'undefined' ? process.env?.[name] : undefined;
  const value = (raw ?? '').trim();
  return value === '' ? fallback : value;
}

/**
 * Read a required environment variable.
 *
 * @throws Error when the variable is unset or empty — fail loudly at setup rather
 *   than mid-test with a confusing selector error.
 */
export function requireEnv(name: string): string {
  const value = env(name);
  if (!value) throw new Error(`Missing required environment variable: ${name}`);
  return value;
}

/** Read a boolean env var. `1|true|yes|on` (case-insensitive) are true. */
export function envBool(name: string, fallback = false): boolean {
  const value = env(name).toLowerCase();
  if (!value) return fallback;
  if (['1', 'true', 'yes', 'on'].includes(value)) return true;
  if (['0', 'false', 'no', 'off'].includes(value)) return false;
  return fallback;
}

/** Read an integer env var, returning `fallback` when unset or unparseable. */
export function envInt(name: string, fallback: number): number {
  const parsed = Number.parseInt(env(name), 10);
  return Number.isFinite(parsed) ? parsed : fallback;
}

/** Read a comma/whitespace-separated list env var. */
export function envList(name: string, fallback: string[] = []): string[] {
  const value = env(name);
  if (!value) return fallback;
  return value
    .split(/[,\s]+/)
    .map((part) => part.trim())
    .filter(Boolean);
}

/** A resolved test environment. */
export interface EnvironmentConfig {
  /** Environment name, e.g. `"dev"`. From `QAGENT_ENV` / `TEST_ENV`. */
  name: string;
  /** Application base URL. From `QAGENT_BASE_URL` / `BASE_URL`. */
  baseUrl: string;
  /** API base URL, defaulting to `baseUrl`. From `QAGENT_API_URL` / `API_URL`. */
  apiUrl: string;
  /** Whether the run is headless. From `QAGENT_HEADLESS` (default true). */
  headless: boolean;
  /** Default action/assertion timeout in ms. From `QAGENT_TIMEOUT_MS` (default 30000). */
  timeoutMs: number;
  /** Free-form extra values callers stash on the environment record. */
  extra: Record<string, string>;
}

/** Overrides for {@link loadEnvironment}; anything omitted comes from the environment. */
export type EnvironmentOverrides = Partial<Omit<EnvironmentConfig, 'extra'>> & {
  extra?: Record<string, string>;
};

/**
 * Resolve the current {@link EnvironmentConfig} from `process.env`, applying
 * `overrides` last so a project or fixture can pin values explicitly.
 */
export function loadEnvironment(overrides: EnvironmentOverrides = {}): EnvironmentConfig {
  const baseUrl = overrides.baseUrl ?? env('QAGENT_BASE_URL', env('BASE_URL'));
  return {
    name: overrides.name ?? env('QAGENT_ENV', env('TEST_ENV', 'local')),
    baseUrl,
    apiUrl: overrides.apiUrl ?? env('QAGENT_API_URL', env('API_URL', baseUrl)),
    headless: overrides.headless ?? envBool('QAGENT_HEADLESS', true),
    timeoutMs: overrides.timeoutMs ?? envInt('QAGENT_TIMEOUT_MS', 30_000),
    extra: overrides.extra ?? {},
  };
}

/**
 * Join a path onto a base URL without doubling or dropping the separator.
 *
 * @param baseUrl Absolute base, e.g. `https://app.example.com`.
 * @param path Path or absolute URL. An absolute URL is returned unchanged.
 */
export function resolveUrl(baseUrl: string, path = ''): string {
  if (/^[a-z][a-z0-9+.-]*:\/\//i.test(path)) return path;
  if (!baseUrl) return path;
  const left = baseUrl.replace(/\/+$/, '');
  if (!path) return left;
  const right = path.replace(/^\/+/, '');
  return `${left}/${right}`;
}
