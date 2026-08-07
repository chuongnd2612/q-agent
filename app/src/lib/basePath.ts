// Where this app is mounted, for the code that bypasses React Router.
//
// The router is given a `basename` (see `router.tsx`), so every `navigate()`
// and `<Link to>` already resolves correctly and needs nothing from this file.
// What does need it:
//
//   * `window.location.assign(...)` — a hard navigation, which the router never
//     sees and therefore never prefixes;
//   * `window.location.pathname` comparisons, which arrive *with* the prefix
//     still attached;
//   * plain `<a href>` to a non-router URL, such as the installer download.
//
// Getting one of these wrong is quiet rather than loud: the request 404s at the
// front door, or a redirect lands on the hub's shell instead of this app, and
// nothing in the console says why.

/**
 * The mount point, always with a leading and trailing slash — `/` or `/qagent/`.
 *
 * From Vite's `base`, so it is fixed at build time and identical to the prefix
 * the router was given.
 */
export const BASE_PATH: string = import.meta.env.BASE_URL || "/";

/** `BASE_PATH` without its trailing slash: `""` at the root, `/qagent` under one. */
export const BASE_PREFIX: string = BASE_PATH.replace(/\/$/, "");

/**
 * An app-absolute path (`/login`) as a browser-absolute one (`/qagent/login`).
 *
 * Use for `window.location.assign` and `<a href>`. Never for router navigation,
 * which would then get the prefix twice.
 */
export function withBase(path: string): string {
  if (!path.startsWith("/")) return path;
  return `${BASE_PREFIX}${path}`;
}

/**
 * A browser path (`/qagent/login`) as the app sees it (`/login`).
 *
 * For comparing `window.location.pathname` against the app's own routes. A path
 * outside the mount point is returned unchanged rather than mangled — it is not
 * ours to interpret.
 */
export function stripBase(pathname: string): string {
  if (!BASE_PREFIX) return pathname;
  if (pathname === BASE_PREFIX) return "/";
  if (pathname.startsWith(`${BASE_PREFIX}/`)) return pathname.slice(BASE_PREFIX.length);
  return pathname;
}
