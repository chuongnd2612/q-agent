import type { RunOut } from "@/types/api";

/**
 * The run overlay's stage model (ADR 0015 §4, #730).
 *
 * ## Two state variables, not one
 *
 * There are two different questions about a run and they must never share a
 * variable:
 *
 * 1. **How far has the run got?** — `run.status` on the server. It is *furthest
 *    progress* and it only ever moves forward (ADR 0005's terminal invariant
 *    depends on that). {@link furthestStage} projects it onto the wizard.
 * 2. **Which stage is the user looking at?** — the `:stage` path segment. It is
 *    navigation, so it lives in the URL and nowhere else (ADR 0003; CLAUDE.md
 *    forbids navigation state in `store/ui.ts`).
 *
 * The old `RunContextHeader` collapsed the two into one mapping, which is why
 * "go back to Review" would have had to move `run.status` backwards. Here, Back
 * is a pure URL change: it fires no mutation, so the server's status is
 * untouched. Only Next runs the stage's action, and only the server advances
 * `run.status`.
 *
 * ## Hidden automatic stages
 *
 * `processing` (Analyze) and `sync` (Link) are worker stages with nothing for the
 * user to do, so they get **no stepper entry**. Each one instead names the human
 * stage the wizard sits on while it works ({@link AUTO_STAGES}); the only
 * indication is a spinner chip beside the run name, and because the wizard is
 * already parked on the stage that follows, it "advances by itself" the moment
 * the worker finishes and `run.status` catches up.
 */
export const RUN_STAGES = [
  { key: "review", seg: "review" },
  { key: "automation", seg: "automation" },
  { key: "execution", seg: "execution" },
  { key: "evidence", seg: "evidence" },
  // The `comment` status, relabelled "Publish" (ADR 0015 §4). The route segment
  // stays `comment` — it is the existing screen's URL and not worth breaking.
  { key: "publish", seg: "comment" },
] as const;

export type RunStageKey = (typeof RUN_STAGES)[number]["key"];

/** Total human stages — "Step N of {STAGE_COUNT}". */
export const STAGE_COUNT = RUN_STAGES.length;

/**
 * The two automatic statuses → the human stage the wizard shows while they work,
 * and the i18n key for the spinner chip's label. Neither has a stepper entry.
 *
 * `processing` analyses tickets *before* Review, so the wizard waits ON Review.
 * `sync` creates and links cases *after* Review, so the wizard waits on the next
 * stage (Automation) — which is what makes the auto-advance free.
 */
export const AUTO_STAGES: Record<string, { stage: RunStageKey; labelKey: string }> = {
  processing: { stage: "review", labelKey: "busy.analyzing" },
  sync: { stage: "automation", labelKey: "busy.linking" },
};

/**
 * The terminal completion stage's route segment (ADR 0015 §6, #731).
 *
 * Deliberately NOT a member of {@link RUN_STAGES}: it is not a sixth step. The
 * five pills all read complete on it, Back is disabled, and the run is over. It
 * is a route rather than a modal so that exiting and reopening a finished run
 * lands back on it — which is also what keeps "Retry failed publish" reachable
 * later, instead of only in the seconds after the failure.
 */
export const DONE_SEG = "done";

/** Terminal statuses (ADR 0005 §1). A terminal run has no *current* stage. */
const TERMINAL = new Set(["done", "cancelled", "failed"]);

/** `run.status` → the index of the furthest human stage the run has reached. */
const STATUS_TO_STAGE: Record<string, number> = {
  processing: 0, // Analyze — hidden; the wizard waits on Review
  review: 0,
  sync: 1, // Link — hidden; the wizard waits on Automation
  automation: 1,
  executing: 2,
  evidence: 3,
  comment: 4, // Publish
  done: 4,
};

export function stageIndex(key: RunStageKey): number {
  return RUN_STAGES.findIndex((s) => s.key === key);
}

/** The stage index a route segment addresses, or `null` if it names none. */
export function stageIndexForSegment(seg: string | undefined): number | null {
  if (!seg) return null;
  const i = RUN_STAGES.findIndex((s) => s.seg === seg);
  return i === -1 ? null : i;
}

/**
 * How far the RUN has got — never how far the user has navigated.
 *
 * `cancelled` / `failed` do not name a pipeline position, so they fall back to
 * `failedStage` (the status the run failed *at*) rather than collapsing to
 * Review, which would misreport a run that died at Evidence as untouched.
 */
export function furthestStage(run: Pick<RunOut, "status" | "failedStage"> | undefined): number {
  if (!run) return 0;
  const direct = STATUS_TO_STAGE[run.status];
  if (direct != null) return direct;
  const failed = run.failedStage ? STATUS_TO_STAGE[run.failedStage] : undefined;
  return failed ?? 0;
}

/** Is the run over? A terminal run has every stage behind it and no Next. */
export function isTerminalStatus(status: string | undefined): boolean {
  return status != null && TERMINAL.has(status);
}

/** Has the run finished the whole pipeline successfully (#724)? */
export function isRunFinished(status: string | undefined): boolean {
  return status === "done";
}

/**
 * The automatic stage currently working, if any — the spinner chip's source.
 * Returns `null` whenever the run is waiting on the user or is terminal.
 */
export function activeAutoStage(
  status: string | undefined,
): { stage: RunStageKey; labelKey: string } | null {
  if (!status || TERMINAL.has(status)) return null;
  return AUTO_STAGES[status] ?? null;
}

// ------------------------------------------------------------------ resume
//
// Resume is NOT navigation — it is "where was I", read once when a run is
// opened without a stage in the URL. So it deliberately does not live in the
// URL. It also must survive a reload (that is most of the point), which rules
// out the in-memory Zustand store, so it goes to localStorage. Per-run key, so
// two runs resume independently.

const RESUME_PREFIX = "qagent.runStage.";

export function readResumeStage(runId: number): RunStageKey | null {
  try {
    const raw = window.localStorage.getItem(RESUME_PREFIX + runId);
    return RUN_STAGES.some((s) => s.key === raw) ? (raw as RunStageKey) : null;
  } catch {
    // Private mode / storage disabled — resume degrades to "start at furthest".
    return null;
  }
}

export function writeResumeStage(runId: number, key: RunStageKey): void {
  try {
    window.localStorage.setItem(RESUME_PREFIX + runId, key);
  } catch {
    /* ignore — resume is a convenience, never a requirement */
  }
}
