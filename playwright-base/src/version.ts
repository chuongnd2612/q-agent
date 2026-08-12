/**
 * The package's own version, as a compile-time constant.
 *
 * Exposed so a consumer can assert compatibility at runtime — Q-Agent records what a
 * project was scaffolded against in `AutomationProject.base_version` (#538), and the
 * Local Agent's version guard (#541) compares that against what is actually installed
 * on the execution host.
 *
 * Kept in step with `package.json` / `VERSION` by `scripts/release.mjs`; the build
 * fails if they diverge (`scripts/check-version.mjs`, run by `npm run build`).
 */

/** Semver of `@q-agent/playwright-base`. */
export const BASE_VERSION = '1.0.0';

/** The package name, for diagnostics. */
export const BASE_PACKAGE_NAME = '@q-agent/playwright-base';

/** Parse a semver string into its numeric parts (pre-release/build ignored). */
export function parseVersion(version: string): { major: number; minor: number; patch: number } {
  const match = /^(\d+)\.(\d+)\.(\d+)/.exec(version.trim());
  if (!match) throw new Error(`Not a semver version: ${version}`);
  return { major: Number(match[1]), minor: Number(match[2]), patch: Number(match[3]) };
}

/**
 * Whether the installed base package can serve a project scaffolded against
 * `requiredVersion`, under caret semantics: same major, and installed >= required.
 */
export function isCompatibleWith(requiredVersion: string, installedVersion: string = BASE_VERSION): boolean {
  let required: { major: number; minor: number; patch: number };
  let installed: { major: number; minor: number; patch: number };
  try {
    required = parseVersion(requiredVersion);
    installed = parseVersion(installedVersion);
  } catch {
    return false;
  }
  if (required.major !== installed.major) return false;
  if (installed.minor !== required.minor) return installed.minor > required.minor;
  return installed.patch >= required.patch;
}

/**
 * Throw a clear, actionable error when the installed base package cannot serve a
 * project scaffolded against `requiredVersion`. A no-op when compatible.
 */
export function assertCompatibleWith(requiredVersion: string): void {
  if (isCompatibleWith(requiredVersion)) return;
  throw new Error(
    `${BASE_PACKAGE_NAME}@${BASE_VERSION} is installed, but this automation project was ` +
      `scaffolded against ${requiredVersion}. Reinstall dependencies for this project ` +
      `(npm install) or regenerate it against the installed base version.`,
  );
}
