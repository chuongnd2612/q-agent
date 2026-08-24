/**
 * Tests for the pause/resume decision logic of live authoring (#619).
 *
 * The interesting failure modes here are all decisions, not I/O:
 *
 *  - Claude CLI's own `session_id` was never captured (`job.sessionId` is
 *    Q-Agent's queue id, useless to `claude --resume`), so {@link sessionIdFrom}
 *    is the one thing that makes resume possible at all.
 *  - `--resume` reads a transcript out of `$CLAUDE_CONFIG_DIR/projects/...`, and
 *    that config dir lives inside the ephemeral authoring workdir — so the
 *    transcript really can be gone, and the code must NOTICE rather than run a
 *    `--resume` that fails.
 *  - The FALLBACK is the requirement most likely to be skipped, because the happy
 *    path looks fine without it. It is asserted here from both directions: it
 *    fires when it must, and it does NOT fire when a real resume is available.
 *
 * The `spawn`/HTTP half is exercised through `api.ts` with `fetch` mocked, in the
 * same style as api.test.ts.
 */

import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { afterEach, test } from "node:test";
import * as api from "../src/api";
import {
  MANUAL_NAV_NOTE,
  buildPassArgs,
  findTranscript,
  pauseWaitVerdict,
  planPass,
  sessionIdFrom,
} from "../src/authoringResume";
import { AgentConfig } from "../src/config";

const cfg: AgentConfig = {
  serverUrl: "http://127.0.0.1:8787",
  deviceToken: "test-token",
  deviceId: 1,
  deviceName: "test-device",
} as AgentConfig;

const realFetch = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = realFetch;
});

// ------------------------------------------------- capturing Claude's session id

test("the CLI's own session_id is read off the stream-json envelope", () => {
  // The shape seen in a live log: `top_keys=[... 'session_id' ...]` on the init
  // envelope. Before #619 nothing read it, which is exactly why resume was
  // impossible — so this is the single most load-bearing assertion in the slice.
  assert.equal(
    sessionIdFrom({ type: "system", subtype: "init", session_id: "abc-123" }),
    "abc-123"
  );
  assert.equal(sessionIdFrom({ type: "result", session_id: "abc-123" }), "abc-123");
});

test("a session_id that is absent, blank or not a string is treated as ABSENT", () => {
  // Each of these must degrade to the fallback rather than producing a
  // `claude --resume ""`, which would fail after the user already waited.
  for (const ev of [
    {},
    { type: "assistant" },
    { session_id: "" },
    { session_id: "   " },
    { session_id: 42 },
    { session_id: null },
    null,
    "not an object",
  ]) {
    assert.equal(sessionIdFrom(ev), "", `unexpected id from ${JSON.stringify(ev)}`);
  }
});

// ------------------------------------------------------------ transcript lookup

/** A throwaway CLAUDE_CONFIG_DIR laid out the way the CLI lays one out. */
function makeConfigDir(sessionId: string | null): string {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "qagent-resume-test-"));
  const projects = path.join(dir, "projects", "-tmp-qagent-authoring-xyz");
  fs.mkdirSync(projects, { recursive: true });
  if (sessionId) fs.writeFileSync(path.join(projects, `${sessionId}.jsonl`), "{}\n", "utf-8");
  return dir;
}

test("the transcript is found under projects/<slug>/ without guessing the slug", () => {
  // Deliberately NOT computing the cwd slug: how the CLI derives it is not part
  // of any contract we control, so the lookup scans for the session's own file.
  const dir = makeConfigDir("sess-1");
  const found = findTranscript(dir, "sess-1");
  assert.ok(found.endsWith(`${path.sep}sess-1.jsonl`), found);
  assert.equal(fs.existsSync(found), true);
  fs.rmSync(dir, { recursive: true, force: true });
});

test("a missing transcript, a missing store and an empty id all report ABSENT", () => {
  const dir = makeConfigDir("sess-1");
  // Right store, wrong session — the transcript for THIS session is gone.
  assert.equal(findTranscript(dir, "sess-2"), "");
  // No id at all.
  assert.equal(findTranscript(dir, ""), "");
  // The whole workdir was swept by the OS while the session sat paused.
  fs.rmSync(dir, { recursive: true, force: true });
  assert.equal(findTranscript(dir, "sess-1"), "");
});

// -------------------------------------------------------- resume vs. fallback

const baseInput = {
  guidance: ["click the Approve button in the modal I just opened"],
  guidanceHistory: ["log in as the admin first", "click the Approve button in the modal I just opened"],
  taskPrompt: "Author a spec for SUR-1: approve a claim.",
  remainingBudgetUsd: 1.25,
};

test("resume is chosen only when an id AND its transcript both exist", () => {
  const plan = planPass({ ...baseInput, claudeSessionId: "sess-1", transcriptPresent: true });
  assert.equal(plan.mode, "resume");
  assert.deepEqual(plan.sessionArgs, ["--resume", "sess-1"]);
  // The new guidance is delivered as the turn...
  assert.match(plan.prompt, /click the Approve button/);
  // ...and the OLD turn is not repeated: the resumed session already remembers
  // it, and re-issuing it reads as a fresh instruction to redo the login.
  assert.doesNotMatch(plan.prompt, /log in as the admin first/);
  // ...and the original task prompt is NOT re-issued either. Re-sending it is the
  // "restart with an accumulated prompt" behaviour the design explicitly rejects.
  assert.doesNotMatch(plan.prompt, /Author a spec for SUR-1/);
  assert.match(plan.trailLine, /same Claude session/i);
});

test("resume always warns that a human may have moved the page", () => {
  // Manual navigation across the pause is the POINT of the feature, so a resumed
  // Claude that trusts its remembered page state will click the wrong things.
  const plan = planPass({ ...baseInput, claudeSessionId: "sess-1", transcriptPresent: true });
  assert.ok(plan.prompt.includes(MANUAL_NAV_NOTE));
  assert.match(MANUAL_NAV_NOTE, /Re-inspect the current page state/);
});

test("FALLBACK: no session id ⇒ a fresh pass carrying ALL the guidance", () => {
  const plan = planPass({ ...baseInput, claudeSessionId: "", transcriptPresent: true });
  assert.equal(plan.mode, "fresh");
  assert.equal(plan.reason, "no-session-id");
  assert.deepEqual(plan.sessionArgs, [], "a fresh pass must NOT pass --resume");
  // A fresh Claude has no memory, so it needs the task and EVERY guidance turn —
  // the newest one alone would be a non-sequitur.
  assert.match(plan.prompt, /Author a spec for SUR-1/);
  assert.match(plan.prompt, /log in as the admin first/);
  assert.match(plan.prompt, /click the Approve button/);
  // And it must SAY so on the trail: silently degrading would let a user believe
  // the earlier context was preserved when it was not.
  assert.match(plan.trailLine, /Cannot resume/i);
  assert.match(plan.trailLine, /session id/i);
});

test("FALLBACK: a lost transcript ⇒ fresh, with a different stated reason", () => {
  const plan = planPass({ ...baseInput, claudeSessionId: "sess-1", transcriptPresent: false });
  assert.equal(plan.mode, "fresh");
  assert.equal(plan.reason, "transcript-missing");
  assert.deepEqual(plan.sessionArgs, []);
  assert.match(plan.trailLine, /transcript is gone/i);
});

test("the fresh pass is told how little budget is left", () => {
  // It redoes the WHOLE task on the session remainder, which can be far less than
  // the first pass had; without knowing that it explores as if it were pass one.
  const plan = planPass({
    ...baseInput,
    claudeSessionId: "",
    transcriptPresent: false,
    remainingBudgetUsd: 0.37,
  });
  assert.match(plan.prompt, /\$0\.37 of budget left/);
});

test("a resume with no new guidance still continues instead of hanging", () => {
  const plan = planPass({
    ...baseInput,
    guidance: [],
    guidanceHistory: [],
    claudeSessionId: "sess-1",
    transcriptPresent: true,
  });
  assert.equal(plan.mode, "resume");
  assert.match(plan.prompt, /Continue where you left off/);
});

// ------------------------------------------------------------------- pass argv

test("a resumed pass runs with the SAME tools/system prompt as the pass it continues", () => {
  const common = {
    model: "sonnet",
    systemPromptFile: "/w/system-prompt.txt",
    workDir: "/w",
  };
  const first = buildPassArgs({ ...common, prompt: "task", budgetUsd: 2 });
  const resumed = buildPassArgs({
    ...common,
    prompt: "guidance",
    budgetUsd: 0.5,
    sessionArgs: ["--resume", "sess-1"],
  });
  // Everything except the prompt, the budget and the --resume prefix is identical.
  // Two hand-maintained copies of this list is how a resumed pass silently loses
  // a tool and fails for reasons nobody can see.
  const normalise = (args: string[]) =>
    args.filter((a) => !["--resume", "sess-1", "task", "guidance", "2", "0.5"].includes(a));
  assert.deepEqual(normalise(resumed), normalise(first));
  assert.equal(resumed[0], "--resume");
  assert.equal(resumed[1], "sess-1");
  // The budget handed to the resumed pass is the SESSION remainder, not the
  // original ceiling — the whole point of the cost requirement in #619.
  assert.equal(resumed[resumed.length - 1], "0.5");
  assert.equal(resumed[resumed.length - 2], "--max-budget-usd");
});

// ---------------------------------------------------------------- wire protocol

test("a progress post reports both liveness and the pause directive", async () => {
  globalThis.fetch = (async () =>
    new Response(JSON.stringify({ ok: true, control: "pause" }), { status: 200 })) as typeof fetch;
  assert.deepEqual(await api.postAuthoringEventAlive(cfg, "s1", "authoring.progress", {}), {
    alive: true,
    control: "pause",
  });
});

test("an older server with no `control` field is not read as a pause", async () => {
  // The agent updates independently of the server (its own installer / npm
  // publish), so a new agent WILL talk to an older API. Inventing a pause there
  // would park a browser the user never asked to park.
  globalThis.fetch = (async () =>
    new Response(JSON.stringify({ ok: true }), { status: 200 })) as typeof fetch;
  const beat = await api.postAuthoringEventAlive(cfg, "s1", "authoring.progress", {});
  assert.deepEqual(beat, { alive: true, control: "" });
});

test("a 404 still means the run was stopped, and never means pause", async () => {
  globalThis.fetch = (async () => new Response("", { status: 404 })) as typeof fetch;
  assert.deepEqual(await api.postAuthoringEventAlive(cfg, "s1", "authoring.progress", {}), {
    alive: false,
    control: "",
  });
});

test("a network blip neither aborts nor pauses a live session", async () => {
  globalThis.fetch = (async () => {
    throw new Error("ECONNRESET");
  }) as typeof fetch;
  assert.deepEqual(await api.postAuthoringEventAlive(cfg, "s1", "authoring.progress", {}), {
    alive: true,
    control: "",
  });
});

test("the resume poll parses a resume directive, including the guidance", async () => {
  let seen = "";
  globalThis.fetch = (async (url: string) => {
    seen = String(url);
    return new Response(
      JSON.stringify({
        action: "resume",
        guidance: ["do the thing"],
        guidanceHistory: ["log in", "do the thing"],
        claudeSessionId: "sess-9",
        remainingBudgetUsd: 0.75,
        resumeCount: 2,
      }),
      { status: 200 }
    );
  }) as unknown as typeof fetch;
  const d = await api.pollAuthoringResume(cfg, "s1");
  assert.equal(seen, "http://127.0.0.1:8787/agent/authoring/s1/resume");
  assert.equal(d.action, "resume");
  assert.deepEqual(d.guidance, ["do the thing"]);
  assert.deepEqual(d.guidanceHistory, ["log in", "do the thing"]);
  assert.equal(d.claudeSessionId, "sess-9");
  assert.equal(d.remainingBudgetUsd, 0.75);
  assert.equal(d.resumeCount, 2);
});

test("a failed resume poll WAITS rather than tearing the user's browser down", async () => {
  // The device is holding a Chrome window the user is actively clicking in.
  // Treating one failed poll as "abort" would destroy their work; the server-side
  // expiry and the agent's own hard cap are what bound the wait instead.
  globalThis.fetch = (async () => {
    throw new Error("ECONNRESET");
  }) as typeof fetch;
  assert.equal((await api.pollAuthoringResume(cfg, "s1")).action, "wait");
  globalThis.fetch = (async () => new Response("", { status: 500 })) as typeof fetch;
  assert.equal((await api.pollAuthoringResume(cfg, "s1")).action, "wait");
});

test("a 404 on the resume poll aborts — the session is gone for good", async () => {
  globalThis.fetch = (async () => new Response("", { status: 404 })) as typeof fetch;
  const d = await api.pollAuthoringResume(cfg, "s1");
  assert.equal(d.action, "abort");
  assert.equal(d.reason, "session-gone");
});

test("an unknown action degrades to wait, not to an unhandled state", async () => {
  globalThis.fetch = (async () =>
    new Response(JSON.stringify({ action: "explode" }), { status: 200 })) as typeof fetch;
  assert.equal((await api.pollAuthoringResume(cfg, "s1")).action, "wait");
});

test("the paused post hands over the session id and the SESSION-total cost", async () => {
  let body: Record<string, unknown> = {};
  let seen = "";
  globalThis.fetch = (async (url: string, init: RequestInit) => {
    seen = String(url);
    body = JSON.parse(String(init.body));
    return new Response(JSON.stringify({ ok: true }), { status: 200 });
  }) as unknown as typeof fetch;
  await api.postAuthoringPaused(cfg, "s1", { claudeSessionId: "sess-3", costUsd: 0.42 });
  assert.equal(seen, "http://127.0.0.1:8787/agent/authoring/s1/paused");
  assert.deepEqual(body, { claudeSessionId: "sess-3", costUsd: 0.42 });
});

/**
 * #645: a pause is worth holding only while the browser it preserves is alive.
 * If the user closes Chrome, a later Continue cannot resume against it and
 * browser-harness has nothing to attach to — but nothing noticed, so the device
 * sat in its poll loop and the case stayed wedged until the server expired the
 * pause an hour later.
 */
test("a closed browser stops the wait, and says so", () => {
  assert.equal(pauseWaitVerdict({ browserGone: false, pastHardCap: false }), null);
  assert.deepEqual(pauseWaitVerdict({ browserGone: true, pastHardCap: false }), {
    reason: "browser-closed",
  });
  assert.deepEqual(pauseWaitVerdict({ browserGone: false, pastHardCap: true }), {
    reason: "pause-expired",
  });
  // Ordering is load-bearing: with both true, "you closed the browser" is the
  // honest reason. Reporting the timeout instead sends the user off to look at
  // expiry settings for something they did themselves.
  assert.deepEqual(pauseWaitVerdict({ browserGone: true, pastHardCap: true }), {
    reason: "browser-closed",
  });
});

