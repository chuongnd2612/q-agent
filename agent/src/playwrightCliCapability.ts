/**
 * Capability preflight for the `playwright-cli` browser driver (#899).
 *
 * The old guard was `if (!fs.existsSync(playwrightCli())) bail`. That checks the
 * wrong thing: `cli.js` is ALSO the entry for `playwright test` and `playwright
 * install`, so it exists in every version of the package — including versions
 * that do not implement the `cli` subcommands the ported agents script against.
 * The guard therefore passed on the agent's previously pinned Playwright 1.61.1
 * and the session died halfway through on `Unknown command: find`.
 *
 * `find` is the load-bearing command: it is the FIRST-CHOICE element-discovery
 * step in all three ported agents (generator, healer, planner) — "use `find`,
 * not a full snapshot" — so a Playwright without it does not merely lose a
 * convenience, it invalidates the methodology. Measured against real installs:
 * `attach --cdp`, `resize`, `snapshot`, `eval`, `generate-locator`, `state-load`
 * and `close-all` are all present in 1.61.1; only `find` is missing. 1.63.0 has
 * it, which is why `package.json` pins `^1.63.0`.
 *
 * So probe the CAPABILITY, not the path: run `node <cli.js> cli --help` and look
 * for `find` in the command list. Two details make the help text the only usable
 * signal:
 *   - `playwright cli find …` on 1.61.1 prints `Unknown command: find` and still
 *     EXITS 0, so the exit code cannot be trusted.
 *   - `--help` launches no browser and needs no CDP endpoint, so the probe is
 *     cheap and safe to run before the session starts.
 */
import { spawn } from "child_process";
import * as fs from "fs";
import * as path from "path";
import { nodeBin, playwrightCli } from "./paths";

/** Minimum Playwright that ships `playwright cli find` (see module docstring). */
export const PLAYWRIGHT_FIND_MIN_VERSION = "1.63.0";

/** How long to wait for `playwright cli --help` before giving up. */
const HELP_TIMEOUT_MS = 20_000;

/**
 * Does `playwright cli --help` output advertise the `find` command?
 *
 * Split out from the spawning so it can be tested against captured help text
 * from a real old and a real new Playwright, with no install of either.
 *
 * @param help combined stdout+stderr of `node <cli.js> cli --help`
 * @returns true when `find` appears as a command entry in the listing
 */
export function helpAdvertisesFind(help: string): boolean {
  // Commands are listed one per line, indented, name first:
  //   `  find [text]                 search the page snapshot for text ...`
  // Anchor on that shape so the word "find" inside a prose description of some
  // other command cannot satisfy the guard.
  return /^[ \t]+find(?:[ \t]|$)/m.test(help);
}

/**
 * Preflight the `playwright-cli` driver by proving it can `find`.
 *
 * @param cliJs path to Playwright's `cli.js`; defaults to the agent's bundled copy
 * @returns `{ok:true}` when the CLI resolves AND advertises `find`; otherwise
 *   `{ok:false, error}` with a message naming the actual problem (missing
 *   package vs. too-old package) so the failure is legible in the run trail
 *   instead of surfacing mid-session as `Unknown command: find`. Never throws.
 */
export async function playwrightCliFindAvailable(
  cliJs: string = playwrightCli()
): Promise<{ ok: boolean; error?: string }> {
  if (!fs.existsSync(cliJs)) {
    return { ok: false, error: "playwright-cli unavailable: playwright is not bundled with this agent build" };
  }
  let help: string;
  try {
    help = await runHelp(cliJs);
  } catch (e) {
    return { ok: false, error: `playwright-cli unavailable: could not probe ${cliJs} (${(e as Error).message})` };
  }
  if (!helpAdvertisesFind(help)) {
    return {
      ok: false,
      error:
        `playwright-cli unavailable: the bundled Playwright (${playwrightVersion(cliJs) || "unknown version"}) ` +
        `has no \`cli find\` command, which the authoring/healing/planning agents rely on. ` +
        `Update this Local Agent — it needs playwright >= ${PLAYWRIGHT_FIND_MIN_VERSION}.`,
    };
  }
  return { ok: true };
}

/** Best-effort version of the `playwright` package that owns `cliJs` (for the message). */
function playwrightVersion(cliJs: string): string {
  try {
    const pkg = JSON.parse(fs.readFileSync(path.join(path.dirname(cliJs), "package.json"), "utf-8"));
    return typeof pkg.version === "string" ? pkg.version : "";
  } catch {
    return "";
  }
}

/** Run `node <cliJs> cli --help` and return its combined output. */
function runHelp(cliJs: string): Promise<string> {
  return new Promise((resolve, reject) => {
    const child = spawn(nodeBin(), [cliJs, "cli", "--help"], {
      stdio: ["ignore", "pipe", "pipe"],
      windowsHide: true,
    });
    let out = "";
    const collect = (chunk: unknown): void => {
      out += String(chunk);
    };
    child.stdout?.on("data", collect);
    child.stderr?.on("data", collect);
    const timer = setTimeout(() => {
      try {
        child.kill();
      } catch {
        /* already gone */
      }
      reject(new Error(`timed out after ${HELP_TIMEOUT_MS}ms`));
    }, HELP_TIMEOUT_MS);
    child.on("error", (e) => {
      clearTimeout(timer);
      reject(e);
    });
    child.on("close", () => {
      clearTimeout(timer);
      resolve(out);
    });
  });
}
