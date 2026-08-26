import { RotateCcw, Sparkles } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { useQueryClient } from "@tanstack/react-query";
import { toast } from "@/lib/toast";
import { Button } from "@/components/ui/Button";
import { PipelineRail } from "@/components/ui/PipelineRail";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { useRunPath } from "@/hooks/useRunRouteId";
import {
  ALL_TICKETS_PAGE_SIZE,
  useAutomationStatus,
  useExecution,
  useExploreSpec,
  useExploreStatus,
  useGenerateAutomation,
  useHealReport,
  useHealSpec,
  useHealStatus,
  useKnowledgeList,
  useRegenerateSpec,
  useRun,
  useRunCases,
  useRunRepos,
  useRunSpec,
  useSetRunTicketRepo,
  useSpecs,
  useStartExecution,
  useTickets,
  useUpdateSpec,
} from "@/hooks/queries";
import { queryKeys } from "@/lib/queryKeys";
import { useRunEvents } from "@/hooks/useRunEvents";
import type { AutomationSpecOut, ChatReplyPayload, HealReport } from "@/types/api";
import { usePrefersReducedMotion } from "@/hooks/usePrefersReducedMotion";
import { normalizeSpecStatus, parseGateReport, parsePlanReport } from "./automation/specStatus";
import { PlanReport } from "./automation/PlanReport";
import { useAutomationEvents } from "./automation/useAutomationEvents";
import { useThinkingSteps } from "./automation/useThinkingSteps";
import { useCodeFolding } from "./automation/useCodeFolding";
import { TargetRepoPanel } from "./automation/TargetRepoPanel";
import { ExportProjectPanel } from "./automation/ExportProjectPanel";
import { ThinkingBanner, GeneratingBanner, HealProgressBanner, ExploreProgressBanner, AuthoringProgressBanner } from "./automation/ProgressBanners";
import { NoAutomationEmptyState } from "./automation/EmptyState";
import { GenerationFailureBanner } from "./automation/GenerationFailureBanner";
import { SpecList } from "./automation/SpecList";
import { ProductDefectBanner, BlockedBanner } from "./automation/banners";
import { SpecCodePanel } from "./automation/SpecCodePanel";
import { RunSuiteBar } from "./automation/RunSuiteBar";
import { HealTimeline } from "./automation/HealTimeline";
import { ExploreReview } from "./automation/ExploreReview";
import { diffLines } from "./automation/lineDiff";
import { SpecChatPanel } from "./automation/chat/SpecChatPanel";
import { useUI } from "@/store/ui";
import { SetupBlockers } from "@/components/setup/SetupBlockers";
import { useSetupGuard } from "@/components/setup/useSetupGuard";
import { RegenSummary, deriveTags } from "./automation/RegenSummary";
import { ProjectFileTree } from "./automation/ProjectFileTree";
import { ProjectFilePanel } from "./automation/ProjectFilePanel";
import { buildFileList } from "./automation/projectFiles";

export function Automation() {
  const { t } = useTranslation("pipeline");
  const runId = Number(useParams().runId);
  const runPath = useRunPath();
  const navigate = useNavigate();
  const { data: run } = useRun(runId);
  const { data: specs, isLoading } = useSpecs(runId);
  const { data: cases } = useRunCases(runId);
  const generateAutomation = useGenerateAutomation(runId);
  const regenerateSpec = useRegenerateSpec(runId);
  const guardSetup = useSetupGuard();
  const updateSpec = useUpdateSpec(runId);
  const startExecution = useStartExecution(runId);
  const { data: autoStatus } = useAutomationStatus(runId);
  const { data: repoOptions } = useRunRepos(runId);
  const { data: ticketsPage } = useTickets({ pageSize: ALL_TICKETS_PAGE_SIZE });
  const tickets = ticketsPage?.items;
  const { data: execution } = useExecution(runId);
  const setTicketRepo = useSetRunTicketRepo(runId);
  const healSpec = useHealSpec(runId);
  const exploreSpec = useExploreSpec();
  const { data: knowledgeList } = useKnowledgeList();
  const openChat = useUI((s) => s.openChat);
  const runSpec = useRunSpec(runId);
  const qc = useQueryClient();

  // Which spec is selected — a deep-linkable selection in the URL (`?case=`).
  const [searchParams, setSearchParams] = useSearchParams();
  const caseParam = searchParams.get("case");
  const selectedSpecCaseId = caseParam != null ? Number(caseParam) : null;
  const selectSpec = useCallback(
    (caseId: number, replace = false) =>
      setSearchParams(
        (prev) => {
          const next = new URLSearchParams(prev);
          next.set("case", String(caseId));
          // Picking a spec always lands on that spec's editor, never on whichever
          // read-only project file (`?file=`) happened to be open.
          next.delete("file");
          return next;
        },
        { replace },
      ),
    [setSearchParams],
  );

  const [copyLabel, setCopyLabel] = useState(t("automation.copy"));

  // Ephemeral, client-side inline-diff state for the last regeneration of the
  // selected case: which lines changed vs the previous code, a lines-changed
  // count, heuristic tags, and a per-case version number. Cleared when the
  // selected case changes; never persisted (no migration).
  const [regenResult, setRegenResult] = useState<{
    caseId: number;
    prevCode: string;
    changed: Set<number>;
    count: number;
    tags: string[];
    version: number;
  } | null>(null);
  const [versionByCase, setVersionByCase] = useState<Record<number, number>>({});
  // Bumped by the RegenSummary "Feedback" button to force-open the note composer.
  const [feedbackSignal, setFeedbackSignal] = useState(0);
  // Regeneration runs off-request (background thread → `spec.regenerated` WS
  // event) so it can't hit the proxy timeout. Track which cases are in flight
  // (drives the "Regenerating…" state), and the pre-regen code per case so the
  // event handler can diff old vs new when the result lands.
  const [regeneratingCases, setRegeneratingCases] = useState<Set<number>>(new Set());
  const [prevCodeByCase, setPrevCodeByCase] = useState<Record<number, string>>({});
  // Generation runs in the background on the server; the POST returns
  // immediately. Derive the running state from the persisted server status so it
  // survives navigation and blocks re-triggering.
  const generating = (autoStatus?.generating ?? false) || generateAutomation.isPending;
  const lastGenerationError = autoStatus?.lastError ?? null;

  // Live generation, self-heal, and DOM-exploration progress from the run's WS stream.
  const { genProgress, healProgress, exploreProgress, authoringProgress } = useAutomationEvents(runId, generating);

  const specCount = specs ? specs.length : 0;

  // The run's automatable cases (approved + not Manual) — what generation targets.
  const automatableCount = useMemo(
    () => (cases ?? []).filter((c) => c.approval === "approved" && c.automation !== "Manual").length,
    [cases],
  );

  // Approved cases still missing a spec — the target of incremental generation.
  const missingCount = Math.max(0, automatableCount - specCount);

  // What the server would actually execute, mirroring the filter in
  // `POST /runs/{id}/execution`: approved, not Manual, and not sitting behind a
  // blocked/product-defect spec. The specs list endpoint filters on nothing but
  // the run, so `specCount > 0` says nothing about runnability — without this the
  // suite-run button would look ready and 400 with "No runnable specs to
  // execute" (#701).
  const runnableCount = useMemo(() => {
    const statusByCase = new Map((specs ?? []).map((s) => [s.testCaseId, s.status]));
    return (cases ?? []).filter(
      (c) =>
        c.approval === "approved" &&
        c.automation !== "Manual" &&
        !["blocked", "product_defect"].includes(statusByCase.get(c.id) ?? ""),
    ).length;
  }, [cases, specs]);

  // Latest execution status per case, for the status dot next to each spec.
  const resultStatusByCase = useMemo(() => {
    const map = new Map<number, string>();
    for (const r of execution?.results ?? []) map.set(r.testCaseId, r.status);
    return map;
  }, [execution]);

  // Per-work-item target repositories. Options come from the run's project repos;
  // each work item defaults to the repo Claude guessed (its `repo`), falling back
  // to the project default repo when unset.
  const runTickets = run?.runTickets ?? [];
  const repoSelectOptions = useMemo(
    () => (repoOptions ?? []).map((r) => ({ value: r.name, label: r.name })),
    [repoOptions],
  );
  const defaultRepoName = useMemo(
    () => repoOptions?.find((r) => r.default)?.name ?? repoOptions?.[0]?.name ?? "",
    [repoOptions],
  );
  const repoStatusOf = useCallback(
    (name: string) => repoOptions?.find((r) => r.name === name)?.status,
    [repoOptions],
  );
  const showRepoPanel = runTickets.length > 0 && (repoOptions?.length ?? 0) > 0;

  // Incremental generation: only cases that don't yet have a spec. Newly
  // approved cases get specs while already-generated (and possibly edited) ones
  // are left untouched.
  const startGenerate = () => {
    // Pre-flight (#643). These are exactly the prerequisites whose silent failure
    // #641 had to make readable after the fact — `_enqueue_agent_authoring`
    // raises for a missing agent or base URL, the pass ends looking like a
    // successful one, and the screen said "No automation yet". Refusing here
    // turns that into an answer before the click.
    guardSetup(["claudeCredential", "localAgent", "projectBaseUrl"], () => generateNow());
  };

  const generateNow = () => {
    generateAutomation.mutate(false, {
      onError: (e) =>
        toast.error(e instanceof Error ? e.message : t("automation.generationFailedToStart")),
    });
  };

  // Force regeneration of every approved case, overwriting existing specs
  // (including manual edits). Guarded by a confirm since it is destructive.
  const regenerateAll = () => {
    if (!window.confirm(t("automation.regenerateAllConfirm"))) return;
    generateAutomation.mutate(true, {
      onError: (e) =>
        toast.error(e instanceof Error ? e.message : t("automation.generationFailedToStart")),
    });
  };

  // Kick off a real execution for the active run, then land on the Execution
  // screen where progress is streamed. Navigating alone would leave it idle.
  const startExecutionAndView = () => {
    startExecution.mutate(
      {},
      {
        onSuccess: () => navigate(runPath("execution")),
        onError: (e) =>
          toast.error(e instanceof Error ? e.message : t("automation.executionFailedToStart")),
      },
    );
  };

  const thinking = specCount === 0 && (generating || (isLoading && specCount === 0));
  const thinkStep = useThinkingSteps(thinking);

  // Default to the first spec once the list loads.
  useEffect(() => {
    if (specs && specs.length && selectedSpecCaseId == null) {
      selectSpec(specs[0].testCaseId, true);
    }
  }, [specs, selectedSpecCaseId, selectSpec]);

  const selectedSpec = useMemo(
    () => specs?.find((s) => s.testCaseId === selectedSpecCaseId) ?? specs?.[0] ?? null,
    [specs, selectedSpecCaseId],
  );

  // ---- Automation project files (#543) ---------------------------------------
  // A spec that lives in a persistent automation project ships the project's other
  // files alongside it (page objects, fixtures, data, …), read-only. A legacy spec
  // (`project_id IS NULL`) has no `projectFiles`: `fileList` is then null, nothing
  // extra renders, and the screen behaves exactly as it did before this slice.
  const fileList = useMemo(
    () => buildFileList(selectedSpec?.projectFiles, selectedSpec?.filename),
    [selectedSpec?.projectFiles, selectedSpec?.filename],
  );
  // Which project file is open — a deep-linkable intra-screen selection in the URL
  // (`?file=`), never store state. Absent, unknown, or pointing at the spec itself
  // means "show the editable spec", so the spec is the default selection.
  const fileParam = searchParams.get("file");
  const openFile = useMemo(
    () =>
      fileList != null && fileParam != null && fileParam !== fileList.specPath
        ? selectedSpec?.projectFiles?.find((f) => f.path === fileParam) ?? null
        : null,
    [fileList, fileParam, selectedSpec?.projectFiles],
  );
  const selectFile = useCallback(
    (path: string) =>
      setSearchParams(
        (prev) => {
          const next = new URLSearchParams(prev);
          // The spec row is the absence of `?file=` — keeps legacy URLs canonical.
          if (path === fileList?.specPath) next.delete("file");
          else next.set("file", path);
          return next;
        },
        { replace: true },
      ),
    [setSearchParams, fileList],
  );

  // Code-folding state for the read-only spec viewer. Reset whenever the selected
  // spec changes so folds never carry over between files.
  const { foldRanges, folded, toggleFold, collapseAll, expandAll } = useCodeFolding(
    selectedSpec?.code,
    `${selectedSpec?.testCaseId}:${selectedSpec?.filename}`,
  );

  // Inline edit state for the selected spec. `draft` holds the textarea contents
  // while editing. Reset (exit edit mode) whenever the selected spec changes, so
  // an in-progress edit never bleeds into a different file.
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  useEffect(() => {
    setEditing(false);
  }, [selectedSpec?.testCaseId, selectedSpec?.filename]);

  // True while this spec's per-file regenerate mutation is in flight.
  const specRegenerating =
    selectedSpec != null && regeneratingCases.has(selectedSpec.testCaseId);

  // The background regeneration streams its result here. Drop the case from the
  // in-flight set, refresh the spec so the panel shows the new code, and — when
  // the code actually changed and didn't come back blocked — compute the inline
  // diff banner vs the code captured before the regenerate started.
  useRunEvents((evt) => {
    if (evt.event !== "spec.regenerated") return;
    const p = evt.payload as { caseId: number; spec?: AutomationSpecOut; error?: string };
    // Live-authoring (#400) publishes a PENDING (status="running") spec.regenerated
    // when the job is merely ENQUEUED — the real authored spec arrives on a later
    // terminal event. Refresh so the running row + live trail appear, but keep the
    // case in-flight and show NO completion feedback yet (that was the premature
    // "Regeneration finished" toast).
    if (p.spec != null && p.spec.status === "running") {
      qc.invalidateQueries({ queryKey: queryKeys.specs(runId) });
      return;
    }
    setRegeneratingCases((prev) => {
      const next = new Set(prev);
      next.delete(p.caseId);
      return next;
    });
    if (p.error) {
      toast.error(p.error);
      return;
    }
    qc.invalidateQueries({ queryKey: queryKeys.specs(runId) });
    const spec = p.spec;
    if (spec == null) return;
    const prevCode = prevCodeByCase[p.caseId];
    const isBlocked = spec.status === "blocked";
    const didChange = prevCode != null && spec.code !== prevCode;
    let count = 0;

    // Show the inline diff whenever the code actually changed — even when the
    // result is still blocked, so the reviewer can see what this attempt did
    // differently (the banner tones amber + says "still blocked" in that case).
    if (didChange) {
      const diff = diffLines(prevCode, spec.code);
      count = diff.count;
      const nextLines = spec.code.split("\n");
      const added = [...diff.changed].map((i) => nextLines[i] ?? "");
      const tags = deriveTags(added, diff.removed);
      const nextVersion = (versionByCase[p.caseId] ?? 1) + 1;
      setVersionByCase((prev) => ({ ...prev, [p.caseId]: nextVersion }));
      setRegenResult({
        caseId: p.caseId,
        prevCode,
        changed: diff.changed,
        count,
        tags,
        version: nextVersion,
      });
    }

    // Completion feedback for every outcome — a regeneration can take minutes.
    if (isBlocked) {
      toast.error(t("automation.regenBlocked"), {
        description: didChange
          ? t("automation.regenBlockedDescChanged")
          : t("automation.regenBlockedDescUnchanged"),
      });
    } else if (!didChange) {
      toast.message(
        prevCode == null ? t("automation.regenFinished") : t("automation.regenFinishedNoChanges"),
      );
    } else {
      toast.success(t("automation.regeneratedLines", { count }));
    }
  });

  // Self-heal state for the selected spec. Poll the server so "Healing…"
  // survives navigating away/back; OR it with the mutation's pending flag and
  // the live WS phase for instant feedback.
  const selectedCaseId = selectedSpec?.testCaseId ?? 0;
  const { data: healStatusData } = useHealStatus(selectedCaseId, !!selectedCaseId);
  // Drive the button from server truth (a queued/running heal, via useHealStatus)
  // plus the in-flight trigger POST — NOT from live WS progress. Gating on the WS
  // stream would stick "Healing…" forever if any terminal heal.progress event is
  // missed (WS blip, backgrounded tab) or for a phase the terminal handler doesn't
  // clear. healStatus polls every 1.5s while healing and flips off the moment the
  // heal execution completes, so the button always reconciles. The WS stream still
  // powers the detailed progress banner below.
  const healingThisCase = healSpec.isPending || (healStatusData?.healing ?? false);

  // Editor typewriter: when a chat edit lands, "type" the new spec code into the
  // code viewer (not just the chat reply), then release to the query-backed code.
  const reducedMotion = usePrefersReducedMotion();
  const [editorType, setEditorType] = useState<{ caseId: number; prev: string; full: string } | null>(
    null,
  );
  const [editorShown, setEditorShown] = useState("");
  // Line-level change highlight + scroll target for the last chat edit typed into
  // the editor (the chat "re-type" path, mirroring regenResult). Set inside the
  // re-type effect below — NOT in the WS handler — so `seq` bumps in the same
  // batched update that sets the compressed editorShown; otherwise the viewer's
  // scroll effect would fire against the stale previous code and find no target.
  // `seq` bumps per edit so the viewer re-scrolls even when firstLine repeats.
  const [editResult, setEditResult] = useState<{
    caseId: number;
    changed: Set<number>;
    firstLine: number | null;
    seq: number;
  } | null>(null);
  useRunEvents((evt) => {
    if (evt.event === "automation.chat.reply") {
      const p = evt.payload as unknown as ChatReplyPayload;
      setEditorType({ caseId: p.caseId, prev: p.prevCode, full: p.spec.code });
    }
  });
  useEffect(() => {
    if (!editorType) return;
    const { caseId, prev, full } = editorType;
    if (prev === full) {
      setEditorShown(full);
      return;
    }
    // Re-type only the changed span: keep the common prefix + suffix visible and
    // animate just the middle region that actually changed — not the whole spec.
    const maxHead = Math.min(prev.length, full.length);
    let head = 0;
    while (head < maxHead && prev[head] === full[head]) head++;
    let tail = 0;
    const maxTail = maxHead - head;
    while (tail < maxTail && prev[prev.length - 1 - tail] === full[full.length - 1 - tail]) tail++;
    const changedEnd = full.length - tail; // exclusive end of the changed middle
    // Scroll target = the line where the common prefix ends (where the edit
    // begins). This index is stable for the whole animation (the prefix never
    // changes) and is rendered from the first frame — unlike min(diffLines),
    // whose index only lines up with the DOM once the compressed middle settles.
    const firstLine = full.slice(0, head).split("\n").length - 1;
    setEditResult((prevR) => ({
      caseId,
      changed: diffLines(prev, full).changed,
      firstLine,
      seq: (prevR?.seq ?? 0) + 1,
    }));
    if (reducedMotion) {
      setEditorShown(full);
      return;
    }
    let i = head;
    // Prefix + (empty middle) + suffix; the middle grows in below.
    setEditorShown(full.slice(0, head) + full.slice(changedEnd));
    if (i >= changedEnd) {
      setEditorShown(full);
      return;
    }
    const id = setInterval(() => {
      i = Math.min(i + 12, changedEnd);
      setEditorShown(full.slice(0, i) + full.slice(changedEnd));
      if (i >= changedEnd) clearInterval(id);
    }, 16);
    return () => clearInterval(id);
  }, [editorType, reducedMotion]);
  const editorCodeOverride =
    editorType && editorType.caseId === selectedSpec?.testCaseId && editorShown.length < editorType.full.length
      ? editorShown
      : undefined;

  // "Run" stays in its loading state for the whole background execution, not
  // just the POST: true while the mutation is in flight, or while the latest
  // execution is still running this spec's case (pending/running result).
  const selectedResult = execution?.results.find((r) => r.testCaseId === selectedCaseId);
  const runningThisSpec =
    runSpec.isPending ||
    (execution?.status === "running" &&
      (selectedResult?.status === "running" || selectedResult?.status === "pending"));

  // The last self-heal trail for the selected spec (per-attempt error + diff).
  const { data: healReportRaw } = useHealReport(selectedCaseId, !!selectedCaseId);
  const healReport =
    healReportRaw && "attempts" in healReportRaw && healReportRaw.attempts?.length
      ? (healReportRaw as HealReport)
      : null;

  // Authoritative status for the selected spec, driving which actions are
  // suppressed and which status banner shows in the right panel.
  const selectedStatus = normalizeSpecStatus(selectedSpec?.status);
  // Only a product defect is truly non-runnable (triaged app bug → report). A
  // blocked spec can be Run / Self-healed on demand as a manual override (the
  // gate stays authoritative for normal/bulk runs).
  const runSuppressed = selectedStatus === "product_defect";
  const isProductDefect = selectedStatus === "product_defect";
  const isBlocked = selectedStatus === "blocked";
  // Live authoring (#400) for the SELECTED spec: show the streamed trail in the
  // code panel (instead of an empty editor) while it's being authored — either an
  // active trail for this case, or a running+empty spec waiting on the first event.
  const authoringForSelected =
    authoringProgress && authoringProgress.caseId === selectedSpec?.testCaseId ? authoringProgress : null;
  // Show the live trail (not the empty/stale editor) whenever the selected spec is
  // being authored: an active trail for it, OR a "running" spec that isn't being
  // healed/executed (status "running" is also used by heal/exec, so exclude those).
  // Covers the regenerate case (old code preserved) and the window before the first
  // authoring.progress event / before the specs query refetches.
  const authoringActive =
    (authoringForSelected != null && !authoringForSelected.done) ||
    (selectedStatus === "running" && !healingThisCase && !runningThisSpec);
  // Last placeholder-gate outcome for the selected spec: surface a non-destructive
  // note when the most recent regeneration was rejected (previous good spec kept).
  const gateReport = useMemo(() => parseGateReport(selectedSpec?.gateReport), [selectedSpec?.gateReport]);
  const gateRejected = gateReport?.outcome === "rejected";
  // The ticket's REUSE/EXTEND/CREATE plan (#544), persisted on the spec row exactly
  // like the gate report — so it renders beside it with no extra request.
  const planReport = useMemo(() => parsePlanReport(selectedSpec?.planReport), [selectedSpec?.planReport]);

  // The persistent automation project backing this run's specs, if any (#549) —
  // the export target. `null` for a legacy run (every spec has `projectId: null`),
  // which hides the export panel entirely since there is no repo to push.
  const exportableProjectId = useMemo(
    () => specs?.find((s) => s.projectId != null)?.projectId ?? null,
    [specs],
  );

  // ---- DOM exploration (ADR 0010): "Explore to unblock" a blocked case. -------
  // The selected case's target repo — the per-work-item repo (else the project
  // default), the same source TargetRepoPanel uses.
  const selectedCase = useMemo(
    () => cases?.find((c) => c.id === selectedCaseId) ?? null,
    [cases, selectedCaseId],
  );
  const targetRepo = useMemo(() => {
    const rt = runTickets.find((t) => t.ticketExternalId === selectedCase?.ticketExternalId);
    return rt?.repo || defaultRepoName;
  }, [runTickets, selectedCase, defaultRepoName]);
  // RunOut carries no project key and there is no run→project endpoint, so derive
  // it from the knowledge list: the run's repos (useRunRepos) belong to exactly
  // one project, so pick the project whose indexed repos overlap them the most.
  const projectKey = useMemo(() => {
    const runRepoNames = new Set((repoOptions ?? []).map((r) => r.name));
    if (runRepoNames.size === 0 || !knowledgeList) return "";
    const overlap = new Map<string, number>();
    for (const k of knowledgeList) {
      const pk = k.projectKey || k.key;
      if (pk && k.repo && runRepoNames.has(k.repo)) overlap.set(pk, (overlap.get(pk) ?? 0) + 1);
    }
    let best = "";
    let bestN = 0;
    for (const [pk, n] of overlap) {
      if (n > bestN) {
        best = pk;
        bestN = n;
      }
    }
    return best;
  }, [repoOptions, knowledgeList]);

  // Which case triggered the current/last exploration — the review panel shows
  // only under that case (exploration is repo-scoped, but the review is per-case).
  const [exploredCaseId, setExploredCaseId] = useState<number | null>(null);

  // Poll exploration status (repo-scoped) so "Exploring…" and the discovered
  // summary survive navigating away/back. Only relevant for a blocked case or an
  // in-flight/just-finished session. Mirrors useHealStatus.
  const exploreRelevant = isBlocked || exploreSpec.isPending || exploreProgress != null;
  const { data: exploreStatusData } = useExploreStatus(
    projectKey,
    targetRepo,
    exploreRelevant && !!projectKey && !!targetRepo,
  );
  // Drive the button/banner from server truth (in-flight status) + the trigger
  // POST — NOT the WS stream (a missed terminal step would otherwise stick it).
  const exploringThisCase = exploreSpec.isPending || (exploreStatusData?.exploring ?? false);

  // Kick off a DOM-exploration session for the selected blocked case: drive a real
  // browser to discover the missing routes/selectors, write them (runtime-verified)
  // to the KB, then the case can be regenerated. Progress streams over WS.
  const startExplore = () => {
    if (!selectedSpec) return;
    if (!projectKey || !targetRepo) {
      toast.error(t("automation.resolveRepoFailed"));
      return;
    }
    const caseId = selectedSpec.testCaseId;
    setExploredCaseId(caseId);
    exploreSpec.mutate(
      {
        projectKey,
        repo: targetRepo,
        body: {
          target: {
            ticket: selectedCase?.ticketExternalId,
            screen: selectedCase?.title,
            goal: selectedCase?.objective,
          },
          runId,
          caseId,
        },
      },
      {
        onSuccess: () =>
          qc.invalidateQueries({ queryKey: queryKeys.exploreStatus(projectKey, targetRepo) }),
        onError: (e) => toast.error(e instanceof Error ? e.message : t("automation.explorationFailedToStart")),
      },
    );
  };

  const handleCopy = () => {
    if (!selectedSpec) return;
    navigator.clipboard.writeText(selectedSpec.code);
    setCopyLabel(t("automation.copied"));
    toast.success(t("automation.codeCopied"));
    setTimeout(() => setCopyLabel(t("automation.copy")), 1500);
  };

  const handleDownload = () => {
    if (!selectedSpec) return;
    const blob = new Blob([selectedSpec.code], { type: "text/typescript" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = selectedSpec.filename;
    a.click();
    URL.revokeObjectURL(url);
  };

  const startEdit = () => {
    if (!selectedSpec) return;
    setDraft(selectedSpec.code);
    setEditing(true);
  };

  // Kick off self-heal for the selected spec. Classic mode runs the fix+re-run
  // loop; live-harness mode (#428) reuses the browser-harness authoring pipeline —
  // either way the spec flips to "running" server-side, so refresh specs too so the
  // in-panel live trail / running state appears immediately (not only once the
  // first WS event lands). Progress streams over WS (see useAutomationEvents).
  const startHeal = () => {
    if (!selectedSpec) return;
    healSpec.mutate(selectedSpec.testCaseId, {
      onSuccess: () => {
        qc.invalidateQueries({ queryKey: queryKeys.healStatus(selectedSpec.testCaseId) });
        qc.invalidateQueries({ queryKey: queryKeys.specs(runId) });
      },
      onError: (e) => toast.error(e instanceof Error ? e.message : t("automation.healFailedToStart")),
    });
  };

  // Run just this one spec (not the whole suite). Status dots refresh via the
  // execution query invalidation in useRunSpec.
  const runThisSpec = () => {
    if (!selectedSpec) return;
    const file = selectedSpec.filename;
    runSpec.mutate(selectedSpec.testCaseId, {
      onSuccess: () => toast.success(t("automation.runningSpec", { file })),
      onError: (e) => toast.error(e instanceof Error ? e.message : t("automation.runTestFailed")),
    });
  };

  // Clear the inline-diff banner whenever the selected case changes so a diff
  // never bleeds across specs.
  useEffect(() => {
    setRegenResult(null);
    setEditResult(null);
  }, [selectedSpecCaseId]);

  // Regenerate the selected spec, optionally with a reviewer note. Captures the
  // current code first so success can diff old vs new; a regeneration that leaves
  // the code unchanged OR comes back blocked shows no diff banner (the
  // GateRejectedNote / BlockedBanner already explain those outcomes).
  const handleRegenerate = (comment?: string) => {
    if (!selectedSpec) return;
    const caseId = selectedSpec.testCaseId;
    // Capture the pre-regen code so the `spec.regenerated` WS handler can diff
    // old vs new when the background worker finishes. Mark the case in-flight;
    // clear any stale diff banner while it regenerates.
    setPrevCodeByCase((prev) => ({ ...prev, [caseId]: selectedSpec.code }));
    setRegenResult((prev) => (prev?.caseId === caseId ? null : prev));
    setRegeneratingCases((prev) => new Set(prev).add(caseId));
    regenerateSpec.mutate(
      { caseId, comment },
      {
        // Fire-and-forget: success just means the job started; the result
        // arrives over the run WS. Only a failure to *start* is handled here.
        onError: (e) => {
          setRegeneratingCases((prev) => {
            const next = new Set(prev);
            next.delete(caseId);
            return next;
          });
          toast.error(e instanceof Error ? e.message : t("automation.regenerationFailedToStart"));
        },
      },
    );
  };

  const cancelEdit = () => setEditing(false);

  const saveEdit = () => {
    if (!selectedSpec) return;
    updateSpec.mutate(
      { caseId: selectedSpec.testCaseId, code: draft },
      {
        onSuccess: () => {
          setEditing(false);
          toast.success(t("automation.specSaved"));
        },
        onError: (e) => toast.error(e instanceof Error ? e.message : t("automation.saveSpecFailed")),
      },
    );
  };

  return (
    <div className="animate-fade-in-up px-1 pb-10 pt-0.5">
      <div className="mb-3.5 flex flex-col gap-3 md:flex-row md:items-end md:justify-between">
        <div>
          <div className="mb-1 text-[13px] font-medium text-muted">
            {run?.code} &middot; Playwright · TypeScript · {t("automation.approvedCasesOnly")}
          </div>
          <h1 className="m-0 text-[24px] font-black tracking-tight md:text-[28px]">{t("automation.title")}</h1>
        </div>
        {specCount > 0 && (
          <div className="flex flex-col gap-2 md:flex-row md:items-center">
            {missingCount > 0 && (
              <Button variant="primary" onClick={startGenerate} disabled={generating} className="w-full md:w-auto">
                <Sparkles size={15} strokeWidth={2.2} /> {t("automation.generateNew", { count: missingCount })}
              </Button>
            )}
            <Button variant="glass" onClick={regenerateAll} disabled={generating} className="w-full md:w-auto">
              <RotateCcw size={15} strokeWidth={2.2} /> {t("automation.regenerateAll")}
            </Button>
          </div>
        )}
      </div>
      <div className="mb-4 hidden md:block">
        <PipelineRail stage={3} />
      </div>

      {/* Only what generation itself needs (#643) — the Execution screen reports
          its own, so neither page accuses the other of being broken. */}
      <SetupBlockers only={["claudeCredential", "localAgent", "projectBaseUrl"]} />

      {showRepoPanel && (
        <TargetRepoPanel
          runTickets={runTickets}
          tickets={tickets}
          repoSelectOptions={repoSelectOptions}
          repoStatusOf={repoStatusOf}
          defaultRepoName={defaultRepoName}
          onChangeRepo={(tid, repo) =>
            setTicketRepo.mutate(
              { tid, repo },
              {
                onError: (e) =>
                  toast.error(e instanceof Error ? e.message : t("automation.setRepoFailed")),
              },
            )
          }
        />
      )}

      {/* Export the project: ZIP now (v1), git remote in v2 (#686).
          Only for a project-backed run: a legacy spec has no
          `projectId`, so there is no git repo to push and the panel is hidden. */}
      <ExportProjectPanel runId={runId} projectId={exportableProjectId} />

      {thinking && <ThinkingBanner runCode={run?.code} thinkStep={thinkStep} />}

      {generating && !thinking && <GeneratingBanner genProgress={genProgress} />}

      {healProgress && !["passed", "failed", "product_defect"].includes(healProgress.phase) && (
        <HealProgressBanner healProgress={healProgress} />
      )}

      {exploreProgress && exploringThisCase && (
        <ExploreProgressBanner exploreProgress={exploreProgress} />
      )}

      {/* Top banner only when authoring a case that is NOT the selected spec — the
          selected spec shows its trail inside the code panel (no duplication). */}
      {authoringProgress && authoringProgress.caseId !== selectedSpec?.testCaseId && (
        <AuthoringProgressBanner authoringProgress={authoringProgress} />
      )}

      {/* AI chat panel — edit the selected spec conversationally (portals to body). */}
      <SpecChatPanel runId={runId} spec={selectedSpec} />

      {/* Why the last pass produced nothing (#641) — shown whether or not any
          spec exists, since a partial failure matters too. */}
      {!generating && lastGenerationError && (
        <GenerationFailureBanner
          error={lastGenerationError}
          generating={generating}
          onRetry={startGenerate}
        />
      )}

      {!thinking && specs && specs.length === 0 && (
        <NoAutomationEmptyState
          automatableCount={automatableCount}
          generating={generating}
          onGenerate={startGenerate}
          // A failed pass already explains itself in the banner above; without
          // this the empty state would add "no approved, automatable cases",
          // contradicting it (#641).
          failed={Boolean(lastGenerationError)}
        />
      )}

      {/* The suite-wide action, above the editor and outside the spec panel — see
          RunSuiteBar for why it is no longer the code panel's footer (#701). */}
      {!thinking && specs && specs.length > 0 && (
        <RunSuiteBar
          pending={startExecution.isPending}
          runnable={runnableCount > 0}
          onRun={startExecutionAndView}
        />
      )}

      {!thinking && specs && specs.length > 0 && (
        <div className="flex flex-col gap-3.5 md:grid md:grid-cols-[230px_1fr] md:items-start">
          <div className="flex flex-col gap-3.5">
            <SpecList
              specs={specs}
              selectedTestCaseId={selectedSpec?.testCaseId ?? null}
              resultStatusByCase={resultStatusByCase}
              healProgress={healProgress}
              onSelect={selectSpec}
            />
            {/* Only for project-backed specs — a legacy spec renders nothing here. */}
            {fileList && (
              <ProjectFileTree
                groups={fileList.groups}
                specPath={fileList.specPath}
                selectedPath={openFile?.path ?? fileList.specPath}
                onSelect={selectFile}
              />
            )}
          </div>

          {openFile ? (
            <div className="min-w-0">
              <ProjectFilePanel file={openFile} />
            </div>
          ) : (
          <div className="flex min-w-0 flex-col gap-3.5">
          {isProductDefect && <ProductDefectBanner />}
          {isBlocked && (
            <BlockedBanner
              reason={selectedSpec?.blockReason ?? ""}
              onRegenerate={handleRegenerate}
              regenerating={specRegenerating}
              onExplore={startExplore}
              exploring={exploringThisCase}
            />
          )}
          {exploreProgress &&
            !exploringThisCase &&
            exploredCaseId === selectedSpec?.testCaseId && (
              <ExploreReview
                progress={exploreProgress}
                status={exploreStatusData}
                regenerating={specRegenerating}
                onRegenerate={handleRegenerate}
              />
            )}
          {regenResult && regenResult.caseId === selectedSpec?.testCaseId && (
            <RegenSummary
              version={regenResult.version}
              count={regenResult.count}
              tags={regenResult.tags}
              reverting={updateSpec.isPending}
              blocked={isBlocked}
              onFeedback={() => setFeedbackSignal((n) => n + 1)}
              onRevert={() => {
                if (!selectedSpec) return;
                updateSpec.mutate(
                  { caseId: selectedSpec.testCaseId, code: regenResult.prevCode },
                  {
                    onSuccess: () => {
                      setRegenResult(null);
                      toast.success(t("automation.revertedToPrevious"));
                    },
                    onError: (e) =>
                      toast.error(e instanceof Error ? e.message : t("automation.revertSpecFailed")),
                  },
                );
              }}
            />
          )}
          <SpecCodePanel
            selectedSpec={selectedSpec}
            editing={editing}
            draft={draft}
            setDraft={setDraft}
            foldRanges={foldRanges}
            folded={folded}
            toggleFold={toggleFold}
            collapseAll={collapseAll}
            expandAll={expandAll}
            generating={generating}
            specRegenerating={specRegenerating}
            healingThisCase={healingThisCase}
            exploringThisCase={exploringThisCase}
            runningThisSpec={!!runningThisSpec}
            runSuppressed={runSuppressed}
            isBlocked={isBlocked}
            isProductDefect={isProductDefect}
            gateRejected={gateRejected}
            gateReport={gateReport}
            authoringActive={authoringActive}
            authoringLines={authoringForSelected?.lines ?? []}
            authoringDone={authoringForSelected?.done ?? false}
            authoringPaused={authoringForSelected?.paused ?? false}
            updateSpecPending={updateSpec.isPending}
            copyLabel={copyLabel}
            changedLines={
              // While the chat edit is still re-typing, codeOverride shows a
              // compressed view (prefix+suffix, changed middle not yet typed), so
              // editResult's line indices wouldn't line up — defer that highlight
              // until the code settles. Reduced-motion has no override, so it shows
              // immediately.
              editResult && editResult.caseId === selectedSpec?.testCaseId && !editorCodeOverride
                ? editResult.changed
                : regenResult && regenResult.caseId === selectedSpec?.testCaseId
                  ? regenResult.changed
                  : undefined
            }
            scrollToLine={
              editResult && editResult.caseId === selectedSpec?.testCaseId
                ? editResult.firstLine ?? undefined
                : undefined
            }
            scrollSignal={
              editResult && editResult.caseId === selectedSpec?.testCaseId ? editResult.seq : undefined
            }
            regenVersion={selectedSpec ? versionByCase[selectedSpec.testCaseId] : undefined}
            feedbackSignal={feedbackSignal}
            onCopy={handleCopy}
            onDownload={handleDownload}
            onStartEdit={startEdit}
            onCancelEdit={cancelEdit}
            onSaveEdit={saveEdit}
            onRegenerate={handleRegenerate}
            onRunSpec={runThisSpec}
            onStartHeal={startHeal}
            onStartExplore={startExplore}
            onOpenChat={openChat}
            codeOverride={editorCodeOverride}
          />
          <PlanReport plan={planReport} />
          {healReport && <HealTimeline report={healReport} />}
          </div>
          )}
        </div>
      )}
    </div>
  );
}
