# Handoff to EmeHub — project configuration seen from Q-Agent

**Date:** 2026-08-13 · **From:** Q-Agent · **Subject:** what the hub must change so Q-Agent can show
hub-owned project configuration (repos, environments, connections, test accounts).

> ## ✅ ANSWERED — EmeHub, 2026-08-13
>
> **All four are resolved. Two shipped, two answered, and one answer is not what we assumed.**
>
> | # | Outcome |
> |---|---|
> | 1 · change detection | **Shipped.** `updatedAt` on the config payload **and** `ETag` + `If-None-Match` → `304`. emehub#148, emehub#149 |
> | 2 · test-account passwords | **Answered — our assumption was wrong.** An agent token *can* receive them. See below. |
> | 3 · agent writes knowledge | **Answered: yes.** Already the intended, tested path. |
> | 4 · project GUID | **Shipped.** `guid` on the payloads, accepted anywhere `{key}` is. emehub#151 |
>
> ### 1 — change detection *(shipped, live)*
>
> `ProjectConfig.updated_at` already existed with `onupdate=utcnow`; it was only ever a serialisation
> gap. Both of our top two options landed, since they come from the same value:
>
> - `updatedAt` on `GET /projects/{key}/config` — `null` means never configured.
> - `ETag` + `If-None-Match` → `304 Not Modified`, no body. The `304` is declared in the OpenAPI schema.
>
> It is the **config row's** timestamp, not the project's — a project row does not change when its
> configuration does, so polling `GET /projects/{key}` would miss exactly the edits we care about.
>
> Note the ETag **varies with password visibility**: one row has two representations, so a validator
> obtained as the owner will not match a masked read. Do not share one cached validator across
> identities.
>
> ### 2 — test-account passwords: we were wrong, and it matters
>
> Our understanding was "never to an agent, so we cannot use them". **Not so.** `GET
> /projects/{key}/config` is `require_principal`, so an agent token is accepted and resolves to the hub
> user it acts for. Passwords are revealed when `config.owner_id == caller.id` — and that caller can be
> an agent.
>
> So the rule is about **ownership, not audience**:
>
> - **Personal (owner-owned) config → we DO get the plaintext password**, including via our
>   `aud: qagent` token.
> - **Shared config (`owner_id IS NULL`) → masked for everyone**, admins included. A shared credential
>   everyone can read is a credential that has left the hub.
>
> One trap they found while testing: **a shared *project* does not imply a shared *config***. `shared`
> is set on the config `PUT`; a config saved without it is owner-owned even on a shared project.
>
> **What this changes for us:** we can run a test that logs in with a hub-configured test account, for
> personal projects. The settings screen must therefore distinguish *"masked because this config is
> shared"* from *"none configured"* — those are different states and we were about to render both as an
> empty list.
>
> ### 3 — may an agent write knowledge? *Yes.*
>
> `PUT` and `PATCH /projects/{key}/repos/{repo}/knowledge` are both `require_principal`; the handlers
> are named `report_repo_knowledge` / `contribute_repo_knowledge`, and `test_the_full_status_lifecycle`
> drives the whole write lifecycle with an agent token. It is the intended path, not a side effect.
>
> Contrast: `PUT /projects/{key}/config` is `require_user` (hub audience only) and 401s for us — so
> knowledge is ours to report, configuration is not ours to rewrite. `POST …/knowledge/build` is also
> hub-only: it clones, runs Claude for minutes and spends money.
>
> ### 4 — project GUID *(shipped, live)*
>
> `guid` (uuid4) on `GET /projects` and `GET /projects/{key}`, and **accepted anywhere `{key}` is** —
> including `/config` and `/knowledge`, since a GUID we could not use for those would have been half a
> migration. `key` and `id` are unchanged and stay working permanently; there is no transition to run
> out of.
>
> Resolution is **by shape**, never key-then-GUID fallback: otherwise a project keyed like someone
> else's GUID could capture that identifier. There is a test that attempts it.
>
> Two corrections to our reasoning, recorded so we do not re-derive them as hub bugs:
>
> - **"Keys are not unique per user"** is not true of the hub — its keys are namespaced per owner and a
>   test pins it. Our collision (#583) was ours.
> - **"A rename orphans everything"** was already handled: we persist their numeric `id`, which a rename
>   does not change.
>
> The GUID's real value is a non-enumerable external reference and one identity vocabulary. Live data
> makes the point: the hub currently holds **two distinct projects both keyed `surency`**, in different
> namespaces, now separable.
>
> They did **not** extend GUIDs to tickets or connections — we raised the parallel but did not ask.

## Summary — the hub needs to do almost nothing

Everything Q-Agent needs is **already exposed and already readable by an agent-audience token**. The
reason Q-Agent shows nothing today is a gap on *our* side: our mirror creates a bare project row and
never copies the configuration. We are fixing that.

There are **four requests** below. One blocks us today (change detection); one is a larger, non-urgent alignment we would like agreed early (GUID identity).

---

## What was measured

Probed against `https://hub.chuongnd.click/api` on 2026-08-13 with a real `aud: qagent` token bound
to a live hub session.

| Endpoint | Agent token | Content |
|---|---|---|
| `GET /projects` | **200** | `id`, `key`, `name`, `shared`, `summary` (repo, repoUrl, branch, repoCount, knowledgeStatus, knowledgeConfidence, ticketCount) |
| `GET /projects/{key}` | **200** | as above, plus timestamps |
| `GET /projects/{key}/config` | **200** | `baseUrl`, `environments`, `repos`, `testAccounts`, `manualAuth`, `workItemConnectionId`, `repositoryConnectionId`, `extra` |
| `GET /projects/{key}/repos/{repo}/knowledge` | **200** | the built knowledge base |
| `GET /projects/{key}/knowledge` | 404 | only because none is built for this project |

`repos` carries exactly what our Repos tab needs:

```json
{"name": "surency-admin-hub",
 "repo_url": "https://DDKS@dev.azure.com/DDKS/Surency/_git/surency-admin-hub",
 "default_branch": "main", "local_repo_path": "", "default": true}
```

**So: no new endpoint, no new field, no audience change is required for the main case.** Q-Agent will
read `GET /projects/{key}/config` and mirror it.

---

## Request 1 — change detection *(the only one that blocks us)*

`GET /projects/{key}/config` returns **no `ETag`, no `Last-Modified`, no `Cache-Control`**, and there
is no revision counter or webhook anywhere in the API.

Please add **one** of, in order of preference:

1. `ETag` + `If-None-Match` on the project/config reads, or
2. a monotonic `revision` (or `updatedAt`) field in the payload, or
3. a webhook on project-config change.

**Why it matters more than it sounds.** Q-Agent now renders hub-owned project settings **read-only**
and tells the user to edit them in EmeHub. Without a change signal we cannot tell a stale copy from a
current one, so the user edits in EmeHub and Q-Agent keeps showing the old values with no indication
anything is out of date. That is the exact drift the hub exists to remove, and a read-only screen
showing stale data is worse than an editable one, because the user has no way to correct it.

Any of the three is enough; (2) is the cheapest and we can poll it.

This is the same gap recorded in our `docs/HUB-INTEGRATION.md` §5 blocker 2 — raising it again because
it has moved from "a future concern" to "the thing that will make our read-only UI lie".

---

## Request 2 — confirm the policy on test-account passwords

`testAccounts` is empty on the project we can see, so we could not observe the behaviour.

Our understanding from `INTEGRATION.md` is that **test-account passwords are returned only to the
owning user, and a *shared* project is owned by nobody, so they stay masked even for an admin**.

Please confirm, and decide explicitly:

- If passwords **never** reach an agent: Q-Agent cannot run a test that logs in using a hub-configured
  test account. We would keep test accounts configured locally, and the project settings screen must
  say so rather than showing an empty list that looks like a sync failure.
- If they **may** reach an agent under some condition (owner-owned project, specific audience): tell us
  the condition and we will honour it.

Either answer is workable — we just need to know which, because it changes what the UI tells the user.

---

## Request 3 — confirm whether an agent may *write* knowledge

`GET /projects/{key}/repos/{repo}/knowledge` is confirmed readable by an agent token (200).
`PUT` and `PATCH` exist on the same path, but **we did not test them** — they write to your data and we
would not do that unprompted.

If an agent-built knowledge base should be reportable back to the hub, confirm the intended audience
for `PUT`/`PATCH`. If not, say so and we will treat hub knowledge as read-only.

---

## Request 4 — adopt a GUID for project identity

**Not blocking, and larger than the others — which is why we would rather agree the direction now than
discover a mismatch later.**

Q-Agent has just moved projects to a generated **GUID** as their identity, because identifying a project
by its name or key turned out to be wrong in two concrete ways:

- **Keys/names are not unique per user.** Two users each with a "Surency" project collided, and the
  second was permanently locked out of their own project configuration. That was a real bug on our side
  (#583), not a hypothetical.
- **A rename orphans everything attached.** Config, knowledge and automation all pointed at the old
  string, so renaming silently detached them.

The hub currently identifies a project by `key` (a slug, e.g. `surency`) in the API and by a numeric
`id` in its own URLs. Both are stable *within* the hub, but neither is ideal as a cross-system
reference: a key is derived from a name and can be regenerated, and a numeric id is an internal
surrogate that is also enumerable.

**The request:** give hub projects a stable GUID and expose it on the project payloads
(`GET /projects`, `GET /projects/{key}`), keeping `key` and `id` working. We would then store the hub's
GUID rather than its numeric id, and the two systems would agree on identity that survives a rename on
either side.

If it is worth doing for projects, the same argument applies to **tickets and connections** — we store
`hub_ticket_id` and `hub_connection_id` today — but projects are where it actually bit us, so that is
the only one we are asking for.

**Please keep both identifiers during any transition.** When we did this refactor we deliberately kept
the old identifier accepted alongside the new one, because a hard cutover across every call site is how
this class of change breaks quietly. We would need the same grace period: we currently persist your
numeric project id, and a swap with no overlap would strand every mirrored project until we re-mapped
them.

---

## Not requests — recorded so nobody re-derives them

- **Connection ids are hub-scoped, and that is fine.** `workItemConnectionId` / `repositoryConnectionId`
  are the hub's ids; Q-Agent already mirrors connections with the hub id recorded, so we map them
  ourselves. No change needed.
- **The PAT still never crosses, and that is correct.** `GET /connections` returns `hasPat` only. Q-Agent
  makes no direct provider calls for hub-owned projects.
- **Deep links work.** We link users to `<hub>/app/projects/{id}` using the numeric project id, which we
  store when mirroring.
- **Project identity on our side is already done.** Q-Agent identifies projects by its own GUID and
  keeps the hub's numeric id alongside, so a rename on either side no longer breaks the association.
  Request 4 asks the hub to do the same; until then, our mapping holds and nothing is broken.

---

## What Q-Agent is doing on its side (for your awareness, no action needed)

- Mirroring `GET /projects/{key}/config` into the local project config on first view, so repos,
  environments and connection bindings appear.
- Project settings are **read-only** whenever the hub-data flag is on, enforced in the UI *and* refused
  by our API (`409`, naming EmeHub), so the screen and the API cannot disagree.
- A hub-owned project links out to the hub's own project page for editing.

## Contact

Raise anything unclear as an issue on `chuongnd2612/q-agent` referencing this document; we do not modify
the `emehub` repo, per the cross-repo rule.
