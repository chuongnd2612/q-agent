# ADR 0003 — Client-side routing and URL-addressable navigation

- **Status:** Accepted — **route map amended** by
  [ADR 0015](0015-project-scoped-navigation-and-run-overlay.md) §2: screens nest under
  `/projects/:projectGuid/*` and the run stage becomes a path segment. The principle
  below — the URL is the source of truth, not Zustand — is unchanged and reinforced
  (`projectTab` leaves the store entirely).
- **Date:** 2026-07-04
- **Deciders:** Operator (via in-session decisions), Q-Agent build
- **Supersedes/extends:** builds on [ADR 0001](0001-scope-architecture-and-live-integrations.md)

## Context

The frontend had **no router**. Navigation was Zustand-state-driven: a `screen`
string in `store/ui.ts` selected which component `App.tsx` rendered, with three
companion fields carrying context — `activeProject` (project name), `activeTicket`
(externalId), `activeRunId` (number) — plus `projectTab` and several intra-screen
selection fields (`reviewOpenTicket`, `evidenceTicket`, `selectedSpecCaseId`). The
URL never changed.

Because Q-Agent is **run-scoped** (everything after ticket selection belongs to a
Run — see CONTEXT.md), the missing URL broke the things that matter most:
deep-linking a run, refreshing without losing your place, and browser back/forward.
Each run-scoped screen also opened its **own** `useRunSocket` WebSocket, so the
live-progress connection tore down and reconnected on every intra-run navigation.

## Decision

Adopt **`react-router-dom`** (`createBrowserRouter` + `RouterProvider`) as the
navigation source of truth. Migrate navigation state out of Zustand into route
params; keep Zustand for **UI-only** state.

### Route map

```
/                                Dashboard
/projects                        Projects
/projects/:projectName           ProjectDetail        (?tab=overview|knowledge|settings)
/tickets                         Tickets
/tickets/:externalId             TicketDetail
/runs                            Runs
/runs/:runId                     RunLayout — owns the single run WebSocket
  ├ (index)                      RunDetail
  ├ review                       ReviewCenter         (?ticket= expanded accordion)
  ├ sync                         CreateLinkSync
  ├ automation                   Automation           (?case= selected spec)
  ├ execution                    Execution
  ├ evidence                     Evidence             (?ticket= selected ticket)
  └ comment                      CommentPublish
/reports                         Reports
/audit                           AuditLog
/settings                        Settings
*                                → redirect to /
```

### Path vs query params

- **Primary resources are path params:** `:projectName`, `:externalId`, `:runId`.
  The run is the URL spine (`/runs/:runId/*`) so refresh/back/deep-link land on the
  right run. `projectName` is `encodeURIComponent`'d (project names contain spaces).
- **Intra-screen *selection* is a query param:** which accordion is expanded
  (`?ticket=`), which spec (`?case=`) or evidence ticket (`?ticket=`) is selected,
  and which project tab (`?tab=`). These are optional, non-structural, still
  deep-linkable, and keep the route tree flat.
- **Pure UI state stays in Zustand:** command-palette open/query, create-run modal +
  its form fields, tickets list selection/search/filters, review edit draft, evidence
  active media tab, and the annotation tool. These are ephemeral and not worth a URL.

### "Current run" is derived, not stored

`activeRunId` is removed. The sidebar / top-bar / command-palette links that target
"the current run" resolve it via a `useResolvedRunId()` helper: the `:runId` from the
URL when inside a run route, else the most-recent non-done run from the runs query
(the same fallback `App.tsx` used). No stale duplicate in the store.

### `activeProject` is removed

It was never a global filter — only ProjectDetail and the top-bar label read it.
ProjectDetail now derives its project from `:projectName`; the top-bar label shows
the current project when on a project route, else the first project from the query.

### WebSocket bound to the run route

A single `RunSocketProvider` mounted by `RunLayout` opens **one** socket for
`:runId`, performs the standard TanStack Query cache invalidation, and fans events
out to screen-local subscribers via a `useRunEvents(handler)` hook (used by RunDetail
for per-ticket phase messages, Execution for manual-login prompts, and Automation for
spec/exec refresh). Because it lives on the layout, the socket **persists** across
review→automation→execution navigation instead of reconnecting each time.

### Migration strategy — temporary bridge (removed on completion)

To land the migration in independently-green, mostly-parallel slices rather than one
big-bang, the foundation slice introduces the router and a **temporary bridge**:

- Legacy store nav actions (`navigate`, `openTicket`, `openProject`, `setActiveRun`,
  `setProjectTab`) become thin adapters over the data router's `router.navigate(...)`.
- A `UrlStoreSync` component mirrors the URL back into the retained legacy fields so
  not-yet-migrated screens keep reading `activeRunId`/`activeProject`/`activeTicket`
  and keep working — deep-linking/refresh/back are functional after the foundation
  slice alone.

Screens are then migrated off the bridge to native router hooks
(`useParams`/`useSearchParams`/`useNavigate`) in parallel, and a **final cleanup
slice removes the bridge and the legacy nav fields entirely** — at which point
navigation state genuinely lives only in the URL. The bridge is scaffolding, not a
permanent dual source of truth.

## Consequences

- Deep-linking, refresh, and browser back/forward work; runs are shareable by URL.
- One WebSocket per run visit instead of one per screen; fewer reconnects.
- This is a **routing migration, not a redesign** — the dark/glass visual system,
  glass panels, and Framer Motion transitions are preserved (the page transition is
  re-keyed on `location.pathname`).
- No unit-test harness exists in `app/`; the per-slice gate is `npm run typecheck` +
  `npm run build`, plus a Playwright screenshot check for UI-affecting slices.
- Trade-off accepted: the foundation slice ships throwaway bridge code that a later
  slice deletes, in exchange for parallelizable, individually-green slices.
