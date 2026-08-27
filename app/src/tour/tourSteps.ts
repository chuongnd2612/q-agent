/**
 * Declarative step list for the interactive product tour.
 *
 * Pure data (no JSX / no store imports) so both `TourOverlay` and any future
 * consumer can read it. `TourOverlay` walks this array by `stepIndex`; the
 * store deliberately does NOT know the list (see `@/store/tour`).
 *
 * A step either spotlights a `data-tour` target (`spotlight` true, the default)
 * or renders a centered intro/bridge/finish card (`spotlight: false`, no `key`).
 * `route` is navigated to BEFORE the step shows; the literal `:sampleRun` token
 * is replaced with the seeded sample-run id at runtime.
 */

/** How the coach-mark card is placed relative to its spotlighted target. */
export type TourPlacement = "top" | "bottom" | "left" | "right" | "auto";

/** One step of the guided walkthrough. */
export interface TourStep {
  /** `data-tour` attribute value to spotlight. Omit for a centered card. */
  key?: string;
  /** Short, bold heading. */
  title: string;
  /** Friendly one- or two-sentence explanation. */
  body: string;
  /** Preferred side for the coach-mark; `auto` (default) picks the roomiest. */
  placement?: TourPlacement;
  /** Route to navigate to before showing. May contain the `:sampleRun` token. */
  route?: string;
  /** `false` renders a centered modal-style card (no spotlight). Default true. */
  spotlight?: boolean;
  /** Inset padding around the target rect for the spotlight ring. Default 8. */
  padding?: number;
}

/** The ordered walkthrough. Steps 8-13 live inside the seeded sample run. */
export const TOUR_STEPS: TourStep[] = [
  {
    title: "Welcome to Q‑Agent",
    body: "Q‑Agent turns your tickets into reviewed test cases, runnable specs, and published results. Take the 2‑minute tour and we'll walk you through the whole pipeline — including a real sample run.",
    spotlight: false,
    route: "/",
  },
  {
    key: "nav-dashboard",
    title: "Mission control",
    body: "The Dashboard is your home base — live run health, pass rates, and spend at a glance. Everything starts here.",
    route: "/",
    placement: "right",
  },
  {
    key: "topbar-search",
    title: "Search & ask anything",
    body: "Hit ⌘K anytime to jump between screens, launch actions, or ask Q‑Agent a question. It's the fastest way to move around.",
    placement: "bottom",
  },
  // The project is the container (ADR 0015), so the tour walks the tree rather
  // than the global Tickets / Runs screens and the global "New Run" button —
  // all three are gone (#729).
  {
    key: "nav-project-tree",
    title: "Your projects",
    body: "Everything belongs to a project: its tickets, its runs, its reports and the connection they come through. Expand a project here to jump straight to any of its six tabs — the counts are live.",
    route: "/",
    placement: "right",
  },
  {
    key: "nav-projects",
    title: "The full list",
    body: "All projects opens the full list, where you create a new one and compare them side by side. A run always starts from inside a project, so it can never be missing its tickets' provider.",
    route: "/",
    placement: "right",
  },
  {
    title: "Let's open a real run",
    body: "Now the fun part. We've prepared a sample run so you can see the full pipeline end to end — from AI‑drafted cases all the way to published results. Give us a second while we open it.",
    spotlight: false,
    route: "/runs/:sampleRun/review",
  },
  {
    key: "stage-review",
    title: "Review Center",
    body: "Q‑Agent drafts test cases from each ticket. Here you approve, edit, or reject them — you stay in control before anything runs.",
    route: "/runs/:sampleRun/review",
    placement: "right",
  },
  {
    key: "stage-sync",
    title: "Link back to your provider",
    body: "Approved cases sync back to Jira or Azure DevOps as linked test items, so your tracker always reflects what Q‑Agent covers.",
    route: "/runs/:sampleRun/sync",
    placement: "right",
  },
  {
    key: "stage-automation",
    title: "Generated automation",
    body: "Q‑Agent writes runnable Playwright specs from the approved cases. Inspect the generated code and tweak it before execution.",
    route: "/runs/:sampleRun/automation",
    placement: "right",
  },
  {
    key: "stage-execution",
    title: "Run results",
    body: "Watch the specs execute live and see pass/fail results roll in per case, with logs and timing for each step.",
    route: "/runs/:sampleRun/execution",
    placement: "right",
  },
  {
    key: "stage-evidence",
    title: "Evidence for every case",
    body: "Screenshots, video, and traces are captured automatically — so a failure comes with the proof you need to debug it.",
    route: "/runs/:sampleRun/evidence",
    placement: "right",
  },
  {
    key: "stage-comment",
    title: "Publish results back",
    body: "Close the loop: publish outcomes and evidence straight to the linked tickets, so your team sees results where they already work.",
    route: "/runs/:sampleRun/comment",
    placement: "right",
  },
  {
    title: "You're all set",
    body: "That's the full loop — tickets in, reviewed cases and published results out. Reopen this walkthrough anytime from ⌘K → “Start product tour.” Happy testing!",
    spotlight: false,
  },
];
