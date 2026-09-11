import { AlertTriangle, ChevronRight, Image as ImageIcon, Terminal } from "lucide-react";
import { useTranslation } from "react-i18next";
import { api } from "@/lib/api";
import { useResultEvidence } from "@/hooks/queries";
import {
  formatDuration,
  specPathMatches,
  streamText,
  stripAnsi,
  type PwError,
  type PwResult,
  type PwStep,
  type ReportTest,
} from "@/lib/playwrightReport";
import type { ProjectExecutionResultOut } from "@/types/api";

/**
 * One test's full detail, as Playwright's own report shows it — **every attempt**,
 * not just the last (#801).
 *
 * ## Why each retry is its own block
 *
 * A retried test has one `PwResult` per attempt, and collapsing them to
 * `results[results.length - 1]` is what `parsePlaywrightReport` does and what this
 * viewer exists not to do. For a *flaky* test the last result **passed**, so a
 * collapsed view shows a green test with no error at all — the failure that made
 * it flaky is only in attempt 1. So attempts render oldest-first, each with its own
 * status, duration, steps, errors and console output.
 *
 * ## Attachments come from Evidence, not from the report
 *
 * `result.attachments[].path` is an absolute path **on the machine that ran the
 * test** (the paired device for the `local-agent` target). It is not fetchable
 * from a browser and is deliberately not rendered as a link. The failure
 * screenshots #799 uploads are `Evidence` rows instead, reached per
 * `ExecutionResult` and served through the `/artifacts` mount — where
 * `api.artifactUrl`'s `?token=` capability URL is the correct pattern, because a
 * bare `<img src>` cannot carry a bearer header.
 *
 * There is no Traces tab and no video, deliberately: neither is uploaded, and a
 * tab that looks like Playwright's but never works is worse than its absence.
 */
export function ReportTestDetail({
  test,
  results,
}: {
  test: ReportTest;
  /** The execution's per-spec result rows — the bridge to the failure screenshots. */
  results: ProjectExecutionResultOut[];
}) {
  const { t } = useTranslation("projects");

  // The report's `file` is relative to Playwright's rootDir; a result's
  // `specPath` is relative to the repo. `specPathMatches` bridges the two.
  const matched = results.find((r) => specPathMatches(r.specPath, test.file));
  const evidence = useResultEvidence(matched?.id ?? null);
  const screenshots = (evidence.data ?? []).filter((e) => e.kind === "screenshot");

  return (
    <div className="flex flex-col gap-3 border-t border-bd3 bg-inset px-4 py-3.5">
      {test.results.length === 0 && (
        <p className="m-0 text-[11.5px] text-faint">{t("report.noAttempts")}</p>
      )}

      {test.results.map((result, index) => (
        <AttemptBlock key={index} result={result} index={index} />
      ))}

      {screenshots.length > 0 && (
        <section className="flex flex-col gap-2">
          <h4 className="m-0 flex items-center gap-1.5 text-[11px] font-bold tracking-[0.08em] text-label uppercase">
            <ImageIcon size={12} /> {t("report.attachments")}
          </h4>
          {screenshots.map((shot) => (
            <figure key={shot.id} className="m-0 flex flex-col gap-1">
              <img
                src={api.artifactUrl(shot.path)}
                alt={shot.filename}
                loading="lazy"
                className="max-h-[460px] w-full rounded-lg border border-bd2 bg-media-bg object-contain"
              />
              <figcaption className="font-mono text-[10.5px] text-faint">
                {shot.filename}
              </figcaption>
            </figure>
          ))}
        </section>
      )}
    </div>
  );
}

/** One attempt: its status line, steps, errors and console output. */
function AttemptBlock({ result, index }: { result: PwResult; index: number }) {
  const { t } = useTranslation("projects");
  const retry = result.retry ?? index;
  const stdout = streamText(result.stdout);
  const stderr = streamText(result.stderr);
  // `error` and `errors[0]` are the same failure in two forms, and the singular
  // is the **structured** one: verified against 1.61.1, `error` carries
  // `message` / `stack` / `snippet` / `location` as separate fields, while
  // `errors[0].message` is one pre-rendered blob with the code frame and the
  // `at …` frames already baked into it. Rendering the blob would print the
  // snippet twice — once inside the message, once in the snippet block — so the
  // singular wins and only the *additional* entries of `errors` (a second,
  // genuinely different failure) are appended.
  const errors: PwError[] = result.error
    ? [result.error, ...(result.errors ?? []).slice(1)]
    : (result.errors ?? []);

  return (
    <section
      className="flex flex-col gap-2 rounded-xl border border-bd3 bg-pop px-3 py-2.5"
      data-testid="report-attempt"
    >
      <header className="flex flex-wrap items-center gap-x-2.5 gap-y-1 text-[11.5px]">
        <span className="font-bold text-txt2">
          {retry === 0 ? t("report.firstAttempt") : t("report.retryN", { n: retry })}
        </span>
        <ResultStatusPill status={result.status ?? "unknown"} />
        <span className="text-faint">{formatDuration(result.duration)}</span>
        {result.workerIndex != null && (
          <span className="text-faint">
            {t("report.worker", { n: result.workerIndex })}
          </span>
        )}
      </header>

      {(result.steps?.length ?? 0) > 0 && (
        <StepTree steps={result.steps as PwStep[]} depth={0} />
      )}

      {errors.map((error, i) => (
        <ErrorBlock key={i} error={error} />
      ))}

      {stdout && <ConsoleBlock label={t("report.stdout")} text={stdout} tone="out" />}
      {stderr && <ConsoleBlock label={t("report.stderr")} text={stderr} tone="err" />}
    </section>
  );
}

/**
 * The nested steps tree with per-step durations.
 *
 * Rendered recursively because a step's `steps` nest arbitrarily deep, and
 * indentation is the only thing that makes the hierarchy legible. Verified
 * against Playwright 1.61.1, whose step objects carry `title`, `duration` and
 * `steps` and nothing else — no category, no location — so this shows exactly
 * what the report knows.
 */
function StepTree({ steps, depth }: { steps: PwStep[]; depth: number }) {
  return (
    <ul className="m-0 flex list-none flex-col gap-[3px] p-0" data-testid="report-steps">
      {steps.map((step, i) => (
        <li key={i} className="flex flex-col gap-[3px]">
          <div
            className="flex items-start gap-1.5 text-[11.5px] leading-snug"
            style={{ paddingLeft: depth * 14 }}
          >
            <ChevronRight size={11} className="mt-[3px] shrink-0 text-faint" />
            <span className={step.error ? "text-danger" : "text-txt3"}>{step.title}</span>
            <span className="ml-auto shrink-0 pl-2 font-mono text-[10.5px] text-faint">
              {formatDuration(step.duration)}
            </span>
          </div>
          {(step.steps?.length ?? 0) > 0 && (
            <StepTree steps={step.steps as PwStep[]} depth={depth + 1} />
          )}
        </li>
      ))}
    </ul>
  );
}

/** An error's message, its code frame/snippet when the report carries one, and its stack. */
function ErrorBlock({ error }: { error: PwError }) {
  const { t } = useTranslation("projects");
  const message = stripAnsi(error.message ?? error.value ?? "");
  const snippet = error.snippet ? stripAnsi(error.snippet) : "";
  // The stack repeats the message as its first lines; show only the frames.
  const stack = error.stack ? stripAnsi(error.stack).split("\n").filter((l) => l.trimStart().startsWith("at ")).join("\n") : "";

  return (
    <div className="flex flex-col gap-1.5" data-testid="report-error">
      <div className="flex items-start gap-1.5 rounded-lg border border-bd3 bg-danger-tint px-2.5 py-2">
        <AlertTriangle size={12} className="mt-[3px] shrink-0 text-danger" />
        <pre className="m-0 min-w-0 flex-1 overflow-x-auto font-mono text-[11.5px] leading-relaxed whitespace-pre-wrap text-txt2">
          {message}
        </pre>
      </div>
      {error.location && (
        <p className="m-0 font-mono text-[10.5px] text-faint">
          {error.location.file}:{error.location.line}:{error.location.column}
        </p>
      )}
      {snippet && (
        <pre className="m-0 overflow-x-auto rounded-lg border border-bd3 bg-code px-2.5 py-2 font-mono text-[11px] leading-relaxed text-txt3">
          {snippet}
        </pre>
      )}
      {stack && (
        <details>
          <summary className="cursor-pointer text-[11px] font-semibold text-muted">
            {t("report.stack")}
          </summary>
          <pre className="m-0 mt-1 overflow-x-auto rounded-lg border border-bd3 bg-code px-2.5 py-2 font-mono text-[10.5px] leading-relaxed text-faint">
            {stack}
          </pre>
        </details>
      )}
    </div>
  );
}

/** One console stream, verbatim (ANSI already stripped by `streamText`). */
function ConsoleBlock({
  label,
  text,
  tone,
}: {
  label: string;
  text: string;
  tone: "out" | "err";
}) {
  return (
    <div className="flex flex-col gap-1">
      <h4 className="m-0 flex items-center gap-1.5 text-[11px] font-bold tracking-[0.08em] text-label uppercase">
        <Terminal size={12} /> {label}
      </h4>
      <pre
        className={`m-0 max-h-60 overflow-auto rounded-lg border border-bd3 bg-code px-2.5 py-2 font-mono text-[11px] leading-relaxed whitespace-pre-wrap ${
          tone === "err" ? "text-danger" : "text-txt3"
        }`}
      >
        {text}
      </pre>
    </div>
  );
}

/** The raw per-attempt status (`passed` / `failed` / `timedOut` / `interrupted` / `skipped`). */
function ResultStatusPill({ status }: { status: string }) {
  const tone =
    status === "passed"
      ? "bg-ok-tint text-ok"
      : status === "skipped"
        ? "bg-neutral-tint text-neutral"
        : "bg-danger-tint text-danger";
  return (
    <span className={`rounded-md px-1.5 py-0.5 font-mono text-[10px] font-bold ${tone}`}>
      {status}
    </span>
  );
}
