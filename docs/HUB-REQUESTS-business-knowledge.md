# Handoff to EmeHub — mirroring Business Knowledge

**Date:** 2026-09-13 · **From:** Q-Agent · **Subject:** what the hub would have to offer before a
project's Business Knowledge can be mirrored rather than cloned.

> **Status: OPEN — nothing is implemented on our side.** This file exists so that the gap is
> *stated* rather than silently absent. Same shape as
> [`HUB-REQUESTS-project-config.md`](HUB-REQUESTS-project-config.md), which is the precedent for
> how a cross-team ask is recorded here.

## What exists today (#831)

Business Knowledge ([ADR 0016](adr/0016-business-knowledge.md)) is per-project, per-user data in
Q-Agent: `business_source` + `business_fact` rows plus a document snapshot on disk under
`workspace/<scope>/business/`. An admin curates it in the shared namespace
(`owner_id IS NULL`) and a member gets a **copy** of it through the ADR 0009 §4 project clone —
rows re-stamped to the member, snapshots copied into their scope.

That is a copy, and copies drift. Two members of one team each hold their own snapshot of the
same wiki, and an update by the admin reaches neither until each re-clones or re-syncs. ADR 0016
§3 says so explicitly and accepts it for v1.

## What we would need to do better than copy

1. **A hub-side document surface.** `GET /projects/{key}/config` carries repos, environments,
   connections and test accounts; `…/knowledge` carries the code KB. There is **no** endpoint for
   business/domain documents at all, so there is nothing for us to mirror *from*.
2. **Change detection on it**, the same way the config payload got `updatedAt` + `ETag` /
   `If-None-Match` (emehub#148/#149). Our snapshots are hash-pinned deliberately (a changed
   upstream document must surface as *stale*, never shift under an existing test case), so a
   mirror needs a cheap "has this changed" read, not a re-fetch.
3. **An answer on who may read whose.** A business document is organisationally shaped — it
   belongs to the team, not to whoever uploaded it — but every authorisation helper we have is
   `owner_id`-shaped. Mirroring needs the hub to say which principals may read a project's
   documents before we can serve one member a document another ingested.
4. **Whether an agent token may write.** If mirroring is ever bidirectional (a document ingested
   in Q-Agent appearing in the hub), it needs the `require_principal` treatment that knowledge
   writes already have; `PUT /projects/{key}/config` is `require_user` and 401s for us.

Until (1) and (3) are answered, the clone is Q-Agent-local **by design**, and
`app/services/clone_service.py` points here rather than leaving a silent gap.
