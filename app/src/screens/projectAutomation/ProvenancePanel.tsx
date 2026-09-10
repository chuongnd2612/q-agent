import { useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import {
  ChevronDown,
  Copy,
  ExternalLink,
  Fingerprint,
  GitBranch,
  History,
  Info,
  Share2,
  Ticket,
} from "lucide-react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";
import { timeAgo, runBadge, type RunDisplayStatus } from "@/components/dashboard/runStatus";
import { toast } from "@/lib/toast";
import { cn } from "@/lib/cn";
import { useProjectRoute } from "@/screens/ProjectDetail";
import { PathTooltip } from "@/screens/automation/PathTooltip";
import { SpecStatusDot } from "@/screens/automation/SpecStatusDot";
import { normalizeSpecStatus, SPEC_STATUS_DOT } from "@/screens/automation/specStatus";
import type { AutomationFileOut, SpecProvenanceEntry } from "@/types/api";

/**
 * Where the open file came from (#765/#772) — rendered directly above
 * `ProjectFilePanel` in the project Automation tab's right column.
 *
 * ## Two answers, because the schema only has two
 *
 * `provenance` is an `AutomationSpec` join (#769), and only a **spec** file has
 * such a row. So this component is a fork, not a template with holes:
 *
 * * **A spec** → the run / ticket / case that generated it, named exactly, from a
 *   persisted foreign key. A fact.
 * * **Anything else** (page object, component, fixture, data, util, config) →
 *   `null`, and the honest sentence that says so. These files are authored once
 *   and then *extended* by later runs (ADR 0014, and the REUSE/EXTEND/CREATE plan
 *   in `parsePlanReport`), so "the run that made it" is not a fact that exists.
 *
 * The one thing this must never do is bridge that gap by guessing. `updatedAt` is
 * adjacent to *some* run's finish time and it would be trivial to render "probably
 * RUN-0031" — which is #178's failure mode in miniature: a guess rendered in the
 * same typeface as a fact. So the non-spec branch shows only what is actually
 * known (the mirror's sync stamp and the content digest) and points at git for the
 * per-commit answer, which genuinely does exist in the repo and is simply not
 * exposed over HTTP yet.
 *
 * ## Why `history` is shown at all, and why collapsed
 *
 * ADR 0014 lets a later run rewrite a spec path while the earlier run's
 * `AutomationSpec` row keeps its own, now-stale copy of the code. Rendering only
 * `latest` would hide real lineage — the earlier run *did* produce this file, and
 * its result is still on the record. Rendering all of them flat would be worse: it
 * would imply that any of them might be the bytes on screen. So `latest` is the
 * headline and `history` is a collapsed list whose stale entries say, in words,
 * that their stored spec is not what is being shown.
 *
 * ## Surfaces
 *
 * Opaque `rgba(8,8,13,.92)` throughout, never `GlassCard`/`backdrop-filter`: this
 * is small, text-heavy chrome over the shell's animated constellation, where a
 * translucent card makes it genuinely unreadable (the same finding as
 * `ProjectFilePanel`, `ExportProjectPanel` and slice E's `RepoHeader`). The only
 * floating element is the full-digest tooltip, which reuses {@link PathTooltip} —
 * already portalled to `document.body` with fixed positioning anchored to the
 * trigger rect, as required, since this tab renders inside the project layout's
 * `motion` wrapper (a transform stacking context that traps `z-index`). The copy
 * confirmation is a `toast` rather than a second popover, matching
 * `ProjectFilePanel`.
 *
 * Status vocabulary is **reused, not reinvented**: `SpecStatusDot` /
 * `normalizeSpecStatus` for the spec, `runBadge` for the run.
 */
export function ProvenancePanel({ file }: { file: AutomationFileOut }) {
  // Non-spec files are the majority of a mature repo, so this branch is the
  // common one — and it is a strip, not a blank space (an absent panel reads as
  // "not implemented"; the sentence reads as "no such fact").
  if (!file.provenance) return <SharedAssetStrip file={file} />;
  return <SpecProvenanceCard provenance={file.provenance} />;
}

/** The provenance of a generated spec: its latest producing run, plus lineage. */
function SpecProvenanceCard({
  provenance,
}: {
  provenance: NonNullable<AutomationFileOut["provenance"]>;
}) {
  const { t } = useTranslation("projects");
  const [open, setOpen] = useState(false);
  const { latest, history, overwritten } = provenance;
  // `overwritten` is the API's own verdict; `history.length` is belt and braces
  // so an empty list can never render a toggle that expands to nothing.
  const hasHistory = overwritten && history.length > 0;

  return (
    <div
      className="rounded-2xl border border-white/[0.09] px-4 py-3.5"
      style={{ background: "rgba(8,8,13,.92)" }}
      data-testid="provenance-spec"
    >
      <div className="flex items-center gap-2">
        <GitBranch size={14} className="shrink-0 text-violet" strokeWidth={2.2} />
        <span className="text-[11px] font-bold uppercase tracking-wide text-faint">
          {t("automation.provenance.title")}
        </span>
      </div>

      {/* The headline: the run whose write produced the code on screen. */}
      <ProvenanceEntryRow entry={latest} />

      {hasHistory && (
        <>
          <button
            type="button"
            onClick={() => setOpen((v) => !v)}
            aria-expanded={open}
            data-testid="provenance-history-toggle"
            className="mt-3 flex w-full items-center gap-1.5 rounded-lg border border-white/[0.08] px-2.5 py-1.5 text-left text-[11.5px] font-semibold text-ink-dim transition-colors hover:text-white"
          >
            <History size={12} className="shrink-0 text-faint" />
            <span className="min-w-0 flex-1">
              {t("automation.provenance.historyToggle", { count: history.length })}
            </span>
            <ChevronDown
              size={13}
              className={cn("shrink-0 transition-transform", open && "rotate-180")}
            />
          </button>

          {/* AnimatePresence is the DIRECT parent of the motion element, per
              CLAUDE.md — wrapping it one level higher silently skips the
              animation. Height-animated rather than faded so the code panel
              below slides instead of jumping. */}
          <AnimatePresence initial={false}>
            {open && (
              <motion.div
                key="history"
                initial={{ height: 0, opacity: 0 }}
                animate={{ height: "auto", opacity: 1 }}
                exit={{ height: 0, opacity: 0 }}
                transition={{ duration: 0.18, ease: "easeOut" }}
                className="overflow-hidden"
                data-testid="provenance-history"
              >
                <p className="m-0 mt-2.5 flex items-start gap-1.5 text-[11px] leading-relaxed text-muted">
                  <Info size={12} className="mt-[2px] shrink-0 text-faint" />
                  {t("automation.provenance.historyNote")}
                </p>
                <div className="mt-1 flex flex-col divide-y divide-white/[0.06]">
                  {history.map((entry) => (
                    <ProvenanceEntryRow
                      key={entry.specId}
                      entry={entry}
                      supersededBy={entry.stale ? latest.runCode : null}
                    />
                  ))}
                </div>
              </motion.div>
            )}
          </AnimatePresence>
        </>
      )}
    </div>
  );
}

/**
 * One `AutomationSpec` row: ticket → case → run, and how both ended.
 *
 * Identical shape for `latest` and every `history` entry, because the wire type is
 * identical — the only difference is the `supersededBy` line, which is what stops
 * a history entry from reading as though it produced the visible code.
 */
function ProvenanceEntryRow({
  entry,
  supersededBy = null,
}: {
  entry: SpecProvenanceEntry;
  /** The run code that overwrote this entry's file, when it was overwritten. */
  supersededBy?: string | null;
}) {
  const { t } = useTranslation("projects");
  const { projectKey, projectGuid } = useProjectRoute();
  // The tab already lives under `:projectGuid`, so the run overlay's automation
  // stage is addressable without a lookup. `projectKey` is the fallback for a
  // pre-#587 name-based URL, which the API and the layout both still resolve.
  const guid = projectGuid ?? projectKey ?? "";
  // `?case=` is exactly what `screens/Automation.tsx` reads for its selection, so
  // the link lands on this case's spec rather than the stage's default.
  const to = `/projects/${encodeURIComponent(guid)}/runs/${entry.runId}/automation?case=${entry.testCaseId}`;

  const specStatus = normalizeSpecStatus(entry.specStatus);
  const run = runBadge(entry.runStatus as RunDisplayStatus);

  return (
    <div className="flex flex-col gap-1.5 py-2.5" data-testid="provenance-entry">
      {/* Ticket + case: what was asked for. */}
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        {entry.ticketExternalId && (
          <span className="flex items-center gap-1 font-mono text-[11px] font-bold text-violet">
            <Ticket size={11} strokeWidth={2.4} /> {entry.ticketExternalId}
          </span>
        )}
        {entry.caseCode && (
          <span className="font-mono text-[11.5px] font-bold text-ink">{entry.caseCode}</span>
        )}
        {entry.caseTitle && (
          <span className="min-w-0 flex-1 truncate text-[12px] text-ink-soft" title={entry.caseTitle}>
            {entry.caseTitle}
          </span>
        )}
        {/* Spec outcome — the shared dot vocabulary, with the label spelled out
            because this panel has room for it where the left list does not. */}
        <span
          className="flex shrink-0 items-center gap-1.5 rounded-md px-1.5 py-0.5 text-[10.5px] font-bold"
          style={{ background: "rgba(255,255,255,.05)", color: SPEC_STATUS_DOT[specStatus] }}
          data-testid="provenance-spec-status"
        >
          <SpecStatusDot specStatus={entry.specStatus} />
          {t(`automation.provenance.specStatus.${specStatus}`)}
        </span>
      </div>

      {/* The run: who produced it, and a deep link back into it. */}
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <Link
          to={to}
          data-testid="provenance-run-link"
          className="flex min-w-0 items-center gap-1.5 text-[12px] text-ink-soft transition-colors hover:text-white"
          title={t("automation.provenance.openRun")}
        >
          <span className="font-mono font-bold">{entry.runCode}</span>
          {entry.runName && <span className="min-w-0 truncate text-muted">{entry.runName}</span>}
          <ExternalLink size={11} className="shrink-0 text-faint" />
        </Link>
        <span
          className="shrink-0 rounded-md px-1.5 py-0.5 text-[10.5px] font-bold"
          style={{ background: "rgba(255,255,255,.05)", color: run.color }}
        >
          {run.label}
        </span>
        <span className="text-[11px] text-faint">
          {entry.runFinishedAt
            ? t("automation.provenance.runFinished", { when: timeAgo(entry.runFinishedAt) })
            : t("automation.provenance.runStarted", { when: timeAgo(entry.runCreatedAt) })}
        </span>
      </div>

      {/* Why the gate stopped it, when it did — the spec status alone doesn't say. */}
      {entry.blockReason && (
        <p className="m-0 text-[11px] leading-relaxed text-amber-300/80">
          {t("automation.provenance.blockReason", { reason: entry.blockReason })}
        </p>
      )}

      {/* ADR 0014's overwrite rule, made visible on the entry it applies to. */}
      {supersededBy && (
        <p
          className="m-0 text-[11px] leading-relaxed text-muted"
          data-testid="provenance-superseded"
        >
          {t("automation.provenance.superseded", { runCode: supersededBy })}
        </p>
      )}
    </div>
  );
}

/**
 * The non-spec branch: a strip that says what is *not* recorded, plus the two
 * facts that are.
 *
 * `updatedAt` is labelled "last mirror sync" rather than "last edited" on purpose
 * — it is when the `AutomationFile` row was written, which is not the same claim
 * and must not be dressed up as one.
 */
function SharedAssetStrip({ file }: { file: AutomationFileOut }) {
  const { t } = useTranslation("projects");
  const short = file.sha256 ? file.sha256.slice(0, 8) : "";

  const copy = () => {
    navigator.clipboard.writeText(file.sha256);
    toast.success(t("automation.provenance.shaCopied"));
  };

  return (
    <div
      className="rounded-2xl border border-white/[0.09] px-4 py-3"
      style={{ background: "rgba(8,8,13,.92)" }}
      data-testid="provenance-shared"
    >
      <p className="m-0 flex items-start gap-2 text-[11.5px] leading-relaxed text-ink-soft">
        <Share2 size={13} className="mt-[2px] shrink-0 text-violet" strokeWidth={2.2} />
        {t("automation.provenance.sharedTitle")}
      </p>

      <div className="mt-2 flex flex-wrap items-center gap-x-3.5 gap-y-1.5">
        {file.updatedAt && (
          <span className="text-[11px] text-faint">
            {t("automation.provenance.syncedAt", { when: timeAgo(file.updatedAt) })}
          </span>
        )}
        {short && (
          // The digest is the file's identity — the one durable handle on "this
          // exact content" — so it is copyable in full while displaying short.
          // PathTooltip carries the full 64 chars and is already portalled.
          <PathTooltip label={file.sha256}>
            <button
              type="button"
              onClick={copy}
              data-testid="provenance-sha-copy"
              title={t("automation.provenance.copySha")}
              className="flex items-center gap-1.5 rounded-md border border-white/[0.08] px-1.5 py-0.5 font-mono text-[10.5px] text-ink-dim transition-colors hover:text-white"
            >
              <Fingerprint size={11} className="shrink-0 text-faint" />
              {short}
              <Copy size={10} className="shrink-0 text-faint" />
            </button>
          </PathTooltip>
        )}
      </div>

      {/* The pointer at the answer that does exist, and an explicit statement that
          reading it is not this slice — so the gap reads as scoped, not forgotten. */}
      <p className="m-0 mt-2 flex items-start gap-1.5 text-[11px] leading-relaxed text-muted">
        <Info size={12} className="mt-[2px] shrink-0 text-faint" />
        {t("automation.provenance.gitNote")}
      </p>
    </div>
  );
}
