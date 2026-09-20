import type { SpecStatus } from "@/types/api";

/**
 * Fuchsia hue reserved for "product defect" so it reads as clearly distinct from
 * the script-failure red. Now the shared `--defect` token (#784) rather than a
 * literal kept in sync by hand with the Execution slice: it darkens in light
 * mode, where the dark-mode fuchsia is unreadable on paper.
 */
export const PRODUCT_DEFECT_HUE = "var(--defect)";

/** Dot colour per normalised spec status (used in the left spec list).
 * `var(--token)` strings, not hexes — these are handed to inline `style`, so the
 * browser resolves them per element and they follow `data-mode` for free. */
export const SPEC_STATUS_DOT: Record<SpecStatus, string> = {
  // Draft is deliberately the dimmest dot; `--paused` is the ramp's "inactive"
  // grey and is legible on both surfaces (the old #3f3f4a vanished on paper).
  draft: "var(--paused)",
  blocked: "var(--warn)",
  running: "var(--warn)",
  passed: "var(--ok)",
  failed: "var(--danger)",
  product_defect: PRODUCT_DEFECT_HUE,
};

/**
 * Coerce a raw `spec.status` wire value to a known SpecStatus, defaulting unknown
 * or empty values to "draft" so the UI degrades gracefully before the backend
 * wiring that sets these lands.
 */
export function normalizeSpecStatus(raw: string | undefined): SpecStatus {
  switch (raw) {
    case "blocked":
    case "running":
    case "passed":
    case "failed":
    case "product_defect":
      return raw;
    default:
      return "draft";
  }
}

/**
 * The status to render for a spec: the authoritative `spec.status` when set,
 * otherwise fall back to the latest execution result so pass/fail dots keep
 * working while the backend status wiring is a separate slice.
 */
export function effectiveSpecStatus(specStatus: string | undefined, execStatus: string | undefined): SpecStatus {
  const s = normalizeSpecStatus(specStatus);
  if (s !== "draft") return s;
  if (execStatus === "pass") return "passed";
  if (execStatus === "fail") return "failed";
  if (execStatus === "running") return "running";
  return "draft";
}

/**
 * Defensively parse a `spec.gateReport` JSON string. Returns null for empty or
 * malformed input; never throws.
 */
export function parseGateReport(
  raw: string | undefined,
): { outcome?: string; reason?: string; planViolations?: string[] } | null {
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === "object"
      ? (parsed as { outcome?: string; reason?: string; planViolations?: string[] })
      : null;
  } catch {
    return null;
  }
}

/** One asset decision in an automation plan (#544). */
export interface PlanEntry {
  name: string;
  path: string;
  action: string;
  methods?: string[];
  existingMethods?: string[];
  reason?: string;
}

/** The normalised automation plan the API persists in `spec.planReport` (#544). */
export interface AutomationPlan {
  feature?: string;
  ticket?: string;
  specGroups?: { name: string; testCases: string[] }[];
  pages?: PlanEntry[];
  components?: PlanEntry[];
  fixtures?: PlanEntry[];
  data?: PlanEntry[];
  utils?: PlanEntry[];
  counts?: Record<string, number>;
  importable?: string[];
  writable?: string[];
  cases?: string[];
  /** Set once the project editor has run for this ticket (#545) — it runs once per
   *  ticket, so this stamp is also what stops the second case paying for it again. */
  authoredAt?: string;
  /** Why an authoring pass was rolled back, when one was (#545). */
  authoringError?: string;
}

/** The plan's asset buckets, in the order the panel renders them. */
export const PLAN_GROUPS = ["pages", "components", "fixtures", "data", "utils"] as const;

/**
 * Defensively parse a `spec.planReport` JSON string, exactly like
 * `parseGateReport`. Returns null for empty, malformed, or empty-of-decisions
 * input — so the plan panel simply doesn't render rather than showing a husk.
 */
export function parsePlanReport(raw: string | null | undefined): AutomationPlan | null {
  if (!raw) return null;
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return null;
  }
  if (!parsed || typeof parsed !== "object") return null;
  const plan = parsed as AutomationPlan;
  const hasDecisions = PLAN_GROUPS.some((g) => (plan[g] ?? []).length > 0);
  return hasDecisions ? plan : null;
}
