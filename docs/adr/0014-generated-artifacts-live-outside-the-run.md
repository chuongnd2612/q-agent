# ADR 0014 — Generated artifacts live outside the run

- **Status:** Accepted — delivery deferred behind
  [ADR 0015](0015-project-scoped-navigation-and-run-overlay.md); the decision stands,
  the slices are not scheduled yet.
- **Date:** 2026-08-26
- **Deciders:** Operator (PM review session), Q-Agent build
- **Narrows:** [ADR 0013](0013-project-scoping-model.md) — §9 promotes its slice 1 from
  a schema tidy-up to a prerequisite.
- **Extends:** [ADR 0002](0002-project-knowledge-config-and-multi-repo.md) (per-repo
  knowledge), [ADR 0006](0006-multiple-provider-connections.md) (connections own
  credentials), [ADR 0009](0009-per-user-workspace-filesystem-and-cloning.md) (per-user
  workspace)
- **Leaves [ADR 0005](0005-run-lifecycle-management.md) unchanged, deliberately.** The
  run keeps its name, its stages and its terminal rule.
- **Related issues:** #537/#538/#544/#546/#549 (automation-project epic), #178
  (invented references), #177 (AC coverage), #91/#98 (per-user ownership)

## Context

Q-Agent's unit of work is **testing a ticket**. A run analyses tickets, generates test
cases, has them approved, generates specs, executes them, collects evidence and
comments back. Both durable outputs of that pipeline are modelled as children of the
run: `TestCase.run_id` with `ondelete=CASCADE`, and `AutomationSpec` one-to-one with
`TestCase`.

For **test cases** the pipeline already publishes outward. The `sync` (Link) stage
creates approved cases in the provider and records `LinkedTestCase` — whose `run_id`
is nullable `SET NULL`, whose `ticket_external_id` is indexed, and which
`runs.py:668` deliberately *keeps* (nulling `run_id`) when a run is deleted. That row
is already a durable, ticket-keyed pointer to an artifact that outlives its run. The
model is right.

For **specs**, #538 introduced a persistent git-backed `AutomationProject` and #549
added a user-triggered export to a customer remote. But the repo is born inside
Q-Agent's workspace and the DB row keeps its own copy of the code — two owners for one
artifact. Since `spec_filename` is keyed on `(ticket, case_code)` (#540) and
`tests/<TICKET-ID>/` accumulates forever, a second run on the same ticket silently
overwrites the first run's file while run #1's `AutomationSpec` row keeps the old code.

Three observed consequences, all the same shape — **a run never reads back what an
earlier run published**:

1. **Provider duplicates.** `link_service` dedupes on `(run_id, test_case_id)` only.
   Run #2 approving "Login with valid credentials" creates a *second* ADO test case.
   After a few runs the customer's Test Plan holds the same case four times. Nothing
   reads `LinkedTestCase` at generation time — its only readers are the Ticket detail
   endpoint and the audit service.
2. **Blind spec regeneration.** Nothing consults the repo before generating, so a
   ticket that already has specs gets new ones written over the old. The
   REUSE > EXTEND > CREATE planner (#544) reuses *page objects* but never a spec.
3. **Coverage regression illusion.** The AC coverage matrix (#177) is built from the
   current run's cases alone, so a run that correctly generates cases for *new* AC only
   appears to cover less than its predecessor.

Two alternatives were designed and rejected — a `Session` entity replacing `Run`, and a
rename of `Run` to `Session`. See *Alternatives considered*.

## Decision

### 1. The run keeps its shape and its name

Run remains the ticket-testing pipeline of ADR 0005: linear, same stages, same statuses,
same terminal rule, same name. `TestCase` stays run-scoped — it is the **working
draft**, and that is correct.

### 2. Every generated artifact has a home outside the run

| Artifact | Draft, inside the run | Home, outside | Durable pointer |
| --- | --- | --- | --- |
| Test case | `TestCase` (`run_id`, CASCADE) | provider test plan | `LinkedTestCase` (ticket-keyed) |
| Automation spec | `AutomationSpec` | automation git repo | branch + commit sha on the run |

The run is an event. The artifacts it produces are not.

### 3. The automation repo is a real remote, bound per project

- **One automation repo per project**, separate from the application codebase, declared
  in project config with its own repository connection (ADR 0006). A project with
  several app repos partitions *inside* that one repo by directory rather than taking a
  repo each.
- **Two entry paths that converge immediately.** *Adopt* clones an existing repo;
  *Bootstrap* scaffolds Q-Agent's layout and pushes it. A local-only project is
  bootstrap that has not been pushed yet, so "Push to remote" is a state promotion,
  **not** a third mode — after the first step the two paths share one code path.
- `@q-agent/playwright-base` stays the default on Bootstrap and becomes **optional** on
  Adopt: a repo Q-Agent did not scaffold may legitimately not want it.
- **#549's rules hold, with one narrow exception.** Pushing to the remote's default
  branch is permitted only when the remote has **zero refs** — creating the first
  mainline of an empty repo is the entire point of Bootstrap. A remote holding any
  commit is refused and the user is directed to Adopt. There is no force push on either
  path, ever.
- The per-user scope of `AutomationProject` (ADR 0009, #91) is **retained**. The shared
  asset library is the remote; a per-user local clone is correct.

### 4. Clone at the start of the run, PR at the end

The PR is the last step but the checkout is the **first**. The planner's `inventory()`
and the `playwright test --list` + `tsc --noEmit` gates (#546) are only meaningful
against the whole real library; generating outside the repo and grafting the result in
at PR time would prove that a spec collects in a tree that is not the one being merged
— precisely the failure mode #546 exists to prevent.

- **Run start:** clone or pull the repo, create branch `qagent/<RUN-CODE>`.
- **During:** generate, gate, heal and execute against that checkout.
- **Run end:** commit, push the branch, open a PR. **Q-Agent never merges.**
- The run records branch + head sha, so evidence is always traceable to the exact code
  that produced it.

### 5. What is allowed into the PR

- **Per-case commits.** One commit per case, carrying that case's spec plus exactly the
  paths in its `plan_report.writable` (#544). Dropping a case is dropping its commit —
  no untangling a shared commit by hand.
- **Only specs with status `passed` or `product_defect`.** A test that correctly catches
  a real bug is a good test, and `failure_classifier` already tells the two apart;
  `failed` and `blocked` never reach the repo. Case-level `approval` — granted at
  Review, *before any code exists* — is not sufficient to merge code.
- **PR target** is configurable, defaulting to the repo's default branch.
- **No automation repo bound → the PR step is skipped**, and specs stay in the local
  project exactly as they do today.
- The PR belongs to the **Publish** stage of ADR 0015's run overlay (the `comment`
  status), which already exists to send results outward. Two targets — comment → ticket,
  code → repo — one stage. No ninth status.

### 6. Automation KB teaches style; the disk grants permission

Adopting a repo Q-Agent did not scaffold means its layout cannot be assumed. A Claude
pass — the `project-bootstrap` analogue for automation repos — records layout, naming
and organisation conventions, and **refreshes only on request** (never on a schedule,
never on HEAD movement).

`inventory()`, read from disk, remains the **sole** source of `importable`. The KB feeds
the planner's prompt and nothing else. #178 died from trusting a model's claim over the
disk; an AI-built knowledge base must never be able to authorise an import.

### 7. Read back before generating

- **Test cases.** Consult `LinkedTestCase` for the ticket. Match on `linked_ac` to
  decide *whether a new case is needed*; match on the provider `external_id` to decide
  *which case to update*. When AC change, **update the existing provider case** — its id
  is already referenced by suites and test-run history — and create a new one only for
  genuinely new AC. Update is the **default**, and the QA can override it and create.
- **Specs.** A ticket that already has specs in the repo defaults to **updating** them
  rather than regenerating from scratch. REUSE > EXTEND > CREATE applies to specs, not
  only to page objects.
- **Rejections are surfaced, not enforced.** A case rejected in an earlier run appears
  as a warning at Review. No automatic blocking: matching on content would be wrong more
  often than right.
- **The coverage matrix unions** the run's cases with the ticket's `LinkedTestCase`
  rows. Without this, a correct incremental run reports falling coverage.

### 8. Known degradations, accepted

- **An open, unmerged PR is invisible to the next run**, which clones the default branch
  and may re-create an asset that PR already adds. The mitigation is a warning ("N open
  PRs for this project") at the automation stage — **not** branching on top of pending
  PRs, a cure worse than the disease.
- **Deleting a run** cascades its `TestCase` rows while `LinkedTestCase` survives with a
  dangling `test_case_id` (a bare integer, deliberately not an FK). Case detail then
  exists only in the provider. Accepted: the provider is the home.

### 9. `Run.project_guid` is a prerequisite

ADR 0013 proposed this column as a schema correction. Under this ADR the run must know
its project in order to resolve **which repo to clone and open a PR into**, so ADR 0013
slice 1 becomes a prerequisite — and it is also required by ADR 0015, which schedules
it.

ADR 0013's proposed *"reject runs that mix projects"* validation is largely obviated by
ADR 0015: run creation happens inside a project and the scope pickers only offer that
project's tickets, so a mixed-project run is unreachable by construction. Keep the
server-side check as a cheap invariant, not as the mechanism.

## Consequences

**Positive**

- The automation suite accumulates in one place **the customer owns**, and each run
  starts from what previous runs left behind. Epic #537's goal — "each feature generates
  less code than the last" — extends from page objects to specs.
- Provider test plans stop filling with duplicates, the most externally visible defect
  of the current model.
- Run keeps its shape and its vocabulary: no status-machine migration, ADR 0005
  untouched, no rename.
- The gates now prove properties of the tree that will actually be merged.
- The per-user library-fragmentation worry dissolves without a schema change: the remote
  is the shared artifact, the clone is personal.

**Negative / cost**

- Every run now depends on a reachable git remote at start. Network or auth failure
  needs a clean degradation — fall back to the local project and warn; never fail the
  run over it.
- Concurrent runs on one project produce conflicting PRs. Resolution is human, in the
  provider's PR UI, by design.
- Per-case commits require `plan_report.writable` to be accurate. A case that writes
  outside its plan breaks commit partitioning and must be gated.
- Updating a provider case in place needs an adapter **update** path; today's flow only
  creates.

## Implementation slices

| # | Slice | Depends on |
| --- | --- | --- |
| 1 | `Run.project_guid` set at creation + backfill *(shared with ADR 0015 slice 1 — do it once)* | — |
| 2 | Automation-repo binding in project config + connection; Adopt (clone) and Bootstrap (scaffold + push, empty-remote rule); base package optional on Adopt | 1 |
| 3 | Automation KB: build, on-request refresh, planner prompt consumes it; `inventory()` unchanged | 2 |
| 4 | Run clones + branches at start; generation, gating, healing and execution run against that checkout | 2 |
| 5 | Per-case commits from `plan_report.writable`; `passed`/`product_defect` filter; push + open PR; record branch + sha | 4 |
| 6 | **Test-case read-back**: `LinkedTestCase` lookup, `linked_ac` matching, provider update path, coverage-matrix union | — |
| 7 | **Spec read-back**: existing spec in repo → update instead of regenerate | 4 |
| 8 | Warnings: open PRs for the project, cases rejected in earlier runs | 5, 6 |

Slice 6 depends on nothing here and fixes the most user-visible defect (duplicate
provider cases), so it can ship independently of the repo work — including before ADR
0015 completes.

The project-level automation-repo **UI** is not specified here. Everything above holds
whatever shape that screen takes.

## Alternatives considered

**A reopenable `Session` replacing `Run`** — durable and non-linear, with pipeline stages
demoted to per-case state and ADR 0005's terminal rule relocated to `Execution`, so a
closed session could be opened and continued. Rejected: Q-Agent's unit of work genuinely
*is* testing a ticket, so the linear pipeline matches the domain, and the accumulation
that model was meant to provide comes from the repo and the provider instead. It would
have cost a migration of the status machine, an amendment to ADR 0005 and a rewritten
pipeline UI, and bought nothing the two external homes do not already give.

**Renaming `Run` to `Session`** (vocabulary only, same lifecycle). Considered and
rejected: it is a wide, cross-layer rename — statuses, codes, routes, shell components,
WebSocket topics — for zero behaviour change, and the v2 design handoff keeps "Run"
throughout. Not worth the churn.

**Generate in Q-Agent's workspace, graft into the repo at PR time.** Rejected — §4: the
planner cannot reuse assets that are not present, and the gates would prove the wrong
tree.

**Let the Automation KB supply `importable`.** Rejected — #178.

**Hard-block duplicate provider cases.** Rejected: two cases against one AC is sometimes
legitimate. Default to update and let the QA override.

**One automation repo per application repo.** Rejected: the goal is *one* quality
automation project per project; splitting it by app repo splits the asset library that
makes it valuable.
