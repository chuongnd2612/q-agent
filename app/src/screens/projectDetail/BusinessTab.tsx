import { useState } from "react";
import { useTranslation } from "react-i18next";
import {
  AlertTriangle,
  BookOpen,
  CheckCircle2,
  Clock,
  EyeOff,
  FileText,
  Github,
  Globe,
  Eye,
  HelpCircle,
  Plus,
  RefreshCw,
  SearchCheck,
  Trash2,
  X,
} from "lucide-react";
import { Button } from "@/components/ui/Button";
import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import { timeAgo } from "@/components/dashboard/runStatus";
import {
  useBusinessSources,
  useDeleteBusinessSource,
  useProbeBusinessSource,
  useSyncBusinessSource,
  useUpdateBusinessSource,
} from "@/hooks/queries";
import { BusinessFactsPanel } from "./BusinessFactsPanel";
import { BusinessSourceForm } from "./BusinessSourceForm";
import { toast } from "@/lib/toast";
import type { BusinessSourceKind, BusinessSourceOut } from "@/types/api";

/**
 * Project → **Business Knowledge** tab (#817, epic #813, ADR 0016).
 *
 * The project's DOMAIN grounding — what the product is supposed to do — as a
 * PEER of Project Knowledge rather than a section of it, which is why it is its
 * own tab beside it rather than another panel inside it.
 *
 * ## Syncing is explicit on the server, automatic in the UI (#845)
 *
 * `POST /sources` deliberately does not fetch: registering a document should not
 * make its 201 depend on a remote host being reachable, and #821/#822 add kinds
 * whose fetch needs a connection that may be picked after the row exists. But a
 * source that sits at `pending` with nothing able to advance it reads as a
 * broken feature — so the panel fires the sync itself on the create it just
 * made, and every link row carries a **Sync now** action for the re-fetch and
 * the retry. The side effect stays where the user can see it (the row goes to
 * `Fetching…`) instead of hiding inside a POST.
 *
 * An upload has no address to re-fetch from, so it gets no sync action at all
 * rather than one that 400s — the server enforces the same rule.
 *
 * `excluded` is a context switch, not a soft delete: the snapshot and its
 * provenance survive so an artifact already generated from the source stays
 * attributable.
 *
 * ## Staleness is shown, and only ever claimed where it is known (#830)
 *
 * ADR 0016 §4 takes snapshots over live sync for ATTRIBUTABILITY, and that
 * bargain is only honest if a user can see when a snapshot has gone stale. The
 * badge therefore renders the server's `staleness.mode`, never a boolean the
 * client re-derives:
 *
 * - `revision` — a probe compared upstream versions, so "Upstream changed" /
 *   "Up to date" is a real statement.
 * - `age` — nothing can be probed (an upload has no address; a page sends no
 *   ETag), so the badge says "Last fetched 34 days ago" and says WHY, rather
 *   than a confident "changed". Claiming knowledge we do not have is worse
 *   than admitting the gap — that is the point of the slice.
 * - `unknown` — a probe exists but nobody has run it. Deliberately not styled
 *   as "up to date": nobody has looked.
 *
 * The provenance line under each row carries `fetchedAt` and the content hash,
 * because those are what a generated case's "grounded in" list names.
 *
 * ## The overlay lives below, in its own panel
 *
 * `BusinessFactsPanel` (#827) is the *distilled* half: the facts the documents
 * produced, plus the human overlay on them — correct, add, exclude. It is a
 * second panel rather than a section of this one because it is a different
 * object (a fact, not a document) with a different lifecycle, and because a
 * source can be excluded wholesale here while a single fact is corrected there.
 *
 * ## Opaque surface, not GlassCard
 *
 * This is a text-heavy panel (URLs and statuses at 11-12px) layered over the
 * shell's animated background, where a translucent card makes small text
 * genuinely unreadable — the finding from `ProjectFilePanel` and
 * `ExecutionReportPanel`. So: `bg-pop`, the opaque token, which is `#191921` on
 * dark and `#ffffff` on light, rather than a hardcoded `rgba(8,8,13,.92)` that
 * would stay near-black over a paper page. Every other colour is an appearance
 * token too (#783/#784), so the panel follows `data-mode` and `data-accent`.
 *
 * ## The add form lives in its own module
 *
 * Each kind is addressed and credentialed differently — a file, a page, a repo
 * address, a wiki plus a wiki-scoped token — so the form is `BusinessSourceForm`
 * (#848) rather than a generic kind/title/URL block that could register a
 * `github_md` or `ado_wiki` row and never supply what makes it work.
 *
 * ## Local form state
 *
 * Whether the add form is open is `useState`, not a query param: it is a
 * transient composer, not an addressable view, and a `?add=` that reopened a
 * half-typed form on reload would be a URL that lies. Navigation stays in the
 * path (the tab itself is `projects/:projectGuid/business`), per CLAUDE.md.
 */

const KIND_ICON: Record<BusinessSourceKind, typeof Globe> = {
  url: Globe,
  github_md: Github,
  ado_wiki: BookOpen,
  upload: FileText,
};

/** Status → [text token, tint token]. Semantic tokens, so both modes work. */
const STATUS_STYLE: Record<BusinessSourceOut["status"], [string, string]> = {
  pending: ["text-neutral", "bg-neutral-tint"],
  syncing: ["text-info", "bg-info-tint"],
  synced: ["text-ok", "bg-ok-tint"],
  error: ["text-danger", "bg-danger-tint"],
};

export function BusinessTab({ projectGuid }: { projectGuid: string | null }) {
  const { t } = useTranslation("projects");
  const sources = useBusinessSources(projectGuid);
  const update = useUpdateBusinessSource(projectGuid);
  const remove = useDeleteBusinessSource(projectGuid);
  const sync = useSyncBusinessSource(projectGuid);
  const probe = useProbeBusinessSource(projectGuid);

  const [adding, setAdding] = useState(false);
  const [confirming, setConfirming] = useState<BusinessSourceOut | null>(null);

  const rows = sources.data ?? [];

  /**
   * A source was registered: close the composer and start its fetch.
   *
   * Quietly, and only for a link: the row's own status is where the outcome
   * belongs, and an upload arrives already ingested (the server 400s a sync on
   * one — it has no address to re-fetch from).
   */
  const onCreated = (row: BusinessSourceOut) => {
    setAdding(false);
    if (row.kind !== "upload") sync.mutate(row.id);
  };

  const toggleExcluded = (row: BusinessSourceOut) =>
    update.mutate(
      { id: row.id, body: { excluded: !row.excluded } },
      {
        onSuccess: (next) =>
          toast.success(
            t(next.excluded ? "businessTab.excludedToast" : "businessTab.includedToast", {
              title: next.title,
            }),
          ),
        onError: (e) =>
          toast.error(e instanceof Error ? e.message : t("businessTab.updateError")),
      },
    );

  /** Fetch this source now — the affordance that moves a row off `pending`. */
  const syncNow = (row: BusinessSourceOut) =>
    sync.mutate(row.id, {
      onSuccess: () => toast.success(t("businessTab.syncStarted", { title: row.title })),
      onError: (e) => toast.error(e instanceof Error ? e.message : t("businessTab.syncError")),
    });

  /**
   * Ask upstream whether this document has moved. Never a fetch.
   *
   * A probe that could not answer is still a success — it records WHY on the
   * row, which is what drops the badge to the age label — so the toast reports
   * the verdict rather than treating "could not check" as an error.
   */
  const checkNow = (row: BusinessSourceOut) =>
    probe.mutate(row.id, {
      onSuccess: (next) =>
        toast.success(
          next.probeError
            ? t("businessTab.probeUnknown", { title: next.title })
            : t(next.stale ? "businessTab.probeStale" : "businessTab.probeFresh", {
                title: next.title,
              }),
        ),
      onError: (e) => toast.error(e instanceof Error ? e.message : t("businessTab.probeError")),
    });

  const confirmDelete = () => {
    const row = confirming;
    if (!row) return;
    remove.mutate(row.id, {
      onSuccess: () => {
        toast.success(t("businessTab.deleted", { title: row.title }));
        setConfirming(null);
      },
      onError: (e) => {
        toast.error(e instanceof Error ? e.message : t("businessTab.deleteError"));
        setConfirming(null);
      },
    });
  };

  // The GUID comes from the route, so this is a brief resolving window rather
  // than an error — say that, instead of rendering an empty state that would
  // claim the project has no sources when we have not asked yet.
  if (!projectGuid) {
    return (
      <div className="rounded-2xl border border-bd2 bg-pop px-6 py-10 text-center text-[13px] text-txt4">
        {t("businessTab.noProject")}
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-3.5">
      <section className="overflow-hidden rounded-2xl border border-bd2 bg-pop">
        <header className="flex flex-wrap items-start gap-x-3 gap-y-2 border-b border-bd3 px-5 py-4">
          <div className="min-w-[240px] flex-1">
            <div className="flex items-center gap-2">
              <BookOpen size={16} className="shrink-0 text-p" strokeWidth={2.2} />
              <h2 className="m-0 text-[15px] font-bold text-txt">{t("businessTab.title")}</h2>
              {rows.length > 0 && (
                <span className="text-[11.5px] font-semibold text-faint">
                  {t("businessTab.sources", { count: rows.length })}
                </span>
              )}
            </div>
            <p className="m-0 mt-1.5 max-w-[62ch] text-[12.5px] leading-relaxed text-txt4">
              {t("businessTab.subtitle")}
            </p>
          </div>
          {rows.length > 0 && (
            <Button
              variant={adding ? "ghost" : "primary"}
              size="sm"
              onClick={() => setAdding(!adding)}
              data-testid="business-add-toggle"
            >
              {adding ? <X size={14} strokeWidth={2.4} /> : <Plus size={14} strokeWidth={2.4} />}
              {adding ? t("businessTab.cancel") : t("businessTab.addSource")}
            </Button>
          )}
        </header>

        {adding && (
          <BusinessSourceForm
            projectGuid={projectGuid}
            onCreated={onCreated}
            onCancel={() => setAdding(false)}
          />
        )}

        {sources.isLoading ? (
          <div className="px-5 py-10 text-center text-[13px] text-txt4">{t("common:loading")}</div>
        ) : rows.length === 0 ? (
          <EmptyState onAdd={() => setAdding(true)} formOpen={adding} />
        ) : (
          <ul className="m-0 flex list-none flex-col p-0" data-testid="business-source-list">
            {rows.map((row) => (
              <SourceRow
                key={row.id}
                row={row}
                busy={update.isPending && update.variables?.id === row.id}
                syncing={row.status === "syncing" || (sync.isPending && sync.variables === row.id)}
                probing={probe.isPending && probe.variables === row.id}
                onToggle={() => toggleExcluded(row)}
                onSync={() => syncNow(row)}
                onProbe={() => checkNow(row)}
                onDelete={() => setConfirming(row)}
              />
            ))}
          </ul>
        )}

        {rows.length > 0 && (
          <p className="m-0 border-t border-bd3 px-5 py-3 text-[11.5px] leading-relaxed text-faint">
            {t("businessTab.pendingNote")}
          </p>
        )}
      </section>

      <BusinessFactsPanel projectGuid={projectGuid} />

      <ConfirmDialog
        open={confirming !== null}
        danger
        loading={remove.isPending}
        title={t("businessTab.deleteTitle")}
        message={t("businessTab.deleteMessage", { title: confirming?.title ?? "" })}
        confirmLabel={t("businessTab.delete")}
        onConfirm={confirmDelete}
        onClose={() => setConfirming(null)}
      />
    </div>
  );
}

/** The empty state says what the tab is FOR, not that it is empty: a QC who has
 *  never seen this tab has no way to guess which documents belong in it. */
function EmptyState({ onAdd, formOpen }: { onAdd: () => void; formOpen: boolean }) {
  const { t } = useTranslation("projects");
  return (
    <div
      className="flex flex-col items-center px-8 py-12 text-center"
      data-testid="business-empty"
    >
      <div className="mb-5 flex h-[68px] w-[68px] items-center justify-center rounded-[20px] bg-pt">
        <BookOpen size={30} className="text-pl" strokeWidth={1.9} />
      </div>
      <h3 className="m-0 mb-2 text-[18px] font-extrabold text-txt">
        {t("businessTab.emptyTitle")}
      </h3>
      <p className="m-0 mb-6 max-w-[54ch] text-[13px] leading-relaxed text-txt4">
        {t("businessTab.emptyBody")}
      </p>
      {!formOpen && (
        <Button variant="primary" size="md" onClick={onAdd} data-testid="business-empty-cta">
          <Plus size={15} strokeWidth={2.3} /> {t("businessTab.emptyCta")}
        </Button>
      )}
    </div>
  );
}


/**
 * Days since an ISO timestamp, or `null` when there is none.
 *
 * Whole days on purpose: the age label is a judgement aid ("34 days ago"), not
 * a clock, and an hours-precise number would suggest a precision the snapshot
 * model does not have.
 */
function daysSince(iso: string | null): number | null {
  if (!iso) return null;
  const ms = Date.now() - new Date(iso).getTime();
  if (Number.isNaN(ms)) return null;
  return Math.max(0, Math.floor(ms / 86_400_000));
}

/**
 * The freshness badge — the honest one.
 *
 * Every branch here corresponds to a `staleness.mode` the SERVER decided
 * (#830). The client never turns "we have not checked" into "up to date", and
 * never turns "we cannot check" into "changed": those two conflations are the
 * entire failure mode the badge exists to avoid, so each mode gets its own
 * wording, its own icon and its own token pair.
 *
 * `title` carries the reason in every uncertain mode, so a user who asks "why
 * does this only show an age?" gets the adapter's own sentence rather than a
 * shrug.
 */
function StalenessBadge({ row }: { row: BusinessSourceOut }) {
  const { t } = useTranslation("projects");
  const { mode, stale, detail } = row.staleness;
  const age = daysSince(row.fetchedAt);

  // Nothing has ever been fetched: there is no snapshot to be stale, and
  // saying anything about freshness here would be noise on top of `pending`.
  if (!row.fetchedAt) return null;

  if (mode === "revision" && stale) {
    return (
      <span
        data-testid="business-staleness"
        data-mode="stale"
        title={t("businessTab.staleness.staleHint")}
        className="flex items-center gap-1 rounded-md bg-warn-tint px-2 py-0.5 text-[10.5px] font-bold text-warn"
      >
        <AlertTriangle size={11} strokeWidth={2.6} />
        {t("businessTab.staleness.stale")}
      </span>
    );
  }
  if (mode === "revision") {
    return (
      <span
        data-testid="business-staleness"
        data-mode="fresh"
        title={t("businessTab.staleness.freshHint", {
          when: row.probedAt ? timeAgo(row.probedAt) : "",
        })}
        className="flex items-center gap-1 rounded-md bg-ok-tint px-2 py-0.5 text-[10.5px] font-bold text-ok"
      >
        <CheckCircle2 size={11} strokeWidth={2.6} />
        {t("businessTab.staleness.fresh")}
      </span>
    );
  }
  if (mode === "unknown") {
    return (
      <span
        data-testid="business-staleness"
        data-mode="unknown"
        title={t("businessTab.staleness.unknownHint")}
        className="flex items-center gap-1 rounded-md bg-neutral-tint px-2 py-0.5 text-[10.5px] font-bold text-neutral"
      >
        <HelpCircle size={11} strokeWidth={2.6} />
        {t("businessTab.staleness.unknown")}
      </span>
    );
  }
  // `age` — the honest fallback. It states the AGE and never a verdict, and
  // the tooltip says which limitation put us here.
  return (
    <span
      data-testid="business-staleness"
      data-mode="age"
      title={detail || t("businessTab.staleness.ageHint")}
      className="flex items-center gap-1 rounded-md bg-neutral-tint px-2 py-0.5 text-[10.5px] font-bold text-neutral"
    >
      <Clock size={11} strokeWidth={2.6} />
      {age === null
        ? t("businessTab.staleness.ageUnknown")
        : t("businessTab.staleness.age", { count: age })}
    </span>
  );
}

function SourceRow({
  row,
  busy,
  syncing,
  probing,
  onToggle,
  onSync,
  onProbe,
  onDelete,
}: {
  row: BusinessSourceOut;
  busy: boolean;
  syncing: boolean;
  probing: boolean;
  onToggle: () => void;
  onSync: () => void;
  onProbe: () => void;
  onDelete: () => void;
}) {
  const { t } = useTranslation("projects");
  const Icon = KIND_ICON[row.kind] ?? FileText;
  const [statusText, statusTint] = STATUS_STYLE[row.status] ?? STATUS_STYLE.pending;

  return (
    <li
      className="flex flex-wrap items-center gap-x-3 gap-y-2 border-b border-bd3 px-5 py-3.5 last:border-b-0"
      data-testid="business-source-row"
      data-source-id={row.id}
    >
      <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-[10px] bg-card2">
        <Icon size={15} className="text-txt4" strokeWidth={2} />
      </span>

      <div className="min-w-[200px] flex-1">
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
          <span
            className={`text-[13px] font-semibold ${row.excluded ? "text-txt4 line-through" : "text-txt"}`}
          >
            {row.title}
          </span>
          <span className="text-[10.5px] font-semibold tracking-wide text-label uppercase">
            {t(`businessTab.kinds.${row.kind}`)}
          </span>
        </div>
        {row.url && (
          <a
            href={row.url}
            target="_blank"
            rel="noreferrer"
            className="mt-0.5 block max-w-[52ch] truncate font-mono text-[11px] text-txt4 hover:text-p"
          >
            {row.url}
          </a>
        )}
        {row.lastError && (
          <p className="m-0 mt-1 max-w-[52ch] text-[11px] leading-snug text-danger">
            {row.lastError}
          </p>
        )}
        {row.excluded && (
          <p className="m-0 mt-1 text-[11px] leading-snug text-faint">
            {t("businessTab.excludedHint")}
          </p>
        )}
        {/* Provenance: exactly what a generated case's "grounded in" list names
            (#830). The hash is truncated for reading, not for identity — the
            full value is on the title attribute. */}
        {row.fetchedAt && (
          <p
            className="m-0 mt-1 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[11px] text-faint"
            data-testid="business-provenance"
          >
            <span>{t("businessTab.fetchedAt", { when: timeAgo(row.fetchedAt) })}</span>
            {row.contentHash && (
              <span className="font-mono" title={row.contentHash}>
                {t("businessTab.hash", { hash: row.contentHash.slice(0, 12) })}
              </span>
            )}
            {row.docCount > 0 && (
              <span>{t("businessTab.docCount", { count: row.docCount })}</span>
            )}
          </p>
        )}
      </div>

      <div className="flex items-center gap-2">
        <StalenessBadge row={row} />
        <span
          className={`rounded-md px-2 py-0.5 text-[10.5px] font-bold ${statusText} ${statusTint}`}
          data-testid="business-source-status"
        >
          {row.excluded ? t("businessTab.excluded") : t(`businessTab.status.${row.status}`)}
        </span>
        {row.probeSupported && row.fetchedAt && (
          <button
            onClick={onProbe}
            disabled={probing}
            title={t("businessTab.checkNow")}
            aria-label={t("businessTab.checkNow")}
            data-testid="business-probe"
            className="cursor-pointer rounded-lg p-1.5 text-txt4 hover:bg-card3 hover:text-p disabled:cursor-default disabled:opacity-50"
          >
            <SearchCheck size={15} className={probing ? "animate-pulse" : undefined} />
          </button>
        )}
        {row.kind !== "upload" && (
          <button
            onClick={onSync}
            disabled={syncing}
            title={t("businessTab.syncNow")}
            aria-label={t("businessTab.syncNow")}
            data-testid="business-sync"
            className="cursor-pointer rounded-lg p-1.5 text-txt4 hover:bg-card3 hover:text-p disabled:cursor-default disabled:opacity-50"
          >
            <RefreshCw size={15} className={syncing ? "animate-spin" : undefined} />
          </button>
        )}
        <button
          onClick={onToggle}
          disabled={busy}
          title={t(row.excluded ? "businessTab.include" : "businessTab.exclude")}
          aria-label={t(row.excluded ? "businessTab.include" : "businessTab.exclude")}
          className="cursor-pointer rounded-lg p-1.5 text-txt4 hover:bg-card3 hover:text-txt disabled:opacity-50"
        >
          {row.excluded ? <Eye size={15} /> : <EyeOff size={15} />}
        </button>
        <button
          onClick={onDelete}
          title={t("businessTab.delete")}
          aria-label={t("businessTab.delete")}
          className="cursor-pointer rounded-lg p-1.5 text-txt4 hover:bg-danger-tint hover:text-danger"
        >
          <Trash2 size={15} />
        </button>
      </div>
    </li>
  );
}
