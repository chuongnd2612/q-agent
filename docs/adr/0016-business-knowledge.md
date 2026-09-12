# ADR 0016 — Business Knowledge as a first-class, project-scoped grounding source

- **Status:** Accepted
- **Date:** 2026-09-12
- **Deciders:** Operator (in-session), Q-Agent build
- **Extends:** [ADR 0002](0002-project-knowledge-config-and-multi-repo.md) (the code-derived
  Knowledge Base), [ADR 0009](0009-per-user-workspace-filesystem-and-cloning.md) (per-user
  ownership + the admin shared namespace), [ADR 0010](0010-dom-exploration-agent-kb-enrichment.md)
  (runtime-verified KB entries), [ADR 0015](0015-project-scoped-navigation-and-run-overlay.md)
  (project is the container)
- **Related issues:** #813 (epic), #814 (this ADR), #815 (data model), #822 (Azure DevOps wiki
  and its credential constraint), #501 (the hub never releases a PAT), #585 (`project_guid`)

## Context

Q-Agent grounds every AI stage in one knowledge source: `ProjectKnowledge`, built by
`project-bootstrap` pointing the Claude CLI at a repo checkout. Its payload is code-shaped —
`routes`, `selectors`, `pageObjects`, `fixtures` — and it is assembled for prompts through
exactly one path (`project_config_service.build_context()` → `prompts.render_project_context()`).

That source answers *how the product is built*. It cannot answer *what the product is supposed
to do*, which is the question a QC actually writes a test case from. Eligibility rules, claim
adjudication order, what "suspended" means for a member, which states a plan may not be sold in
— none of that is recoverable from a repository, however well it is parsed. A test suite
grounded only in code can verify that a button exists and never notice that pressing it does the
wrong thing for the business.

So a project needs a **second grounding source of its own kind**: wiki pages and uploaded
documents carrying the project's domain, rules and vocabulary. The decisive question is not
whether to ingest them — it is what *relationship* they have to the existing KB, because that
single framing choice determines whether the cold-start case (a project with no repository yet)
needs its own code path or falls out for free.

Three facts about this codebase constrain the design before any preference does:

- **There is no vector store anywhere.** No pgvector, no chroma, no embeddings in `api/`.
  Retrieval today is keyword overlap (`prompts._rank_by_relevance`).
- **There is no scheduler.** No APScheduler, no Celery, no cron entry point; `api/pyproject.toml`
  carries none, and nothing in `api/app` runs periodic work. Background work is per-request
  threads, started and awaited by the request that needs them.
- **`apply_build()` replaces the knowledge blob wholesale** — `row.knowledge = payload["knowledge"]`
  (`api/app/services/knowledge_service.py`) — and the knowledge endpoints are `GET` + `POST
  …/knowledge/build` only. There is **no `PATCH` on knowledge anywhere**. The code KB today is a
  machine-owned artifact with no human-edit surface at all.

## Decision

### 1. Business Knowledge is a peer source, and for authoring it is the primary one

Business Knowledge is **not** an enrichment of `ProjectKnowledge`, not an extra field on its
payload and not a document attached to a ticket. It is a first-class source alongside the code
KB, with its own rows, its own ingestion and its own lifecycle.

For **test-case authoring**, it is the **primary** source and code is secondary: business
knowledge says what must be true, code says which screens and fields exist to express it. For
**automation** (selector choice, spec generation) the ordering is the reverse, which is one
reason §7 defers those consumers rather than doing all stages at once.

This framing is the whole leverage of the epic. Treated as an enrichment, a project with no
repo has *no* knowledge and cold start needs a bespoke path. Treated as a peer, a project with
no repo simply has one of its two sources populated, and every downstream consumer already
handles a partially-populated context (`render_project_context` degrades per-section today).

*Would justify overturning:* if business facts turned out to be useful **only** in combination
with code facts — that is, if no consumer could do anything with business knowledge alone — the
peer framing would be ceremony and it should collapse back into the KB payload.

### 2. Scope unit is the project, keyed on `project_guid`

Business Knowledge rows are scoped to a single project, keyed on **`project_guid`** — the
identity that survives a rename (#585), already carried by `ProjectConfig`, `ProjectKnowledge`,
`AutomationProject` and `Run`. Not the project *key*/name, which is mutable; not the ticket,
which is too narrow (the domain outlives any work item); not the repository, which is the wrong
axis entirely (a project may have several repos and its business rules belong to none of them).

This matches ADR 0015 §1: the project is the container, and nothing ticket- or run-shaped exists
above it.

**There is no org-wide or global tier in v1, and that is a scheduling decision, not a blocked
one.** The mechanism already exists and is described in ADR 0009 §2: a shared tier would be
`owner_id IS NULL` rows in the admin-managed shared namespace, writes gated by `require_admin`,
and members obtaining a copy through the existing clone path (copy-on-clone, ADR 0009 §4) rather
than reading the shared rows live. Nothing in this ADR forecloses it. It is out of v1 because
the corpus that actually exists today is per-project (one client's wiki, one product's rules),
and because a shared tier is only worth its precedence and staleness complexity once two
projects demonstrably want the same document.

*Would justify overturning:* the same document being uploaded into three or more projects, or a
compliance/standards corpus that is genuinely organisation-wide. At that point implement the
ADR 0009 §2 shape above — do not invent a third sharing mechanism.

### 3. Ownership is unchanged from ADR 0009 — and this is the one place the model chafes

Business Knowledge uses the existing ownership model verbatim: per-user rows carrying `owner_id`,
plus the admin-managed shared namespace (`owner_id IS NULL`) that members clone from. It does
**not** fork the ownership model for one entity.

**Be honest about the cost.** Business documents are *organisationally* shaped in a way that
runs, specs and evidence are not. A run belongs to the person who started it; a company's
eligibility policy does not belong to whoever happened to upload it. Under this model, two
members of the same team working on the same project each ingest their own copy of the same
wiki, each pay the ingestion, and each hold a snapshot that can drift from the other's. That is
a real duplication and it is the reason §2's shared tier is the first thing to revisit.

It is accepted anyway, for reasons that are about blast radius rather than modelling purity:

- Every row-level authorisation helper, every workspace path resolver and every clone routine in
  the system assumes `owner_id`. A single entity with different ownership semantics would be a
  permanent special case in each of them, and the artifact-authorisation bugs that keep
  surfacing (most recently #819/#820) are precisely what a special case here would multiply.
- The admin shared namespace **already is** the org tier. A team that wants one canonical copy
  can have it today: an admin ingests it in the shared namespace and members clone. What is
  missing is not a model, it is the clone-through for this entity — which is scheduled (#831),
  not absent.
- Cloning an admin-curated snapshot is also the *attributable* answer (§4): each member's
  generated cases point at a document version that actually existed in their workspace.

*Would justify overturning:* real multi-user teams on one project hitting the duplication in
practice, or a requirement that a policy update reach every member without each of them
re-syncing. The fix then is a project-level (not user-level) row with member read access — a
genuine third ownership shape, and it should be taken deliberately with its own ADR, not
smuggled in as a nullable column.

### 4. Snapshot with manual re-sync — never live sync

Ingestion stores, per document: the **raw bytes**, the **normalized markdown**, `fetched_at`, and
a **content hash**. Nothing re-fetches on its own. A user re-syncs a source explicitly; until
they do, the stored snapshot is what the prompts see.

The argument is **attributability**, not implementation convenience. A generated test case is an
artifact a human reviews, approves, and publishes to a work-item tracker. It must be traceable to
the exact document version it was derived from — otherwise "why does this case assert a 30-day
grace period?" has no answer once the wiki page has moved on. Under live sync, the ground shifts
under an approved artifact silently and retroactively. Under snapshots, an upstream change
surfaces as a **stale badge** on the source (hash differs from the last fetch) and becomes a
decision the user takes, with the old cases still explicable.

The supporting fact is that this codebase **has no scheduler** (verified above). Live sync would
therefore mean introducing a new architectural component — a periodic worker, its own failure
and retry semantics, its own per-user credential lifetime — to buy a property (freshness) that
actively damages the property we want (attributability). It is not a feature flag away; it is a
new tier of the system, and it would be bought at a loss.

*Would justify overturning:* nothing about freshness alone. Only a consumer that genuinely
cannot tolerate a stale snapshot — and such a consumer should first be asked why it is not
re-syncing at the point of use, which requires no scheduler.

### 5. Override is an overlay; ingested content is immutable

Ingested content is **never edited in place**. Human corrections, additions and exclusions are
**separate rows** that reference the ingested unit and win over it. Re-syncing a source replaces
the ingested snapshot and must leave every correction standing.

Resolution order when the same fact is asserted twice, highest wins:

| # | Layer | Origin |
| --- | --- | --- |
| 1 | **Human pinned corrections** | a human explicitly overriding an ingested or inferred fact |
| 2 | **Human additions** | a human stating a fact no source carries |
| 3 | **Ingested business facts** | distilled from a synced business document (§4) |
| 4 | **Runtime-verified code KB entries** | observed in the running app — `verified_at_runtime` (ADR 0010 §6) |
| 5 | **Source-inferred code KB entries** | parsed from source by `project-bootstrap` (ADR 0002) |

The ladder is two principles stacked, not an arbitrary ranking. **Humans outrank machines** (1–2
over 3–5) because a correction is the only signal that a source was *wrong*, and a system that
lets a re-sync silently undo it will not be corrected twice. **Observation outranks inference**
(3–4 over 5), which is exactly ADR 0010 §6's rule extended one level up: a business document is a
statement of intent by the people who own the product, a runtime observation is evidence, and a
source parse is a guess.

Immutability is what makes the whole thing safe to re-sync, and it is deliberately the opposite
of how the code KB behaves today: `apply_build()` overwrites `row.knowledge` wholesale, so any
manual change to the code KB would be destroyed by the next bootstrap. That asymmetry is the
highest-risk detail in the epic. It is not hypothetical — there is no `PATCH` on knowledge today
precisely because the KB has never been human-editable — and making it editable (#828) is
therefore gated on carrying pinned entries through `apply_build` (#827), with its own test.

### 6. Cold start works for authoring — and stops there

**Business knowledge alone is sufficient to author manual test cases.** A project with a wiki and
no repository can produce reviewable, publishable test cases. This is a consequence of §1, not a
separate feature: with business knowledge as the primary authoring source, "no code KB" is a
context with one section empty, which the renderer already tolerates. The actual blocker is a
*prompt sentence*, not code — `skills/test-case-generator/SKILL.md` lists `knowledge.md /
knowledge.json` under **Inputs → Required** and says *"If either is missing, stop and request
it."* The Python path degrades gracefully; the skill refuses.

**State the boundary plainly, because the phrase "works without code" will otherwise be read as
more than it is.** Everything downstream of authoring still requires a running application and
stays gated:

- **Automation generation** needs real selectors. `placeholder_gate` marking a case `blocked`
  for missing grounding is **correct** and must not be relaxed — ADR 0002's never-invent-a-selector
  rule is unchanged, and ADR 0010 exists precisely because the honest answer to a missing selector
  is to go and observe one.
- **Execution** needs a deployed app and a base URL.
- **Healing** needs a live DOM; `heal_service` returning `blocked` with a missing-KB-grounding
  reason is the right behaviour and stays.

Business knowledge makes those failures better *described* — it can say what the screen is for —
but it cannot make them succeed. Cold start is a claim about authoring only.

### 7. v1 consumers: the test-case prompts, and nothing else

Business knowledge is injected only into the three test-case prompts —
`build_combined_prompt`, `build_review_prompt`, `build_case_regenerate_prompt` — and it reaches
them through the existing single seam: `project_config_service.build_context()` assembles it,
`prompts.render_project_context()` renders it. No new assembly path, no per-consumer plumbing.
That one seam is what makes this feature tractable at all.

The other stages — automation planning, spec generation, healing, failure classification — are
**deferred to v2** (#833) for a reason that is about value per token rather than difficulty:
those prompts are already the largest in the system, and business context helps them with
*naming*, not with selector choice. Spending the scarcest budget in the system on the stage least
able to use it is the wrong first move.

A **summarization layer is a brief, not an index.** Documents are distilled into a short,
prompt-sized brief plus discrete facts. Given the absence of any vector store (verified above)
and the expected corpus size — a project wiki, not a document warehouse — introducing embeddings
would be both unjustified and inconsistent with everything else in the codebase, where relevance
is keyword overlap.

One latent defect gets fixed on the way in, because it becomes materially worse once business
facts join the block: all three test-case prompts call `render_project_context()` with **no
`rank_query`** (`api/app/services/prompts.py` — four such call sites), so the test-case path
blind-slices the first N routes and selectors instead of ranking them by the case at hand. Only
`spec_service` ranks today (`_case_rank_query`). Adding more content to an unranked, truncated
block would quietly push the relevant material out of the prompt (#825).

### 8. QC voice is enforced deterministically, not requested in a prompt

The output must read as a QC wrote it. That is enforced by a **deterministic validator over
generated cases** with a fixture corpus (#823/#829), not by adding instructions to a prompt.
Prompts drift between model versions and across re-generations; a gate with fixtures does not,
and it is the only form of the requirement that can fail a build.

### 9. v1 sources, and the Azure DevOps wiki credential constraint

v1 ingests: **file upload** (`.md` / `.txt` only), a **generic URL**, **GitHub markdown**, and an
**Azure DevOps wiki**. PDF/DOCX need `pypdf`/`python-docx` plus a conversion-fidelity problem, and
Notion needs a new credential kind and a blocks-to-markdown converter; both are deferred (#832).

The ADO wiki source carries a constraint that is recorded here because discovering it later would
read as an oversight, and because it changes the *design*, not just the error copy (#822):

- **A hub-backed connection holds no credential at all.** `ProviderConnection.hub_connection_id`
  is documented in the model as mirroring a connection EmeHub owns, whose *"`secrets` are empty
  and always will be: the hub never releases the PAT (#501). Nothing may attempt a direct
  provider call with it."* And `services/hub_client.py` exposes **no wiki endpoint** — tickets,
  connections, projects, saved queries, credential grants, project/repo knowledge, and nothing
  else. For a hub-backed deployment, direct wiki ingestion through the project's existing
  connection is **impossible in-product** without a new hub endpoint.
- **Even a locally-held ADO PAT is probably scoped wrong.** Existing connections were provisioned
  for work items (`vso.work`) and code (`vso.code`). Wiki read needs **`vso.wiki`**, so an
  otherwise healthy connection simply 401s on `/_apis/wiki/wikis`.

**Therefore the per-source, wiki-scoped token is the primary path, not a fallback.** The project's
existing connection is used only when it is local *and* a preflight confirms it can read wikis.
This keeps the slice correct in both deployment shapes and stops the feature being hostage to the
hub roadmap. The preflight runs **at source creation**, not at first sync, so a scope problem is
learned while the user is configuring the source rather than hours later in a failed run — and
the three refusals (hub-backed, wrong scope, no wiki enabled) must be told apart in the copy, since
"connection broken" would send a user to re-do a connection that is fine.

*Would justify overturning:* EmeHub gaining a wiki endpoint. The per-source token then becomes the
fallback it would otherwise have been, and nothing else in this ADR changes.

## Consequences

**Positive**

- Test cases are grounded in what the product is *for*, not only in how it is built — the gap the
  code KB structurally cannot close.
- Cold start (project with a wiki, no repo) is authoring-capable with no bespoke path, purely as a
  consequence of the peer framing in §1.
- Attributability is preserved: every generated case traces to a document version that existed.
- Human corrections become durable for the first time in the system, and the ladder in §5 gives
  every future source a defined place to slot into.
- The single context seam means one insertion point reaches all current and future consumers.

**Negative / cost**

- **Ownership duplication** (§3): per-user rows mean teammates on one project each hold their own
  snapshot. Accepted knowingly; it is the first thing to revisit.
- **Snapshots go stale** (§4). Staleness is surfaced, never fixed automatically; a user who ignores
  the badge authors against an old policy, and the product cannot prevent that.
- **The `apply_build` asymmetry** (§5): ingested business content is immutable and re-syncable, while
  the code KB is still overwritten wholesale. Until #827 lands, "corrections survive" is true of one
  source and not the other — a confusing half-state that must not be left standing long.
- **Two grounding sources mean two ways to be wrong**, and a contradiction between a wiki page and
  the shipped code now has to be adjudicated by the ladder rather than noticed by a human.
- The prompt block grows. §7's ranking fix is a prerequisite, not a nicety.

## Not in scope (deferred / excluded)

- **An org/global shared tier** (§2) — the ADR 0009 §2 mechanism is named so it can be picked up.
- **Notion, and PDF/DOCX upload** (#832).
- **Grounding the automation / spec / heal / classifier stages** (#833).
- **Ticketless authoring** — a free-text requirement with no work item. Every entry point today is
  a `Ticket` row and project resolution runs *through* the ticket's connection. That is a new run
  kind, not a variation on this one.
- **A vector store / embedding retrieval** (§7) — excluded on grounds of corpus size and codebase
  consistency, not difficulty.
- **Live sync of any kind** (§4).
