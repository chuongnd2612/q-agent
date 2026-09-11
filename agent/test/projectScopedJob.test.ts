/**
 * The project-scoped job branch (#799): a job claimed from a project's
 * Automation tab has no run, no ticket and no case code, so the agent has to
 * name every spec by its repo-relative path, upload only failure screenshots,
 * and ship the raw JSON report the report viewer (#801) renders.
 *
 * These cover the four things that silently break if the branch is wrong:
 * identity degradation, the screenshot-only filter, the verbatim report upload,
 * and the workDir staying alive until the deferred uploads finish.
 */

import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { afterEach, test } from "node:test";
import { ParsedAttachment } from "../src/report";
import { AgentConfig } from "../src/config";
import { evidenceToUpload, identityFor, uploadEvidenceThenCleanup } from "../src/runner";

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

// ---------------------------------------------------------------- identityFor

test("a run spec keeps its ticket + case identity", () => {
  const id = identityFor({ filename: "tests/SUR-1428/1428-TC-01.spec.ts", code: "" }, false);
  assert.equal(id.ticket, "1428");
  assert.equal(id.caseCode, "TC-01");
  assert.equal(id.label, "1428 TC-01");
  assert.equal(id.specPath, "tests/SUR-1428/1428-TC-01.spec.ts");
});

test("an explicit ticket/case on the spec wins over the filename parse", () => {
  const id = identityFor(
    { filename: "tests/SUR-1428/1428-TC-01.spec.ts", code: "", ticketExternalId: "SUR-1428", caseCode: "TC-01" },
    false
  );
  assert.equal(id.ticket, "SUR-1428");
  assert.equal(id.label, "SUR-1428 TC-01");
});

test("a project spec degrades to its path, NOT to a parsed garbage ticket", () => {
  // The whole point: `smoke.spec.ts` would otherwise parse to ticket "smoke",
  // which matches no row on the server and reads as a real id in the log.
  const id = identityFor(
    { filename: "tests/checkout/smoke.spec.ts", code: "", specPath: "tests/checkout/smoke.spec.ts" },
    true
  );
  assert.equal(id.ticket, "");
  assert.equal(id.caseCode, "");
  assert.equal(id.specPath, "tests/checkout/smoke.spec.ts");
  assert.equal(id.label, "tests/checkout/smoke.spec.ts");
});

test("a project spec whose name LOOKS like the case convention still degrades", () => {
  const id = identityFor({ filename: "tests/1428-TC-01.spec.ts", code: "" }, true);
  assert.equal(id.ticket, "");
  assert.equal(id.caseCode, "");
  assert.equal(id.label, "tests/1428-TC-01.spec.ts");
});

// ----------------------------------------------------------- evidence filter

const ALL: ParsedAttachment[] = [
  { kind: "screenshot", path: "shot.png" },
  { kind: "video", path: "clip.webm" },
  { kind: "trace", path: "trace.zip" },
  { kind: "dom", path: "dom.html" },
  { kind: "dom-distilled", path: "dom.json" },
  { kind: "console", path: "console.json" },
];

test("a run job uploads every attachment, pass or fail", () => {
  assert.deepEqual(evidenceToUpload(false, "fail", ALL), ALL);
  assert.deepEqual(evidenceToUpload(false, "pass", ALL), ALL);
});

test("a failed project spec uploads its screenshots and nothing else", () => {
  assert.deepEqual(evidenceToUpload(true, "fail", ALL), [{ kind: "screenshot", path: "shot.png" }]);
});

test("a passing or skipped project spec uploads nothing at all", () => {
  assert.deepEqual(evidenceToUpload(true, "pass", ALL), []);
  assert.deepEqual(evidenceToUpload(true, "skipped", ALL), []);
});

// ------------------------------------------------ report upload + workDir life

function stagedWorkDir(): { workDir: string; shot: string } {
  const workDir = fs.mkdtempSync(path.join(os.tmpdir(), "qagent-test-"));
  const shot = path.join(workDir, "shot.png");
  fs.writeFileSync(shot, "fake-png-bytes");
  return { workDir, shot };
}

test("the raw report is uploaded VERBATIM, before the evidence, then the workDir goes", async () => {
  const { workDir, shot } = stagedWorkDir();
  // Deliberately odd whitespace + a key the agent's parser drops: if anything
  // round-trips this through JSON.parse/stringify, the body stops matching.
  const raw = '{\n  "stats": {"expected": 1},\n  "suites": []\n}\n';
  const existedDuringUpload: boolean[] = [];
  mockFetch((url) => {
    // Only the uploads themselves — the trailing `exec.evidence.done` event is
    // posted from the finally block, i.e. deliberately after cleanup.
    if (/\/(report|evidence)$/.test(url)) existedDuringUpload.push(fs.existsSync(shot));
    return new Response(null, { status: 200 });
  });

  await uploadEvidenceThenCleanup(
    cfg,
    42,
    [{ ticket: "", caseCode: "", kind: "screenshot", filePath: shot, specPath: "tests/checkout/smoke.spec.ts" }],
    workDir,
    raw
  );

  const report = calls.find((c) => c.url.endsWith("/agent/jobs/42/report"));
  assert.ok(report, "the report was not uploaded");
  assert.equal(report.init.method, "POST");
  assert.equal(report.init.body, raw);
  assert.equal((report.init.headers as Record<string, string>)["Content-Type"], "application/json");

  const evidence = calls.find((c) => c.url.endsWith("/agent/jobs/42/evidence"));
  assert.ok(evidence, "the screenshot was not uploaded");
  const form = evidence.init.body as FormData;
  assert.equal(form.get("specPath"), "tests/checkout/smoke.spec.ts");
  assert.equal(form.get("ticket_external_id"), "");
  assert.equal(form.get("case_code"), "");

  // The trap: processJob's finally must NOT have removed workDir first.
  assert.ok(
    existedDuringUpload.every(Boolean),
    "the workDir was removed before the deferred uploads finished"
  );
  // …and the uploader still owns cleanup once they have.
  assert.equal(fs.existsSync(workDir), false);
});

test("no report text means no report call, and cleanup still happens", async () => {
  const { workDir } = stagedWorkDir();
  mockFetch(() => new Response(null, { status: 200 }));
  await uploadEvidenceThenCleanup(cfg, 42, [], workDir, "");
  assert.equal(calls.filter((c) => c.url.endsWith("/report")).length, 0);
  assert.equal(fs.existsSync(workDir), false);
});

test("a failing report upload neither throws nor strands the workDir", async () => {
  const { workDir } = stagedWorkDir();
  mockFetch(() => new Response("boom", { status: 500 }));
  await uploadEvidenceThenCleanup(cfg, 42, [], workDir, "{}");
  assert.equal(fs.existsSync(workDir), false);
});
