/**
 * Mocked-fetch tests for the wire-protocol client (`src/api.ts`) — verifies
 * the agent builds correct requests (method, auth header, body shape) for
 * claim/results/evidence without needing a live server.
 */

import assert from "node:assert/strict";
import { afterEach, test } from "node:test";
import * as api from "../src/api";
import { AgentConfig } from "../src/config";

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

test("claimNextJob returns null on 204 (no queued job)", async () => {
  mockFetch(() => new Response(null, { status: 204 }));
  const job = await api.claimNextJob(cfg);
  assert.equal(job, null);
  assert.equal(calls[0].url, "http://127.0.0.1:8787/agent/jobs/next");
  assert.equal((calls[0].init.headers as Record<string, string>).Authorization, "Bearer test-token");
  assert.equal(calls[0].init.method, "POST");
});

test("claimNextJob returns the parsed job payload on 200", async () => {
  const payload = {
    executionId: 42,
    runCode: "RUN-1",
    env: "Staging",
    browser: "chromium",
    workers: 2,
    headless: true,
    baseUrl: "https://app.example.com",
    manualAuth: true,
    authOrigins: ["https://app.example.com"],
    specs: [{ filename: "1428-TC-01.spec.ts", code: "// spec" }],
  };
  mockFetch(() => Response.json(payload));
  const job = await api.claimNextJob(cfg);
  assert.deepEqual(job, payload);
});

test("claimNextJob throws ApiError on a non-ok, non-204 response", async () => {
  mockFetch(() => new Response("nope", { status: 500 }));
  await assert.rejects(() => api.claimNextJob(cfg), api.ApiError);
});

test("postResult sends the parsed-report shape as JSON", async () => {
  mockFetch(() => new Response(null, { status: 200 }));
  await api.postResult(cfg, 42, { file: "1428-TC-01.spec.ts", status: "pass", duration_ms: 900, error_message: "" });
  assert.equal(calls[0].url, "http://127.0.0.1:8787/agent/jobs/42/results");
  const body = JSON.parse(calls[0].init.body as string);
  assert.deepEqual(body, { file: "1428-TC-01.spec.ts", status: "pass", duration_ms: 900, error_message: "" });
});

test("postEvent wraps event+payload", async () => {
  mockFetch(() => new Response(null, { status: 200 }));
  await api.postEvent(cfg, 42, "exec.auth.waiting", { url: "https://app.example.com" });
  const body = JSON.parse(calls[0].init.body as string);
  assert.deepEqual(body, { event: "exec.auth.waiting", payload: { url: "https://app.example.com" } });
});

test("postEvidence sends a multipart form with the right fields", async () => {
  const fs = await import("node:fs");
  const os = await import("node:os");
  const path = await import("node:path");
  const tmpFile = path.join(os.tmpdir(), `qagent-test-${Date.now()}.png`);
  fs.writeFileSync(tmpFile, "fake-png-bytes");

  mockFetch(() => new Response(null, { status: 200 }));
  await api.postEvidence(cfg, 42, {
    ticketExternalId: "SUR-1428",
    caseCode: "TC-01",
    kind: "screenshot",
    filePath: tmpFile,
    filename: "shot.png",
  });
  fs.rmSync(tmpFile);

  const init = calls[0].init;
  assert.equal(calls[0].url, "http://127.0.0.1:8787/agent/jobs/42/evidence");
  assert.ok(init.body instanceof FormData);
  const form = init.body as FormData;
  assert.equal(form.get("ticket_external_id"), "SUR-1428");
  assert.equal(form.get("case_code"), "TC-01");
  assert.equal(form.get("kind"), "screenshot");
  assert.ok(form.get("file") instanceof Blob);
});

test("postEvidence omits specPath for a run job, and sends it for a project job", async () => {
  const fs = await import("node:fs");
  const os = await import("node:os");
  const path = await import("node:path");
  const tmpFile = path.join(os.tmpdir(), `qagent-test-${Date.now()}-sp.png`);
  fs.writeFileSync(tmpFile, "fake-png-bytes");

  mockFetch(() => new Response(null, { status: 200 }));
  await api.postEvidence(cfg, 42, {
    ticketExternalId: "SUR-1428", caseCode: "TC-01", kind: "screenshot", filePath: tmpFile, filename: "shot.png",
  });
  await api.postEvidence(cfg, 43, {
    ticketExternalId: "", caseCode: "", kind: "screenshot", filePath: tmpFile, filename: "shot.png",
    specPath: "tests/checkout/smoke.spec.ts",
  });
  fs.rmSync(tmpFile);

  const runForm = calls[0].init.body as FormData;
  assert.equal(runForm.get("specPath"), null);
  assert.equal(runForm.get("spec_path"), null);

  const projectForm = calls[1].init.body as FormData;
  // Both spellings: the contract pins `specPath`, the endpoint's other fields
  // are snake_case, and guessing wrong would 404 the upload silently.
  assert.equal(projectForm.get("specPath"), "tests/checkout/smoke.spec.ts");
  assert.equal(projectForm.get("spec_path"), "tests/checkout/smoke.spec.ts");
});

test("postReport sends the report body through untouched", async () => {
  mockFetch(() => new Response(null, { status: 200 }));
  // Whitespace and a top-level `stats` key that parsePlaywrightReport discards:
  // the viewer needs them, so the body must be byte-identical to report.json.
  const raw = '{\n  "stats": {"expected": 2, "unexpected": 1},\n  "suites": []\n}\n';
  await api.postReport(cfg, 42, raw);
  assert.equal(calls[0].url, "http://127.0.0.1:8787/agent/jobs/42/report");
  assert.equal(calls[0].init.method, "POST");
  assert.equal(calls[0].init.body, raw);
  assert.equal((calls[0].init.headers as Record<string, string>).Authorization, "Bearer test-token");
});

test("postReport throws ApiError when the server rejects the report", async () => {
  mockFetch(() => new Response("too big", { status: 413 }));
  await assert.rejects(() => api.postReport(cfg, 42, "{}"), api.ApiError);
});

test("claimNextJob carries projectScoped and per-spec specPath through", async () => {
  const payload = {
    executionId: 77,
    runCode: "checkout-suite",
    env: "Staging",
    browser: "chromium",
    workers: 2,
    headless: true,
    baseUrl: "https://app.example.com",
    manualAuth: false,
    authOrigins: [],
    projectScoped: true,
    specs: [{ filename: "tests/checkout/smoke.spec.ts", code: "// spec", specPath: "tests/checkout/smoke.spec.ts" }],
  };
  mockFetch(() => Response.json(payload));
  const job = await api.claimNextJob(cfg);
  assert.deepEqual(job, payload);
});

test("postComplete sends the aggregate body", async () => {
  mockFetch(() => new Response(null, { status: 200 }));
  await api.postComplete(cfg, 42, { passed: 3, failed: 1, log: "tail" });
  const body = JSON.parse(calls[0].init.body as string);
  assert.deepEqual(body, { passed: 3, failed: 1, log: "tail" });
});

test("postHealFix starts a job then polls /agent/heal/{caseId}/fix/{jobId} for the action", async () => {
  // Async flow (#313): POST starts the job (returns jobId), GET polls until done.
  mockFetch((url, init) => {
    if (init.method === "POST") return Response.json({ jobId: "job-1", status: "running" });
    return Response.json({ status: "done", result: { action: "fixed", code: "// fixed", diff: "@@" } });
  });
  const out = await api.postHealFix(cfg, 99, {
    currentCode: "// old", error: "boom", output: "tail", domDistilled: { path: "/x" }, attempt: 2,
  });
  // First call: POST to start with the attempt body.
  assert.equal(calls[0].url, "http://127.0.0.1:8787/agent/heal/99/fix");
  assert.equal(calls[0].init.method, "POST");
  const body = JSON.parse(calls[0].init.body as string);
  assert.deepEqual(body, { currentCode: "// old", error: "boom", output: "tail", domDistilled: { path: "/x" }, attempt: 2 });
  // Second call: GET poll on the returned job id.
  assert.equal(calls[1].url, "http://127.0.0.1:8787/agent/heal/99/fix/job-1");
  assert.equal(calls[1].init.method, "GET");
  assert.equal(out.action, "fixed");
  assert.equal(out.code, "// fixed");
});

test("postHealFix surfaces a server-side error job", async () => {
  mockFetch((url, init) => {
    if (init.method === "POST") return Response.json({ jobId: "job-2", status: "running" });
    return Response.json({ status: "error", error: "Claude timed out" });
  });
  await assert.rejects(
    () =>
      api.postHealFix(cfg, 99, {
        currentCode: "// old", error: "boom", output: "", domDistilled: null, attempt: 1,
      }),
    /Claude timed out/
  );
});

test("postHealFinalize posts the outcome to /agent/heal/{caseId}/finalize", async () => {
  mockFetch(() => new Response(null, { status: 200 }));
  await api.postHealFinalize(cfg, 99, { finalStatus: "pass", finalCode: "// x", attempts: [] });
  assert.equal(calls[0].url, "http://127.0.0.1:8787/agent/heal/99/finalize");
  const body = JSON.parse(calls[0].init.body as string);
  assert.equal(body.finalStatus, "pass");
});

test("redeemDevice posts code+name with no auth header", async () => {
  mockFetch(() => Response.json({ deviceToken: "abc", deviceId: 7 }));
  const result = await api.redeemDevice("http://127.0.0.1:8787", "PAIR123", "my-laptop");
  assert.deepEqual(result, { deviceToken: "abc", deviceId: 7 });
  assert.equal(calls[0].url, "http://127.0.0.1:8787/agent/devices/redeem");
  assert.equal((calls[0].init.headers as Record<string, string>).Authorization, undefined);
  const body = JSON.parse(calls[0].init.body as string);
  assert.deepEqual(body, { code: "PAIR123", name: "my-laptop" });
});

test("fetchWithTimeout aborts a stalled request once the timeout elapses", async () => {
  // A server that never responds but honors the abort signal.
  mockFetch(
    (_u, init) =>
      new Promise<Response>((_resolve, reject) => {
        (init.signal as AbortSignal).addEventListener("abort", () => reject(new Error("aborted")));
      }),
  );
  await assert.rejects(api.fetchWithTimeout("http://127.0.0.1:8787/slow", { method: "POST" }, 20));
});

test("fetchWithTimeout returns the response when it resolves before the timeout", async () => {
  mockFetch(() => new Response(null, { status: 200 }));
  const res = await api.fetchWithTimeout("http://127.0.0.1:8787/ok", {}, 1000);
  assert.equal(res.status, 200);
});
