/**
 * Wait + retry helpers (doc §9 `utils/`).
 *
 * These are for **non-UI** conditions only — an eventually-consistent API, a queued
 * job, a file appearing on disk. UI waiting is Playwright's job: use web-first
 * assertions and auto-waiting locators, never a fixed sleep. Q-Agent's generation
 * rules ban `page.waitForTimeout` in specs, and nothing here reintroduces it.
 */

/** Options for {@link waitFor}. */
export interface WaitForOptions {
  /** Give up after this many ms. Default 10000. */
  timeoutMs?: number;
  /** Delay between polls, ms. Default 250. */
  intervalMs?: number;
  /** Message used in the timeout error. */
  message?: string;
}

/** Resolve after `ms`. Prefer web-first assertions for anything UI-related. */
export function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/**
 * Poll `predicate` until it returns a truthy value.
 *
 * @returns The predicate's truthy value.
 * @throws Error on timeout.
 */
export async function waitFor<T>(
  predicate: () => T | Promise<T>,
  options: WaitForOptions = {},
): Promise<NonNullable<T>> {
  const { timeoutMs = 10_000, intervalMs = 250, message = 'Condition not met' } = options;
  const deadline = Date.now() + timeoutMs;
  let lastError: unknown = null;
  while (Date.now() < deadline) {
    try {
      const value = await predicate();
      if (value) return value as NonNullable<T>;
    } catch (error) {
      lastError = error;
    }
    await sleep(intervalMs);
  }
  const suffix = lastError instanceof Error ? ` (last error: ${lastError.message})` : '';
  throw new Error(`${message} within ${timeoutMs}ms${suffix}`);
}

/** Options for {@link retry}. */
export interface RetryOptions {
  /** Total attempts, including the first. Default 3. */
  attempts?: number;
  /** Delay before the first retry, ms. Default 250. */
  delayMs?: number;
  /** Multiplier applied to the delay after each failure. Default 2 (exponential). */
  backoffFactor?: number;
  /** Return false to stop retrying a given error. Default: always retry. */
  retryOn?: (error: unknown, attempt: number) => boolean;
}

/**
 * Run `fn`, retrying on rejection with exponential backoff.
 *
 * @returns `fn`'s resolved value.
 * @throws The last error when every attempt failed.
 */
export async function retry<T>(fn: (attempt: number) => Promise<T>, options: RetryOptions = {}): Promise<T> {
  const { attempts = 3, delayMs = 250, backoffFactor = 2, retryOn } = options;
  let delay = delayMs;
  let lastError: unknown;
  for (let attempt = 1; attempt <= attempts; attempt++) {
    try {
      return await fn(attempt);
    } catch (error) {
      lastError = error;
      if (attempt === attempts) break;
      if (retryOn && !retryOn(error, attempt)) break;
      await sleep(delay);
      delay *= backoffFactor;
    }
  }
  throw lastError instanceof Error ? lastError : new Error(String(lastError));
}

/**
 * Reject with a clear message if `promise` does not settle within `timeoutMs`.
 *
 * The underlying promise is not cancelled (promises aren't cancellable); this only
 * bounds how long the caller waits.
 */
export async function withTimeout<T>(promise: Promise<T>, timeoutMs: number, message = 'Operation timed out'): Promise<T> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    return await Promise.race([
      promise,
      new Promise<never>((_, reject) => {
        timer = setTimeout(() => reject(new Error(`${message} after ${timeoutMs}ms`)), timeoutMs);
      }),
    ]);
  } finally {
    if (timer) clearTimeout(timer);
  }
}
