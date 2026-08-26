# Q-Agent — flow redesign (v2)

Source of truth: `Q-Agent v2.dc.html`. Old model preserved in `Q-Agent.dc.html`.

## The one rule that changed

**Project is the container.** A ticket belongs to a project, and it arrives through the
provider configured on that project. Runs belong to a project too. Nothing ticket- or
run-shaped exists at workspace level any more.

Before: `Tickets` and `Runs` were global screens. The ticket list had its own
provider switcher (Azure DevOps / Jira / GitHub), so the same list could show tickets from a
provider that had nothing to do with the project you thought you were in.

After: provider is configured once per project (Connection tab) and every downstream screen
inherits it. There is no provider switch anywhere in the ticket flow.

## Navigation model

| | Before | After |
|---|---|---|
| Sidebar | Dashboard, Projects, Tickets, Runs, Reports, Audit, Settings | Dashboard, **Projects tree**, All projects, Audit Log, Settings |
| Project | detail screen, tabs redirected to global screens | detail screen owning **6 tabs** |
| Tickets | global screen + provider switcher | **project tab**, source read-only |
| Runs | global screen | **project tab** |
| Reports | global screen | **project tab** |
| Run stages | sidebar swapped into "run workspace mode" | **full-screen wizard overlay** |
| Guard screen | "pick a run" interstitial | removed — unreachable by construction |

### Sidebar project tree
Every project is an expandable row (all collapsed on load). Expanding reveals
Overview · Tickets · Runs · Project Knowledge · Connection · Reports, with live counts for
Tickets and Runs and a pulsing badge for a run that is currently executing. There is no quick
project switcher — you move between projects through the tree or the Projects list, by design.

### Header
Lost the run-context bar, the project pill (it was a switcher) and the global **New Run**
button. Run creation now only exists inside a project, so a run can never be created without
a project and its provider.

## Providers: one project, several roles

A project has multiple connections but exactly one is the ticket source:

- `TICKET SOURCE` — the only place tickets come from (Azure DevOps or Jira).
- `CODE & KNOWLEDGE` — GitHub repo + PRs feeding Project Knowledge and automation.
- `TEST CASE TARGET` — where approved cases are created/linked (ADO Test Plans) and where
  results are published back.

All three live on the project's **Connection** tab with fields, connection state, last sync,
Test connection and Edit credentials. Workspace Settings → Integrations keeps the credential
vault; the project decides which connection it uses.

Consequence in the data model: a ticket's provider is no longer a property of the ticket.
It is derived from its project. Ticket filter facets also follow the project's source
(sprint/area/state/work-item-type for ADO, sprint/epic/status/type/priority for Jira).

## Run = a wizard, not a workspace

Runs open as a **full-screen overlay** on top of the project; exiting returns to
Project → Runs. The sidebar never changes mode.

- **Five human stages**: Review → Automation → Execution → Evidence → Publish.
- **Automatic stages are hidden** (Analyze, Link). While one is working, the only indication
  is a small spinner chip next to the run name in the overlay top bar; it disappears when the
  stage finishes and the wizard advances by itself. No footer strip, no stepper entry.
- **Back / Next only.** Future stages are locked; the stepper is an indicator, not navigation.
  Next is disabled until the stage is satisfied (Review needs ≥1 approved case; Execution
  needs the suite to finish).
- **Revisiting an earlier stage stays fully editable** — no read-only lock, no unlock button.
- **Resume**: exiting stores the stage you left; reopening the run lands there. The sidebar
  badge marks a run that is mid-flight.
- Per-stage PipelineRail strips were removed — the overlay's stepper is the single progress
  indicator.

### Completion screen (new)
**Finish run** no longer just closes the overlay — it lands on a terminal `done` stage with
two variants:

- **Success** — green check, "Run finished and published", five figures (tickets covered,
  cases approved & linked, passed, failed, pass rate), the per-ticket publish list, and two
  exits: *Open project reports* / *Start another run*.
- **Needs attention** — if any ticket failed to publish, the same screen turns amber:
  "Run finished — publishing needs attention", states how many of how many tickets failed, and
  adds a primary **Retry failed publish**. A successful retry flips the screen to the success
  variant.

The footer's Next becomes "Back to <project>"; all five stage pills read as complete; Back is
disabled (the run is over).

## Dashboard

Replaced the KPI-card grid with a **project comparison table**, one row per project:
Project · Ticket source · Tickets · Test cases · Runs · Active run · Knowledge confidence.
Row click opens the project. Workspace KPIs are compressed into a small header strip; the
activity feed and latest-runs list are kept beneath.

## Ticket detail

Reached only from Project → Tickets. Breadcrumb reads "← <Project> · Tickets".
Two actions: primary **Create run from this ticket** (opens the run modal pre-scoped to it),
secondary **Add to run** (a menu of that project's open runs). A caption states which
provider the resulting run inherits.

## Copy

English, technical terms unchanged (run, ticket, test case, Playwright, provider names).

## Run ownership

A run is stamped with its project at creation (`run.project = activeProject`) and the run
scope can only contain that project's tickets — the sprint/assigned/selected pickers filter
against the project's ticket set instead of the whole workspace. Runs list, Dashboard row and
sidebar badge all read the same stamp, so a run created in Claims Portal cannot show up under
Surency Platform.

## Notes for re-work

- `projectConfig` (keyed by project name) is the new source of truth: `source`,
  `ticketIds`, `testCases`, `conns[]`. `runOwner` maps run id → project; replace both with
  real foreign keys (`ticket.projectId`, `run.projectId`, `project.connections[]`).
- Screen state is still a flat `screen` string plus `projectTab`; a real router should be
  `/projects/:id/(tickets|runs|knowledge|connection|reports)` and
  `/projects/:id/runs/:runId/:stage`, with the run route rendering the overlay.
- `requestRunScoped` no longer needs a guard screen: a run stage without an open run simply
  sends you to Project → Runs.
- **`ticket.provider` is a dead field.** Nothing reads it any more. Every provider badge,
  provider label and linked work-item kind (`User Story` vs `Bug`) is derived from
  `projectConfig[project].source`. Delete the column rather than keeping it in sync.
- Counts have a single source: `projTicketIds(name)` and `runsOf(name)`. Sidebar, Dashboard
  table, Projects cards and project Overview all call them — don't reintroduce per-project
  literal totals.
- The completion screen is a real stage (`screen === 'done'`), not a modal, so it survives
  exit/resume: reopening a finished run lands back on it.
- Publish state per ticket (`publish[ticketId]`) drives the completion variant; the retry path
  reuses the existing `retryFailed` action.

## Files

- `Q-Agent v2.dc.html` — the new flow (this document describes it).
- `Q-Agent.dc.html` — previous global-Tickets model, kept for diffing.
- `support.js` — runtime required to open either file in a browser.
- `PipelineRail.dc.html`, `Q-Agent Auth.dc.html` — unchanged; the rail is no longer used by
  the run overlay.
