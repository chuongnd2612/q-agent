/**
 * Random test-data helpers (doc §9 `utils/`).
 *
 * Generic primitives only — a *domain* factory (a valid employer, a policy, an order)
 * is application-specific and belongs in the project's `data/` (doc §10, §18).
 *
 * Every generator is collision-resistant by construction (timestamp + random
 * suffix), because parallel Playwright workers create records concurrently.
 */

const ALPHANUM = 'abcdefghijklmnopqrstuvwxyz0123456789';

/** Integer in `[min, max]` inclusive. */
export function randomInt(min: number, max: number): number {
  const lo = Math.ceil(min);
  const hi = Math.floor(max);
  return lo + Math.floor(Math.random() * (hi - lo + 1));
}

/** Lowercase alphanumeric string of `length` characters. */
export function randomString(length = 8, alphabet: string = ALPHANUM): string {
  let out = '';
  for (let i = 0; i < length; i++) out += alphabet[randomInt(0, alphabet.length - 1)];
  return out;
}

/**
 * A suffix unique across parallel workers and reruns: base-36 timestamp + random
 * tail, e.g. `m4x1q7-a8f2`.
 */
export function uniqueSuffix(randomLength = 4): string {
  return `${Date.now().toString(36)}-${randomString(randomLength)}`;
}

/** A unique identifier with a readable prefix, e.g. `user-m4x1q7-a8f2`. */
export function uniqueId(prefix = 'qa'): string {
  return `${prefix}-${uniqueSuffix()}`;
}

/** A unique, non-deliverable email address. Uses `example.com` per RFC 2606. */
export function randomEmail(prefix = 'qa', domain = 'example.com'): string {
  return `${prefix}+${uniqueSuffix()}@${domain}`;
}

/** A digit string of `length` digits, first digit non-zero. */
export function randomDigits(length = 10): string {
  let out = String(randomInt(1, 9));
  for (let i = 1; i < length; i++) out += String(randomInt(0, 9));
  return out;
}

/** A North-American-style phone number using the reserved 555 exchange. */
export function randomPhone(): string {
  return `555-${randomDigits(3)}-${randomDigits(4)}`;
}

/**
 * A password satisfying the usual complexity rules (upper, lower, digit, symbol),
 * `length` characters long (minimum 8).
 */
export function randomPassword(length = 16): string {
  const size = Math.max(8, length);
  const required = [
    'ABCDEFGHJKLMNPQRSTUVWXYZ'[randomInt(0, 23)],
    'abcdefghijkmnpqrstuvwxyz'[randomInt(0, 23)],
    '23456789'[randomInt(0, 7)],
    '!@#$%^&*'[randomInt(0, 7)],
  ];
  const rest = randomString(size - required.length, `${ALPHANUM}ABCDEFGHJKLMNPQRSTUVWXYZ!@#$%^&*`);
  return shuffle([...required, ...rest]).join('');
}

/** One random element of `items`. Throws on an empty array. */
export function randomPick<T>(items: readonly T[]): T {
  if (items.length === 0) throw new Error('randomPick: cannot pick from an empty array');
  return items[randomInt(0, items.length - 1)];
}

/** `count` distinct random elements of `items` (capped at `items.length`). */
export function randomSample<T>(items: readonly T[], count: number): T[] {
  return shuffle([...items]).slice(0, Math.min(count, items.length));
}

/** Fisher–Yates shuffle, in place; returns the same array for chaining. */
export function shuffle<T>(items: T[]): T[] {
  for (let i = items.length - 1; i > 0; i--) {
    const j = randomInt(0, i);
    [items[i], items[j]] = [items[j], items[i]];
  }
  return items;
}

/** `true` with probability `probability` (default 0.5). */
export function randomBool(probability = 0.5): boolean {
  return Math.random() < probability;
}
