# ADR 0010 — DOM Exploration Agent for Knowledge Base enrichment

- **Status:** Accepted
- **Date:** 2026-07-15
- **Deciders:** Client (via `CLIENT-BRIEF-exploration-agent.md`), Q-Agent build
- **Extends:** [ADR 0001](0001-scope-architecture-and-live-integrations.md) (real engines, no mock),
  [ADR 0002](0002-project-knowledge-config-and-multi-repo.md) (per-repo Knowledge Base)

## Context

Q-Agent generates Playwright specs from a per-repository **Knowledge Base** (KB)
built by `project-bootstrap` reading source code. When the KB lacks a real
selector or route for a screen, `placeholder_gate` correctly marks the case
`blocked` — the generator must never invent selectors (ADR 0002). But some gaps
are *recoverable*: the screen exists in the running app; the static source-parse
simply didn't capture a usable selector (dynamic rendering, missing test-ids in
source, runtime-only routes).

We add an optional **exploration agent** that drives a real browser to observe
the live app, discover the real routes/selectors for a target screen, and write
them back into the KB as **runtime-verified** entries. It does **not** generate
tests — the deterministic `automation-generator` still does, now with a richer
KB. Exploration is **user-triggered** from a blocked case, not automatic.

Several forks had to be settled before slicing the work.

## Decision

### 1. Browser driving — persistent Node Playwright driver

The observe→decide→act (ReAct) loop is inherently interactive: the agent must
observe page state *after each action* to decide the next one. The existing
automation/self-heal pipeline drives Playwright as **one-shot `npx playwright
test` subprocesses** with generated spec files — there is no persistent page.

We introduce a **long-lived Node driver** (`pw_scripts/explore_session.cjs`) that
holds **one** browser + page open for the whole session. The Python service
(`exploration_agent.py`) drives it step-by-step over a **line-delimited JSON
protocol** on stdin/stdout: `{"cmd":"observe"}` → `{a11y, dom}`;
`{"cmd":"act", action, args}` → `{ok, error, changed}`.

Rationale: reuses the **same installed Node Playwright** the rest of the app uses
(no second engine), reuses the ADR-0002 `storageState` auth capture and the
self-heal distilled-DOM extraction logic, keeps state-changing actions correct
(no replay), and keeps per-step latency low.

*Alternatives rejected:* **replay-via-spec-runner** (regenerate+run a full spec
replaying all prior actions each step) — maximal reuse but O(n²) latency and one
stale action fails the whole session; **playwright-python** — standard but adds a
*second* Playwright engine (two installs, version-skew risk, heavier operator
setup) alongside the Node one.

### 2. Observation representation

Each observe returns a text representation the model can read:

- **primary:** the accessibility tree (`page.accessibility.snapshot()`) — role +
  name, maps directly to `getByRole` locators (**net-new**);
- **plus:** a trimmed DOM extract of interactive elements keeping real
  attributes (`data-testid`, `id`, `name`, `aria-*`, visible text) — reuses the
  self-heal distilled-DOM query;
- **optional:** a screenshot only when the text representation is insufficient
  (canvas / non-semantic UI).

### 3. Action contract (fixed, backend-executed)

The agent may emit only: `goto(url)`, `click(role,name)` / `click(selector)`,
`fill(role,name,value)` / `fill(selector,value)`, `expectVisible(role,name)`
(probe, not assertion), `done(summary)`. Per step Claude returns exactly:
`{ "reasoning": "...", "action": "click", "args": { "role": "tab", "name": "Divisions" } }`.
Locator discipline mirrors `automation-generator`: prefer `data-testid` → role →
label, and **record which strategy actually worked** for each element.

### 4. Stop conditions (mandatory — no unbounded loop)

Halt on the first of: **goal complete** (`done`); **step cap** (configurable,
default 15, hard max 20); **repeat detection** (same URL + equivalent
accessibility snapshot ⇒ stuck); **cost budget** (each step is a Claude call;
enforce a per-session cost/token ceiling and surface remaining budget). There is
no existing AI-credit gate — the budget is enforced by summing `ClaudeUsage` for
the session and stopping when the ceiling is hit; `ai_usage_service` records
spend automatically.

### 5. KB output — runtime-verified, no clobber

On completion the agent writes to the target repo's `knowledge.json` (via the
DB `ProjectKnowledge.knowledge` row → `write_knowledge_files`): discovered
`routes`, discovered `selectors` (each with the `strategy` that worked), each
stamped `verified_at_runtime` (ISO timestamp) and `source: "exploration"`, plus a
short ordered **exploration log** so discovery is reproducible. This extends the
existing `merge_discovered_dom` merge semantics (dedup by `path`/`selector`);
existing **verified** entries are never overwritten.

### 6. Runtime-verified selectors take priority

In later generation, `verified_at_runtime` entries are preferred over
source-inferred ones (surfaced first in `render_project_context`, and honored by
`placeholder_gate` grounding). After enrichment, a previously `blocked` case can
be regenerated by the static `automation-generator`.

### 7. Endpoint — repo-scoped, run-aware

`POST /projects/{key}/repos/{repo}/explore` with body
`{ target: {ticket, screen}, runId?, caseId? }` → returns `{ started, sessionId }`
immediately (long-running: mirrors the self-heal async start + poll/WS pattern to
beat the ~100s proxy cap). Progress streams as `explore.progress` over the run
WebSocket when `runId` is given; `GET .../explore/status` supports
navigation-survival polling. We conform to the existing `/repos/{repo}`
convention (repo addressed by **name** — there is no numeric `repoId` in the data
model), diverging from the brief's `/repositories/{repoId}` wording only there.

### 8. Safety & correctness

- **Real engines only** (ADR 0001): real browser, real app, real Claude. No
  simulation. Tests mock only the transport (Claude/Node driver).
- **Read-mostly, by default's origin story — since amended (#877).** State-
  changing actions (`fill`, and clicks that submit) used to be **gated off by
  default** (`allow_state_changing=False`) with an opt-in per project. That
  gate is now **retired**: `allow_state_changing=True` is the policy for **any
  project with a resolvable `base_url`**, globally — not a per-project opt-in.
  The decision (#877) is that test-case generation should be grounded in real
  interactive behavior, not just what a read-only crawl of a screen can show
  (most screens worth testing require filling a form or submitting one to reach
  the state a test case actually needs to verify); confining exploration to
  read-only reconnaissance meant proactive, ticket-driven exploration
  (§9 below) could rarely reach anything worth recording. The loop's existing
  bounds — step cap, cost budget, repeat detection, the fixed action contract —
  are unchanged and are what keeps this safe: exploration still only ever
  drives the configured **test environment + test account**, never a production
  tenant, and every action still goes through the same observer that only
  executes the fixed contract.
- **Never invent.** Only actually-observed page state is written to the KB. If
  the target screen can't be reached, nothing is written and the case stays
  `blocked` (honest failure, no fabricated selectors).
- **Credentials.** Reuse the project's auth strategy / test account
  (role + username; password injected via the existing `storageState` capture).
  Secrets never enter the model prompt.
- **Human-driven & reviewable.** Triggered by the user from a blocked case (the
  reactive path — unchanged); also triggered proactively per ticket by the
  Planner (§9) — either way, the discovered KB additions are shown before
  regeneration.

### 9. Proactive Planner exploration feeds test-case generation (#877)

Exploration was originally **reactive only**: triggered from an already-
`blocked` case, targeting `{ticket, screen, goal}` derived from that case's
title/objective. #877 adds a **proactive** trigger, run from
`ai_service._process_run_ticket` **before** case authoring: when the setting
`testCaseMode == "live-planner"` (default `"text"` — this is a separate,
additional gate from the `allow_state_changing` policy above) and the ticket's
project has a resolvable `base_url`, one live pass runs against a target
derived from the **ticket's own text** (there is no case yet to derive one
from), and its result is threaded into the
`test-case-generator`/`test-case-reviewer` prompts alongside the ticket text and
KB context. The two case-authoring calls stay exactly as before: two cheap,
non-agentic `run_json` calls: this is not case-authoring made agentic, it is
case-authoring **grounded in** one agentic pass that ran first. What it observed
merges into the KB exactly as the reactive path does
(`merge_verified_discovery`) — no new write path. When `testCaseMode == "text"`
(default) or no `base_url` resolves, generation is byte-for-byte what it was
before #877.

**#889 amendment — that pass is the planner AGENT, not the `explore()` loop.**
Measured against playwright.dev with real credentials, the per-step
observe→decide→act loop stopped at `repeat` after three steps without reaching
`done` and cost ~$0.116 *per decide call*, while the ported
`playwright-test-planner` agent (#888) completed the same goal for $0.268 total
and produced a plan whose every step carries the locator it was actually
performed with. So `ai_service._plan_before_authoring` now runs that agent
(`planner_agent_service.plan_ticket`, over the shared dedicated-Chrome plumbing
in `agentic_browser`) and the prompts are grounded by
`prompts.render_planner_plan`. Two artifacts come back — the agent's Markdown
plan, and a **`plan.json` sidecar that is the only source of truth** (mirroring
live authoring's `discovered.json`; Markdown prose drifts). The plan's locators
reach automation through the KB (`merge_plan_to_kb`, stamped
`source="planner-agent"`), **never** on `TestCase.steps`, whose `{a, e}` schema
silently drops extra keys (#882). Every failure — no `playwright-cli`, no
browser, a Claude error, an unparseable sidecar — degrades to text-only. The
reactive `explore()` path is untouched.

## Consequences

- **Positive:** a missing selector becomes a discovered, runtime-verified
  selector instead of a `blocked` case; cost is spent only on a real KB gap;
  generated specs stay fully deterministic (exploration feeds the KB, generation
  stays static).
- **Cost / risk:** a new long-lived Node subprocess protocol to maintain; end-to-
  end validation needs the operator's environment (real app + browsers + Claude),
  so in-repo verification is mock-transport tests + typecheck/build (per ADR 0001).
- **Scope:** one repository per session; web targets only; no model training; not
  an autonomous end-to-end test author (all per the brief's non-goals).

## Alternatives considered

See §1 (browser driving) and §7 (endpoint shape). The overarching alternative — a
full autonomous test-writer — was rejected to keep generated specs reproducible:
exploration enriches structured KB data; the deterministic generator still
produces the spec.
