# Q-Agent API

FastAPI backend for Q-Agent — REST + WebSockets, the Claude CLI integration, the
provider adapters, and the Playwright execution/heal engine. See the repo root
[`README.md`](../README.md) for what the product does and how to deploy the whole
stack.

## Run it

```bash
uv sync --extra dev
uv run alembic upgrade head                              # apply the schema
uv run uvicorn app.main:app --reload --port 8787
```

OpenAPI docs at <http://127.0.0.1:8787/docs>. Copy `.env.example` → `.env` first; all
settings are prefixed `QAGENT_` and documented in the root README's configuration
reference (`app/config.py` is the authority).

Note that **auth is on by default** (`QAGENT_AUTH_REQUIRED=true`). In dev with
`QAGENT_COOKIE_SECURE=false` and no admin vars set, a fallback `admin@qagent.local`
is seeded at startup and its generated password is logged, so you can't lock yourself
out. Set `QAGENT_AUTH_REQUIRED=false` for a single-user local loop.

## Layout

```
api/
├─ app/
│  ├─ main.py            # create_app(), lifespan, auth middleware, WS endpoints
│  ├─ config.py          # Settings (pydantic-settings), workspace dirs
│  ├─ db.py              # Base, SessionLocal, init_db()
│  ├─ ws.py              # ProgressHub — per-topic fan-out + last-event replay
│  ├─ crypto.py          # Fernet helpers (credentials at rest)
│  ├─ routers/           # HTTP surface, one module per area
│  ├─ models/            # SQLAlchemy models
│  ├─ services/          # business logic, AI, adapters, execution
│  └─ seed.py            # demo data (`python -m app.seed`)
├─ migrations/           # Alembic revisions (14)
├─ tests/                # pytest (56 modules, 610 tests)
├─ scripts/              # import_sqlite_to_postgres.py
└─ workspace/            # runtime state — gitignored
```

## Database

`QAGENT_DATABASE_URL` selects the backend:

- **Set** → PostgreSQL, e.g. `postgresql+psycopg://user:pass@localhost:5432/qagent`
  (what Docker Compose uses).
- **Empty** → SQLite at `workspace/q-agent.db`. Fine for local dev.

The schema is managed by **Alembic** and applied automatically at boot by `init_db()`,
so a fresh container self-migrates.

```bash
uv run alembic upgrade head                                   # apply
uv run alembic revision --autogenerate -m "add widget table"  # new revision
uv run alembic downgrade -1                                   # roll back one
```

Migrating an existing SQLite database into Postgres:
`uv run python scripts/import_sqlite_to_postgres.py`.

## HTTP surface

One router module per area (`app/routers/`), all mounted in `main.py`:

| Router | Covers |
|--------|--------|
| `health` | `/health`, `/capabilities` |
| `auth` | Login, MFA, refresh, reset, `/me`, 2FA setup, sessions, admin user CRUD + invites |
| `ai` | Claude activity stream, usage stats, per-user + shared credential management |
| `audit` | Audit events and application log buffer, plus stats |
| `providers` | Provider connections (create/test/delete), sprint & repo discovery, app-wide `/settings` |
| `projects` | Projects, per-project config, repos, per-repo knowledge builds, exploration, manual-login capture |
| `workspace` | Admin shared namespace — reference projects, config, and cloning into a user's scope |
| `tickets` | Paged ticket list, detail, linked/provider test cases, sync, delete |
| `runs` | Run CRUD, sample run, per-run tickets/repos/AI-usage, regenerate, cancel, stop, retry |
| `review` | Test cases: list/create/patch, approval, regenerate, create-and-link, approve-all |
| `automation` | Spec generation, per-case spec read/patch, regenerate, chat, heal (+ status/report) |
| `execution` | Start a run's execution or a single spec, read execution results |
| `evidence` | Per-run and per-result evidence, manual and AI auto-annotation |
| `reports` | Generate and read run reports |
| `comments` | Prepare, edit, publish and retry ticket comments |
| `agent` | Local Agent device pairing + the job protocol (jobs, evidence upload, heal, explore, authoring) |

Two WebSocket endpoints live in `main.py`: `/ws/runs/{run_id}` for pipeline progress
and `/ws/ai` for the global Claude activity indicator. Both authenticate via
`?token=`; run sockets also verify run ownership. `ProgressHub` replays the last event
so a client connecting mid-run is caught up.

Evidence is served as static files from `/artifacts`, gated by a signed `?token=` plus
a run-ownership and workspace-scope cross-check.

## Concurrency model

There is **no queue or broker**. Long-running work (the AI pipeline, execution, heal,
knowledge builds, exploration) runs in `daemon=True` threads that each open their own
`SessionLocal`. Cancellation is cooperative via `services/run_control.py`.

Because threads don't survive a process restart, `main._recover_orphaned_runs()` runs
at startup and sweeps any run left in a non-terminal stage, marking it failed rather
than leaving it stuck forever. Keep this in mind when adding background work: a new
long task needs a non-terminal status that the sweeper understands, and must tolerate
being killed mid-flight.

## Tests

```bash
uv run pytest -q          # 610 tests across 56 modules
uv run ruff check .
```

`pytest` lives in the `dev` optional-dependency extra, so in a fresh checkout (no `.venv`
yet) use `uv run --extra dev pytest -q` — a plain `uv run pytest` fails with
`Failed to spawn: pytest`.

**22 of these fail on `master` (#469).** That is the current baseline, not a broken
checkout — capture the number before making changes and compare against it.

Provider adapters are tested against mocked HTTP (`respx`); AI and Playwright are
tested against mocked engines. No test requires live credentials or a real browser.

> ⚠️ **The suite is currently red on `master`** — 22 failures, tracked in
> [#469](https://github.com/chuongnd2612/q-agent/issues/469). Most look like tests
> left behind by intentional behavior changes (notably the execution target now
> defaulting to `local-agent`). Check that issue before assuming a failure is yours.
