# ADR 0015 — Project-scoped navigation and the run overlay

- **Status:** Accepted
- **Date:** 2026-08-26
- **Deciders:** Operator (PM review session), Q-Agent design handoff (v2), Q-Agent build
- **Supersedes:** [ADR 0004](0004-run-workspace-navigation.md) — run *workspace mode* is
  replaced by a full-screen overlay; its **no-silent-run-default** guarantee is kept and
  restated in §4.
- **Amends:** [ADR 0003](0003-client-side-routing.md) — the route map becomes
  project-nested; its URL-is-the-source-of-truth principle is unchanged and reinforced.
- **Resolves:** [ADR 0013](0013-project-scoping-model.md) decision 2 — **reversed**:
  routes nest under the project instead of staying flat with a `?project=` filter.
- **Extends:** [ADR 0006](0006-multiple-provider-connections.md) (per-project connection
  bindings)
- **Source:** `design/Copy of Q-Agent app design/handoff/` — `Q-Agent v2.dc.html` +
  `FLOW-CHANGES.md`
- **Related issues:** #693 (project tabs removed), #711 (publish dry-run), #720
  (user-driven run completion)

## Context

The domain is hierarchical — tickets, runs, reports and knowledge all belong to a
**project** — but the app presents `/tickets`, `/runs` and `/reports` as flat global
screens. ADR 0013 diagnosed the resulting defect correctly (#693: the project's
Tickets/Runs tabs navigated to *unfiltered* global lists, so they were deleted rather
than fixed) but chose to keep routes flat and add `?project=` as a filter.

The v2 design handoff takes the other branch, and two things it surfaces change the
balance of that argument:

1. **The provider problem.** The global ticket list carried its own provider switcher, so
   the same list could show tickets from a provider that had nothing to do with the
   project the user thought they were in. Provider is a property of the **project**, not
   of the ticket. A filter on a global list does not fix this; containment does.
2. **Run ownership becomes structural, not validated.** When runs are created only from
   inside a project and the scope pickers offer only that project's tickets, a
   mixed-project run is unreachable *by construction* — the validation ADR 0013 proposed
   is no longer the mechanism.

Separately, ADR 0004's run **workspace mode** — the sidebar swapping to a run-scoped nav
— exists to make run screens unreachable without a run. The v2 design achieves the same
guarantee with a full-screen overlay and no mode switch at all, which removes the "pick a
run" interstitial entirely.

## Decision

### 1. Project is the container

Nothing ticket- or run-shaped exists at workspace level. A ticket belongs to a project and
arrives through the connection configured on that project; a run belongs to a project.

- **Sidebar:** Dashboard, a **project tree**, All projects, Audit Log, Settings. The
  global Tickets / Runs / Reports entries are removed.
- Each project row expands to **six tabs**: Overview · Tickets · Runs · Project Knowledge
  · Connection · Reports, with live counts for Tickets and Runs and a pulsing badge for a
  run that is currently executing. All rows collapsed on load. There is deliberately no
  quick project switcher — movement between projects goes through the tree or the
  Projects list.
- The header loses the run-context bar, the project pill (it was a switcher) and the
  global **New Run** button.

**The one thing that must not be lost:** "what is running right now" is a genuinely
cross-project question and was ADR 0013's main argument for flat routes. It moves to the
**Dashboard**, which becomes a project comparison table (one row per project: ticket
source, tickets, test cases, runs, active run, knowledge confidence) over a compressed KPI
strip, with the activity feed and latest-runs list kept beneath. Removing the global lists
without this is a regression, not a simplification.

### 2. Routes nest under the project

```
/projects/:projectGuid/(overview|tickets|runs|knowledge|connection|reports)
/projects/:projectGuid/runs/:runId/:stage
```

Flat `/tickets`, `/runs`, `/reports` and `/runs/:runId/*` redirect to their nested
equivalents so existing links survive. ADR 0003's principle is unchanged: the URL is the
source of truth, and the project tab is now a **path segment** rather than `?tab=` — which
also retires `projectTab` from `store/ui.ts` for good.

### 3. One project, three connection roles

A project holds several connections; exactly one fills each role:

| Role | Purpose | Backend today |
| --- | --- | --- |
| `TICKET SOURCE` | the only place tickets come from (ADO or Jira) | `ProjectConfig.work_item_connection_id` ✅ |
| `CODE & KNOWLEDGE` | repo + PRs feeding Project Knowledge and automation | `ProjectConfig.repository_connection_id` ✅ |
| `TEST CASE TARGET` | where approved cases are created/linked and results published | **new** — `test_case_connection_id` |

Two of the three already exist (ADR 0006 §3), so the Connection tab largely *surfaces*
bindings the backend already has. The third is a new nullable column defaulting to the
ticket-source connection.

**Provider is derived from the project, not stored on the ticket.** Ticket filter facets
follow the source too (sprint/area/state/work-item-type for ADO;
sprint/epic/status/type/priority for Jira), and there is no provider switch anywhere in
the ticket flow.

The design handoff says to *delete* `ticket.provider`. In the prototype it is dead; in the
backend `provider_kind` has ~104 call sites across the adapters, comments, evidence, audit
and `LinkedTestCase`, and adapter resolution keys off it. **The column is kept as a
denormalised cache and its *source of writes* changes**: it is stamped from the project's
ticket-source connection at sync time instead of being an independent property. Same
semantics, no 104-site migration.

### 4. A run is a full-screen overlay, not a navigation mode

Runs open as an overlay on top of the project; exiting returns to Project → Runs. **The
sidebar never changes mode.** ADR 0004's `RunSidebar` / `RunContextHeader` and the in-run
branch of `navConfig` are removed.

- **Five human stages:** Review → Automation → Execution → Evidence → **Publish**.
- **The two automatic stages are hidden.** `processing` (Analyze) and `sync` (Link) get no
  stepper entry; while one is working the only indication is a spinner chip beside the run
  name in the overlay's top bar, and the wizard advances by itself when it finishes.
- **Back / Next only.** Future stages are locked; the stepper indicates, it does not
  navigate. Next is disabled until the stage is satisfied (Review needs ≥1 approved case;
  Execution needs the suite to finish).
- **Revisiting an earlier stage stays fully editable** — no read-only lock, no unlock
  button.
- **Two state variables, not one.** The server's `run.status` is *furthest progress*; the
  overlay tracks *the stage being viewed* separately, and exiting stores it so reopening
  resumes there. Today `RunContextHeader` maps status → stage one-to-one; going back a
  stage must never move `run.status` backwards. ADR 0005's terminal rule is untouched.
- Per-stage `PipelineRail` strips are removed — the overlay's stepper is the single
  progress indicator.

**ADR 0004's guarantee is retained.** A run stage is unreachable without a run *by
construction*, so the "pick a run" interstitial is deleted — but `RunLayout`'s guard
stays: a `:runId` that does not exist or is not the caller's redirects to Project → Runs.
Unreachable-by-design is not the same as unauthorised, and only the guard covers the
second.

### 5. Link options move to Create Run

Hiding the `sync` stage removes the screen (`CreateLinkSync`) that owns *link or not*,
*which subset of tickets*, and **dry-run** (#711, shipped days ago). Those options move
into the **Create Run modal**, where the user is already deciding the run's scope. They
are not dropped, and dry-run keeps a route into the product.

### 6. Completion is a stage, not a modal

**Finish run** lands on a terminal `done` stage (building on #720) with two variants:

- **Success** — "Run finished and published", five figures (tickets covered, cases
  approved & linked, passed, failed, pass rate), the per-ticket publish list, and two
  exits: *Open project reports* / *Start another run*.
- **Needs attention** — if any ticket failed to publish, the same screen turns amber,
  states how many of how many failed, and adds a primary **Retry failed publish**. A
  successful retry flips it to the success variant.

Because it is a real stage rather than a modal, it survives exit/resume: reopening a
finished run lands back on it. The footer's Next becomes "Back to <project>", all five
stage pills read complete, Back is disabled.

### 7. Ticket detail is reached only from the project

Breadcrumb "← <Project> · Tickets". Two actions: primary **Create run from this ticket**
(opens the run modal pre-scoped to it) and secondary **Add to run** (a menu of that
project's open runs), with a caption stating which provider the resulting run inherits.

### 8. Counts have a single source

Sidebar, Dashboard table, Projects cards and project Overview all read the same
project-scoped queries. No per-project literal totals anywhere.

## Consequences

**Positive**

- Closes #693 properly: the project's tabs are real views of the project, not links out to
  unfiltered lists.
- The provider mismatch becomes impossible rather than merely unlikely.
- Run ownership is structural — the scope picker cannot offer another project's tickets —
  so ADR 0013's mixed-project validation degrades from mechanism to cheap invariant.
- One fewer navigation mode and one fewer interstitial screen.
- The Publish stage becomes the natural home for ADR 0014's future "open a PR" action:
  two outward targets, one stage.

**Negative / cost**

- ADR 0004 is superseded and its shell components are deleted; ADR 0003's route map is
  rewritten. Two accepted ADRs change in one go.
- The cross-project view survives only if the Dashboard work (§1) actually ships. If it
  slips, QA leads lose "what is running right now".
- `Ticket.project_id` is a nullable bare integer, not an FK. Tickets synced before project
  stamping are `NULL` and, under containment, belong to no project and appear nowhere.
  They need a fallback — resolve via `connection_id` (ADR 0006) or an explicit
  "Unassigned" bucket — or they silently vanish.
- The overlay is the single largest slice; its two-variable stage state is the most likely
  source of subtle bugs.
- `Run.project_guid` is a hard prerequisite: Runs and Reports tabs cannot be filtered
  without it, and `_resolve_run_project_key()` (deriving from the first ticket) is exactly
  the fragility being removed.

## Implementation slices

| # | Slice | Depends on |
| --- | --- | --- |
| 1 | `Run.project_guid` stamped at creation + backfill; `?project=` filters for runs / tickets / reports; `Ticket.project_id` fallback for unstamped rows | — |
| 2 | Nested routes + redirects from the flat ones; `RunLayout` guard retargeted; `projectTab` removed from the store | 1 |
| 3 | Sidebar project tree: expand/collapse, live counts, active-run badge, "All projects"; global Tickets/Runs/Reports removed | 2 |
| 4 | Run overlay: 5-stage stepper, hidden auto stages + spinner chip, Back/Next gating, viewed-stage separate from `run.status`, resume | 2 |
| 5 | Completion stage: success / needs-attention variants, Retry failed publish | 4 |
| 6 | Connection tab: surface the two existing bindings, add `TEST CASE TARGET`; move link / subset / dry-run options into Create Run | 2 |
| 7 | Dashboard project comparison table + retained cross-project "running now" | 1 |
| 8 | Cleanup: remove `PipelineRail` from stage screens, delete `RunSidebar` / `RunContextHeader` / in-run `navConfig` branch | 4 |

Slice 1 is backend-only and independent. Slices 3 and 8 both edit the shell and
`navConfig.tsx`, so they are sequenced, never parallel (see CLAUDE.md, *Parallel
multi-slice work*). Slice 4 is the largest.

## Alternatives considered

**Flat routes with a `?project=` filter** (ADR 0013 decision 2). Rejected here: a filter on
a global list does not prevent the provider mismatch, and the "primary question is
cross-project" argument is answered better by a Dashboard built for it than by leaving
three global lists in the sidebar.

**Keeping run workspace mode and nesting only the project screens.** Rejected: two
navigation modes plus a project tree is more shell state, not less, and the overlay already
delivers ADR 0004's guarantee without a mode.

**Deleting `Ticket.provider_kind`** as the handoff suggests. Rejected — §3: correct
semantics, wrong blast radius. Change what writes it instead.

**Bundling the `Run` → `Session` rename into this work.** Rejected: the v2 design keeps
"Run" throughout, and a cross-layer rename inside an already-large UI refactor would make
every slice unreviewable. See ADR 0014, *Alternatives*.
