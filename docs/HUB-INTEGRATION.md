# Q-Agent ← EmeHub integration — the agent-side work

> **Handoff document.** Everything Q-Agent has to build to join the EmeHub suite, plus what
> DAgent will need later. The hub-side counterpart lives in the `emehub` repo:
> [`docs/INTEGRATION.md`](https://github.com/chuongnd2612/emehub/blob/master/docs/INTEGRATION.md)
> (the contract) and
> [`docs/SSO-HANDOFF-PLAN.md`](https://github.com/chuongnd2612/emehub/blob/master/docs/SSO-HANDOFF-PLAN.md)
> (the full plan this is drawn from).
>
> Paths without a prefix are in **this** repo. Hub paths are marked `emehub/`.
>
> **Verify contracts against the hub's code, not against this document.** It has been wrong twice, in
> ways that cost real time: the CSRF header was documented as `X-CSRF` when the hub reads
> `X-CSRF-Token`, so SSO answered 403 on every attempt until #495; and ticket sync was documented as
> PAT-blocked when the hub had always accepted an agent token (§5, corrected above).

## Division of work

| Who | Repo | Slices |
|---|---|---|
| **You** | `q-agent` (this repo) | **B1–B5** — everything below in §3 |
| **Me** | `emehub` | **A1–A5** — the mint endpoint, config, `GET /agents`, the Launch buttons, ADR 0008 |

We do not touch each other's repo. That is also the hub's own rule (`emehub/CLAUDE.md:40` —
"Never modify them from a hub task; a change needed in QAgent or DAgent is an issue in that
repo").

**You are not blocked on me.** Everything in B1–B5 can be written and typechecked against the
contract in §2, which is frozen below. You only need my side running for the final end-to-end
click-through (§7 Stage 3).

---

## 1. Why this exists

The hub owns identity, Claude credentials, provider connections, projects, knowledge and
tickets. Q-Agent currently duplicates most of that and shares none of it — the two apps
disagree about who a user even is. The goal is: **sign in once at the hub, land in Q-Agent
already authenticated.**

This first slice is **identity only**. Q-Agent keeps its own login working alongside, keeps its
own users table, and does not yet read any configuration from the hub.

**Everything else is documented here but explicitly out of scope for now** — Claude credentials
(§4b), the ADO / Jira / GitHub connections (§4c), projects and knowledge (§4c), and tickets (§5).
Read §4a for the one-table summary of where each stands. Two of those are *blocked*, not merely
unscheduled, and the blockers are recorded so nobody plans a phase around them by accident.

---

## 2. The contract (frozen — code against this)

### 2.1 The endpoint you call

`POST https://hub.chuongnd.click/auth/agent-token`

```jsonc
// request
{ "audience": "qagent" }

// headers
//   X-CSRF-Token: <value of the emehub_csrf cookie, which is readable by JS>
// fetch options
//   credentials: 'include'      ← sends the shared emehub_refresh cookie

// 200 response
{
  "accessToken": "eyJhbGciOiJIUzI1NiIsImtpZCI6...",
  "audience": "qagent",
  "expiresIn": 900,
  "user": { "id": 3, "email": "duna.nguyen@emesoft.net",
            "firstName": "Duna", "lastName": "Nguyen",
            "role": "admin", "isActive": true, "totpEnabled": false }
}
```

Error codes and what each means to you are in §5 — **the mapping matters**, because "the hub is
down" and "you are logged out" must not render the same screen.

This endpoint **does not rotate** the hub's refresh token. That is deliberate and load-bearing:
`/auth/refresh` *does* rotate, so if you ever call `/auth/refresh` from Q-Agent instead, you and
the hub tab will race and log each other out. **Call `/auth/agent-token`, never `/auth/refresh`.**

### 2.2 The token you get

HS256. Claims:

```jsonc
{
  "sub":   "3",                 // ← the HUB's user id, NOT Q-Agent's. See §3.1.
  "email": "duna.nguyen@emesoft.net",
  "role":  "admin",             // admin | member
  "sid":   "3a7e…",             // hub session id
  "aud":   "qagent",
  "iss":   "emehub",
  "iat":   1785312000,
  "exp":   1785312900           // 15 minutes
}
```

Header carries a `kid` (currently `emehub-hs256-2026-07`).

**You MUST validate:** signature, `iss == "emehub"`, `aud == "qagent"`, `exp`.
**You MUST NOT** issue, refresh or extend hub tokens. Only the hub does that.

**Read `kid` and log it, but do not key verification on it.** The hub moves to RS256 + JWKS in
Phase 3; emitting `kid` now is what makes that upgrade non-breaking.

### 2.3 Distinguishing hub tokens from your own

Convenient accident, worth relying on:

| | `iss` | `aud` | `typ` |
|---|---|---|---|
| Q-Agent's own access token | *absent* | *absent* | `"access"` |
| Hub token | `"emehub"` | `"qagent"` | *absent* |

So a dual-accept decoder can branch on the presence of `iss` with zero ambiguity.

---

## 3. Your work — B1 to B5

### B1 · Foundation (solo — do this first, it blocks B2 and B3)

**`api/app/services/hub_tokens.py`** — new. `decode(token) -> HubClaims`:

- HS256 against `settings.hub_jwt_secret`
- `issuer="emehub"`, `audience="qagent"`
- require `exp`, `iat`, `iss`, `aud`, `sub`
- mirror `emehub/api/app/services/auth_service.py::_decode` **exactly**, so the two cannot drift
- read the `kid` header, log once per distinct value, do not verify against it

**`api/app/config.py`** — new settings:

| Setting | Default | Notes |
|---|---|---|
| `QAGENT_HUB_SSO_ENABLED` | **`false`** | The integration flag. Everything stays dormant while false. |
| `QAGENT_HUB_BASE_URL` | `""` | e.g. `https://hub.chuongnd.click` |
| `QAGENT_HUB_JWT_SECRET` | `""` | Must equal the hub's `EMEHUB_JWT_SECRET` (Phase 1 shared secret). |
| `QAGENT_HUB_AUDIENCE` | `"qagent"` | |

> **Do not reuse `QAGENT_SECRET_KEY`.** It already signs local JWTs *and* derives the Fernet key
> for every encrypted credential (`api/app/crypto.py`). Overloading it a third time makes the
> eventual re-key even worse than it already is. This is
> [`emehub` ADR 0005](https://github.com/chuongnd2612/emehub/blob/master/docs/adr/0005-secret-and-key-management.md).

Also expose `hubSsoEnabled` to the frontend through whatever settings read the SPA already does
— B4 needs it, and a dedicated endpoint for one boolean is not worth it.

**Migration** — `users.hub_user_id`, nullable, unique, indexed.

#### 3.1 Why `hub_user_id` is the crux

The hub's `sub` is a **hub** user id. Q-Agent's is a **Q-Agent** user id. They will never match,
and you must not assume they do.

Nearly every table here carries `owner_id → users.id`, and the per-user workspace filesystem is
literally a path built from it (`api/workspace/users/<owner_id>/{specs,evidence,knowledge,repos,auth}`,
ADR 0009). Re-pointing `owner_id` at hub ids is a migration across every scoped table — **not
this slice**.

So: keep local ids exactly as they are, and add `hub_user_id` as the mapping column. A hub token
resolves to a local user by `hub_user_id`; every existing row, run, evidence file and workspace
path keeps working untouched, and no data migration runs.

### B2 · Dual-accept token validation (parallel with B3)

**`api/app/deps_auth.py`** — in `require_user`: try the local decoder first, then
`hub_tokens.decode`, branching on `iss`.

A hub token resolves the local `User` by `hub_user_id`, **JIT-provisioning** on first sight from
`sub` / `email` / `role`.

Two traps that will bite:

1. **`require_user` stashes `user._sid`**, and the session routes in `api/app/routers/auth.py`
   use it to revoke sessions. A hub `sid` is **not** a Q-Agent session id — guard those routes so
   they never try to revoke a hub sid against the local `auth_sessions` table.
2. **The WebSocket paths** (`/ws/runs/{run_id}`, `/ws/ai`) validate tokens through their own
   helper in `api/app/main.py`, not through `require_user`. Route them through the same
   dual-accept path, or hub-authenticated users will silently lose live run progress — and it
   only shows up at runtime, never in typecheck or tests.

### B3 · The bootstrap round trip (parallel with B2)

**Backend — `POST /auth/sso/complete`** in `api/app/routers/auth.py`:

```
{ hubToken, next? }
  → hub_tokens.decode()
  → find-or-create the local user by hub_user_id
  → auth_service.create_session(...) + set_auth_cookies(...)   ← a NORMAL Q-Agent session
  → return exactly the body /auth/login returns
```

**Returning a login-shaped body is the whole trick.** It means
`app/src/store/auth.ts`, `app/src/lib/api.ts`'s 401→refresh→retry, and
`app/src/screens/RequireAuth.tsx` are **completely untouched**. One new screen, no store surgery,
and Q-Agent's own refresh cookie stays the browser-session credential throughout. Resist the urge
to teach the store about the hub.

**Frontend — `app/src/screens/auth/SsoCallback.tsx`** + a route in `app/src/router.tsx`:

- Register it as a **top-level ungated sibling**, like `signed-out`.
  - *Not* under `RedirectIfAuthed` — it would bounce a returning user mid-bootstrap.
  - *Not* under `RequireAuth` — the entire point is arriving anonymous.
- On mount: `POST {hubUrl}/auth/agent-token` with `credentials:'include'` + the `X-CSRF-Token` header
  read from the `emehub_csrf` cookie → hand the token to `/auth/sso/complete` → navigate to
  `next ?? "/"`.

**Entry point** — with the flag on, an unauthenticated load redirects to `/sso/callback` once
before falling through to `/login`. **Guard the loop** with a one-shot marker
(`sessionStorage`), or a signed-out user ping-pongs forever.

**No nginx change is needed.** `app/nginx.conf` already proxies `/auth/` unrewritten and falls
`/` through to `index.html`, so both the new API route and the new SPA route just work.

### B4 · "Sign in with EmeHub" on `/login` (after B3 — shares `screens/auth/`)

Rendered only when the backend reports SSO enabled. Local email + password stays working
underneath. Purely additive.

### B5 · Degradation — do not skip this

`app/src/lib/api.ts` currently collapses transport failure and auth failure into one logout path.
That is wrong here and produces the single most confusing possible error: telling a user they are
logged out when the hub is simply unreachable.

| Response from the hub | Meaning | What you render |
|---|---|---|
| Refused / DNS / timeout / 502–504 | **The hub is down** | "EmeHub is unreachable — we can't sign you in right now" + Retry. **Never** the login form. |
| `401` (no or dead refresh cookie) | Not signed in at the hub | Fall through to Q-Agent's own `/login`. Not an error, no banner. |
| `403` (CSRF mismatch) | Stale hub cookie state | Prompt to re-sign-in at the hub. |
| `400` unregistered audience | Misconfiguration | Operator-facing error naming `EMEHUB_AGENT_QAGENT_URL` on the hub. |
| `401` on a hub read with a valid-looking token | Session revoked at the hub | **This** is "you are logged out." |

No branch anywhere grants access because the hub was unavailable.

---

## 4. The integration flag

`QAGENT_HUB_SSO_ENABLED` defaults to **`false`**, and everything above stays dormant while it is.

| | Flag off (default) | Flag on |
|---|---|---|
| `/login` | Local form only | Local form **+** "Sign in with EmeHub" |
| Unauthenticated load | → `/login` | → `/sso/callback` once, then `/login` |
| `POST /auth/sso/complete` | 404 (route not registered) | Live |
| Hub tokens at the API | Rejected | Accepted alongside local tokens |
| `npm run dev` | Unchanged | Unchanged (no hub reachable on localhost anyway) |

Two consequences worth stating plainly:

- **Every slice is shippable with the flag off.** B1–B5 can merge to `master` before the hub side
  is deployed, and nothing changes for existing users.
- **The flag is not a fail-open switch.** With it off, hub tokens are *rejected*, not waved
  through. It gates a feature, not an authentication check — unlike `QAGENT_AUTH_REQUIRED`, see
  §8.

---

## 4-pre. What an agent token can actually do (measured)

Measured against the live hub on 2026-08-06 with a real `aud: qagent` token. Recorded because the
narrative sections below were written before anyone tried it, and were wrong in both directions.

| Endpoint | Agent token | Notes |
|---|---|---|
| `GET /tickets` | **200** | paginated, `page`/`pageSize`, **default `pageSize=25`** |
| `GET /tickets/{externalId}` | **200** | `description`, `acceptanceCriteria`, `acceptanceCriteriaHtml`, `attachments`, `comments`, `linkedPrs` |
| `POST /tickets/sync` | **200** | the hub syncs with **its own** PAT — see §5, the PAT never needs to cross |
| `GET /projects` | **200** | incl. `summary` (repo, branch, knowledge status) |
| `GET /connections` | **200** | `hasPat` only — **never the PAT** |
| `GET /credentials/claude/resolve` | **200** | real credential material + `source`/`status`/`expiresAt`/`scopes` |
| `GET /credentials/claude` | 401 | hub audience only |
| `PUT /credentials/claude/mode` | 401 | hub audience only — **an agent cannot switch the credential** |
| `GET /connections/{id}/work-item-metadata` | 401 | hub audience only |
| `GET /connections/{id}/sprints` | 401 | hub audience only |
| `GET /agents`, `GET /audit/events` | 401 | hub audience only |

**Two token properties that constrain every design here:**

1. **Agent tokens are session-bound.** A signature-valid token whose `sid` is not a live hub session
   is refused `401 "Session revoked or expired"` — the hub validates the session behind it, not just
   the signature. Not documented anywhere before; found by probing.
2. **15 minutes, and agents may not refresh.** So there is no useful server-side token cache: a
   stored token is expired or about to be. The browser mints one per request (it holds the hub
   cookies) and sends it in `X-Hub-Token`; the server spends it on a single call. Background work
   must resolve everything it needs **at run start**.

**Vocabulary differs across the boundary.** The hub says `azure_devops`; Q-Agent says `ado`
(`jira`/`github` match). Any join must translate, or it silently matches nothing — which is exactly
what shipped in #507, green, for the most-used provider.

---

## 4a. What the hub owns, and where each concern stands

One table, so nobody has to infer the state of play. **Only the first row is in scope now.**

| Concern | Today in Q-Agent | Hub side | Agent cutover | Section |
|---|---|---|---|---|
| **Identity** | Own users, sessions, 2FA, JWT | Built | **This slice (B1–B5)** | §3 |
| **Claude credentials** | Own encrypted per-user rows | Built, `/credentials/claude/resolve` live | Not started — blocked | §4b |
| **Provider connections** (ADO/Jira/GitHub) | Own encrypted PATs + adapters | Built, live adapters | **Blocked — the PAT never crosses** | §4c |
| **Projects & knowledge** | Own `project_config` / `project_knowledge` | Built, hub even *builds* KBs | Not started | §4c |
| **Tickets** | Own sync + `tickets` table | Built | Not started | §5 |
| **Audit** | Own log | Built, `POST /audit/events` | Not started | — |

---

## 4b. Claude credentials

**Not in this slice.** Do not wire it while doing B1–B5.

**Today.** Q-Agent stores a per-user Fernet-encrypted `.credentials.json`
(`api/app/models/claude_credentials.py`) with a shared-account fallback, and materialises it to
disk before invoking the CLI — `api/app/services/claude_credentials.py:506` `materialize(row, key)`
writes `workspace/claude-config/<key>/.credentials.json` and returns the directory that becomes
`CLAUDE_CONFIG_DIR`.

**Hub side is built.** `GET /credentials/claude/resolve` returns the credential material already
resolved through **own → shared → none**. Also live: `PUT /credentials/claude/refreshed` (the CLI
rotated its token; the hub stays authoritative) and `POST /credentials/claude/usage` — both
explicitly readable with an *agent* audience, unlike the rest of `/credentials/claude/*` which is
hub-only.

**The good news:** `materialize()` already exists here, and the hub wrote its own equivalent for
knowledge builds. So the cutover is "fetch from the hub instead of the local table, then call the
same `materialize()`" — not new machinery.

**What blocks it:**

1. **The re-key.** Every encrypted value in Q-Agent — `claude_credentials.credentials`, provider
   PATs, test-account passwords — is Fernet-encrypted with a key derived from
   `QAGENT_SECRET_KEY`. The hub uses a *separate* `EMEHUB_ENCRYPTION_KEY`. Migration is therefore
   **decrypt-with-old, re-encrypt-with-new**: a one-shot script that needs both secrets present,
   must be idempotent, and must be rehearsed against a database copy. It is a re-key, **not** a
   copy, and it must not be bundled with the user migration.
2. **`QAGENT_SECRET_KEY` does double duty** — it signs local JWTs *and* derives that Fernet key
   (`api/app/crypto.py`). Moving auth to the hub while credentials still live here splits one
   secret across two services.
3. **The 15-minute problem, and this is where it actually bites.** A credential must be *fresh* —
   the contract says a stale cached project list is fine but a stale Claude credential is not, and
   an agent must refuse rather than proceed. But hub tokens live 15 minutes, agents may not refresh
   them, and Q-Agent's AI work runs on background daemon threads where
   `QAGENT_CLAUDE_BOOTSTRAP_TIMEOUT_S` alone is 1200s. **A background run that needs to resolve a
   credential after minute 15 has no legal path today.** This needs a hub-side decision (a
   longer-lived agent grant, or resolving once at run start) before the phase can be planned.

One behavioural change to know about when you do get here: credential metadata now carries
`hasRefreshToken`, and `status` has a fourth value **`refreshable`**. A Claude OAuth *access*
token expires within hours, so a real credential is past `expiresAt` almost immediately; the hub
now reports `refreshable` rather than `expired` when a refresh token is present. **Code that
special-cases `status === "expired"` will see `refreshable` where it used to see `expired`.** The
refresh token itself is never exposed — only the boolean.

---

## 4c. Provider integrations — ADO, Jira, GitHub

**Not in this slice, and the most blocked of the lot.** This is the one to read before anyone
plans Phase 3.

**Today, both apps do this independently.** Q-Agent has `provider_connections`
(`api/app/models/provider_connection.py:54`) with `kind` ∈ `ado | jira | github`, encrypted
`secrets`, per-user `owner_id`, and its own live adapters. The hub has the same thing, with its
own adapters (`api/app/services/adapters/{azure_devops,github,jira}.py`) and the same
capability split:

| Provider | Capability | Credentials |
|---|---|---|
| Azure DevOps | work items **+** repositories | Org URL, Project, PAT |
| Jira | work items | Base URL, Project Key, Email, API token |
| GitHub | repositories | Org/owner, PAT |

So a user configures the same Azure DevOps PAT **twice**. That duplication is the motivating
example in the hub's own founding ADR — and it is still true.

**The blocker, stated plainly.** `GET /connections` returns capabilities and `hasPat`, and
**never the PAT**. The endpoint designed to solve that — `POST /connections/{id}/proxy`, where the
hub makes the provider call on the agent's behalf so the secret never leaves — is **deliberately
unbuilt**: a generic forwarder is an SSRF and header-leak surface that needs its own design.

The contract concedes the consequence: *"agents keep their own provider credentials and the hub's
`/connections` is informational."*

**Therefore Phase 3's stated exit criteria — "QAgent's `provider_connections` tables are gone" —
is unreachable as written.** Something has to be chosen first:

| Option | Trade-off |
|---|---|
| Build the proxy | PAT never leaves the hub. Needs an allowlist of permitted upstreams, header stripping, and a response-size cap — it is its own security-reviewed piece of work. |
| Per-provider scoped short-lived tokens | Cleanest where the provider supports it. GitHub can (installation tokens); classic ADO/Jira PATs cannot. So it is a per-provider answer, not a global one. |
| Return the PAT to the agent | Simple, and throws away the reason the boundary exists. Not recommended. |
| Keep provider calls agent-side indefinitely | Honest status quo. The hub's `/connections` stays informational and the duplication stays. Cheapest, and worth considering explicitly rather than by default. |

**Note what is *not* blocked.** Projects, repositories and **knowledge bases** already cross
cleanly, because the hub clones and builds them itself using its *own* connection and PAT — no
secret crosses the boundary. `GET /projects/{key}/repos/{repo}/knowledge` works with an agent
token today, and the hub's `PUT` path means an agent that builds its own knowledge can still
report it. Two caveats: test-account passwords come back **only to the owning user** (a shared
project is owned by nobody, so they stay masked even for an admin), and `storageState.json`
remains an agent-side browser artifact the hub will never hold.

---

## 5. Ticket management — what changes, and when

**Short answer: nothing in this slice.** Q-Agent keeps syncing and storing its own tickets
exactly as it does today. Do not touch the ticket path while doing B1–B5.

Recording the eventual shape so the direction is not lost:

**Today**

- Q-Agent syncs tickets from ADO/Jira via its own `provider_connections` and stores them in its
  own `tickets` table.
- The hub *also* syncs tickets, into its own store, through its own connections.
- So the same work item exists twice, fetched with two separately-configured PATs. A ticket looked
  at in the hub and the same ticket in Q-Agent are unrelated rows.

**Intended end state** ([`emehub` ROADMAP](https://github.com/chuongnd2612/emehub/blob/master/docs/ROADMAP.md) Phase 4)

- The hub owns the ticket store. Q-Agent reads `GET /tickets` and `GET /tickets/{external_id}`
  from the hub and drops its own sync.
- "A ticket looked at in QAgent is the same ticket DAgent implements."

**What blocks it right now:**

1. ~~**Ticket sync needs a provider PAT, and the PAT never crosses.**~~ **This was wrong** — see
   [emehub#85](https://github.com/chuongnd2612/emehub/issues/85). The hub's own contract document
   listed `POST /tickets/sync` as hub-audience-only; its code has never done that
   (`Depends(require_principal)` accepts any registered audience, `qagent` included). **The PAT does
   not need to cross:** Q-Agent calls `POST /tickets/sync`, the hub resolves the connection, decrypts
   *its own* PAT, calls the provider and upserts — the secret stays on the side that holds it.

   This voids the blocker *and* its conclusion: ticket ownership no longer has to move together with
   connection ownership, and reading tickets from the hub is implementable now, ungated by §4c.
   (Note §4c still stands for **direct** provider calls from Q-Agent — `GET /connections` really does
   withhold the PAT. What was wrong was treating that as blocking *hub-performed* sync.)
2. **No cache invalidation.** Agents may cache any hub `GET`, lifetime their choice, with no
   webhook, ETag or revision counter. A ticket or project config changed at the hub goes stale in
   Q-Agent silently — a smaller version of the drift the hub exists to remove.
3. **15-minute tokens vs. 20-minute work** — §4b, blocker 3. Reading tickets from a background
   run hits exactly that wall. Being addressed hub-side by a run-scoped credential grant
   ([emehub#88](https://github.com/chuongnd2612/emehub/issues/88)), which would also cover ticket
   reads from a background thread.

**This slice dodges all three** by using the hub token exactly once, at bootstrap, and then
running on a Q-Agent-native session. Keep it that way — the moment Q-Agent starts making hub
reads on a background thread, problem 3 becomes real.

**A cheap intermediate step**, if the full move stalls: keep Q-Agent's sync as-is but record the
hub's ticket id alongside, the same `hub_*_id` mapping trick §3.1 uses for users. It makes the
two stores reconcilable without moving ownership, and makes the eventual cutover a join rather
than a re-import.

---

## 6. DAgent (`ticket-executor`) — later, and bigger than it looks

Deferred, not forgotten. Four independent blockers:

1. **No user identity at all.** `lib/auth.ts` sets `te_session` =
   `HMAC-SHA256(APP_ACCESS_PASSWORD, "authenticated")` — a constant. No user records, no subject,
   nothing that can receive a hub identity. Hub SSO into DAgent is not "validate a token", it is
   "invent a user model" (Prisma schema, migrations, the lot).
2. **`authDisabled()` fails open.** When `APP_ACCESS_PASSWORD` is empty it returns true and
   `proxy.ts` lets *every* request through, pages and API alike. The contract says remove it, not
   supplement it — adding hub validation beside an off switch leaves the off switch in production.
3. **No containerisation.** No Dockerfile, no compose file, no nginx, no CI. It cannot sit behind
   the tunnel as-is. Its only gate is `npx tsc --noEmit`.
4. **No server-side Claude credential.** It shells out to whatever `claude` is logged in on the
   host, with `--dangerously-skip-permissions`. Consuming a hub-issued credential means building a
   materialisation path; Q-Agent's `claude_credentials.materialize()` is the worked example.

Plus the product question the hub's roadmap says **gates the whole phase**: is DAgent a local
developer tool or a hosted service? `--dangerously-skip-permissions` is defensible for the former
and indefensible for the latter.

Also unresolved: `ticket-executor` is under `DaoLinh98` while `emehub` and `q-agent` are under
`chuongnd2612`, so cross-repo issue creation may not even be possible.

**Order when it does happen:** user model → delete `authDisabled()` → hub token validation →
credential materialisation → read projects/tickets from the hub. Identity and credentials are two
milestones, not one issue.

---

## 7. Verification

**Gates** (from `CLAUDE.md`): `npm run typecheck` + `npm run build` for `app/` — there is **no**
frontend unit-test harness, so do not run `npm test`. `uv run pytest -q` for `api/` — or
`uv run --extra dev pytest -q` in a fresh worktree, where `pytest` isn't installed yet (it lives in
the `dev` optional-dependency extra).

> **Baseline the backend suite before you start.** 22 of 520 tests already fail on `master`
> (#469). Capture that number first and compare against it — do not expect green, and do not
> assume a failure is yours.

**Stage 1 — B1, unit level.** A valid hub token decodes; wrong `iss` rejected; wrong `aud`
rejected (mint one for `dagent` and confirm it fails); expired rejected; tampered signature
rejected; a token with an unknown `kid` still verifies (proving `kid` is not load-bearing).

**Stage 2 — B2/B3 with the flag on, hub stubbed.** A hub token reaches an authenticated route and
resolves to the right local user; `users.hub_user_id` is populated; a *second* login with the same
hub `sub` reuses the row rather than creating a duplicate; an existing local user's runs, evidence
and workspace path are untouched; a run WebSocket connects with a hub-derived session.

**Stage 3 — end to end, needs my side deployed.** Log in at `hub.chuongnd.click` → Launch QAgent →
land at `qagent.chuongnd.click` **authenticated, with no second login.** That sentence is verbatim
the hub roadmap's Phase 2 exit criterion.

**Stage 4 — the race this design exists to prevent.** With the hub tab still open, launch Q-Agent,
then force a refresh in the hub tab. **The hub session must survive.** If it does not, something is
calling `/auth/refresh` where it should call `/auth/agent-token`.

**Stage 5 — non-regression.** Local login works with the flag off *and* on. `npm run dev` is
unaffected with the flag off. Stop the hub → "EmeHub is unreachable", not the login form.

> **Localhost caveat.** All `localhost` ports share one cookie jar, so the flow can appear to work
> in dev for entirely the wrong reason, and `Secure` / real `SameSite` behaviour is never
> exercised. Localhost is fine for Stages 1–2; Stages 3–5 need the tunnel over HTTPS.

---

## 8. Decisions I need from you

1. **Email collision** — a hub user whose email already exists as a local Q-Agent user. Auto-link
   by setting `hub_user_id` on the existing row (my recommendation, with an audit entry), or refuse
   and require an admin? This is a security default, not a detail: auto-linking means whoever
   controls that email at the hub inherits the local account.
2. **Role authority** — the token carries `role`. The contract's principle is "the hub
   authenticates; agents authorise", which argues Q-Agent takes only *identity* from the token and
   keeps its own role. Confirm, because the alternative is easy to implement by accident.
3. **`QAGENT_AUTH_REQUIRED`** (`api/app/config.py:58`) makes the auth guard a passthrough, and the
   entire test suite runs with it off. This is the same class of bug as DAgent's `authDisabled()`
   that the contract says to *remove, not supplement* — and it sits directly in this work's path.
   Removing it means fixing every test's auth posture, so it is its own sized piece of work: in
   scope now, or a tracked exception?

---

## 9. Out of scope for this slice

- Proxying, then deleting, Q-Agent's local `/auth/*` and migrating users wholesale. (Argon2 hashes
  and plaintext TOTP secrets are portable; sessions are not — that step logs everyone out once, at
  a scheduled time.)
- Re-pointing `owner_id` at hub ids.
- The `QAGENT_SECRET_KEY` re-key — a decrypt-with-old / re-encrypt-with-new operation that **must
  not** be bundled with the user migration.
- Reading projects, knowledge, tickets or Claude credentials from the hub (§5).
- Context deep-linking ("open this ticket in QAgent").
- RS256 + JWKS.
