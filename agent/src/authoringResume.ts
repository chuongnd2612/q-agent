/**
 * Pause / resume plumbing for live authoring (#619) — the pure, testable half.
 *
 * The user's ask was "stop at the mid of the authoring process, feed more input,
 * then continue", with the browser LEFT OPEN so they can click their way to the
 * screen Claude could not reach (a modal, a seeded record, an MFA step) before
 * continuing. Making that work needed three things that did not exist:
 *
 *  1. **Claude's own session id.** `job.sessionId` is Q-Agent's queue id;
 *     `claude --resume` needs the id the CLI mints for itself, which rides on the
 *     `--output-format stream-json` envelope and was simply never read. See
 *     {@link sessionIdFrom}.
 *  2. **A transcript that still exists.** `--resume` reads it from
 *     `$CLAUDE_CONFIG_DIR/projects/<slug>/<session-id>.jsonl`, and that config dir
 *     lives INSIDE the ephemeral authoring workdir. See {@link findTranscript}.
 *  3. **A fallback.** `--resume` behaviour is not ours to control, and the
 *     transcript can be gone (agent restarted, the OS swept the temp dir). If
 *     resume were the only path the feature would hard-fail in exactly the
 *     situations where a user most wants it, so {@link planPass} degrades to a
 *     FRESH pass that carries the whole accumulated guidance, and says so in the
 *     trail rather than pretending context was preserved.
 *
 * Everything here is deliberately free of `spawn`/HTTP so the decision logic —
 * which is where the interesting failure modes are — is unit-testable without a
 * device, a browser or a Claude subscription.
 */

import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";

/**
 * Note appended to every resumed turn.
 *
 * Manual navigation between the pause and the Continue is not an edge case, it is
 * the POINT of the feature — so the resumed Claude must be told that its mental
 * model of the page is stale and to look before acting. Without this it happily
 * carries on clicking against the layout it remembers.
 */
export const MANUAL_NAV_NOTE =
  "IMPORTANT: the session was paused and a human may have driven this same browser " +
  "manually while it was paused — navigating, logging in, opening a dialog or " +
  "creating data. The page is probably NOT where you left it. Re-inspect the " +
  "current page state with browser-harness before your next action, and continue " +
  "from wherever the page actually is now rather than from where you expected to be.";

/** Pull Claude CLI's own `session_id` out of a stream-json envelope line. */
export function sessionIdFrom(ev: unknown): string {
  if (!ev || typeof ev !== "object") return "";
  const sid = (ev as Record<string, unknown>).session_id;
  return typeof sid === "string" && sid.trim() ? sid.trim() : "";
}

/**
 * Locate the transcript `claude --resume <id>` would read, or "" if it is gone.
 *
 * The CLI files transcripts under `<config>/projects/<cwd-slug>/<session>.jsonl`.
 * The slug is derived from the working directory, and we do NOT want to depend on
 * how that derivation is spelled in any given CLI version — so this scans the
 * `projects/` children for the session's own file instead of computing the slug.
 *
 * `configDir` empty means the run used the agent's own `claude login` rather than
 * an uploaded credential, in which case the store is the CLI's default location.
 * Returning "" is a normal outcome, not an error: it is the signal that Continue
 * must take the fallback path.
 */
export function findTranscript(configDir: string, sessionId: string, homeDir?: string): string {
  if (!sessionId) return "";
  const home = homeDir ?? os.homedir();
  const roots = configDir
    ? [configDir]
    : [path.join(home, ".claude"), path.join(home, ".config", "claude")];
  const wanted = `${sessionId}.jsonl`;
  for (const root of roots) {
    const projects = path.join(root, "projects");
    let entries: fs.Dirent[];
    try {
      entries = fs.readdirSync(projects, { withFileTypes: true });
    } catch {
      continue; // no store under this root
    }
    // A transcript can also sit directly under projects/ in some layouts.
    for (const entry of entries) {
      const full = path.join(projects, entry.name);
      if (entry.isFile() && entry.name === wanted) return full;
      if (!entry.isDirectory()) continue;
      const candidate = path.join(full, wanted);
      try {
        if (fs.statSync(candidate).isFile()) return candidate;
      } catch {
        /* not in this project dir */
      }
    }
  }
  return "";
}

export interface PassPlanInput {
  /** Claude CLI's own session id, as captured from the envelope. "" if never seen. */
  claudeSessionId: string;
  /** Whether the transcript for that id is still on disk. */
  transcriptPresent: boolean;
  /** Guidance turns not yet delivered to Claude. */
  guidance: string[];
  /** EVERY guidance turn given so far — needed only by the fallback path. */
  guidanceHistory: string[];
  /** The original server-composed task prompt (the fallback re-issues it). */
  taskPrompt: string;
  /** Budget left for the WHOLE session, already net of what earlier passes spent. */
  remainingBudgetUsd: number;
}

export interface PassPlan {
  /** `resume` continues the same Claude session; `fresh` starts a new one. */
  mode: "resume" | "fresh";
  /** Machine-readable why, for the trail. "" when resuming. */
  reason: string;
  /** Human-readable line to put on the authoring trail. */
  trailLine: string;
  /** The prompt for this pass. */
  prompt: string;
  /** Extra argv that selects the session (`--resume <id>`), or []. */
  sessionArgs: string[];
}

/**
 * Decide how to continue: resume the same Claude session, or start fresh.
 *
 * Resume is preferred because it is the only path that preserves the reasoning
 * context ("use the record you created earlier" only works there). It is chosen
 * only when BOTH facts hold — an id was captured AND its transcript is still on
 * disk — because either one missing makes `--resume` fail, and a failed resume is
 * strictly worse than a fresh pass: the user waited and got nothing.
 */
export function planPass(input: PassPlanInput): PassPlan {
  const guidance = input.guidance.filter((g) => g.trim());
  const history = input.guidanceHistory.filter((g) => g.trim());
  const canResume = Boolean(input.claudeSessionId) && input.transcriptPresent;
  if (canResume) {
    const turns = guidance.length ? guidance : history.slice(-1);
    return {
      mode: "resume",
      reason: "",
      trailLine: "↻ Continuing the same Claude session (context preserved)",
      prompt: [
        turns.length
          ? `New guidance from the user:\n${turns.map((g) => `- ${g}`).join("\n")}`
          : "Continue where you left off.",
        MANUAL_NAV_NOTE,
      ].join("\n\n"),
      sessionArgs: ["--resume", input.claudeSessionId],
    };
  }
  const reason = !input.claudeSessionId ? "no-session-id" : "transcript-missing";
  const why =
    reason === "no-session-id"
      ? "Claude never reported a session id"
      : "Claude's session transcript is gone";
  // A fresh pass has NO memory, so it must be handed the original task AND every
  // guidance turn ever given — not just the newest one, which on its own would
  // read as a non-sequitur.
  const all = history.length ? history : guidance;
  return {
    mode: "fresh",
    reason,
    trailLine: `↻ Cannot resume (${why}) — starting a fresh pass that carries your guidance`,
    prompt: [
      input.taskPrompt,
      all.length
        ? `The user paused an earlier attempt at this task and gave this guidance, which you MUST follow:\n${all
            .map((g) => `- ${g}`)
            .join("\n")}`
        : "",
      // The fallback redoes the whole task on whatever is LEFT of the session
      // budget, which can be much less than the first pass had. Saying so is
      // cheap and changes behaviour: write the spec early rather than exploring.
      `You have about $${input.remainingBudgetUsd.toFixed(2)} of budget left for this task ` +
        "(an earlier attempt already spent some of it), so work efficiently and write the " +
        "spec file as soon as you can rather than exploring exhaustively.",
      MANUAL_NAV_NOTE,
    ]
      .filter(Boolean)
      .join("\n\n"),
    sessionArgs: [],
  };
}

/**
 * Build the full argv for one authoring pass.
 *
 * Shared by the FIRST pass and every resumed one, deliberately: the resumed pass
 * must run with the same tools, the same system prompt and the same `--add-dir` as
 * the pass it continues, and duplicating the list is how those silently drift.
 * `--max-budget-usd` is the only value that changes per pass — it carries the
 * SESSION remainder, so a pause/continue loop cannot spend the ceiling again and
 * again (#619).
 */
export function buildPassArgs(opts: {
  prompt: string;
  model: string;
  systemPromptFile: string;
  workDir: string;
  budgetUsd: number;
  sessionArgs?: string[];
}): string[] {
  return [
    ...(opts.sessionArgs ?? []),
    "-p",
    opts.prompt,
    "--output-format",
    "stream-json",
    "--verbose",
    "--model",
    opts.model,
    "--append-system-prompt-file",
    opts.systemPromptFile,
    "--allowedTools",
    "Bash",
    "Read",
    "Write",
    "Glob",
    "Grep",
    "--dangerously-skip-permissions",
    "--add-dir",
    opts.workDir,
    "--max-budget-usd",
    String(opts.budgetUsd),
  ];
}

/**
 * Why a parked session should stop waiting, or `null` to keep waiting (#645).
 *
 * The pause exists to keep a browser open for the user to drive. If they CLOSE
 * that browser, the thing being preserved is gone: a later Continue cannot resume
 * against a dead Chrome, and browser-harness has nothing to attach to. Before
 * this, nothing noticed — the device sat in its poll loop and the session stayed
 * `paused` until the server expired it an hour later, with the case wedged and
 * nothing for the user to click.
 *
 * Pure so the ordering can be tested: the browser check comes FIRST, because when
 * both the browser is gone and the hard cap has passed, "you closed the browser"
 * is the honest reason and "pause expired" is a guess that sends the user looking
 * at timeouts.
 */
export function pauseWaitVerdict(state: {
  browserGone: boolean;
  pastHardCap: boolean;
}): { reason: string } | null {
  if (state.browserGone) return { reason: "browser-closed" };
  if (state.pastHardCap) return { reason: "pause-expired" };
  return null;
}

