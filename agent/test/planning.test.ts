/**
 * Mocked-fetch tests for the live-PLANNING wire client (#900).
 *
 * Planning is the only device job with a server thread BLOCKED on its result
 * (the plan feeds the very next generation prompt), so the two things worth
 * pinning are the ones a stall would come from: the claim hits
 * `/agent/planning/next` with the device bearer, and the finalize posts the
 * sidecar's RAW TEXT — not a parsed object — because normalisation is the
 * server's contract and a device that parsed here would fork it.
 */

import assert from "node:assert/strict";
import { afterEach, test } from "node:test";
import * as api from "../src/api";
import { AgentConfig } from "../src/config";
import { processPlanningJob } from "../src/runner";

const cfg: AgentConfig = {
  serverUrl: "http://127.0.0.1:8787",
  deviceToken: "test-token",
  deviceId: 1,
  deviceName: "test-machine",
};

type FetchCall = { url: string; init: RequestInit };
let calls: FetchCall[] = [];
const originalFetch = globalThis.fetch;

function mockFetch(handler: (url: string, init: RequestInit) => Response | Promise<Response>): void {
  globalThis.fetch = (async (url: string | URL | Request, init?: RequestInit) => {
    const u = typeof url === "string" ? url : url.toString();
    calls.push({ url: u, init: init ?? {} });
    return handler(u, init ?? {});
  }) as typeof fetch;
}

afterEach(() => {
  globalThis.fetch = originalFetch;
  calls = [];
});

test("claimNextPlanning returns null on 204 (nothing queued)", async () => {
  mockFetch(() => new Response(null, { status: 204 }));
  assert.equal(await api.claimNextPlanning(cfg), null);
  assert.equal(calls[0].url, "http://127.0.0.1:8787/agent/planning/next");
  assert.equal(calls[0].init.method, "POST");
  assert.equal((calls[0].init.headers as Record<string, string>).Authorization, "Bearer test-token");
});

test("claimNextPlanning returns the claim payload on 200", async () => {
  const payload = {
    sessionId: "S1",
    baseUrl: "https://app.example.com",
    origin: "https://app.example.com",
    projectKey: "surency",
    repo: "web",
    runCode: "RUN-1",
    ticket: "SUR-1428",
    sidecarFilename: "plan.json",
    systemPrompt: "# planner methodology",
    taskPrompt: "Plan the manual test scenarios...",
    model: "sonnet",
    maxBudgetUsd: 2.5,
    logVerbosity: "concise",
  };
  mockFetch(() => Response.json(payload));
  assert.deepEqual(await api.claimNextPlanning(cfg), payload);
});

test("claimNextPlanning throws ApiError on a non-ok, non-204 response", async () => {
  mockFetch(() => new Response("nope", { status: 500 }));
  await assert.rejects(() => api.claimNextPlanning(cfg), api.ApiError);
});

test("postPlanningFinalize sends the sidecar as RAW TEXT, not a parsed object", async () => {
  mockFetch(() => new Response(null, { status: 200 }));
  // Deliberately not valid-looking JSON-object input: whatever the planner wrote
  // travels verbatim so the SERVER decides whether it is usable.
  const raw = '{"scenarios":[{"title":"Reset","steps":[{"action":"click","expect":"form"}]}]}';
  await api.postPlanningFinalize(cfg, "S1", {
    planJson: raw,
    summary: "Planned live",
    ok: true,
    costUsd: 0.27,
  });
  assert.equal(calls[0].url, "http://127.0.0.1:8787/agent/planning/S1/finalize");
  const body = JSON.parse(String(calls[0].init.body));
  assert.equal(typeof body.planJson, "string");
  assert.equal(body.planJson, raw);
  assert.equal(body.ok, true);
  assert.equal(body.costUsd, 0.27);
});

test("postPlanningEvent posts the event envelope to the session's events route", async () => {
  mockFetch(() => Response.json({ ok: true, alive: true }));
  await api.postPlanningEvent(cfg, "S1", "planning.progress", { phase: "step" });
  assert.equal(calls[0].url, "http://127.0.0.1:8787/agent/planning/S1/events");
  assert.deepEqual(JSON.parse(String(calls[0].init.body)), {
    event: "planning.progress",
    payload: { phase: "step" },
  });
});

test("postPlanningEvent reports alive:false when the server stopped waiting", async () => {
  // Two ways the server says "stop": the explicit flag, and a 404 once the
  // session row is gone (purged with a stopped run). Both must read as "not
  // alive" rather than as an error, or the device keeps spending budget.
  mockFetch(() => Response.json({ ok: true, alive: false }));
  assert.deepEqual(await api.postPlanningEvent(cfg, "S1", "planning.progress", {}), {
    ok: true,
    alive: false,
  });

  mockFetch(() => new Response("gone", { status: 404 }));
  assert.deepEqual(await api.postPlanningEvent(cfg, "S1", "planning.progress", {}), {
    ok: false,
    alive: false,
  });
});

test("a planning job with no captured profile finalizes immediately instead of stalling the server", async () => {
  // The waiting server learns nothing until its deadline unless every failure
  // path posts back — which is what turns a five-second "no captured login"
  // into a multi-minute stall. `origin` here has no session directory on this
  // machine, so the handler must bail AND report.
  const finalizes: Array<Record<string, unknown>> = [];
  mockFetch((url, init) => {
    if (url.endsWith("/finalize")) finalizes.push(JSON.parse(String(init.body)));
    return new Response(null, { status: 200 });
  });
  await processPlanningJob(cfg, {
    sessionId: "S-missing-profile",
    baseUrl: "https://nothing-captured.invalid",
    origin: "https://nothing-captured.invalid",
    projectKey: "p",
    repo: "r",
    runCode: "RUN-1",
    ticket: "SUR-1",
    sidecarFilename: "plan.json",
    systemPrompt: "m",
    taskPrompt: "t",
    model: "sonnet",
    maxBudgetUsd: 1,
  });
  assert.equal(finalizes.length, 1);
  assert.equal(finalizes[0].ok, false);
  assert.equal(finalizes[0].planJson, "");
  assert.match(String(finalizes[0].summary), /capture a manual login first/);
});
