# ADR 0004 — Run workspace-mode navigation (no silent run default)

- **Status:** Superseded by
  [ADR 0015](0015-project-scoped-navigation-and-run-overlay.md) — workspace mode is
  replaced by a full-screen run overlay and the "pick a run" interstitial is deleted.
  **The no-silent-run-default guarantee below still holds**; ADR 0015 §4 restates it and
  keeps `RunLayout`'s guard.
- **Date:** 2026-07-04
- **Deciders:** Operator (via in-session decisions), Q-Agent build
- **Supersedes/extends:** builds on [ADR 0003](0003-client-side-routing.md)

## Context

ADR 0003 made navigation URL-driven, but the sidebar still mixed **global** and
**run-scoped** screens in one flat list, and the shell resolved "the current run"
via `useResolvedRunId()` — which, off a run route, silently fell back to the
most-recent non-done run. A reviewer could therefore open Review Center (or
Automation/Execution/Evidence) with **no run explicitly chosen**, and the app would
pick one — the direct cause of approving/acting on the wrong run's cases.

## Decision

Adopt **workspace mode** (design option 1a). Screens split into two groups:

- **Global** — Dashboard, Projects, Tickets, Runs, Reports, Audit Log, Settings.
- **Run-scoped** — Review Center, Automation, Execution, Evidence (plus Link/Publish
  pipeline stages).

Rules:

1. **Run-scoped screens are reachable only from within a run.** The global sidebar
   (`GlobalSidebar`) lists only global screens — no run-scoped entries exist there.
   Under `/runs/:runId/*` the sidebar swaps to the run workspace (`RunSidebar`: exit,
   run card, pipeline-as-navigation, pinned global mini-row) and the top bar becomes
   the run-context header (`RunContextHeader`: run identity + stage pill + Switch run).
2. **No silent default.** The `useResolvedRunId` hook (and its most-recent-non-done
   fallback) is **deleted**. Shell chrome reads `useRunRouteId()` — the `runId` parsed
   from the URL only, `null` off run routes. Nothing resolves "the latest run."
3. **Route guard.** `RunLayout` validates `:runId` via `useRun`; an invalid
   (non-numeric) or nonexistent (404) run redirects to `/runs` (the run picker). It
   never auto-selects, and the run WebSocket only mounts once the run is confirmed.
4. **Run identity/stage come from the URL.** Switching run-scoped tabs preserves the
   `runId` (pipeline links are `/runs/:runId/<seg>`). The run switcher navigates to
   `/runs/<newId>/<currentSeg>` — same stage, new run. Zustand holds only UI state
   (e.g. `runSwitcherOpen`), never run identity.

## Consequences

- The silent-default bug is structurally impossible: there is no global surface from
  which a run-scoped screen opens without a `runId` already in the URL.
- Deep-linking a stale/invalid run-scoped URL lands on the run picker, not a guessed run.
- The dark/glass visual language is unchanged — this is a navigation-structure change,
  reusing existing components/tokens.
- Shipped as a foundation slice (`useRunRouteId` + `runSwitcherOpen`) + five
  file-disjoint slices (sidebar, header+switcher, guard, dashboard, palette) + a
  cleanup slice (delete `useResolvedRunId`, drop the Runs hero fallback), per the
  repo's parallel-slice workflow.
