/**
 * The job loop: claim a queued execution, run it locally with Playwright,
 * and push progress/results/evidence back to the server. Faithful port of
 * `api/app/services/playwright_runner.py`'s `run_execution`, with DB writes
 * and `hub.publish` WS events replaced by the `/agent/jobs/*` HTTP calls
 * (`api.ts`).
 */

import { ChildProcess, spawn } from "child_process";
import * as fs from "fs";
import * as net from "net";
import * as os from "os";
import * as path from "path";
import * as readline from "readline";
import * as api from "./api";
import { buildPassArgs, findTranscript, pauseWaitVerdict, planPass, sessionIdFrom } from "./authoringResume";
import { emit } from "./bus";
import { AgentConfig } from "./config";
import { ensureChromium } from "./ensureBrowser";
import { agentNodeModules, childNodeEnv, claudeCli, nodeBin, playwrightCli, vendorAuthoringScript, vendorCaptureScript, vendorExploreScript, vendorLiveReporter } from "./paths";
import { ensureBrowserHarness } from "./ensureTooling";
import { applyFixtures, writeConfig } from "./playwrightConfig";
import { installBaseFramework, materializeProject, writeProjectFile } from "./projectBundle";
import { ParsedAttachment, ParsedResult, normalizeStatus, parsePlaywrightReport, parseSpecIdentity } from "./report";
import { hasSessionStorage, hasValidSession, sessionPathsForOrigin } from "./session";
import { agentVersion } from "./version";

// Mirrors api/app/config.py's Settings.exec_timeout_s / auth_capture_timeout_s.
const EXEC_TIMEOUT_MS = 600_000;
const AUTH_CAPTURE_TIMEOUT_MS = 300_000;
const IDLE_POLL_MS = 1_000;

let activeChild: ChildProcess | null = null;

/** Kill whatever child process (capture browser or Playwright run) is active. Used by the CLI's SIGINT handler. */
export function killActiveChild(): void {
  if (activeChild && !activeChild.killed) {
    try {
      activeChild.kill();
    } catch {
      // Already gone.
    }
  }
}

function nodePathEnv(nm: string): NodeJS.ProcessEnv {
  const existing = process.env.NODE_PATH;
  return { ...process.env, ...childNodeEnv(), NODE_PATH: existing ? `${nm}${path.delimiter}${existing}` : nm };
}

interface ProcResult {
  code: number | null;
  stdout: string;
  stderr: string;
  timedOut: boolean;
}

function runProcess(
  cmd: string,
  args: string[],
  cwd: string,
  env: NodeJS.ProcessEnv,
  timeoutMs: number,
  onLine?: (line: string) => void,
): Promise<ProcResult> {
  return new Promise((resolve) => {
    // No shell: cmd is always an absolute node path (nodeBin()) and args are
    // absolute script/CLI paths, so this is safe even when paths contain spaces.
    const child = spawn(cmd, args, { cwd, env, windowsHide: true });
    activeChild = child;
    let stdout = "";
    let stderr = "";
    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      try {
        child.kill();
      } catch {
        // Already gone.
      }
    }, timeoutMs);
    // Read stdout line-by-line so a per-line callback (the live test reporter)
    // can forward results mid-run; still accumulate the full text for diagnostics.
    if (child.stdout) {
      const rl = readline.createInterface({ input: child.stdout });
      rl.on("line", (line) => {
        stdout += line + "\n";
        if (onLine) {
          try { onLine(line); } catch { /* a forwarder must never break the run */ }
        }
      });
    }
    child.stderr?.on("data", (d) => {
      stderr += d.toString();
    });
    const finish = (code: number | null) => {
      clearTimeout(timer);
      if (activeChild === child) activeChild = null;
      resolve({ code, stdout, stderr, timedOut });
    };
    child.on("close", finish);
    child.on("error", () => finish(null));
  });
}

/**
 * Open a HEADED browser for manual login (port of
 * `playwright_runner.py`'s `_capture_once`, vendored verbatim as
 * `vendor/capture_auth.cjs`). Returns true only once `storageStatePath` is
 * non-empty after the browser closes.
 */
async function captureAuth(baseUrl: string, storageStatePath: string, sessionStoragePath: string): Promise<boolean> {
  fs.mkdirSync(path.dirname(storageStatePath), { recursive: true });
  const script = vendorCaptureScript();
  const nm = agentNodeModules();
  const result = await runProcess(
    nodeBin(),
    [script, baseUrl, storageStatePath, sessionStoragePath],
    nm,
    nodePathEnv(nm),
    AUTH_CAPTURE_TIMEOUT_MS
  );
  if (result.code !== 0) {
    console.error(`Auth capture exited ${result.code}: ${(result.stderr || result.stdout || "").slice(0, 800)}`);
  }
  try {
    return fs.statSync(storageStatePath).size > 0;
  } catch {
    return false;
  }
}

async function runPlaywright(
  workDir: string,
  workers: number,
  specFile: string,
  onLine?: (line: string) => void,
): Promise<ProcResult> {
  // Invoke Playwright's CLI through the resolved node runtime (`node cli.js test …`)
  // so the same path works from source, via npx, and in a packaged bundle where
  // `node`/`node_modules` live beside the executable rather than on PATH.
  const args = [playwrightCli(), "test", `--workers=${workers}`];
  if (specFile) args.push(specFile);
  const nm = agentNodeModules();
  return runProcess(nodeBin(), args, workDir, nodePathEnv(nm), EXEC_TIMEOUT_MS, onLine);
}

/**
 * Best identity `{ticket, caseCode}` for wire events: prefers explicit
 * fields on the job spec (forward-compatible with a server patch that adds
 * them), falling back to parsing the filename convention otherwise. See
 * README "Known limitation" — the fallback only recovers the ticket's
 * short numeric suffix, not its full provider-prefixed external id.
 */
function identityFor(spec: api.JobSpec): { ticket: string; caseCode: string } {
  if (spec.ticketExternalId && spec.caseCode) {
    return { ticket: spec.ticketExternalId, caseCode: spec.caseCode };
  }
  // A layered spec's `filename` is project-relative (`tests/SUR-1428/…`), so parse
  // the basename — the convention only ever described the file's own name.
  const parsed = parseSpecIdentity(path.basename(spec.filename));
  return { ticket: spec.ticketExternalId || parsed.shortTicket, caseCode: spec.caseCode || parsed.caseCode };
}

/**
 * Stage a job's whole tree into `workDir`: the shipped automation project (#541),
 * then this job's spec sources at their (possibly nested) paths, then the base
 * framework in `node_modules/`.
 *
 * Order matters. The project ships without `tests/**` (other runs' specs are
 * irrelevant and would be re-executed), so the specs are written after it — and
 * their `filename` is project-relative for a layered run, which is why both
 * writes go through the path-confined `writeProjectFile`.
 *
 * @returns An error message when the job cannot be staged (an over-cap bundle),
 *   or `""` on success. Logs the bundle size on this side to match the server's
 *   log line.
 */
function stageJobTree(workDir: string, job: api.Job, specs: { filename: string; code: string }[]): string {
  if (job.project) {
    const staged = materializeProject(workDir, job.project);
    if (staged.overCap) {
      return (
        `Automation project bundle is too large to stage (${staged.bytes} bytes) — ` +
        `remove large or generated files from the project.`
      );
    }
    console.log(
      `Staged automation project (bundle v${job.project.baseVersion || "?"}): ` +
        `${staged.files} file(s), ${staged.bytes} bytes`
    );
    if (staged.skipped.length) {
      console.error(`Refused ${staged.skipped.length} unsafe bundle path(s): ${staged.skipped.join(", ")}`);
    }
    const base = installBaseFramework(workDir);
    if (base === "unavailable") {
      console.error(
        "@q-agent/playwright-base is not bundled with this agent build — specs importing it will " +
          "fail to resolve. Update the Local Agent."
      );
    }
  }
  for (const spec of specs) {
    if (!writeProjectFile(workDir, spec.filename, spec.code)) {
      return `Refusing to write spec outside the job workdir: ${spec.filename}`;
    }
  }
  return "";
}

/** Mark every spec in the job failed with `message` and finalize — used when a run cannot proceed (e.g. manual login was not completed). */
async function failAllResults(cfg: AgentConfig, job: api.Job, message: string): Promise<void> {
  for (const spec of job.specs) {
    const { ticket, caseCode } = identityFor(spec);
    await api
      .postResult(cfg, job.executionId, { file: spec.filename, status: "fail", duration_ms: 0, error_message: message })
      .catch((err) => console.error("postResult failed:", err));
    await api
      .postEvent(cfg, job.executionId, "exec.case.result", { ticket, caseCode, status: "fail", durationMs: 0 })
      .catch((err) => console.error("postEvent failed:", err));
  }
  const total = job.specs.length;
  await api.postComplete(cfg, job.executionId, { passed: 0, failed: total, log: message }).catch((err) =>
    console.error("postComplete failed:", err)
  );
}

/** Resolved local auth for a job: paths to a saved session, or an `error` when a
 * required manual login could not be obtained (caller decides how to report it). */
interface ResolvedAuth {
  storageState: string;
  sessionStoragePath: string;
  error?: string;
}

/** Reuse a valid local session, or capture one headed — shared by the run + heal paths. */
async function resolveJobAuth(cfg: AgentConfig, job: api.Job): Promise<ResolvedAuth> {
  if (!job.manualAuth) return { storageState: "", sessionStoragePath: "" };
  let origin = job.authOrigins[0] || "";
  if (!origin && job.baseUrl) {
    try {
      origin = new URL(job.baseUrl).origin;
    } catch {
      origin = "";
    }
  }
  if (origin && hasValidSession(origin)) {
    const paths = sessionPathsForOrigin(origin);
    return { storageState: paths.storageStatePath, sessionStoragePath: paths.sessionStoragePath };
  }
  if (origin && job.baseUrl) {
    const paths = sessionPathsForOrigin(origin);
    await api.postEvent(cfg, job.executionId, "exec.auth.waiting", { url: job.baseUrl });
    emit("auth-waiting", { url: job.baseUrl });
    const captured = await captureAuth(job.baseUrl, paths.storageStatePath, paths.sessionStoragePath);
    if (captured) {
      await api.postEvent(cfg, job.executionId, "exec.auth.captured", {});
      emit("auth-captured", {});
      return { storageState: paths.storageStatePath, sessionStoragePath: paths.sessionStoragePath };
    }
    const message = "Manual login was not completed — enable/redo login capture";
    await api.postEvent(cfg, job.executionId, "exec.auth.error", { message });
    emit("error", { message });
    return { storageState: "", sessionStoragePath: "", error: message };
  }
  const message = "Set a base URL for the project first.";
  await api.postEvent(cfg, job.executionId, "exec.auth.error", { message });
  emit("error", { message });
  return { storageState: "", sessionStoragePath: "", error: message };
}

/** Best-effort: read the distilled DOM JSON from a parsed attachment list. */
function loadDistilledDom(workDir: string, attachments: ParsedAttachment[]): unknown {
  const att = attachments.find((a) => a.kind === "dom-distilled");
  if (!att) return null;
  const p = path.isAbsolute(att.path) ? att.path : path.join(workDir, att.path);
  try {
    return JSON.parse(fs.readFileSync(p, "utf-8"));
  } catch {
    return null;
  }
}

/** Process one claimed job end-to-end: write specs/config, resolve auth, run Playwright, push results. */
export async function processJob(cfg: AgentConfig, job: api.Job): Promise<void> {
  if (job.heal) {
    await processHealJob(cfg, job, job.heal);
    return;
  }
  const workDir = fs.mkdtempSync(path.join(os.tmpdir(), "qagent-"));
  // Set once the (detached) evidence uploader takes ownership of workDir — it
  // then removes the dir when its uploads finish. Until then, this function's
  // finally cleans up (error / early-return paths).
  let handedOff = false;
  try {
    const stageError = stageJobTree(workDir, job, job.specs);
    if (stageError) {
      await failAllResults(cfg, job, stageError);
      return;
    }

    const auth = await resolveJobAuth(cfg, job);
    if (auth.error) {
      await failAllResults(cfg, job, auth.error);
      return;
    }
    const { storageState, sessionStoragePath } = auth;

    writeConfig(workDir, job.workers, job.headless, job.baseUrl, storageState, {
      liveReporterPath: vendorLiveReporter(),
      captureVideo: Boolean(job.captureVideo),
    });

    // Always inject the generated fixtures.ts (DOM capture on every run) + rewrite
    // spec imports to it. sessionStorage replay (MSAL/SPA tokens) is additionally
    // enabled only when a manual-auth session actually exists.
    const replaySession = Boolean(job.manualAuth && storageState && sessionStoragePath && fs.statSync(sessionStoragePath).size > 0);
    applyFixtures(
      workDir,
      sessionStoragePath || path.join(workDir, "sessionStorage.json"),
      replaySession
    );

    const total = job.specs.length;
    // Keyed by BASENAME: a layered spec's `filename` is project-relative, and
    // Playwright's report/live reporter emit their own (config-relative) paths, so
    // the basename is the only stable join key.
    const specByFile = new Map(job.specs.map((s) => [path.basename(s.filename), s]));
    for (let i = 0; i < job.specs.length; i++) {
      const { ticket, caseCode } = identityFor(job.specs[i]);
      await api.postEvent(cfg, job.executionId, "exec.case.running", { ticket, caseCode, index: i + 1, total });
      emit("case-running", { ticket, caseCode, index: i + 1, total });
    }

    // A single-spec job targets just that one file (the "run this test"
    // action); a multi-case job executes the whole suite — same distinction
    // as the server's `single_spec`.
    const singleSpec = job.specs.length === 1 ? job.specs[0].filename : "";

    // Post one case's result + progress. The `handled` guard makes it idempotent
    // per spec, so a result streamed live by the reporter is NOT re-posted by the
    // end-of-run reconcile pass. Counts are reserved synchronously (before the
    // awaits), so they're correct by the time the run ends.
    let passed = 0;
    let failed = 0;
    const handled = new Set<string>();
    const postCaseResult = async (
      spec: api.JobSpec,
      status: "pass" | "fail" | "skipped",
      durationMs: number,
      error: string,
    ): Promise<void> => {
      if (handled.has(spec.filename)) return;
      handled.add(spec.filename);
      if (status === "pass") passed++;
      else if (status === "fail") failed++;
      const { ticket, caseCode } = identityFor(spec);
      const progress = total ? Math.trunc((100 * handled.size) / total) : 100;
      await api
        .postResult(cfg, job.executionId, { file: spec.filename, status, duration_ms: durationMs, error_message: error })
        .catch(() => {});
      await api
        .postEvent(cfg, job.executionId, "exec.case.result", { ticket, caseCode, status, durationMs })
        .catch(() => {});
      await api
        .postEvent(cfg, job.executionId, "exec.progress", { progress, passed, failed, remaining: total - handled.size })
        .catch(() => {});
      emit("case-result", { ticket, caseCode, status, durationMs });
      emit("progress", { progress, passed, failed, remaining: total - handled.size });
    };

    // Live streaming (#exec-live): the vendored reporter prints one
    // `QAGENT_TEST {json}` line per finished test, which runProcess feeds here so
    // each spec's status reaches the UI the moment Playwright finishes it — rather
    // than in a burst after the whole run + report.json parse.
    const LIVE_PREFIX = "QAGENT_TEST ";
    const onLine = (line: string): void => {
      if (!line.startsWith(LIVE_PREFIX)) return;
      let r: { file?: string; status?: string; durationMs?: number; error?: string };
      try {
        r = JSON.parse(line.slice(LIVE_PREFIX.length));
      } catch {
        return;
      }
      const spec = specByFile.get(path.basename(String(r.file || "")));
      if (!spec) return;
      void postCaseResult(spec, normalizeStatus(String(r.status || "")), Math.trunc(Number(r.durationMs) || 0), r.error || "");
    };

    const started = Date.now();
    const { stdout, stderr, timedOut } = await runPlaywright(workDir, job.workers, singleSpec, onLine);
    const elapsedMs = Date.now() - started;
    const procOutput = [stdout, stderr].filter(Boolean).join("\n").trim();
    let runError = "";
    if (timedOut) runError = `Playwright run timed out after ${EXEC_TIMEOUT_MS / 1000}s`;

    const reportPath = path.join(workDir, "report.json");
    let parsed: ParsedResult[] = [];
    if (!runError) {
      if (fs.existsSync(reportPath)) {
        try {
          parsed = parsePlaywrightReport(JSON.parse(fs.readFileSync(reportPath, "utf-8")));
        } catch (exc) {
          runError = `Could not parse Playwright report: ${(exc as Error).message}`;
        }
      } else {
        const detail = procOutput ? ` — ${procOutput.slice(0, 600)}` : "";
        runError = `Playwright produced no report.json${detail}`;
      }
    }

    // End-of-run reconcile: gather evidence for every reported spec (from
    // report.json), and post any case the live reporter didn't already stream (a
    // missed test, or a run-error where nothing streamed). Already-streamed specs
    // are skipped by postCaseResult's guard — only their evidence is collected.
    // Evidence uploads are deferred until AFTER results + complete are posted so
    // the web marks the run done immediately rather than waiting on (multi-MB)
    // video/trace/DOM uploads.
    const pendingEvidence: { ticket: string; caseCode: string; kind: string; filePath: string }[] = [];
    for (const spec of job.specs) {
      const entry = parsed.find((e) => path.basename(e.file) === path.basename(spec.filename));
      const { ticket, caseCode } = identityFor(spec);
      if (entry) {
        for (const att of entry.attachments) {
          const filePath = path.isAbsolute(att.path) ? att.path : path.join(workDir, att.path);
          if (fs.existsSync(filePath)) pendingEvidence.push({ ticket, caseCode, kind: att.kind, filePath });
        }
        await postCaseResult(spec, entry.status, entry.duration_ms || elapsedMs, entry.error_message);
      } else {
        // No report entry: if it wasn't streamed either, it's a failure (run error
        // / crash). The guard no-ops when the reporter already streamed it.
        await postCaseResult(spec, "fail", elapsedMs, runError || "No result reported by Playwright");
      }
    }

    // Keep the LAST ~20000 chars so the failing tail survives truncation,
    // matching the server's own log persistence.
    const logText = (procOutput || runError || "").slice(-20000);
    await api.postComplete(cfg, job.executionId, { passed, failed, log: logText });
    emit("job-complete", { executionId: job.executionId, passed, failed });

    // Now that the run is marked done, upload the (deferred) evidence artifacts.
    // These run DETACHED from the claim loop: the run's outcome is already
    // reported, so the agent must be free to claim the next run immediately
    // rather than blocking here until every (potentially multi-MB) artifact
    // finishes uploading — that block is what stalled the agent when a new run
    // was started mid-upload. The uploader owns workDir cleanup from here.
    handedOff = true;
    void uploadEvidenceThenCleanup(cfg, job.executionId, pendingEvidence, workDir);
  } finally {
    if (!handedOff) fs.rmSync(workDir, { recursive: true, force: true });
  }
}

/**
 * Upload a completed job's deferred evidence artifacts, then remove its workDir.
 * Runs detached from the claim loop (see {@link processJob}) so uploads never
 * delay claiming the next run. Never throws: each upload's failure is logged and
 * the workDir is always removed, even if some uploads fail or time out.
 */
async function uploadEvidenceThenCleanup(
  cfg: AgentConfig,
  executionId: number,
  pendingEvidence: { ticket: string; caseCode: string; kind: string; filePath: string }[],
  workDir: string
): Promise<void> {
  // Tell the UI evidence is still incoming so it can show a loader instead of an
  // empty panel while these deferred uploads land (results are already visible).
  if (pendingEvidence.length > 0) {
    await api
      .postEvent(cfg, executionId, "exec.evidence.uploading", { count: pendingEvidence.length })
      .catch(() => {});
  }
  try {
    for (const ev of pendingEvidence) {
      await api
        .postEvidence(cfg, executionId, {
          ticketExternalId: ev.ticket,
          caseCode: ev.caseCode,
          kind: ev.kind,
          filePath: ev.filePath,
          filename: path.basename(ev.filePath),
        })
        .catch((err) => console.error("postEvidence failed:", err));
    }
  } finally {
    fs.rmSync(workDir, { recursive: true, force: true });
    // Signal completion so the UI drops the loader and refetches the now-uploaded
    // evidence — always fire (even on partial failure) so the loader never sticks.
    if (pendingEvidence.length > 0) {
      await api
        .postEvent(cfg, executionId, "exec.evidence.done", {})
        .catch(() => {});
    }
  }
}

/**
 * Process a standalone manual-login capture: open a headed browser at the
 * project's base URL ON THIS MACHINE, let the operator log in, and save the
 * session locally (keyed by origin) so subsequent runs reuse it. The session
 * never leaves this machine — only the pass/fail outcome is reported back.
 */
async function processCapture(cfg: AgentConfig, capture: api.CaptureJob): Promise<void> {
  let origin = capture.origin;
  if (!origin && capture.baseUrl) {
    try {
      origin = new URL(capture.baseUrl).origin;
    } catch {
      origin = "";
    }
  }
  if (!origin || !capture.baseUrl) {
    await api.postCaptureComplete(cfg, capture.captureId, false, "Missing base URL / origin").catch(() => {});
    return;
  }
  const paths = sessionPathsForOrigin(origin);
  // Force a fresh login: remove any prior session so captureAuth's
  // "storageState is non-empty" success check can't pass off a stale file
  // (which would falsely report success without opening a browser).
  try {
    fs.rmSync(paths.storageStatePath, { force: true });
    fs.rmSync(paths.sessionStoragePath, { force: true });
  } catch {
    // best-effort
  }
  emit("auth-waiting", { url: capture.baseUrl });
  console.log(`Capturing login for ${capture.projectKey} at ${capture.baseUrl}`);
  let ok = false;
  try {
    ok = await captureAuth(capture.baseUrl, paths.storageStatePath, paths.sessionStoragePath);
  } catch (err) {
    console.error("Capture crashed:", (err as Error).message);
  }
  if (ok) emit("auth-captured", {});
  else emit("error", { message: "Login was not captured — the window closed before a session was saved." });
  await api
    .postCaptureComplete(cfg, capture.captureId, ok, ok ? undefined : "Login was not captured")
    .catch((err) => console.error("postCaptureComplete failed:", err));
}

/**
 * Run the self-heal LOOP for one case locally (#260): run the spec + capture DOM,
 * and while it fails ask the server to classify + fix it (Claude + KB live
 * server-side), apply the returned code, and re-run — up to `maxAttempts`. Streams
 * `heal.progress`, uploads the final attempt's result + evidence, then posts the
 * outcome to `/agent/heal/{caseId}/finalize`.
 */
async function processHealJob(cfg: AgentConfig, job: api.Job, heal: NonNullable<api.Job["heal"]>): Promise<void> {
  const spec = job.specs[0];
  const { ticket, caseCode } = identityFor(spec);
  const workDir = fs.mkdtempSync(path.join(os.tmpdir(), "qagent-heal-"));
  const progress = (phase: string, attempt: number, message: string, error = ""): Promise<void> =>
    api
      .postEvent(cfg, job.executionId, "heal.progress", {
        caseId: heal.caseId, ticket, caseCode, attempt, maxAttempts: heal.maxAttempts,
        phase, message, error: (error || "").slice(0, 600),
      })
      .catch(() => {});

  let currentCode = spec.code;
  let finalStatus: "pass" | "fail" | "blocked" | "product_defect" = "fail";
  let finalError = "";
  let elapsedMs = 0;
  let lastDom: unknown = null;
  let lastAttachments: ParsedAttachment[] = [];
  let lastFixBefore: string | null = null;
  let lastFixAfter: string | null = null;
  let blockReason = "";
  let gateReport = "";
  const attempts: Array<Record<string, unknown>> = [];

  try {
    const auth = await resolveJobAuth(cfg, job);
    if (auth.error) {
      finalError = auth.error;
    } else {
      const { storageState, sessionStoragePath } = auth;
      for (let attempt = 1; attempt <= heal.maxAttempts; attempt++) {
        // Re-stage the whole tree each attempt: the project bundle is idempotent,
        // and the spec is rewritten with the latest fix at its (possibly nested)
        // project-relative path.
        const stageError = stageJobTree(workDir, job, [{ filename: spec.filename, code: currentCode }]);
        if (stageError) {
          finalError = stageError;
          await progress("failed", attempt, stageError, stageError);
          break;
        }
        // Heal re-runs fail fast (shorter timeouts) and skip heavy trace/video +
        // raw-DOM capture except on the final attempt (#398).
        const isFinalAttempt = attempt === heal.maxAttempts;
        writeConfig(workDir, 1, job.headless, job.baseUrl, storageState, {
          testTimeoutMs: heal.testTimeoutMs,
          actionTimeoutMs: heal.actionTimeoutMs,
          heavyEvidence: isFinalAttempt,
          captureVideo: Boolean(job.captureVideo),
        });
        const replaySession = Boolean(
          job.manualAuth && storageState && sessionStoragePath && fs.statSync(sessionStoragePath).size > 0
        );
        applyFixtures(
          workDir,
          sessionStoragePath || path.join(workDir, "sessionStorage.json"),
          replaySession,
          isFinalAttempt,
        );

        await progress("running", attempt, `Running spec (attempt ${attempt}/${heal.maxAttempts})`);
        const started = Date.now();
        const { stdout, stderr, timedOut } = await runPlaywright(workDir, 1, spec.filename);
        elapsedMs = Date.now() - started;
        const output = [stdout, stderr].filter(Boolean).join("\n").trim();

        let entry: ParsedResult | undefined;
        const reportPath = path.join(workDir, "report.json");
        if (!timedOut && fs.existsSync(reportPath)) {
          try {
            const parsed = parsePlaywrightReport(JSON.parse(fs.readFileSync(reportPath, "utf-8")));
            entry = parsed.find((e) => path.basename(e.file) === path.basename(spec.filename));
          } catch {
            // fall through to the no-report error below
          }
        }
        const rec: Record<string, unknown> = { attempt };
        if (entry) {
          lastAttachments = entry.attachments;
          lastDom = loadDistilledDom(workDir, entry.attachments);
        }

        if (entry && entry.status === "pass") {
          finalStatus = "pass";
          finalError = "";
          rec.status = "pass";
          attempts.push(rec);
          await progress("passed", attempt, "Spec passed");
          break;
        }

        finalStatus = "fail";
        finalError = entry
          ? entry.error_message || output || "Test failed"
          : timedOut
            ? "Playwright run timed out"
            : output || "No result reported by Playwright";
        rec.status = "fail";
        rec.error = finalError;

        if (attempt >= heal.maxAttempts) {
          attempts.push(rec);
          await progress("failed", attempt, "Still failing after max attempts", finalError);
          break;
        }

        await progress("fixing", attempt, "Asking the server to fix the spec", finalError);
        let fix: api.HealFixResult;
        try {
          fix = await api.postHealFix(cfg, heal.caseId, {
            currentCode, error: finalError, output, domDistilled: lastDom, attempt,
          });
        } catch (err) {
          finalError = `Heal fix request failed: ${(err as Error).message}`;
          rec.error = finalError;
          attempts.push(rec);
          await progress("failed", attempt, finalError, finalError);
          break;
        }

        rec.action = fix.action;
        if (fix.action === "product_defect") {
          finalStatus = "product_defect";
          rec.failureClass = fix.failureClass;
          rec.reason = fix.reason;
          attempts.push(rec);
          await progress("product_defect", attempt, "Product defect suspected — routing to report", finalError);
          break;
        }
        if (fix.action === "blocked") {
          finalStatus = "blocked";
          blockReason = fix.reason || "";
          gateReport = fix.gate || "";
          rec.gate = "blocked";
          rec.error = fix.reason;
          attempts.push(rec);
          await progress("failed", attempt, "Fix blocked by placeholder gate", fix.reason || "");
          break;
        }
        if (fix.action === "rejected") {
          finalStatus = "fail";
          rec.gate = "rejected";
          rec.error = fix.reason;
          attempts.push(rec);
          await progress("failed", attempt, "Fix rejected", fix.reason || "");
          break;
        }
        // action === "fixed": apply it and re-run.
        // The repair may be in the shared LIBRARY rather than in the spec (#547) —
        // a stale locator inside an imported page object, fixed where it lives so
        // the architecture stays layered. Merge the returned sources into the job's
        // bundle: `stageJobTree` re-materializes it at the top of every attempt, so
        // this is all it takes for the next re-run to exercise the repair. `code`
        // then comes back unchanged, and the spec is deliberately left alone.
        if (fix.libraryFiles?.length && job.project) {
          const merged = new Map((job.project.files || []).map((f) => [f.path, f]));
          for (const file of fix.libraryFiles) merged.set(file.path, file);
          job.project.files = [...merged.values()];
          console.log(
            `Server repaired the shared library: ${fix.libraryFiles.map((f) => f.path).join(", ")}` +
              (fix.librarySummary ? ` — ${fix.librarySummary}` : "")
          );
          rec.libraryHealed = fix.libraryFiles.map((f) => f.path);
        }
        rec.fixed = true;
        rec.diff = fix.diff;
        attempts.push(rec);
        lastFixBefore = currentCode;
        lastFixAfter = fix.code || currentCode;
        currentCode = fix.code || currentCode;
      }
    }

    // Report the final attempt's result + evidence against the heal execution.
    const passResult = finalStatus === "pass";
    await api
      .postResult(cfg, job.executionId, {
        file: spec.filename, status: passResult ? "pass" : "fail",
        duration_ms: elapsedMs, error_message: passResult ? "" : finalError,
      })
      .catch((err) => console.error("postResult failed:", err));
    for (const att of lastAttachments) {
      const filePath = path.isAbsolute(att.path) ? att.path : path.join(workDir, att.path);
      if (!fs.existsSync(filePath)) continue;
      await api
        .postEvidence(cfg, job.executionId, {
          ticketExternalId: ticket, caseCode, kind: att.kind, filePath, filename: path.basename(filePath),
        })
        .catch((err) => console.error("postEvidence failed:", err));
    }
    await api
      .postEvent(cfg, job.executionId, "exec.case.result", {
        ticket, caseCode, status: passResult ? "pass" : "fail", durationMs: elapsedMs,
      })
      .catch(() => {});

    await api
      .postHealFinalize(cfg, heal.caseId, {
        finalStatus, finalError, finalCode: currentCode,
        blockReason, gateReport, domDistilled: lastDom,
        lastFixBefore, lastFixAfter, attempts,
      })
      .catch((err) => console.error("postHealFinalize failed:", err));

    await api
      .postComplete(cfg, job.executionId, { passed: passResult ? 1 : 0, failed: passResult ? 0 : 1, log: finalError })
      .catch((err) => console.error("postComplete failed:", err));
    emit("job-complete", { executionId: job.executionId, passed: passResult ? 1 : 0, failed: passResult ? 0 : 1 });
  } finally {
    fs.rmSync(workDir, { recursive: true, force: true });
  }
}

// ── DOM exploration (#338) ────────────────────────────────────────────────
// Mirror the agent-driven self-heal loop for exploration sessions: the browser
// + observe→decide→act loop run HERE (on the paired device); the server only
// does the Claude decide step (+ cost budget) and KB-merges the finalized
// discovery. Frozen wire contract: epic #336.

/** One observation returned by the vendored driver's `{cmd:"observe"}`. */
interface ExploreObservationResult {
  ok: boolean;
  url: string;
  path: string;
  a11y: unknown;
  elements: unknown;
  error?: string;
}

/** Result of the driver's `{cmd:"act"}`. */
interface ExploreActResult {
  ok: boolean;
  error: string | null;
  changed: boolean;
}

/** Minimal request/response interface over the persistent explore_session.cjs
 * subprocess. Injected into {@link runExplorationLoop} so the loop is unit-
 * testable with a fake driver (no real browser). */
interface ExploreDriver {
  observe(): Promise<ExploreObservationResult>;
  act(action: string, args: Record<string, unknown>): Promise<ExploreActResult>;
  close(): Promise<void>;
}

/** Actions that mutate app/server state — gated by `allowStateChanging`. `goto`
 * (navigation) and `expectVisible` (a pure probe) are always allowed. */
const STATE_CHANGING_ACTIONS = new Set(["click", "fill"]);

/** How long to wait for any single driver command before giving up on it (a
 * hung observe/act must not stall the whole loop; the loop tolerates the error
 * and finalizes). Generous because a `goto` can load a slow page. */
const EXPLORE_CMD_TIMEOUT_MS = 120_000;

/** The `{strategy,value}` of a locator, for the discovered-selectors log. */
function describeSelector(args: Record<string, unknown> | undefined): Record<string, unknown> | null {
  if (!args) return null;
  if (args.testId) return { strategy: "testId", value: args.testId };
  if (args.role) return { strategy: "role", value: args.role, name: args.name };
  if (args.selector) return { strategy: "selector", value: args.selector };
  return null;
}

/**
 * The pure exploration loop, decoupled from the subprocess/HTTP so it can be
 * unit-tested with fakes. Observes → asks the server to decide → acts, up to
 * `maxSteps`, stopping on `done`, on a server `stop` (e.g. budget), or on
 * repeat detection (a url+a11y signature seen before). A gated state-changing
 * action is SKIPPED (recorded, not executed). Never throws: any error is logged
 * and the session is finalized with whatever was accumulated.
 */
export async function runExplorationLoop(
  cfg: AgentConfig,
  session: api.ExplorationSession,
  driver: ExploreDriver,
  apiClient: Pick<typeof api, "postExploreDecide" | "postExploreEvent" | "postExploreFinalize">,
  emitFn: (type: string, data?: Record<string, unknown>) => void
): Promise<void> {
  const history: Array<Record<string, unknown>> = [];
  const log: Array<Record<string, unknown>> = [];
  const discovered: { routes: unknown[]; selectors: unknown[] } = { routes: [], selectors: [] };
  const seen = new Set<string>();
  let stopReason = "maxSteps";
  let stepsTaken = 0;

  try {
    for (let step = 0; step < session.maxSteps; step++) {
      const obs = await driver.observe();
      if (!obs || !obs.ok) {
        stopReason = "observe-error";
        log.push({ step, phase: "observe", error: (obs && obs.error) || "observe failed" });
        break;
      }

      // Repeat detection: identical url + a11y snapshot means we're looping.
      const signature = `${obs.url}::${JSON.stringify(obs.a11y)}`;
      if (seen.has(signature)) {
        stopReason = "repeat";
        log.push({ step, phase: "repeat", url: obs.url });
        break;
      }
      seen.add(signature);

      let decision: api.DecideResult;
      try {
        decision = await apiClient.postExploreDecide(cfg, session.sessionId, {
          observation: { accessibility: obs.a11y, elements: obs.elements, url: obs.url, path: obs.path },
          history,
          stepsTaken,
        });
      } catch (err) {
        stopReason = "decide-error";
        log.push({ step, phase: "decide", error: (err as Error).message });
        break;
      }

      history.push({ action: decision.action, args: decision.args, reasoning: decision.reasoning });

      if (decision.stop || decision.action === "done") {
        stopReason = decision.action === "done" ? "done" : decision.stopReason || "stop";
        log.push({ step, action: decision.action, reasoning: decision.reasoning, stop: true, stopReason });
        break;
      }

      const gated = STATE_CHANGING_ACTIONS.has(decision.action) && !session.allowStateChanging;
      const rec: Record<string, unknown> = {
        step,
        action: decision.action,
        args: decision.args,
        reasoning: decision.reasoning,
      };
      if (gated) {
        // Do NOT execute a state-changing action when it isn't allowed — record
        // the skip and move on so the server sees it was considered.
        rec.skipped = true;
        rec.skipReason = "state-changing action gated (allowStateChanging=false)";
        log.push(rec);
      } else {
        let result: ExploreActResult;
        try {
          result = await driver.act(decision.action, decision.args || {});
        } catch (err) {
          result = { ok: false, error: (err as Error).message, changed: false };
        }
        rec.result = result;
        log.push(rec);
        if (result.ok) {
          // Record the route/selector that actually worked (with its strategy).
          if (decision.action === "goto") {
            const route = (decision.args && decision.args.url) || obs.path || obs.url;
            if (route) discovered.routes.push(route);
          } else {
            const sel = describeSelector(decision.args);
            if (sel) discovered.selectors.push({ action: decision.action, ...sel });
          }
        }
      }

      stepsTaken++;

      const progress = {
        step: step + 1,
        action: decision.action,
        reasoning: decision.reasoning,
        skipped: gated,
        stepsTaken,
        maxSteps: session.maxSteps,
      };
      await apiClient.postExploreEvent(cfg, session.sessionId, "explore.progress", progress).catch(() => {});
      emitFn("explore-progress", progress);
    }
  } catch (err) {
    // Belt-and-braces: the loop already guards each awaited step, but never let
    // an unexpected throw escape — finalize with what we have.
    stopReason = "error";
    log.push({ phase: "loop", error: (err as Error).message });
  } finally {
    try {
      await driver.close();
    } catch {
      // Driver already gone.
    }
    await apiClient
      .postExploreFinalize(cfg, session.sessionId, { discovered, log, stopReason, stepsTaken })
      .catch((err) => console.error("postExploreFinalize failed:", err));
    emitFn("explore-complete", { sessionId: session.sessionId, stopReason, stepsTaken });
  }
}

/**
 * Wrap the persistent explore_session.cjs subprocess in an {@link ExploreDriver}:
 * a serialized newline-delimited-JSON request/response channel. The driver emits
 * one unsolicited `{ok,ready}` line at startup (consumed by {@link ExploreDriver.observe}'s
 * first caller via `ready()`); thereafter it is strictly one response line per
 * command. Any command that outlives {@link EXPLORE_CMD_TIMEOUT_MS} or a dead
 * process resolves with an error object rather than hanging.
 */
function makeExploreDriver(child: ChildProcess): ExploreDriver & { ready(): Promise<ExploreObservationResult> } {
  const rl = readline.createInterface({ input: child.stdout! });
  const pending: Array<(v: Record<string, unknown>) => void> = [];
  let gotReady = false;
  let readyResolve: (v: Record<string, unknown>) => void;
  const readyPromise = new Promise<Record<string, unknown>>((r) => {
    readyResolve = r;
  });

  rl.on("line", (line) => {
    if (!line.trim()) return;
    let obj: Record<string, unknown>;
    try {
      obj = JSON.parse(line);
    } catch {
      return; // ignore non-JSON diagnostic lines
    }
    if (!gotReady) {
      gotReady = true;
      readyResolve(obj);
      return;
    }
    const resolve = pending.shift();
    if (resolve) resolve(obj);
  });

  const failAll = () => {
    if (!gotReady) {
      gotReady = true;
      readyResolve({ ok: false, error: "driver exited before ready" });
    }
    while (pending.length) {
      const resolve = pending.shift();
      if (resolve) resolve({ ok: false, error: "driver process closed" });
    }
  };
  child.on("close", failAll);
  child.on("error", failAll);

  const request = (cmd: Record<string, unknown>): Promise<Record<string, unknown>> =>
    new Promise((resolve) => {
      const timer = setTimeout(() => {
        const idx = pending.indexOf(wrapped);
        if (idx >= 0) pending.splice(idx, 1);
        resolve({ ok: false, error: "driver command timed out" });
      }, EXPLORE_CMD_TIMEOUT_MS);
      const wrapped = (v: Record<string, unknown>) => {
        clearTimeout(timer);
        resolve(v);
      };
      pending.push(wrapped);
      try {
        child.stdin!.write(JSON.stringify(cmd) + "\n");
      } catch {
        // stdin closed — the close/error handler will resolve pending.
      }
    });

  return {
    ready: () => readyPromise as unknown as Promise<ExploreObservationResult>,
    observe: () => request({ cmd: "observe" }) as unknown as Promise<ExploreObservationResult>,
    act: (action, args) => request({ cmd: "act", action, args }) as unknown as Promise<ExploreActResult>,
    close: async () => {
      try {
        child.stdin!.write(JSON.stringify({ cmd: "close" }) + "\n");
      } catch {
        // already gone
      }
    },
  };
}

/**
 * Process one claimed exploration session end-to-end (#338): reuse a saved
 * local session for the origin (auth never leaves this machine), spawn the
 * vendored persistent Playwright driver, run the observe→decide→act loop, then
 * finalize. The driver subprocess is ALWAYS killed in `finally`.
 */
export async function processExplorationJob(cfg: AgentConfig, session: api.ExplorationSession): Promise<void> {
  // Reuse a captured session for the origin if one exists (exploration never
  // prompts for a headed login — it explores with whatever auth is available).
  let origin = session.origin;
  if (!origin && session.baseUrl) {
    try {
      origin = new URL(session.baseUrl).origin;
    } catch {
      origin = "";
    }
  }
  const storageState = origin && hasValidSession(origin) ? sessionPathsForOrigin(origin).storageStatePath : "";
  // Pair the sessionStorage snapshot (MSAL/SPA auth tokens) with the saved
  // session so the explore browser authenticates the same way a run does — the
  // run path replays it via fixtures (see `replaySession`). Without it an
  // MSAL/SPA app boots unauthenticated and bounces to login.
  const sessionStoragePath =
    storageState && origin && hasSessionStorage(origin) ? sessionPathsForOrigin(origin).sessionStoragePath : "";

  const script = vendorExploreScript();
  const nm = agentNodeModules();
  const args = [script, session.baseUrl];
  if (storageState) args.push(storageState);
  if (storageState && sessionStoragePath) args.push(sessionStoragePath);
  // Run the explore browser HEADED on the paired device: the user watches the
  // session, and a headed browser dodges WAF/bot-protection that blocks headless.
  // windowsHide (#421): CREATE_NO_WINDOW is INHERITED by the whole descendant
  // tree, so this also keeps any console-subsystem grandchild the driver reaches
  // for from flashing a window on the paired device. Stdio stays piped — the
  // flag suppresses the console, never the pipes.
  const child = spawn(nodeBin(), args, {
    cwd: nm,
    env: { ...nodePathEnv(nm), QAGENT_EXPLORE_HEADED: "1" },
    windowsHide: true,
  });
  activeChild = child;
  // Capture the driver's stderr so a launch failure (missing browser, bad
  // Playwright resolution, …) is visible instead of a silent "complete".
  let driverStderr = "";
  child.stderr?.on("data", (d) => {
    driverStderr += String(d);
  });
  const driver = makeExploreDriver(child);

  try {
    // Wait for the driver's readiness signal before the first observe. If the
    // driver died before signalling ready, finalize with a clear driver-error
    // (carrying its stderr) rather than letting the loop break silently.
    const ready = await driver.ready();
    if (!ready || ready.ok === false) {
      const error = (ready && (ready as { error?: string }).error) || "driver failed to start";
      const detail = driverStderr.trim();
      await api
        .postExploreFinalize(cfg, session.sessionId, {
          discovered: { routes: [], selectors: [] },
          log: [{ phase: "driver", error, stderr: detail || undefined }],
          stopReason: "driver-error",
          stepsTaken: 0,
        })
        .catch((err) => console.error("postExploreFinalize failed:", err));
      emit("explore-complete", { sessionId: session.sessionId, stopReason: "driver-error", error });
      console.error(`Exploration ${session.sessionId} driver-error: ${error}${detail ? ` — ${detail}` : ""}`);
      return;
    }
    await runExplorationLoop(cfg, session, driver, api, emit);
  } finally {
    try {
      child.kill();
    } catch {
      // Already gone.
    }
    if (activeChild === child) activeChild = null;
  }
}

/**
 * Long-poll loop: claim → process → repeat, backing off `IDLE_POLL_MS`
 * between empty claims. Runs until `signal.aborted`.
 */
// --- Live authoring (#403) — drive `claude` + browser-harness locally ----------

const AUTHORING_CDP_READY_TIMEOUT_MS = 30_000;

/** How often a parked (paused) session asks the server whether to continue (#619). */
const AUTHORING_RESUME_POLL_MS = 3_000;

/**
 * Device-side ceiling on a pause, as a backstop only.
 *
 * The server expires a pause (``PAUSE_EXPIRES_AFTER``) and answers the poll with
 * `abort`, which is the normal teardown path. This exists for the case where the
 * server is unreachable for the whole pause: without it an unreachable server
 * would leave a Chrome window and a temp dir on the user's machine forever, which
 * is precisely the leak #619 says must not happen. Deliberately LONGER than the
 * server window so the server's decision wins whenever it can be heard.
 */
const AUTHORING_PAUSE_HARD_CAP_MS = 75 * 60 * 1_000;

/** Human-readable trail line for a `abort` directive from the server. */
function resumeAbortMessage(reason: string): string {
  switch (reason) {
    case "pause-expired":
      return "Pause expired — closing the browser and wrapping up with whatever was authored.";
    case "budget-exhausted":
      return "The authoring budget for this session is spent — wrapping up.";
    case "resume-limit":
      return "This session has been resumed too many times — wrapping up.";
    case "session-gone":
      return "The run was stopped — closing the browser.";
    case "cancelled":
      return "Cancelled — closing the browser.";
    case "browser-closed":
      return "The browser was closed, so this session cannot continue — wrapping up with whatever was authored.";
    default:
      return `Cannot continue (${reason || "unknown"}) — closing the browser.`;
  }
}

/** Grab a free localhost TCP port for the dedicated Chrome's CDP endpoint. */
async function getFreePort(): Promise<number> {
  return new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.on("error", reject);
    srv.listen(0, "127.0.0.1", () => {
      const addr = srv.address();
      const port = typeof addr === "object" && addr ? addr.port : 0;
      srv.close(() => (port ? resolve(port) : reject(new Error("no free port"))));
    });
  });
}

/**
 * Author one spec live on this machine (#403): launch the dedicated,
 * pre-authenticated Chrome (vendored launcher), point the local `browser-harness`
 * at it via BU_CDP_URL, and run the local `claude` agentically (prompts composed
 * server-side) to perform the case and write `<specFilename>` + `discovered.json`
 * into a temp workspace. Posts the result back for the server to gate + persist.
 * Requires `claude` + `browser-harness` on the agent machine's PATH and a
 * pre-authenticated `browser-profile` for the origin (from manual-login capture).
 */
export async function processAuthoringJob(cfg: AgentConfig, job: api.AuthoringJob): Promise<void> {
  let origin = job.origin;
  if (!origin && job.baseUrl) {
    try { origin = new URL(job.baseUrl).origin; } catch { origin = ""; }
  }
  const sess = origin ? sessionPathsForOrigin(origin) : null;
  const profileDir = sess ? path.join(sess.dir, "browser-profile") : "";

  const finalize = async (
    code: string,
    discovered: unknown,
    summary: string,
    ok: boolean,
    costUsd = 0,
    refreshedCredentials = "",
  ) => {
    await api
      .postAuthoringFinalize(cfg, job.sessionId, {
        code,
        discovered: (discovered as Record<string, unknown>) || {},
        summary,
        ok,
        costUsd,
        refreshedCredentials,
      })
      .catch((err) => console.error("postAuthoringFinalize failed:", err));
  };

  if (!profileDir || !fs.existsSync(profileDir)) {
    await finalize(
      "",
      { routes: [], selectors: [] },
      "No authenticated browser profile for this origin — capture a manual login first.",
      false
    );
    return;
  }

  const workDir = fs.mkdtempSync(path.join(os.tmpdir(), "qagent-authoring-"));
  const port = await getFreePort();
  let launcher: ChildProcess | null = null;
  // A REF cell, not a plain `let`: the Claude child is now (re)assigned inside
  // `runPass`, a closure, and TypeScript's control-flow analysis would narrow a
  // plain `let` back to `null` in the `finally`, making the optional-chained
  // kill there a compile error against `never`. Behaviourally identical, and the indirection
  // is the point: across a pause there is a SEQUENCE of Claude children, while
  // the launcher (Chrome) and the workdir stay the same throughout.
  const claudeChild: { current: ChildProcess | null } = { current: null };
  try {
    await api
      .postAuthoringEvent(cfg, job.sessionId, "authoring.progress", {
        case: job.caseId, phase: "launching", message: "Starting authenticated browser",
      })
      .catch(() => {});

    // 1) Dedicated pre-auth Chrome (vendored launcher). Pass the saved
    //    sessionStorage path so the launcher replays MSAL/SPA tokens into the
    //    Chrome browser-harness attaches to; NODE_PATH lets the launcher resolve
    //    the bundled Playwright it uses for that replay. Wait for its READY line
    //    (emitted AFTER the replay is armed) so browser-harness never navigates
    //    before the session is restored.
    const nm = agentNodeModules();
    launcher = spawn(
      nodeBin(),
      [
        vendorAuthoringScript(),
        job.baseUrl,
        String(port),
        profileDir,
        sess ? sess.sessionStoragePath : "",
        // The captured storageState (cookies + localStorage) is the AUTHORITATIVE
        // auth material — the profile alone is mutable state a failed run poisons
        // (#638). Same file the run path feeds to playwright.config.ts.
        sess ? sess.storageStatePath : "",
      ],
      { env: nodePathEnv(nm), stdio: ["pipe", "pipe", "pipe"], windowsHide: true }
    );
    activeChild = launcher;
    let launcherErr = "";
    // Chrome going away is a first-class signal, not just cleanup: while paused it
    // means the user closed the window the pause existed to keep open (#645).
    let launcherExited = false;
    launcher.on("exit", () => { launcherExited = true; });
    launcher.stderr?.on("data", (d) => { launcherErr += String(d); });
    const ready = await new Promise<boolean>((resolve) => {
      const to = setTimeout(() => resolve(false), AUTHORING_CDP_READY_TIMEOUT_MS);
      launcher!.stdout?.on("data", (d) => {
        if (String(d).includes("AUTHORING_BROWSER_READY")) { clearTimeout(to); resolve(true); }
      });
      launcher!.on("exit", () => { clearTimeout(to); resolve(false); });
    });
    if (!ready) {
      await finalize(
        "", { routes: [], selectors: [] },
        `Authoring browser did not become ready on port ${port}. ${launcherErr.trim()}`.trim(), false
      );
      return;
    }

    // Ensure browser-harness is installed (first run provisions it via uv). Its
    // bin dir is prepended to the claude subprocess PATH so `browser-harness`
    // resolves for the Bash calls Claude makes.
    const bh = await ensureBrowserHarness();
    if (!bh.ok) {
      await finalize("", { routes: [], selectors: [] }, `browser-harness unavailable: ${bh.error || "unknown"}`, false);
      return;
    }

    await api
      .postAuthoringEvent(cfg, job.sessionId, "authoring.progress", {
        case: job.caseId, phase: "driving", message: "Driving the app live with browser-harness",
      })
      .catch(() => {});

    // 2) Local agentic Claude drives browser-harness (BU_CDP_URL → our Chrome),
    //    writing the spec + discovered.json into workDir. System prompt via a
    //    file (avoids a huge argv); task prompt via stdin. Invoked as
    //    `nodeBin() <bundled claude cli.js>` so no `claude` install/PATH shim is
    //    needed (and no Windows .cmd shell hack).
    const systemFile = path.join(workDir, "system-prompt.txt");
    fs.writeFileSync(systemFile, job.systemPrompt, "utf-8");
    // Use the app's saved Claude credential (shipped in the claim) so we don't
    // need a separate `claude login` on this machine. Write it locked-down into
    // the temp workspace (removed with workDir); empty ⇒ fall back to the agent's
    // own login (no CLAUDE_CONFIG_DIR override).
    let claudeConfigDir = "";
    if (job.claudeCredentials) {
      claudeConfigDir = path.join(workDir, ".claude-config");
      fs.mkdirSync(claudeConfigDir, { recursive: true });
      const credFile = path.join(claudeConfigDir, ".credentials.json");
      fs.writeFileSync(credFile, job.claudeCredentials, "utf-8");
      try { fs.chmodSync(credFile, 0o600); } catch { /* best-effort on Windows */ }
    }
    // Prompt goes as the -p ARGUMENT (not stdin — headless `claude -p` reads the
    // prompt from argv). stream-json + verbose lets us surface every step live.
    // argv is built by `buildPassArgs` because a RESUMED pass (#619) must run with
    // exactly the same tools/system prompt/--add-dir as the pass it continues, and
    // two copies of that list is how they drift apart.
    // Prepend browser-harness's bin dir to PATH (overwrite the same-case key so
    // Windows doesn't end up with both Path and PATH).
    const pathVar = Object.keys(process.env).find((k) => k.toLowerCase() === "path") || "PATH";
    const claudeEnv: NodeJS.ProcessEnv = {
      ...process.env,
      BU_CDP_URL: `http://127.0.0.1:${port}`,
      [pathVar]: `${bh.binDir}${path.delimiter}${process.env[pathVar] || ""}`,
    };
    if (claudeConfigDir) claudeEnv.CLAUDE_CONFIG_DIR = claudeConfigDir;
    // Surface each step to the agent console + the run WebSocket so the operator
    // can watch Claude drive browser-harness live. Each post also reports whether
    // the session is still alive (a 404 means the run was stopped server-side —
    // #420 — so abort instead of burning budget on a rejected result) AND whether
    // the user pressed Pause (#619). Pause rides that existing per-step post
    // rather than a second poller, so it lands within one step.
    let aborted = false;
    let pauseAsked = false;
    // AGENT LOG mirrors the web trail's Settings verbosity (#438): in "concise"
    // (default) skip the raw tool/Bash step lines (▷ …) so the local log shows
    // only Claude's readable narration. The server WS still gets every line — the
    // web filters its own stream — and abort-on-stop keys on those posts.
    const conciseLog = (job.logVerbosity ?? "concise") === "concise";
    const emitStep = (line: string): void => {
      const trimmed = line.length > 300 ? line.slice(0, 300) + "…" : line;
      console.log(`[authoring ${job.caseId}] ${trimmed}`);
      if (!(conciseLog && trimmed.trimStart().startsWith("▷"))) {
        emit("authoring-step", { caseId: job.caseId, line: trimmed });
      }
      void api
        .postAuthoringEventAlive(cfg, job.sessionId, "authoring.progress", {
          case: job.caseId, phase: "step", message: trimmed,
        })
        .then(({ alive, control }) => {
          if (!alive && !aborted) {
            aborted = true;
            console.log(`[authoring ${job.caseId}] session gone — run stopped; aborting Claude`);
            try { claudeChild.current?.kill(); } catch { /* already exited */ }
          } else if (control === "pause" && !pauseAsked && !aborted) {
            // Stop CLAUDE only. Chrome, the workdir and CLAUDE_CONFIG_DIR are all
            // owned by the enclosing try/finally, so parking here leaves the
            // browser open for the user to drive by hand and leaves the transcript
            // that `--resume` needs on disk. That is the whole feature.
            pauseAsked = true;
            console.log(`[authoring ${job.caseId}] pause requested — stopping Claude, keeping the browser`);
            try { claudeChild.current?.kill(); } catch { /* already exited */ }
          }
        });
    };

    // Session-wide state. `costUsd` is the SESSION total across every pass, which
    // is what the budget ceiling is measured against (#619) — a per-pass total
    // would let a pause/continue loop spend the ceiling over and over.
    let cerr = "";
    let finalResult = "";
    let costUsd = 0;
    let exitCode = -1;
    // Claude CLI's OWN session id, scraped off the stream-json envelope. Nothing
    // read this before #619, which is why resume was impossible: `job.sessionId`
    // is Q-Agent's queue id and `claude --resume` does not know it.
    let claudeSessionId = "";
    let fallbackNote = "";

    /** Run one Claude pass to completion (or until it is killed for a pause). */
    const runPass = async (args: string[]): Promise<void> => {
      pauseAsked = false;
      let passCost = 0;
      let buf = "";
      const handleEvent = (ev: {
        type?: string;
        message?: { content?: Array<Record<string, unknown>> };
        result?: unknown;
        total_cost_usd?: unknown;
      }): void => {
        const sid = sessionIdFrom(ev);
        if (sid) claudeSessionId = sid;
        if (ev.type === "assistant" && ev.message?.content) {
          for (const c of ev.message.content) {
            if (c.type === "text" && typeof c.text === "string" && c.text.trim()) {
              emitStep(`Claude: ${c.text.trim()}`);
            } else if (c.type === "tool_use") {
              const inp = (c.input as Record<string, unknown>) || {};
              const detail = inp.command ?? inp.file_path ?? inp.path ?? inp.pattern ?? JSON.stringify(inp).slice(0, 200);
              emitStep(`▷ ${String(c.name)}: ${String(detail).replace(/\s+/g, " ").trim()}`);
            }
          }
        } else if (ev.type === "result") {
          if (typeof ev.result === "string") finalResult = ev.result;
          if (typeof ev.total_cost_usd === "number") passCost = ev.total_cost_usd;
        }
      };
      // Spawn the native `claude` binary directly (it is not a JS entry).
      //
      // windowsHide is LOAD-BEARING for the grandchildren, not just for `claude`
      // itself (#421). Claude's Bash tool runs `browser-harness` (a console-
      // subsystem Python CLI) once per step, and those were the windows seen
      // flashing. Measured on Windows 11, from a console-less parent:
      //
      //   claude spawned WITHOUT windowsHide -> claude gets a VISIBLE console, and
      //     every grandchild ATTACHES TO THAT SAME console window (same HWND) and
      //     is visible. One flash per browser-harness call.
      //   claude spawned WITH windowsHide (CREATE_NO_WINDOW) -> claude gets no
      //     console (GetConsoleWindow() == 0), and a grandchild launched by a plain
      //     CreateProcess we do NOT control (bash -> browser-harness) also gets
      //     none. CREATE_NO_WINDOW is inherited down the whole subtree.
      //
      // So the flag propagates and no native shim is needed. Do NOT "fix" this by
      // routing through `conhost.exe --headless`: measured, it replaces the child's
      // stdout with terminal escape sequences, so the --output-format stream-json
      // events below never arrive and authoring fails silently (#615). Piped stdio
      // + windowsHide suppresses the console WITHOUT touching the pipes.
      const child = spawn(claudeCli(), args, {
        cwd: workDir,
        env: claudeEnv,
        stdio: ["ignore", "pipe", "pipe"],
        windowsHide: true,
      });
      claudeChild.current = child;
      activeChild = child;
      child.stdout?.on("data", (d) => {
        buf += String(d);
        let nl;
        while ((nl = buf.indexOf("\n")) >= 0) {
          const line = buf.slice(0, nl).trim();
          buf = buf.slice(nl + 1);
          if (!line) continue;
          try { handleEvent(JSON.parse(line)); } catch { /* ignore non-JSON noise */ }
        }
      });
      child.stderr?.on("data", (d) => { cerr += String(d); });
      exitCode = await new Promise<number>((resolve) => {
        child.on("close", (c) => resolve(c ?? 0));
        child.on("error", (e) => { cerr += `\nclaude spawn error: ${(e as Error).message}`; resolve(-1); });
      });
      // A pass killed for a pause may never emit its `result` envelope, so its cost
      // can be under-counted. That is why the server ALSO caps the number of
      // resumes: the budget subtraction alone cannot bound spend it never saw.
      costUsd += passCost;
    };

    /** Sit parked until the user continues, the pause expires, or the cap hits. */
    const waitForResume = async (): Promise<api.AuthoringResumeDirective> => {
      const deadline = Date.now() + AUTHORING_PAUSE_HARD_CAP_MS;
      for (;;) {
        const directive = await api.pollAuthoringResume(cfg, job.sessionId);
        if (directive.action !== "wait") return directive;
        // The pause is only worth holding while the browser it preserves is still
        // there (#645). `launcherExited` flips when Chrome goes away — including
        // when the USER closes the window, which is the whole point.
        const verdict = pauseWaitVerdict({
          browserGone: launcherExited,
          pastHardCap: Date.now() > deadline,
        });
        if (verdict) return { ...directive, action: "abort", reason: verdict.reason };
        await new Promise((r) => setTimeout(r, AUTHORING_RESUME_POLL_MS));
      }
    };

    await runPass(
      buildPassArgs({
        prompt: job.taskPrompt,
        model: job.model,
        systemPromptFile: systemFile,
        workDir,
        budgetUsd: job.maxBudgetUsd,
      })
    );

    // 2b) Pause / continue (#619). Each iteration: park (browser still up), wait
    //     for the user, then either resume the SAME Claude session or — when that
    //     is impossible — run a fresh pass carrying the accumulated guidance, and
    //     say which happened on the trail.
    while (pauseAsked && !aborted) {
      emitStep("⏸ Paused — the browser is still open. Drive it yourself, add guidance in the chat, then Continue.");
      await api
        .postAuthoringPaused(cfg, job.sessionId, { claudeSessionId, costUsd })
        .catch((err) => console.error("postAuthoringPaused failed:", err));
      const directive = await waitForResume();
      if (directive.action !== "resume") {
        emitStep(`⏹ ${resumeAbortMessage(directive.reason)}`);
        break;
      }
      const transcript = findTranscript(claudeConfigDir, claudeSessionId);
      const plan = planPass({
        claudeSessionId,
        transcriptPresent: Boolean(transcript),
        guidance: directive.guidance,
        guidanceHistory: directive.guidanceHistory,
        taskPrompt: job.taskPrompt,
        remainingBudgetUsd: directive.remainingBudgetUsd,
      });
      emitStep(plan.trailLine);
      if (plan.mode === "fresh") fallbackNote = plan.trailLine;
      // The temp workdir can be swept by the OS while we sit parked (that is one of
      // the two ways resume becomes impossible), so re-materialise what a pass
      // needs rather than spawning `claude` against a directory that vanished.
      try {
        fs.mkdirSync(workDir, { recursive: true });
        if (!fs.existsSync(systemFile)) fs.writeFileSync(systemFile, job.systemPrompt, "utf-8");
        // The credential lives in the same swept directory, and CLAUDE_CONFIG_DIR
        // still points at it — so without this the fallback pass would fail to
        // AUTHENTICATE, which looks nothing like "the transcript was gone" and
        // would be diagnosed as a broken resume rather than a swept temp dir.
        if (claudeConfigDir && job.claudeCredentials) {
          const credFile = path.join(claudeConfigDir, ".credentials.json");
          if (!fs.existsSync(credFile)) {
            fs.mkdirSync(claudeConfigDir, { recursive: true });
            fs.writeFileSync(credFile, job.claudeCredentials, "utf-8");
            try { fs.chmodSync(credFile, 0o600); } catch { /* best-effort on Windows */ }
          }
        }
      } catch (err) {
        console.error("Could not restore the authoring workdir:", err);
      }
      await runPass(
        buildPassArgs({
          prompt: plan.prompt,
          model: job.model,
          systemPromptFile: systemFile,
          workDir,
          budgetUsd: directive.remainingBudgetUsd,
          sessionArgs: plan.sessionArgs,
        })
      );
    }

    // 3) Read emitted artifacts.
    const specPath = path.join(workDir, job.specFilename);
    const sidecarPath = path.join(workDir, job.sidecarFilename || "discovered.json");
    const code = fs.existsSync(specPath) ? fs.readFileSync(specPath, "utf-8") : "";
    let discovered: unknown = { routes: [], selectors: [] };
    if (fs.existsSync(sidecarPath)) {
      try { discovered = JSON.parse(fs.readFileSync(sidecarPath, "utf-8")); } catch { /* keep default */ }
    }
    const ok = code.trim().length > 0;
    let summary = finalResult || "";
    // Say it in the RESULT too, not just the live trail: a user reading the spec
    // later must be able to tell that their guidance was replayed into a fresh
    // pass rather than carried by the original reasoning context (#619).
    if (fallbackNote) summary = `${summary}\n[resume] ${fallbackNote}`.trim();
    if (!ok) {
      // Make failures diagnosable: include claude's exit + stderr tail.
      const errTail = cerr.trim().slice(-500);
      summary = `${summary || "Live authoring produced no spec."} (claude exit ${exitCode})` +
        (errTail ? `\n[claude stderr] ${errTail}` : "");
    }
    emitStep(ok ? `✓ spec authored · $${costUsd.toFixed(2)}` : `✗ no spec (claude exit ${exitCode})`);
    // Terminal event (carries the run cost) so the UI can close the trail + show $.
    await api
      .postAuthoringEvent(cfg, job.sessionId, "authoring.progress", {
        case: job.caseId, phase: ok ? "done" : "failed", message: (summary || "").slice(0, 300), costUsd,
      })
      .catch(() => {});
    // Auto-rotation (#cred-rotate): if we ran with the app's saved credential and
    // the CLI refreshed the OAuth token in our temp config dir, post the rotated
    // .credentials.json back so the server captures it before workDir is deleted.
    let refreshedCredentials = "";
    if (claudeConfigDir) {
      try {
        const credFile = path.join(claudeConfigDir, ".credentials.json");
        if (fs.existsSync(credFile)) refreshedCredentials = fs.readFileSync(credFile, "utf-8");
      } catch { /* best-effort — a missing/unreadable file just skips rotation */ }
    }
    await finalize(code, discovered, summary, ok, costUsd, refreshedCredentials);
  } finally {
    try { claudeChild.current?.kill(); } catch {}
    // Closing the launcher's stdin tells it to kill Chrome (cross-platform).
    try { launcher?.stdin?.end(); } catch {}
    try { launcher?.kill(); } catch {}
    if (activeChild === launcher || activeChild === claudeChild.current) activeChild = null;
    try { fs.rmSync(workDir, { recursive: true, force: true }); } catch {}
  }
}

export async function runAgentLoop(cfg: AgentConfig, signal: { aborted: boolean }): Promise<void> {
  if (!(await ensureChromium())) {
    console.error("Chromium is required to run tests — aborting.");
    return;
  }
  console.log(
    `Local Agent v${agentVersion()} started — polling ${cfg.serverUrl} as device #${cfg.deviceId} (${cfg.deviceName})`
  );
  while (!signal.aborted) {
    let job: api.Job | null = null;
    try {
      job = await api.claimNextJob(cfg);
    } catch (err) {
      console.error("Claim failed:", (err as Error).message);
    }
    if (!job) {
      // No execution queued — check for a standalone login-capture request.
      let capture: api.CaptureJob | null = null;
      try {
        capture = await api.claimNextCapture(cfg);
      } catch (err) {
        console.error("Capture claim failed:", (err as Error).message);
      }
      if (capture) {
        await processCapture(cfg, capture);
        continue;
      }
      // No capture either — check for a queued DOM-exploration session (#338).
      let explore: api.ExplorationSession | null = null;
      try {
        explore = await api.claimNextExploration(cfg);
      } catch (err) {
        console.error("Exploration claim failed:", (err as Error).message);
      }
      if (explore) {
        console.log(`Claimed exploration ${explore.sessionId} (${explore.target?.goal || explore.baseUrl})`);
        emit("explore-claimed", { sessionId: explore.sessionId, goal: explore.target?.goal });
        try {
          await processExplorationJob(cfg, explore);
          console.log(`Exploration ${explore.sessionId} complete`);
        } catch (err) {
          console.error(`Exploration ${explore.sessionId} crashed:`, err);
          emit("error", { message: `Exploration ${explore.sessionId} crashed: ${(err as Error).message}` });
        }
        continue;
      }
      // No exploration either — check for a queued live-authoring session (#403):
      // drive `claude` + browser-harness locally to author a spec from the real app.
      let authoring: api.AuthoringJob | null = null;
      try {
        authoring = await api.claimNextAuthoring(cfg);
      } catch (err) {
        console.error("Authoring claim failed:", (err as Error).message);
      }
      if (authoring) {
        console.log(`Claimed authoring ${authoring.sessionId} (case ${authoring.caseId}, ${authoring.baseUrl})`);
        emit("authoring-claimed", { sessionId: authoring.sessionId, caseId: authoring.caseId });
        try {
          await processAuthoringJob(cfg, authoring);
          console.log(`Authoring ${authoring.sessionId} complete`);
        } catch (err) {
          console.error(`Authoring ${authoring.sessionId} crashed:`, err);
          emit("error", { message: `Authoring ${authoring.sessionId} crashed: ${(err as Error).message}` });
        }
        continue;
      }
      await new Promise((r) => setTimeout(r, IDLE_POLL_MS));
      continue;
    }
    console.log(`Claimed execution #${job.executionId} (run ${job.runCode}, ${job.specs.length} spec(s))`);
    emit("job-claimed", { executionId: job.executionId, runCode: job.runCode, total: job.specs.length });
    try {
      await processJob(cfg, job);
      console.log(`Execution #${job.executionId} complete`);
    } catch (err) {
      console.error(`Execution #${job.executionId} crashed:`, err);
      emit("error", { message: `Execution #${job.executionId} crashed: ${(err as Error).message}` });
      await api
        .postComplete(cfg, job.executionId, {
          passed: 0,
          failed: job.specs.length,
          log: `Local Agent crashed: ${(err as Error).message}`,
        })
        .catch(() => {});
    }
  }
}
