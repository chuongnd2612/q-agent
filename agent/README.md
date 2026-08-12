# @q-agent/agent — Q-Agent Local Agent

Runs Playwright test executions on **your own machine** instead of the
Q-Agent server. This means:

- Manual login / MFA happens in a real, headed browser right where you are
  sitting — not on a headless server.
- Your session cookies/localStorage/sessionStorage **never leave this
  machine**. Only test specs, pass/fail results, and evidence (screenshots/
  video/trace) are sent back to the server.

## Prerequisites

- Node.js 18+

Chromium is installed automatically: the first time you run `qagent-agent start`
the agent downloads Playwright's Chromium if it isn't already present (one-time,
~100 MB, with visible progress). No manual `npx playwright install` step is needed.

## Run with the UI (easiest)

```bash
npx @q-agent/agent
```

Running with no command opens a local web UI in your browser. Paste a pairing
code (from the Q-Agent app's **Local Agent** screen) and the server URL, click
**Connect**, and watch the pull/run progress live. The device token is stored
locally, so next time it reconnects and starts pulling automatically.

## Command-line

```bash
npx @q-agent/agent pair <code> --server https://your-qagent-server.example.com/api
npx @q-agent/agent start
```

`--server` is the origin serving the API's `/agent/...` routes. On the
same-origin (Cloudflare tunnel) deployment that's `<origin>/api`. In local dev
with the API on its own port, use the API directly (e.g. `http://127.0.0.1:8787`).

Or, if working from a checkout of this package:

```bash
cd agent
npm install
npm run build
node dist/src/cli.js pair <code> --server http://127.0.0.1:8787
node dist/src/cli.js start
```

## Windows binary (no Node required)

For machines without Node, build a self-contained Windows bundle:

```bash
cd agent
npm install
npm run package:win
```

This produces `dist-bin/qagent-agent-win-x64/` — a `qagent-agent.exe` (the CLI as
a Node Single Executable Application) plus a bundled Node runtime, production
`node_modules`, and `vendor/`. Zip that folder for distribution; the user unzips
and runs it with **no global Node installed**:

```bat
qagent-agent.exe pair <code> --server https://your-qagent-server.example.com/api
qagent-agent.exe start
```

Chromium still auto-installs on first `start`. Build the bundle on Windows with a
Node 20+ that supports SEA (the local `node.exe` is used as the executable base —
no compiler or download needed).

## Desktop app (Electron)

A native window that wraps the same web UI:

```bash
cd agent
npm install
npm run desktop        # dev: builds, then opens the app window
npm run dist:desktop   # build a Windows installer (electron-builder → dist-bin/desktop/)
```

The Electron shell runs the agent's UI server in-process and shows it in a
window — pair by entering the 6-digit code + server URL, then watch progress.
Child processes (Playwright / login capture) run via Electron-as-Node
(`ELECTRON_RUN_AS_NODE`), so no separate Node install is needed.

## Commands

| Command | Description |
| --- | --- |
| `pair <code> [--server <url>] [--name <name>]` | Redeem a one-time pairing code (generated in the Q-Agent SPA's Local Agent screen) for a durable device token. Stored at `~/.qagent-agent/config.json` (mode 0600). |
| `start [--server <url>]` | Long-poll the server for queued `local-agent` executions and run them. Ctrl-C shuts down cleanly, killing any in-flight Playwright/capture process. |
| `status` | Show whether this machine is paired, and to which server. |
| `logout` | Forget the device token. |

## How a job runs

1. Claim a job (`POST /agent/jobs/next`, sending `{agentVersion}`) — spec sources,
   run params and, for a **layered** run, the whole automation project, **never**
   any session/credentials.
2. Write the project bundle and the specs into a temp workdir, then a
   `playwright.config.ts`.
3. If the project requires manual login and no valid local session exists for
   its origin yet, open a **headed** browser (`vendor/capture_auth.cjs`) for
   you to log in. The captured session is saved under
   `~/.qagent-agent/sessions/<origin>/` and reused on future runs until it
   expires.
4. Run `@playwright/test` headed, parse `report.json`.
5. Push each case's result, its evidence (screenshots/video/trace), and
   progress events back to the server, then mark the job complete.

## Layered automation projects (#541)

Q-Agent keeps a **persistent, git-backed automation project** per customer
project — page objects, components, fixtures and test data that accumulate
across runs, so each new feature generates less code than the last (epic #537).
The agent is stateless and has no read access to the server's filesystem, so the
project ships **wholesale with every claim** as `project.files[]` and is written
back out at its real nesting:

```
<temp workdir>/
  pages/ components/ fixtures/ data/ utils/ config/   <- the shipped library
  tests/SUR-1428/SUR-1428-TC-01.spec.ts               <- only THIS run's specs
  node_modules/@q-agent/playwright-base               <- from vendor/, no network
  playwright.config.ts  fixtures.ts                   <- generated per run
```

Three consequences worth knowing:

- **`applyFixtures` is depth-aware.** Files sit at different depths, so each
  `'@playwright/test'` import is rewritten to the correct relative specifier
  (`./fixtures`, `../fixtures`, `../../fixtures`). This mirrors the server's
  `playwright_runner._apply_fixtures` exactly — divergence would mean a spec that
  passes on the server fails on the device.
- **`@q-agent/playwright-base` resolves offline.** The agent carries a built copy
  at `vendor/playwright-base/` and copies it into the workdir's `node_modules/`
  instead of running `npm install` per job. Regenerate it with
  `npm run vendor:base` after the base package changes. The trade-off: the base
  version is pinned to the agent release rather than to each project's lockfile.
- **Version skew is refused, not tolerated.** An agent that predates this
  materializes nothing and would flatten the tree, making every import fail
  collection — a silent mass failure. The agent therefore reports its version on
  every claim, and the server 409s a layered claim from a build below its minimum
  (failing the execution with "Update your Local Agent to run layered specs")
  rather than handing it over. **A device that reports no version at all is
  treated as below minimum.** Legacy self-contained specs are unaffected and keep
  running on any build.

## Known limitation (flag for the server cleanup phase)

`POST /agent/jobs/next`'s `specs[]` entries currently only carry
`{filename, code}` — not the case's full `ticketExternalId`/`caseCode`
(even though the server has both in scope when building the payload, see
`api/app/routers/agent.py` `claim_next_job`). The agent recovers a
best-effort identity by parsing the `{shortTicket}-{caseCode}.spec.ts`
filename convention (`src/report.ts` `parseSpecIdentity`), assuming the
fixed `TC-NN` case-code format used everywhere else in this codebase. This
only recovers the ticket's **short numeric suffix** (e.g. `"1428"`), not its
full provider-prefixed external id (e.g. `"SUR-1428"`) — so:

- `exec.case.running` / `exec.case.result` WS events carry that short id in
  `ticket`, which may not match what the frontend expects to key rows by.
- `POST /agent/jobs/{id}/evidence` requires an **exact** `ticket_external_id`
  match server-side (`ExecutionResult.ticket_external_id == ticket_external_id`)
  — the short id will likely **not** match, so evidence uploads may 404
  until this is fixed.

The client (`src/api.ts` `JobSpec`, `src/runner.ts` `identityFor`) already
prefers explicit `ticketExternalId`/`caseCode` fields on each spec entry if
present, so the fix is additive: add those two fields to the `specs.append(...)`
call in `claim_next_job` (the loop variable `r` already has them in scope) —
no agent-side change needed once that lands.
