# Q-Agent — AI-native QA Operating System

Q-Agent turns a work item into tested, evidenced, published QA work. It syncs
tickets from **Azure DevOps / Jira**, uses the **Claude CLI** to analyze the
requirement and generate Azure DevOps-style test cases, lets a QA engineer review
the AI's work like a pull request, generates and self-heals **Playwright**
automation, executes it, collects evidence, and publishes the result back to the
originating ticket.

Built to feel like Cursor / Linear / Vercel — not a traditional enterprise QA tool:
dark glassmorphism, ambient glow, a live neural background, and an AI that appears
continuously active across the whole pipeline.

> **Self-hosted.** You run the stack on your own infrastructure with your own
> provider tokens and your own Claude credentials. Nothing leaves your deployment
> except the Claude API calls the AI actions make — and browser sessions for
> manual-login apps never leave the tester's device at all (see
> [The Local Agent](#the-local-agent)).

## Pipeline

Ticket sync and selection happen first; everything after that belongs to a **Run**,
whose `status` is the pipeline state machine (`api/app/models/run.py`):

| Stage | `Run.status` | Route | Screen |
|-------|--------------|-------|--------|
| Analyze + generate test cases | `processing` | `/runs/:id` | `RunDetail` |
| **Review** | `review` | `/runs/:id/review` | `ReviewCenter` |
| **Link** — create & link approved cases in the provider | `sync` | `/runs/:id/sync` | `CreateLinkSync` |
| **Automation** — generate Playwright specs | `automation` | `/runs/:id/automation` | `Automation` |
| **Execution** | `executing` | `/runs/:id/execution` | `Execution` |
| **Evidence** | `evidence` | `/runs/:id/evidence` | `Evidence` |
| **Publish** — comment back to the ticket | `comment` | `/runs/:id/comment` | `CommentPublish` |
| Finished | `done` · `cancelled` · `failed` | — | — |

Two names are worth pinning down, because the status strings don't match the UI
labels: the **`sync`** stage is **Link** (create approved cases in the provider and
link them to their work items) — *not* "sync tickets", which happens before a run
exists. The **`comment`** stage is **Publish**.

Runs can be cancelled, retried and deleted at any stage
([ADR 0005](docs/adr/0005-run-lifecycle-management.md)).

## Current state

Everything below is implemented and working today.

**Core pipeline** — ticket sync from ADO/Jira, run creation over one/many tickets
(single · selected · assigned · sprint), AI requirement analysis, ADO-style test-case
generation with AC→case traceability, PR-style review with per-case approval,
create-and-link into the provider (with a local dry-run mode), Playwright spec
generation, execution, evidence capture, report generation, and publish-back with
optional status transition.

**Multi-user & security** — email + password login (argon2), JWT access tokens with
httpOnly refresh cookies, TOTP 2FA, password reset, member invites, admin/member
RBAC, session listing and revocation, and an append-only audit log. Nearly every row
carries an `owner_id`, so users' runs, tickets, projects, provider connections and
knowledge are private to them
([ADR 0007](docs/adr/0007-application-authentication.md),
[ADR 0008](docs/adr/0008-per-user-ownership-and-claude-credentials.md)).

**Knowledge & grounding** — a per-repository **Knowledge Base** (stack, routes, real
selectors, auth flow, reusable Page Objects) plus per-project config (base URL,
per-env URLs, encrypted test accounts) grounds every downstream AI action
([ADR 0002](docs/adr/0002-project-knowledge-config-and-multi-repo.md)). An optional
**Exploration Agent** drives a real browser in an observe→decide→act loop to discover
real routes/selectors and merge them back into the KB
([ADR 0010](docs/adr/0010-dom-exploration-agent-kb-enrichment.md)). Admins curate
reference projects in a **shared namespace** that members **clone**, reusing the
~20-minute bootstrap instead of repeating it
([ADR 0009](docs/adr/0009-per-user-workspace-filesystem-and-cloning.md)).

**Automation intelligence** — two authoring modes: `blind` (generate from the KB,
then heal) and `live-harness` (an agentic Claude drives the real signed-in app via
`browser-harness` and emits a spec from what it actually saw —
[ADR 0012](docs/adr/0012-live-spec-authoring-via-browser-harness.md)). Plus a
**placeholder gate** that blocks specs containing invented selectors, per-spec
**chat** for targeted edits, a bounded **self-heal** loop that feeds failures back to
Claude, and AI **failure classification** (`test_defect` · `product_defect` · `flaky`
· `environment` · `timeout`).

**Execution** — runs target either the paired **Local Agent** (the default) or the
server. Evidence per case: screenshot, video, Playwright trace, console, network,
summary, DOM and distilled DOM, with Pillow annotation and AI auto-annotate.

**Operations** — Docker Compose deployment, PostgreSQL with Alembic migrations,
per-call Claude cost/token tracking with a weekly budget, English/Vietnamese UI
([ADR 0011](docs/adr/0011-frontend-internationalization-en-vi.md)), a guided product
tour backed by a generated sample run, and a mobile layout.

See [Not yet built](#not-yet-built) for the honest other half of this picture.

## Architecture

| Layer | Stack |
|-------|-------|
| Frontend (`app/`) | React 19, Vite 6, TypeScript, Tailwind 4, TanStack Query, Zustand, react-router-dom 7, Framer Motion, Three.js, i18next, DOMPurify, lucide, sonner, cmdk |
| Backend (`api/`) | FastAPI, SQLAlchemy 2, Pydantic v2, **PostgreSQL + Alembic** (SQLite fallback for local dev), httpx, Loguru, Pillow, WebSockets, argon2-cffi, PyJWT, pyotp |
| AI engine | **Claude Code CLI** invoked as a subprocess (`api/app/services/claude_cli.py`), guided per action by a dedicated skill |
| Automation | Playwright + TypeScript, executed by the Local Agent by default; `browser-harness` for live authoring |
| Local Agent (`agent/`) | Node CLI (`npx @q-agent/agent`) + Electron desktop app |
| Providers | Azure DevOps, Jira, GitHub — real REST adapters, no mock path |

```
q-agent/
├─ app/          # React frontend (+ Dockerfile, nginx.conf, e2e/)
├─ api/          # FastAPI backend (app/, migrations/, tests/, Dockerfile; workspace/ at runtime)
├─ agent/        # Local Agent — npm package + Electron desktop app
├─ skills/       # 11 Claude skills (SKILL.md + templates) injected as system prompts
├─ scripts/      # setup.(sh|bat), start.(sh|bat)
├─ design/       # approved UI source (design bundles + DESIGN_SYSTEM.md) — the fidelity target
├─ docs/         # briefs, API contract, Docker guide, architecture review, ADRs
├─ downloads/    # staged Local Agent installers, bind-mounted read-only into nginx
└─ template/     # test-case export templates (ADO XML, Jira/Xray, Jira plaintext)
```

### Deployment topology

Three containers behind **one published port**, so a single Cloudflare tunnel fronts
everything (`docker-compose.yml`, `app/nginx.conf`):

| Service | Image | Port | Role |
|---------|-------|------|------|
| `web` | nginx serving the built SPA | **5174** (published) | Serves the SPA; reverse-proxies same-origin `/api`, `/auth` and WebSockets to `api`. Also serves `/downloads/`. |
| `api` | Python 3.13 + uv + Node + Claude CLI + git + chromium | 8787 (internal) | REST + WebSocket backend. AI runs here. |
| `db` | `postgres:16-alpine` | **5456** (published) → 5432 | PostgreSQL. Reachable from the host for psql/DBeaver. |

`/auth/` is proxied **without** a path rewrite so the httpOnly refresh and CSRF
cookies work, and `/api/` carries a 30-minute proxy timeout so long AI calls
(analysis, `project-bootstrap`) don't hit a 504.

The `api` image ships **no Playwright test runner** — execution is offloaded to the
Local Agent.

## Prerequisites

- **Docker** + Docker Compose (deployment), or **Node 20+** and
  **[uv](https://docs.astral.sh/uv/)** for local development (Python 3.13 is fetched
  automatically)
- A machine with an authenticated **Claude CLI** (`claude login`), so you can upload
  its `~/.claude/.credentials.json` to the app — this is what authorizes server-side
  AI actions
- For test execution: a **Local Agent** on the tester's machine (it downloads its own
  Chromium on first start)

## Deploy with Docker

```bash
cp .env.example .env          # set QAGENT_SECRET_KEY + QAGENT_ADMIN_PASSWORD
docker compose up -d --build
```

Open <http://localhost:5174> (or point a Cloudflare tunnel at `:5174`).

### First-run setup (in the app)

1. **Sign in** as `QAGENT_ADMIN_EMAIL` / `QAGENT_ADMIN_PASSWORD` from your `.env` —
   the first admin is seeded when the users table is empty.
2. **Settings → AI** — upload your Claude `.credentials.json` (from a machine where
   `claude` is logged in). This authorizes all server-side AI actions. Admins can
   also publish a **shared** credential for members to use.
3. **Settings** — set **Execution target = Local Agent**, then pair an agent from
   **Local Agent** (it issues a pairing code). ⚠️ The `api` image ships no
   Playwright, so choosing server-side execution in a Docker deployment **will
   fail** — runs must target the Local Agent.
4. **Configure providers** and do the [per-project setup](#per-project-setup) below.

Full deployment notes, persistence details and an external-Postgres recipe:
[`docs/DOCKER.md`](docs/DOCKER.md).

## Local development

```bash
# 1. Install everything (backend deps, frontend deps, Playwright browsers)
scripts/setup.sh          # Windows: scripts\setup.bat

# 2. Apply the database schema
cd api && uv run alembic upgrade head && cd ..

# 3. (optional) Load demo data so the UI has content without live providers
cd api && uv run python -m app.seed && cd ..

# 4. Run backend + frontend
scripts/start.sh          # Windows: scripts\start.bat
```

- Frontend: http://localhost:5173 (Vite falls off 5173 if it's busy)
- Backend: http://127.0.0.1:8787 (OpenAPI docs at `/docs`)

Notes:

- **Auth applies locally too** (`QAGENT_AUTH_REQUIRED` defaults to `true`). Set
  `QAGENT_ADMIN_EMAIL` / `QAGENT_ADMIN_PASSWORD` in `api/.env` to choose your own
  admin. If you leave them blank in dev (`QAGENT_COOKIE_SECURE=false`), a fallback
  `admin@qagent.local` is auto-seeded and its generated password is logged at
  startup, so you can't be locked out. Set `QAGENT_AUTH_REQUIRED=false` for a
  single-user local loop.
- `scripts/start.sh` auto-starts the Compose `db` service when `api/.env` points at
  `127.0.0.1:5456`, because Alembic's boot-time migration fails immediately
  otherwise.
- Leave `QAGENT_DATABASE_URL` empty to use SQLite at `api/workspace/q-agent.db`.

## The Local Agent

Test execution defaults to a **paired device**, not the server. The reason is
credentials: apps behind SSO/MFA need a real human login in a real headed browser,
and the resulting session cookies and `storageState` **never leave that device** —
only specs, results and evidence travel back to the server. The agent also runs
agent-side self-heal, DOM exploration, live spec-authoring, and manual-login capture.

**Pairing** — open **Local Agent** in the app to mint a short-lived code, then:

```bash
npx @q-agent/agent          # starts the local UI (default port 7420)
qagent-agent pair <code>    # redeems the code for a durable device token
qagent-agent start          # claims jobs (downloads Chromium on first run)
```

The device token is stored at `~/.qagent-agent/config.json` (mode 0600) and can be
revoked from the app.

**Two distribution channels**, both compiled from the same `agent/src`:

1. `npx @q-agent/agent` — cross-platform, published to npm.
2. A Windows Electron installer, served at `<origin>/downloads/` and auto-updating
   via electron-updater.

Details, CLI reference and the single-binary build: [`agent/README.md`](agent/README.md).

## Configuring providers

Add connections in **Settings**, or `POST /providers/{kind}/connections`. You can
have **multiple named connections per provider kind**
([ADR 0006](docs/adr/0006-multiple-provider-connections.md)), each classified by what
it can do — work items, repositories, or both:

| Provider | Capability | Credentials |
|----------|------------|-------------|
| Azure DevOps | work items **+** repositories | Organization URL, Project, Personal Access Token |
| Jira | work items | Base URL, Project Key, Email, API Token |
| GitHub | repositories | Organization/owner, Personal Access Token |

Credentials are **encrypted at rest** (Fernet key derived from `QAGENT_SECRET_KEY`)
and never returned in plaintext. Use **Test connection** to verify, then **Sync** on
the Tickets page.

### Per-project setup

Once per project, on **Project Details**:

- **Settings** — add the project's **repositories** (auto-discovered from Azure
  DevOps / GitHub, or added manually), pick the default automation target, and set
  the base URL, per-environment URLs and **test accounts** (passwords encrypted at
  rest, masked in the UI). A local repo path or a remote clone URL lets
  `project-bootstrap` traverse the real source; remote repos are cloned into the
  caller's own workspace scope (private repos use the connection's PAT).
- **Project Knowledge** — build a **Knowledge Base per repository**. Each
  `knowledge.md` + `knowledge.json` captures stack, routes, real selectors, auth flow
  and reusable assets, so generated specs run with little to no manual editing.

On-disk artifacts are **scoped per owner**
([ADR 0009](docs/adr/0009-per-user-workspace-filesystem-and-cloning.md)):

```
api/workspace/
├─ users/<owner_id>/{specs,evidence,knowledge,repos,auth}/
└─ shared/{specs,evidence,knowledge,repos,auth}/     # admin-curated, cloned by members
```

### AI skills

Every Claude action is guided by a dedicated skill in `skills/<name>/SKILL.md` — the
backend injects it as that action's **system prompt** while the caller's prompt pins
the exact output shape the backend parses. Override the location with
`QAGENT_SKILLS_DIR`.

| Skill | Used for |
|-------|----------|
| `project-bootstrap` | Per-repo Knowledge Base build |
| `requirement-analyst` | Ticket requirement analysis |
| `test-case-generator` | Test-case generation / regeneration |
| `test-case-reviewer` | AI review pass over generated cases |
| `automation-generator` | Playwright spec generation (and exploration grounding) |
| `automation-reviewer` | Spec quality gate |
| `live-authoring` | Live spec authoring via `browser-harness` |
| `execution-analyzer` | Failure classification + report analysis |
| `screenshot-annotator` | Evidence auto-annotation |
| `ticket-comment-generator` | Result comment for the ticket |

`report-generator` also ships in `skills/` but is not wired to an action yet.

## Configuration reference

### Environment variables

Backend (`api/.env`, all prefixed `QAGENT_` — see `api/.env.example`):

| Variable | Purpose | Default |
|----------|---------|---------|
| `QAGENT_HOST` / `QAGENT_PORT` | Bind address / port | `127.0.0.1` / `8787` |
| `QAGENT_DATABASE_URL` | PostgreSQL DSN; empty → SQLite under `workspace/` | *(empty)* |
| `QAGENT_SECRET_KEY` | **Required in production.** Derives the credential-encryption key and signs auth JWTs. Changing it invalidates stored credentials | dev placeholder |
| `QAGENT_AUTH_REQUIRED` | Global auth guard on every route, WebSocket and artifact | `true` |
| `QAGENT_COOKIE_SECURE` | `Secure` flag on auth cookies. When `true`, reset tokens aren't echoed and the dev fallback admin is disabled | `false` |
| `QAGENT_ADMIN_EMAIL` / `QAGENT_ADMIN_PASSWORD` | First-admin seed, applied on an empty users table | *(empty)* |
| `QAGENT_CLAUDE_BIN` / `QAGENT_CLAUDE_MODEL` / `QAGENT_CLAUDE_TIMEOUT_S` | Claude CLI binary, default model, per-call timeout | `claude` / `claude-sonnet-5` / `300` |
| `QAGENT_EXEC_TIMEOUT_S` | Playwright execution timeout | `600` |

`api/app/config.py` exposes more `QAGENT_*` overrides not in the example file, incl.
`CORS_ORIGINS`, `WORKSPACE_DIR`, `CLAUDE_HOME`, `CLAUDE_BOOTSTRAP_TIMEOUT_S` (1200),
`SKILLS_DIR`, `HEAL_MAX_ATTEMPTS` (3), `HEAL_FIX_MODEL`, `EXPLORE_MAX_STEPS` (15),
`EXPLORE_COST_BUDGET_USD` (0.50), `AUTHORING_COST_BUDGET_USD` (2.00),
`AUTHORING_TIMEOUT_S` (900) and `AUTH_CAPTURE_TIMEOUT_S` (300).

Docker Compose (`.env` at the repo root — see `.env.example`): `QAGENT_SECRET_KEY`
and `QAGENT_ADMIN_PASSWORD` are **required**; `QAGENT_ADMIN_EMAIL`,
`QAGENT_CLAUDE_MODEL`, `QAGENT_DB_USER`, `QAGENT_DB_PASSWORD`, `QAGENT_DB_NAME` and
`QAGENT_DB_PORT` are optional.

Frontend (`app/.env` — see `app/.env.example`): `VITE_API_BASE`, the backend base
URL. Left unset in the Docker build so the SPA calls same-origin `/api`.

### Runtime settings

These are **not** environment variables — they're edited in **Settings** and
persisted as JSON in the workspace (`GET`/`PUT /settings`,
`api/app/services/settings_store.py`):

`executionTarget` (`local-agent` default · `server`) · `authoringMode` (`blind` ·
`live-harness`) · `healMode` (`classic` · `live-harness`) · `claudeModel` +
`skillModels` (per-skill overrides) · `weeklyTokenBudget` · `aiPipelineWorkers` ·
`authoringCostBudgetUsd` · `parallel` · `retryFlaky` · `maxCasesPerTicket` ·
`headless` · `video` · `screenshotOnFail` · `autoAnnotate` · `gateEnabled` ·
`neuralBackground`.

### Default ports

API **8787** · web/nginx **5174** · Vite dev **5173** · PostgreSQL (host) **5456** ·
Local Agent UI **7420**.

## Development

Each package has its own gate. There is **no frontend unit-test harness** — for
`app/`, typecheck + build *is* the gate.

| Package | Commands |
|---------|----------|
| `api/` | `uv run uvicorn app.main:app --reload --port 8787` · `uv run pytest -q` (520 tests across 53 modules) · `uv run alembic upgrade head` |
| `app/` | `npm run dev` · `npm run typecheck` · `npm run build` · `npm run test:e2e` (Playwright, `app/e2e/`) |
| `agent/` | `npm run build` · `node --test "dist/test/**/*.test.js"` (42 tests) |

The agent's own `npm test` shortcut is broken on Node 23 (it can't discover the test
directory) — use the glob form above until
[#470](https://github.com/chuongnd2612/q-agent/issues/470) lands.

Integration behavior needs the operator's own environment
([ADR 0001](docs/adr/0001-scope-architecture-and-live-integrations.md)): live
ADO/Jira/GitHub credentials, valid Claude credentials, and a paired Local Agent.
There is **no mock fallback** — the app talks to the real systems.

After merging backend or frontend changes, rebuild the running stack — the container
is stale until you do:

```bash
docker compose up -d --build
```

Local Agent fixes are **not** delivered by that rebuild: the agent runs on the paired
device and updates via npm or its own installer.

## Not yet built

Known gaps, so nobody has to discover them the hard way:

- **No job queue or worker pool.** Long work (AI pipeline, execution, heal, KB build,
  exploration) runs in in-process daemon threads. They don't survive a restart, so
  `_recover_orphaned_runs()` sweeps runs stuck in non-terminal stages at boot.
- **No server-side Playwright** in the Docker image — execution requires a paired
  Local Agent.
- **No interactive server-side browser sessions** (noVNC → WebRTC) — deferred; see
  [`docs/MULTI-USER-MIGRATION-PLAN.md`](docs/MULTI-USER-MIGRATION-PLAN.md).
- **No CI/CD triggers** and **no Cypress/Selenium runners** — the architecture stays
  extensible for both.
- **No SSO/OIDC** — deferred in [ADR 0007](docs/adr/0007-application-authentication.md).
- **No frontend unit tests** — only typecheck, build and a small E2E suite.
- **The backend suite is currently red** — 22 of 520 tests fail on `master`, tracked
  in [#469](https://github.com/chuongnd2612/q-agent/issues/469). Mostly tests left
  behind by intentional behavior changes, but it means the gate isn't catching
  regressions right now.
- The **desktop Local Agent installer is Windows-only**; the `npx` path is
  cross-platform.
- No `CONTRIBUTING.md`, `CHANGELOG.md` or `LICENSE` yet.

## Documentation

**Product & contracts**

- [`docs/CLIENT-BRIEF.md`](docs/CLIENT-BRIEF.md) — full product brief
- [`docs/CONTEXT.md`](docs/CONTEXT.md) — glossary & core concepts
- [`docs/API-CONTRACT.md`](docs/API-CONTRACT.md) — REST + WebSocket contract

**Operations & engineering**

- [`docs/DOCKER.md`](docs/DOCKER.md) — Docker deployment, persistence, common commands
- [`docs/ARCHITECTURE-REVIEW.md`](docs/ARCHITECTURE-REVIEW.md) — technical review + optimization opportunities
- [`docs/MULTI-USER-MIGRATION-PLAN.md`](docs/MULTI-USER-MIGRATION-PLAN.md) — single-user → multi-user plan and status
- [`docs/SUGGEST-TECHSTACK.md`](docs/SUGGEST-TECHSTACK.md) — stack rationale per layer
- [`docs/UI-DESIGN-PROMPT.md`](docs/UI-DESIGN-PROMPT.md) — the design brief behind the UI
- [`CLAUDE.md`](CLAUDE.md) — working conventions, gates and delivery workflow
- [`api/README.md`](api/README.md) · [`agent/README.md`](agent/README.md) — per-package guides

**Architecture decisions** ([`docs/adr/`](docs/adr/))

| ADR | Decision |
|-----|----------|
| [0001](docs/adr/0001-scope-architecture-and-live-integrations.md) | Scope, architecture, live integrations — real adapters and engines, no mock fallback |
| [0002](docs/adr/0002-project-knowledge-config-and-multi-repo.md) | Per-repo Knowledge Base + Project Config as the grounding for every AI action |
| [0003](docs/adr/0003-client-side-routing.md) | Client-side routing; the URL is the source of truth for navigation |
| [0004](docs/adr/0004-run-workspace-navigation.md) | Run workspace-mode navigation — never silently default to "the latest run" |
| [0005](docs/adr/0005-run-lifecycle-management.md) | Run lifecycle: cancel / retry / delete, with terminal-status guards |
| [0006](docs/adr/0006-multiple-provider-connections.md) | Multiple named connections per provider, split work-item vs repository |
| [0007](docs/adr/0007-application-authentication.md) | Email+password auth, JWT + refresh cookie, RBAC (SSO deferred) |
| [0008](docs/adr/0008-per-user-ownership-and-claude-credentials.md) | Per-user data ownership and server-managed Claude credentials |
| [0009](docs/adr/0009-per-user-workspace-filesystem-and-cloning.md) | Per-user workspace filesystem, artifact cloning, admin shared namespace |
| [0010](docs/adr/0010-dom-exploration-agent-kb-enrichment.md) | DOM Exploration Agent enriches the KB (it does not author tests) |
| [0011](docs/adr/0011-frontend-internationalization-en-vi.md) | Frontend i18n — English + Vietnamese |
| [0012](docs/adr/0012-live-spec-authoring-via-browser-harness.md) | Live spec-authoring via `browser-harness` |
