import { useState } from "react";
import { useTranslation } from "react-i18next";
import {
  BookOpen,
  EyeOff,
  FileText,
  Github,
  Globe,
  Eye,
  Plus,
  Trash2,
  X,
} from "lucide-react";
import { Button } from "@/components/ui/Button";
import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import { Select } from "@/components/ui/Dropdown";
import { timeAgo } from "@/components/dashboard/runStatus";
import {
  useBusinessSources,
  useCreateBusinessSource,
  useDeleteBusinessSource,
  useUpdateBusinessSource,
} from "@/hooks/queries";
import { toast } from "@/lib/toast";
import type { BusinessSourceKind, BusinessSourceOut } from "@/types/api";

/**
 * Project → **Business Knowledge** tab (#817, epic #813, ADR 0016).
 *
 * The project's DOMAIN grounding — what the product is supposed to do — as a
 * PEER of Project Knowledge rather than a section of it, which is why it is its
 * own tab beside it rather than another panel inside it.
 *
 * ## CRUD only in this slice
 *
 * Nothing here starts a fetch. A registered source stays `pending` until the
 * ingestion pipeline (#818) lands behind exactly this row shape, so the panel
 * says so in as many words (`pendingNote`) instead of leaving the user to read
 * "Not fetched yet" as a stuck job. `excluded` is likewise a context switch, not
 * a soft delete: the snapshot and its provenance survive so an artifact already
 * generated from the source stays attributable.
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
 * ## Local form state
 *
 * Whether the add form is open is `useState`, not a query param: it is a
 * transient composer, not an addressable view, and a `?add=` that reopened a
 * half-typed form on reload would be a URL that lies. Navigation stays in the
 * path (the tab itself is `projects/:projectGuid/business`), per CLAUDE.md.
 */

/** The v1 kinds, in the order the picker offers them. `notion` is v2 (#832). */
const KINDS: BusinessSourceKind[] = ["url", "github_md", "ado_wiki", "upload"];

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
  const create = useCreateBusinessSource(projectGuid);
  const update = useUpdateBusinessSource(projectGuid);
  const remove = useDeleteBusinessSource(projectGuid);

  const [adding, setAdding] = useState(false);
  const [kind, setKind] = useState<BusinessSourceKind>("url");
  const [title, setTitle] = useState("");
  const [url, setUrl] = useState("");
  const [confirming, setConfirming] = useState<BusinessSourceOut | null>(null);

  const rows = sources.data ?? [];

  const closeForm = () => {
    setAdding(false);
    setTitle("");
    setUrl("");
  };

  const submit = () => {
    // An upload carries no URL at all (the server drops one that is sent), and
    // its title is its identity — so that is the field the form requires.
    const body =
      kind === "upload"
        ? { kind, title: title.trim() }
        : { kind, title: title.trim() || undefined, url: url.trim() };
    create.mutate(body, {
      onSuccess: (row) => {
        toast.success(t("businessTab.form.created", { title: row.title }));
        closeForm();
      },
      onError: (e) =>
        toast.error(e instanceof Error ? e.message : t("businessTab.form.error")),
    });
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

  const canSubmit =
    kind === "upload" ? title.trim().length > 0 : url.trim().length > 0;

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
              onClick={() => (adding ? closeForm() : setAdding(true))}
              data-testid="business-add-toggle"
            >
              {adding ? <X size={14} strokeWidth={2.4} /> : <Plus size={14} strokeWidth={2.4} />}
              {adding ? t("businessTab.cancel") : t("businessTab.addSource")}
            </Button>
          )}
        </header>

        {adding && (
          <div
            className="flex flex-col gap-3 border-b border-bd3 px-5 py-4"
            data-testid="business-add-form"
          >
            <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
              <Field label={t("businessTab.form.kind")}>
                <Select
                  value={kind}
                  options={KINDS.map((k) => ({
                    value: k,
                    label: t(`businessTab.kinds.${k}`),
                  }))}
                  placeholder={t("businessTab.form.kindPlaceholder")}
                  allowClear={false}
                  fullWidth
                  onChange={(v) => v && setKind(v as BusinessSourceKind)}
                />
              </Field>
              <Field label={t("businessTab.form.title")}>
                <input
                  className={inputCls}
                  value={title}
                  onChange={(e) => setTitle(e.target.value)}
                  placeholder={t("businessTab.form.titlePlaceholder")}
                  data-testid="business-title-input"
                />
              </Field>
            </div>
            {kind === "upload" ? (
              <p className="m-0 text-[11.5px] leading-relaxed text-faint">
                {t("businessTab.form.titleHint")}
              </p>
            ) : (
              <Field label={t("businessTab.form.url")}>
                <input
                  className={inputCls}
                  value={url}
                  onChange={(e) => setUrl(e.target.value)}
                  placeholder={t("businessTab.form.urlPlaceholder")}
                  data-testid="business-url-input"
                />
              </Field>
            )}
            <div className="flex justify-end gap-2">
              <Button variant="ghost" size="sm" onClick={closeForm}>
                {t("businessTab.cancel")}
              </Button>
              <Button
                variant="primary"
                size="sm"
                disabled={!canSubmit || create.isPending}
                onClick={submit}
                data-testid="business-submit"
              >
                {create.isPending
                  ? t("businessTab.form.submitting")
                  : t("businessTab.form.submit")}
              </Button>
            </div>
          </div>
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
                onToggle={() => toggleExcluded(row)}
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

const inputCls =
  "w-full rounded-[10px] border border-bd2 bg-card px-3 py-2 text-[13px] text-txt3 outline-none placeholder:text-label focus:border-p";

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="flex flex-col gap-1.5">
      <span className="text-[11.5px] font-semibold text-txt4">{label}</span>
      {children}
    </label>
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

function SourceRow({
  row,
  busy,
  onToggle,
  onDelete,
}: {
  row: BusinessSourceOut;
  busy: boolean;
  onToggle: () => void;
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
      </div>

      <div className="flex items-center gap-2">
        {row.docCount > 0 && (
          <span className="text-[11px] text-faint">
            {t("businessTab.docCount", { count: row.docCount })}
          </span>
        )}
        {row.fetchedAt && (
          <span className="text-[11px] text-faint">{timeAgo(row.fetchedAt)}</span>
        )}
        <span
          className={`rounded-md px-2 py-0.5 text-[10.5px] font-bold ${statusText} ${statusTint}`}
          data-testid="business-source-status"
        >
          {row.excluded ? t("businessTab.excluded") : t(`businessTab.status.${row.status}`)}
        </span>
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
