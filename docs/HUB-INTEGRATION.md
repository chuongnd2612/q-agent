# Q-Agent ← EmeHub integration — the agent-side work

> **Handoff document.** Everything Q-Agent has to build to join the EmeHub suite, plus what
> DAgent will need later. The hub-side counterpart lives in the `emehub` repo:
> [`docs/INTEGRATION.md`](https://github.com/chuongnd2612/emehub/blob/master/docs/INTEGRATION.md)
> (the contract) and
> [`docs/SSO-HANDOFF-PLAN.md`](https://github.com/chuongnd2612/emehub/blob/master/docs/SSO-HANDOFF-PLAN.md)
> (the full plan this is drawn from).
>
> Paths without a prefix are in **this** repo. Hub paths are marked `emehub/`.

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
own users table, and does not yet read any configuration from the hub. That comes later (§6).

---

## 2. The contract (frozen — code against this)

### 2.1 The endpoint you call

`POST https://hub.chuongnd.click/auth/agent-token`

```jsonc
// request
{ "audience": "qagent" }

// headers
//   X-CSRF: <value of the emehub_csrf cookie, which is readable by JS>
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
- On mount: `POST {hubUrl}/auth/agent-token` with `credentials:'include'` + the `X-CSRF` header
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

**What blocks it right now** — worth knowing before anyone plans that phase:

1. **The provider PAT never crosses the boundary.** `GET /connections` returns `hasPat` and
   nothing more, and the endpoint meant to fix that (`POST /connections/{id}/proxy`) is
   deliberately unbuilt. So Q-Agent cannot yet make provider calls with hub-held credentials, and
   Phase 3's stated exit criteria ("QAgent's `provider_connections` tables are gone") is
   unreachable as written.
2. **No cache invalidation.** Agents may cache any hub `GET`, lifetime their choice, with no
   webhook, ETag or revision counter. A ticket or project config changed at the hub goes stale in
   Q-Agent silently.
3. **15-minute tokens vs. 20-minute work.** Hub access tokens live 15 minutes and agents may not
   refresh them, but Q-Agent's AI pipeline runs in background daemon threads with
   `QAGENT_CLAUDE_BOOTSTRAP_TIMEOUT_S` alone at 1200s. A background run that needs a fresh hub
   read after minute 15 has no legal path today.

**This slice dodges all three** by using the hub token exactly once, at bootstrap, and then
running on a Q-Agent-native session. Keep it that way — the moment Q-Agent starts making
hub reads on a background thread, problem 3 becomes real.

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
frontend unit-test harness, so do not run `npm test`. `uv run pytest -q` for `api/`.

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
