/**
 * Typed HTTP client for the Local Agent wire protocol served by
 * `api/app/routers/agent.py`. Every job call sends
 * `Authorization: Bearer <deviceToken>`. Uses Node's built-in `fetch`
 * (Node 18+) — no extra HTTP dependency.
 */

import * as fs from "fs";
import { AgentConfig } from "./config";
import { ProjectBundle, ProjectFile } from "./projectBundle";
import { agentVersion } from "./version";

/** Raised for any non-2xx/204 response; carries the HTTP status for callers that care (e.g. 409). */
export class ApiError extends Error {
  constructor(
    public status: number,
    public body: string
  ) {
    super(`Request failed with status ${status}: ${body.slice(0, 500)}`);
  }
}

/** One spec source pulled from `AutomationSpec.code` for a claimed job.
 *
 * `ticketExternalId`/`caseCode` are OPTIONAL: the current server foundation
 * (`routers/agent.py` `claim_next_job`) does not include them, only
 * `filename`/`code` (see agent/README.md "Known limitation"). They are
 * typed here so the agent picks them up for free if a future server patch
 * adds them, without another wire-format change.
 */
export interface JobSpec {
  filename: string;
  code: string;
  ticketExternalId?: string;
  caseCode?: string;
}

/** The `/agent/jobs/next` claim payload. */
export interface Job {
  executionId: number;
  runCode: string;
  env: string;
  browser: string;
  workers: number;
  headless: boolean;
  /** Honors the "Capture video" setting (#456): record video for every case when
   * true, none when false. Optional for backward-compat with older servers. */
  captureVideo?: boolean;
  baseUrl: string;
  manualAuth: boolean;
  authOrigins: string[];
  specs: JobSpec[];
  /** The persistent automation project shipped wholesale with the claim (#541).
   * Present only for layered runs; absent for legacy self-contained specs, in
   * which case `specs[].filename` is a bare filename as before. `files[].path`
   * is project-relative (`pages/LoginPage.ts`), and `specs[].filename` is too
   * (`tests/SUR-1428/SUR-1428-TC-01.spec.ts`). */
  project?: ProjectBundle;
  /** Present when this job is an agent-executed self-heal (#260): run the heal
   * LOOP for this one case, calling /agent/heal/{caseId}/fix + /finalize. */
  heal?: {
    caseId: number;
    maxAttempts: number;
    /** Shorter per-test / per-action Playwright timeouts for heal re-runs (#398). */
    testTimeoutMs?: number;
    actionTimeoutMs?: number;
  };
}

function authHeaders(token: string): Record<string, string> {
  return { Authorization: `Bearer ${token}` };
}

async function throwIfNotOk(res: Response): Promise<void> {
  if (!res.ok) {
    throw new ApiError(res.status, await res.text().catch(() => ""));
  }
}

/** Multipart evidence (video/trace) can be multi-MB; cap the upload so a stalled
 * connection aborts instead of hanging the background uploader forever. */
const EVIDENCE_UPLOAD_TIMEOUT_MS = 300_000;

/** `fetch()` with a hard timeout via `AbortController`, so a stalled connection
 * can't hang indefinitely (Node's `fetch` has no default timeout). Rejects with
 * an `AbortError` once `timeoutMs` elapses. */
export async function fetchWithTimeout(
  url: string,
  init: RequestInit,
  timeoutMs: number
): Promise<Response> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(url, { ...init, signal: controller.signal });
  } finally {
    clearTimeout(timer);
  }
}

/** Redeem a one-time pairing code for a durable device token (the `pair` command). */
export async function redeemDevice(
  serverUrl: string,
  code: string,
  name: string
): Promise<{ deviceToken: string; deviceId: number }> {
  const res = await fetch(`${serverUrl}/agent/devices/redeem`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ code, name }),
  });
  await throwIfNotOk(res);
  return (await res.json()) as { deviceToken: string; deviceId: number };
}

/** Self-revoke this device on the server (the "Disconnect" action). Best-effort:
 * the caller wipes the local token regardless, so a failure here only means the
 * server list clears on `last_seen_at` staleness instead of immediately. */
export async function disconnectDevice(cfg: AgentConfig): Promise<void> {
  const res = await fetch(`${cfg.serverUrl}/agent/disconnect`, {
    method: "POST",
    headers: authHeaders(cfg.deviceToken),
  });
  await throwIfNotOk(res);
}

/**
 * Long-poll claim the next queued job for this device's owner. `null` on 204
 * (nothing queued).
 *
 * Sends `{agentVersion}` (#541) so the server can refuse to hand a **layered**
 * project to a build that would flatten it — the alternative being a wall of
 * import errors that reads as a mass test failure. That refusal comes back as a
 * 409 whose body is the "update your Local Agent" message, which the claim loop
 * logs; the execution is failed server-side with the same text, so it is not
 * re-claimed in a loop. Legacy self-contained specs are unaffected.
 */
export async function claimNextJob(cfg: AgentConfig): Promise<Job | null> {
  const res = await fetch(`${cfg.serverUrl}/agent/jobs/next`, {
    method: "POST",
    headers: { ...authHeaders(cfg.deviceToken), "Content-Type": "application/json" },
    body: JSON.stringify({ agentVersion: agentVersion() }),
  });
  if (res.status === 204) return null;
  await throwIfNotOk(res);
  return (await res.json()) as Job;
}

/** A queued manual-login capture: open a headed browser at `baseUrl` on THIS
 * machine, let the operator log in, and save the session for `origin` locally. */
export interface CaptureJob {
  captureId: number;
  projectKey: string;
  baseUrl: string;
  origin: string;
}

/** Claim the next queued auth-capture for this device's owner. `null` on 204. */
export async function claimNextCapture(cfg: AgentConfig): Promise<CaptureJob | null> {
  const res = await fetch(`${cfg.serverUrl}/agent/auth/next`, {
    method: "POST",
    headers: authHeaders(cfg.deviceToken),
  });
  if (res.status === 204) return null;
  await throwIfNotOk(res);
  return (await res.json()) as CaptureJob;
}

/** Report a capture's outcome so the server clears "capturing" + stamps the marker. */
export async function postCaptureComplete(
  cfg: AgentConfig,
  captureId: number,
  ok: boolean,
  error?: string
): Promise<void> {
  const res = await fetch(`${cfg.serverUrl}/agent/auth/${captureId}/complete`, {
    method: "POST",
    headers: { ...authHeaders(cfg.deviceToken), "Content-Type": "application/json" },
    body: JSON.stringify({ ok, error }),
  });
  await throwIfNotOk(res);
}

/** Push one progress event; the server re-emits it on the run's WebSocket unchanged. */
export async function postEvent(
  cfg: AgentConfig,
  executionId: number,
  event: string,
  payload: Record<string, unknown>
): Promise<void> {
  const res = await fetch(`${cfg.serverUrl}/agent/jobs/${executionId}/events`, {
    method: "POST",
    headers: { ...authHeaders(cfg.deviceToken), "Content-Type": "application/json" },
    body: JSON.stringify({ event, payload }),
  });
  await throwIfNotOk(res);
}

/** One parsed result entry, shaped like `parse_playwright_report`'s per-spec output. */
export interface ResultEntry {
  file: string;
  status: string;
  duration_ms: number;
  error_message: string;
}

/** Push one parsed case result; the server matches it to its `ExecutionResult` by filename. */
export async function postResult(cfg: AgentConfig, executionId: number, entry: ResultEntry): Promise<void> {
  const res = await fetch(`${cfg.serverUrl}/agent/jobs/${executionId}/results`, {
    method: "POST",
    headers: { ...authHeaders(cfg.deviceToken), "Content-Type": "application/json" },
    body: JSON.stringify(entry),
  });
  await throwIfNotOk(res);
}

/** One evidence artifact to upload for a case's result. */
export interface EvidenceUpload {
  ticketExternalId: string;
  caseCode: string;
  kind: string;
  filePath: string;
  filename: string;
}

/** Multipart-upload one evidence artifact (screenshot/video/trace). */
export async function postEvidence(cfg: AgentConfig, executionId: number, ev: EvidenceUpload): Promise<void> {
  const data = fs.readFileSync(ev.filePath);
  const form = new FormData();
  form.set("ticket_external_id", ev.ticketExternalId);
  form.set("case_code", ev.caseCode);
  form.set("kind", ev.kind);
  form.set("file", new Blob([data]), ev.filename);
  const res = await fetchWithTimeout(
    `${cfg.serverUrl}/agent/jobs/${executionId}/evidence`,
    {
      method: "POST",
      headers: authHeaders(cfg.deviceToken),
      body: form,
    },
    EVIDENCE_UPLOAD_TIMEOUT_MS
  );
  await throwIfNotOk(res);
}

/** Server's decision for one failed heal attempt (#260) — see heal_service.plan_fix. */
export interface HealFixResult {
  action: "fixed" | "blocked" | "rejected" | "product_defect";
  code?: string;
  diff?: string;
  reason?: string;
  failureClass?: string;
  gate?: string;
  /** Shared-library files the server repaired for this failure (#547): the defect
   * was a stale locator inside a page object the spec imports, so the fix is
   * there and `code` comes back UNCHANGED. The agent is stateless and has no
   * read-file channel to the project, so the only way it can run the repair is
   * for the server to ship the new sources — same reasoning as the claim's
   * project bundle. Merged into `job.project.files` so the next attempt's
   * re-stage writes them. Absent on older servers, which simply re-run the
   * unchanged spec. */
  libraryFiles?: ProjectFile[];
  /** Plain-text summary of what the library repair changed, for the log. */
  librarySummary?: string;
}

/** Heal-fix polling: the server generates the fix (~3 min Claude call) in the
 * background so no single request stays open long enough to hit a fronting
 * proxy's ~100s edge cap (Cloudflare 524). Poll every 3s, capped at 6 min
 * (covers the server's 300s Claude timeout + margin); each request is short. */
const HEAL_FIX_POLL_INTERVAL_MS = 3_000;
const HEAL_FIX_TOTAL_TIMEOUT_MS = 360_000;
const HEAL_FIX_REQUEST_TIMEOUT_MS = 30_000;

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

/** Ask the server to classify + fix one failed heal attempt (Claude + KB live
 * server-side). Starts an async job then polls for the result, so a long fix
 * generation never trips the ~100s proxy timeout that a synchronous call did. */
export async function postHealFix(
  cfg: AgentConfig,
  caseId: number,
  body: { currentCode: string; error: string; output: string; domDistilled: unknown; attempt: number }
): Promise<HealFixResult> {
  const startRes = await fetchWithTimeout(
    `${cfg.serverUrl}/agent/heal/${caseId}/fix`,
    {
      method: "POST",
      headers: { ...authHeaders(cfg.deviceToken), "Content-Type": "application/json" },
      body: JSON.stringify(body),
    },
    HEAL_FIX_REQUEST_TIMEOUT_MS
  );
  await throwIfNotOk(startRes);
  const { jobId } = (await startRes.json()) as { jobId?: string };
  if (!jobId) throw new Error("Heal fix did not return a job id");

  const deadline = Date.now() + HEAL_FIX_TOTAL_TIMEOUT_MS;
  for (;;) {
    const res = await fetchWithTimeout(
      `${cfg.serverUrl}/agent/heal/${caseId}/fix/${jobId}`,
      { method: "GET", headers: authHeaders(cfg.deviceToken) },
      HEAL_FIX_REQUEST_TIMEOUT_MS
    );
    await throwIfNotOk(res);
    const data = (await res.json()) as { status: string; result?: HealFixResult; error?: string };
    if (data.status === "done" && data.result) return data.result;
    if (data.status === "error") throw new Error(data.error || "Heal fix failed on the server");
    if (Date.now() > deadline) throw new Error("Heal fix timed out waiting for the server");
    await sleep(HEAL_FIX_POLL_INTERVAL_MS);
  }
}

/** Persist the heal's final outcome + feed a passing DOM-grounded heal into the KB. */
export async function postHealFinalize(
  cfg: AgentConfig,
  caseId: number,
  body: Record<string, unknown>
): Promise<void> {
  const res = await fetch(`${cfg.serverUrl}/agent/heal/${caseId}/finalize`, {
    method: "POST",
    headers: { ...authHeaders(cfg.deviceToken), "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  await throwIfNotOk(res);
}

/** A claimed DOM-exploration session (#338): drive a real browser on THIS
 * machine through the observe→decide→act loop for one target. Mirrors the
 * frozen `/agent/explore/next` contract (epic #336). */
export interface ExplorationSession {
  sessionId: string;
  baseUrl: string;
  origin: string;
  target: { ticket: string; screen: string; goal: string };
  maxSteps: number;
  allowStateChanging: boolean;
  projectKey: string;
  repo: string;
  runId?: string;
}

/** One observation snapshot handed to the server's decide step. */
export interface ExploreObservation {
  accessibility: unknown;
  elements: unknown;
  url: string;
  path: string;
}

/** The server's decision for one exploration step (Claude call + budget live
 * server-side). `stop` (with `stopReason`) short-circuits the loop — e.g. the
 * server sets `stop:true, stopReason:"budget"` when the cost budget is spent. */
export interface DecideResult {
  action: string;
  args: Record<string, unknown>;
  reasoning: string;
  stop?: boolean;
  stopReason?: string;
}

/** Claim the next queued exploration session for this device's owner. `null` on 204. */
export async function claimNextExploration(cfg: AgentConfig): Promise<ExplorationSession | null> {
  const res = await fetch(`${cfg.serverUrl}/agent/explore/next`, {
    method: "POST",
    headers: authHeaders(cfg.deviceToken),
  });
  if (res.status === 204) return null;
  await throwIfNotOk(res);
  return (await res.json()) as ExplorationSession;
}

/** Ask the server to decide the next exploration step (Claude + budget live
 * server-side). Starts an async job then polls for the result — same async
 * start-then-poll shape as {@link postHealFix} so a long decide never trips the
 * ~100s proxy edge cap. */
export async function postExploreDecide(
  cfg: AgentConfig,
  sessionId: string,
  body: { observation: ExploreObservation; history: Array<Record<string, unknown>>; stepsTaken: number }
): Promise<DecideResult> {
  const startRes = await fetchWithTimeout(
    `${cfg.serverUrl}/agent/explore/${sessionId}/decide`,
    {
      method: "POST",
      headers: { ...authHeaders(cfg.deviceToken), "Content-Type": "application/json" },
      body: JSON.stringify(body),
    },
    HEAL_FIX_REQUEST_TIMEOUT_MS
  );
  await throwIfNotOk(startRes);
  const { jobId } = (await startRes.json()) as { jobId?: string };
  if (!jobId) throw new Error("Explore decide did not return a job id");

  const deadline = Date.now() + HEAL_FIX_TOTAL_TIMEOUT_MS;
  for (;;) {
    const res = await fetchWithTimeout(
      `${cfg.serverUrl}/agent/explore/${sessionId}/decide/${jobId}`,
      { method: "GET", headers: authHeaders(cfg.deviceToken) },
      HEAL_FIX_REQUEST_TIMEOUT_MS
    );
    await throwIfNotOk(res);
    const data = (await res.json()) as { status: string; result?: DecideResult; error?: string };
    if (data.status === "done" && data.result) return data.result;
    if (data.status === "error") throw new Error(data.error || "Explore decide failed on the server");
    if (Date.now() > deadline) throw new Error("Explore decide timed out waiting for the server");
    await sleep(HEAL_FIX_POLL_INTERVAL_MS);
  }
}

/** Push one exploration progress event; the server relays it to the run WS
 * (`explore.progress`) when the session carries a runId. */
export async function postExploreEvent(
  cfg: AgentConfig,
  sessionId: string,
  event: string,
  payload: Record<string, unknown>
): Promise<void> {
  const res = await fetch(`${cfg.serverUrl}/agent/explore/${sessionId}/events`, {
    method: "POST",
    headers: { ...authHeaders(cfg.deviceToken), "Content-Type": "application/json" },
    body: JSON.stringify({ event, payload }),
  });
  await throwIfNotOk(res);
}

/** Finalize an exploration session: the server KB-merges the observed
 * discovery (`merge_verified_discovery`) and stores the terminal result. */
export async function postExploreFinalize(
  cfg: AgentConfig,
  sessionId: string,
  body: Record<string, unknown>
): Promise<void> {
  const res = await fetch(`${cfg.serverUrl}/agent/explore/${sessionId}/finalize`, {
    method: "POST",
    headers: { ...authHeaders(cfg.deviceToken), "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  await throwIfNotOk(res);
}

/** Finalize an execution with its aggregate counts + captured log tail. */
export async function postComplete(
  cfg: AgentConfig,
  executionId: number,
  body: { passed: number; failed: number; log: string }
): Promise<void> {
  const res = await fetch(`${cfg.serverUrl}/agent/jobs/${executionId}/complete`, {
    method: "POST",
    headers: { ...authHeaders(cfg.deviceToken), "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  await throwIfNotOk(res);
}

// ---------------------------------------- Agent-driven live authoring (#400/403)

/** Claim payload for a live-authoring session — everything the agent needs to
 * author a spec locally. Prompts are composed server-side (the agent has no
 * skills/ dir); the agent runs its local `claude` + `browser-harness`. */
export interface AuthoringJob {
  sessionId: string;
  baseUrl: string;
  origin: string;
  projectKey: string;
  repo: string;
  caseId: number;
  runId?: number;
  specFilename: string;
  sidecarFilename: string;
  systemPrompt: string;
  taskPrompt: string;
  model: string;
  maxBudgetUsd: number;
  /** Authoring log verbosity from Settings (#438): "concise" hides raw tool/Bash
   * step lines in the agent's AGENT LOG, "verbose" shows them. */
  logVerbosity?: string;
  /** The run owner's saved Claude credential (.credentials.json content) so the
   * local `claude` uses the app's Settings credential instead of a separate
   * `claude login`. Empty ⇒ fall back to the agent's own local login. */
  claudeCredentials?: string;
}

/** Claim the next queued authoring session, or null (204) if none. */
export async function claimNextAuthoring(cfg: AgentConfig): Promise<AuthoringJob | null> {
  const res = await fetch(`${cfg.serverUrl}/agent/authoring/next`, {
    method: "POST",
    headers: authHeaders(cfg.deviceToken),
  });
  if (res.status === 204) return null;
  await throwIfNotOk(res);
  return (await res.json()) as AuthoringJob;
}

/** Relay an authoring progress event onto the run's WebSocket (server-side). */
export async function postAuthoringEvent(
  cfg: AgentConfig,
  sessionId: string,
  event: string,
  payload: Record<string, unknown>
): Promise<void> {
  const res = await fetch(`${cfg.serverUrl}/agent/authoring/${sessionId}/events`, {
    method: "POST",
    headers: { ...authHeaders(cfg.deviceToken), "Content-Type": "application/json" },
    body: JSON.stringify({ event, payload }),
  });
  await throwIfNotOk(res);
}

/**
 * Post an authoring progress event and report whether the session is still alive
 * (#420). Returns false on a 404 — the server purged the session because the run
 * was stopped — which the caller uses as a mid-flight abort signal to kill the
 * local Claude run. Never throws: a transient network error returns true so a
 * blip doesn't abort a live session.
 */
export async function postAuthoringEventAlive(
  cfg: AgentConfig,
  sessionId: string,
  event: string,
  payload: Record<string, unknown>
): Promise<boolean> {
  try {
    const res = await fetch(`${cfg.serverUrl}/agent/authoring/${sessionId}/events`, {
      method: "POST",
      headers: { ...authHeaders(cfg.deviceToken), "Content-Type": "application/json" },
      body: JSON.stringify({ event, payload }),
    });
    return res.status !== 404;
  } catch {
    return true;
  }
}

/** Finalize an authoring session: the server gates + persists the authored spec
 * and KB-merges the runtime-verified discovery. */
export async function postAuthoringFinalize(
  cfg: AgentConfig,
  sessionId: string,
  body: {
    code: string;
    discovered: Record<string, unknown>;
    summary: string;
    ok: boolean;
    costUsd?: number;
    /** The `.credentials.json` content after the local `claude` run — sent back so
     * the server can capture an OAuth token the CLI rotated on this device
     * (auto-rotation of the uploaded credential). Omit/empty when unchanged. */
    refreshedCredentials?: string;
  }
): Promise<void> {
  const res = await fetchWithTimeout(
    `${cfg.serverUrl}/agent/authoring/${sessionId}/finalize`,
    {
      method: "POST",
      headers: { ...authHeaders(cfg.deviceToken), "Content-Type": "application/json" },
      body: JSON.stringify(body),
    },
    HEAL_FIX_REQUEST_TIMEOUT_MS
  );
  await throwIfNotOk(res);
}
