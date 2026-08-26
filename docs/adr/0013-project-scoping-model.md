# ADR 0013 — Project scoping model (flat routes, project as a first-class filter)

- **Status:** Resolved by later ADRs — do not implement from this document.
  **Decision 1** (`Run.project_guid`) is confirmed and promoted to a prerequisite by
  [ADR 0014](0014-generated-artifacts-live-outside-the-run.md) §9 and scheduled as slice 1
  of [ADR 0015](0015-project-scoped-navigation-and-run-overlay.md); its mixed-project
  *validation* is largely obviated there, since run creation is project-scoped by
  construction. **Decision 2** (keep routes flat, project as a `?project=` filter) is
  **reversed** by ADR 0015 §2 — routes nest under the project. The context and the entity
  table below remain accurate and are why both later ADRs exist.
- **Date:** 2026-08-26
- **Deciders:** Operator (PM review session), Q-Agent build
- **Extends:** [ADR 0003](0003-client-side-routing.md) (URL-driven navigation),
  [ADR 0004](0004-run-workspace-navigation.md) (run workspace mode),
  [ADR 0002](0002-project-knowledge-config-and-multi-repo.md) (project knowledge/config)
- **Related issues:** #693 (project detail tabs removed), #583/#585/#587 (project GUID)

## Context

The domain is hierarchical — Tickets, Runs, Reports and Knowledge all conceptually
belong to a **Project** — but the app presents them as a flat set of global screens
(`/tickets`, `/runs`, `/reports`) alongside `/projects`. The question raised in review
was whether that flattening is wrong.

Inspecting the model shows the flattening is **not primarily a navigation choice — it
is forced by the schema**. Only some entities actually carry a project reference:

| Entity | Project reference | Notes |
| --- | --- | --- |
| `ProjectKnowledge` | ✅ `project_guid` + `project_key` + `repo` | genuinely project-owned |
| `ProjectConfig` | ✅ `project_guid` | |
| `AutomationProject` | ✅ unique `(owner_id, project_key, repo)` | |
| `Ticket` | ⚠️ `project_id: Integer, nullable, index` | **not a real FK** — a bare int |
| **`Run`** | ❌ *none* | the gap |
| `Report`, `Execution`, `Evidence`, `TicketComment` | ❌ only `run_id` | project is 3 hops away |

Because `Run` has no project column, a run's project identity is **re-derived at
runtime from its first ticket** — `playwright_runner._resolve_project_for_run()`:

> *"Walks the run's first ticket to its provider, resolves the project key, and reads
> that project's config."*

Consequences observed:

1. **A run may silently mix projects.** Nothing prevents selecting tickets from two
   projects into one run, yet base URL, storage state (saved login), automation
   project and knowledge base all resolve from a single `project_key` — taken from
   whichever ticket sorts first. Such a run executes against the wrong target quietly.
2. **Project-scoped lists are impossible.** There is no column to filter on. This is
   the root cause of #693: the Project detail screen's Tickets/Runs tabs navigated to
   the *unfiltered* global lists, so they were **deleted** rather than fixed.
3. **Reports cannot be cut by project**, for the same reason.

## Decision

Two separate conclusions — the schema is wrong, the navigation is not.

### 1. Fix the data model (the actual defect)

- Add `Run.project_guid`, assigned **at run creation**, not derived on read.
  Nullable → backfilled from the first ticket → enforced non-null.
- **Reject runs that mix projects** at creation time. A run belongs to exactly one
  project. (See *Open questions* — this is the assumption the whole ADR rests on.)
- Promote `Ticket.project_id` to a real foreign key to `projects.id`.

`Report` / `Execution` / `Evidence` / `TicketComment` keep hanging off `run_id`; once
`Run` carries the project, one join is enough and no denormalisation is needed.

### 2. Keep routes flat; make project a first-class *filter*

Do **not** nest routes as `/projects/:guid/runs/:runId/...`. Instead:

- Query-param scoping, consistent with ADR 0003 (selection lives in query params):
  `/runs?project=<guid>`, `/tickets?project=<guid>`, `/reports?project=<guid>`.
- A project selector in the list-screen header, sticky for the session.
- Restore **Runs** and **Reports** entries on Project detail — this time linking to the
  *filtered* global list with a breadcrumb back to the project. That is precisely what
  #693 complained was missing, and it can only be built after decision 1.

Rationale for staying flat:

- The primary user question is *"what is running right now"*, which is inherently
  cross-project. A flat Runs/Dashboard answers it directly.
- Nesting would deepen every URL and cut across `RunLayout`, which is already a
  separate navigation mode per ADR 0004.
- Mature tools do both: GitHub has global `/pulls` **and** `/<repo>/pulls`; Linear has
  My Issues **and** team views. The defect is the missing **downward path from a
  project**, not the existence of a cross-project path.

## Consequences

**Positive**

- The hierarchy becomes real where it matters (data), enabling project-scoped Runs,
  Tickets and Reports without a router rewrite.
- Removes the fragile first-ticket inference; project resolution becomes a column read.
- Closes the class of silent-wrong-target failures from mixed-project runs.
- Unblocks a correct fix for #693.

**Negative / cost**

- A migration plus backfill over existing `runs`; rows whose first ticket no longer
  resolves need a null-project fallback path in the UI.
- Run creation gains a validation that can now *reject* a selection users could
  previously make. Needs a clear error in the Create-Run flow.
- `project_key` (name) and `project_guid` coexist during the bridge, as with #585.

## Implementation slices

| # | Slice | Depends on |
| --- | --- | --- |
| 1 | **Foundation (solo)** — `Run.project_guid`, migration + backfill, set at creation, reject mixed-project runs | — |
| 2 | `Ticket.project_id` → real FK to `projects.id` + index | 1 |
| 3 | `?project=` filtering for Runs / Tickets / Reports (API + UI) | 1 |
| 4 | Restore Runs/Reports entry points on Project detail, deep-linking with the filter; close #693 | 3 |

## Open questions

1. **May a single run span multiple projects?** This ADR assumes **no** and enforces
   it. If a cross-project run is a real use case, `Run.project_guid` must instead
   become a join table and slice 1 changes shape entirely.
2. Should `?project=` persist across screens (a global scope selector) or reset per
   screen? Proposed: sticky per session, cleared explicitly.
