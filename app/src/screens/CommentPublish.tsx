import { motion } from "framer-motion";
import { Eye, FileText, Pencil, RefreshCw, Send, Sparkles } from "lucide-react";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { toast } from "@/lib/toast";
import { Button } from "@/components/ui/Button";
import { GlassCard } from "@/components/ui/GlassCard";
import { MarkdownLite } from "@/components/ui/MarkdownLite";
import { PipelineRail } from "@/components/ui/PipelineRail";
import { Pill, providerGlyph } from "@/components/ui/badges";
import { EmptyState, ErrorState, Spinner } from "@/components/ui/misc";
import { useParams } from "react-router-dom";
import { useComments, useCommentMutations, useRun } from "@/hooks/queries";
import type { PublishStatus, TicketCommentOut } from "@/types/api";

const PUBLISH_STATUS: Record<PublishStatus, [string, PublishStatus, string]> = {
  draft: ["#a0a0b2", "draft", "rgba(255,255,255,.06)"],
  publishing: ["#fbbf24", "publishing", "rgba(251,191,36,.14)"],
  published: ["#6ee7b7", "published", "rgba(16,185,129,.14)"],
  failed: ["#fb7185", "failed", "rgba(244,63,94,.14)"],
};

/** Ticket Comments / Publish — prepares AI-summarized result comments per ticket
 * and publishes them back to the provider work item. Design: Q-Agent.dc.html 489-506. */
export function CommentPublish() {
  const { t } = useTranslation("pipeline");
  const { t: tCommon } = useTranslation("common");
  const runId = Number(useParams().runId);
  const { data: run } = useRun(runId);
  const { data: comments, isLoading, isError, refetch } = useComments(runId);
  const { prepare, publishOne, publishAll, retry, edit, regenerate } =
    useCommentMutations(runId);

  const anyFailed = (comments ?? []).some((c) => c.status === "failed");

  return (
    <div className="px-1 pb-10 pt-0.5">
      <div className="mb-3.5 flex flex-col gap-3 md:flex-row md:items-end md:justify-between">
        <div>
          <div className="mb-[5px] text-[13px] font-medium text-ink-dim">
            {run?.code} &middot; {t("publish.header.subtitle")}
          </div>
          <h1 className="m-0 text-[24px] font-black tracking-tight md:text-[28px]">{t("publish.header.title")}</h1>
        </div>
        <div className="flex flex-col gap-2.5 md:flex-row">
          {anyFailed && (
            <Button variant="danger" onClick={() => retry.mutate()} disabled={retry.isPending} className="w-full md:w-auto">
              {t("publish.retryFailed")}
            </Button>
          )}
          <Button
            variant="primary"
            onClick={() =>
              publishAll.mutate([], {
                onSuccess: () => toast.success(t("publish.toast.publishingResults")),
                onError: (e) => toast.error(e instanceof Error ? e.message : t("publish.toast.publishFailed")),
              })
            }
            disabled={publishAll.isPending || !comments?.length}
            className="w-full md:w-auto"
          >
            {publishAll.isPending ? (
              <>
                <Spinner size={14} className="border-white/40 border-t-white" /> {t("publish.status.publishing")}
              </>
            ) : (
              <>
                <Send size={15} strokeWidth={2.2} /> {t("publish.publishAll")}
              </>
            )}
          </Button>
        </div>
      </div>

      <div className="mb-4 hidden md:block">
        <PipelineRail stage={6} />
      </div>

      {isLoading ? (
        <div className="flex flex-col gap-3">
          {[0, 1, 2].map((i) => (
            <div key={i} className="glass h-[132px] animate-pulse rounded-[18px]" />
          ))}
        </div>
      ) : isError ? (
        // A failed load is NOT an empty list (#491).
        <ErrorState
          title={tCommon("loadFailed.title")}
          body={tCommon("loadFailed.body")}
          retryLabel={tCommon("loadFailed.retry")}
          onRetry={() => void refetch()}
        />
      ) : !comments?.length ? (
        <EmptyState
          icon={<FileText size={30} color="#7a7a8c" strokeWidth={1.8} />}
          title={t("publish.empty.title")}
          body={t("publish.empty.body")}
          action={
            <div className="flex flex-col items-center gap-2.5">
              <Button
                variant="primary"
                size="lg"
                className="w-full md:w-auto"
                onClick={() =>
                  prepare.mutate(undefined, {
                    onError: (e) => toast.error(e instanceof Error ? e.message : t("publish.toast.prepareFailed")),
                  })
                }
                disabled={prepare.isPending}
              >
                {prepare.isPending ? (
                  <>
                    <Spinner size={14} /> {t("publish.preparing")}
                  </>
                ) : (
                  <>
                    <Sparkles size={16} /> {t("publish.prepareComments")}
                  </>
                )}
              </Button>
              {prepare.isPending && (
                <span className="text-[12.5px] text-ink-dim">{t("publish.summarizing")}</span>
              )}
            </div>
          }
        />
      ) : (
        <div className="flex flex-col gap-3">
          {comments.map((c, i) => (
            <PublishCard
              key={c.id}
              comment={c}
              index={i}
              onPublish={() =>
                publishOne.mutate(c.id, {
                  onError: (e) => toast.error(e instanceof Error ? e.message : t("publish.toast.publishFailed")),
                })
              }
              publishing={publishOne.isPending && (publishOne.variables as number) === c.id}
              onRegenerate={() =>
                regenerate.mutate(c.id, {
                  onSuccess: () => toast.success(t("publish.toast.regenerated")),
                  onError: (e) =>
                    toast.error(e instanceof Error ? e.message : t("publish.toast.regenerateFailed")),
                })
              }
              regenerating={regenerate.isPending && (regenerate.variables as number) === c.id}
              onSave={(body, done) =>
                edit.mutate(
                  { commentId: c.id, body: { body } },
                  {
                    onSuccess: () => {
                      toast.success(t("publish.toast.saved"));
                      done();
                    },
                    onError: (e) => toast.error(e instanceof Error ? e.message : t("publish.toast.saveFailed")),
                  },
                )
              }
              saving={edit.isPending && (edit.variables as { commentId: number })?.commentId === c.id}
            />
          ))}
        </div>
      )}
    </div>
  );
}

function PublishCard({
  comment,
  index,
  onPublish,
  onRegenerate,
  regenerating,
  publishing,
  onSave,
  saving,
}: {
  comment: TicketCommentOut;
  index: number;
  onPublish: () => void;
  onRegenerate: () => void;
  /** A regeneration for THIS comment is in flight (it makes a Claude call). */
  regenerating: boolean;
  publishing: boolean;
  /** Persist an edited body; call `done` once the save succeeds to leave edit mode. */
  onSave: (body: string, done: () => void) => void;
  saving: boolean;
}) {
  const { t } = useTranslation("pipeline");
  const [glyph, glyphBg] = providerGlyph[comment.providerKind] ?? ["?", "#6b7280"];
  const [color, statusKey, bg] = PUBLISH_STATUS[comment.status] ?? PUBLISH_STATUS.draft;

  // Inline editor: `editing` swaps the rendered preview for a raw-markdown
  // textarea; `previewing` toggles a live rendered preview of the current draft.
  const [editing, setEditing] = useState(false);
  const [previewing, setPreviewing] = useState(false);
  const [draft, setDraft] = useState(comment.body);
  // Re-sync the draft if the comment changes underneath us (e.g. re-prepared)
  // while not actively editing.
  useEffect(() => {
    if (!editing) setDraft(comment.body);
  }, [comment.body, editing]);

  const startEdit = () => {
    setDraft(comment.body);
    setPreviewing(false);
    setEditing(true);
  };
  const cancelEdit = () => {
    setEditing(false);
    setPreviewing(false);
  };
  const save = () => onSave(draft, () => setEditing(false));

  return (
    <motion.div
      initial={{ opacity: 0, y: 16 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.4, delay: Math.min(index * 0.05, 0.3), ease: "easeOut" }}
    >
      <GlassCard className="overflow-hidden">
        <div className="flex flex-wrap items-center gap-3 border-b border-white/[0.06] p-3 md:p-[15px_18px]">
          <div
            className="flex h-[30px] w-[30px] shrink-0 items-center justify-center rounded-[9px] text-[13px] font-black text-white"
            style={{ background: glyphBg }}
          >
            {glyph}
          </div>
          <span className="font-mono text-[12px] font-semibold text-violet">
            {comment.ticketExternalId}
          </span>
          <span className="flex-1" />
          <Pill color={color} bg={bg}>
            {publishing ? t("publish.status.publishing") : t(`publish.status.${statusKey}`)}
          </Pill>
          {editing ? (
            <button
              type="button"
              onClick={() => setPreviewing((p) => !p)}
              className="flex items-center gap-1.5 rounded-[9px] border border-white/[0.1] bg-white/[0.05] px-2.5 py-1.5 text-[11.5px] font-semibold text-ink-soft transition-colors hover:bg-white/[0.1]"
            >
              {previewing ? <Pencil size={13} strokeWidth={2} /> : <Eye size={13} strokeWidth={2} />}
              {previewing ? t("publish.editor.write") : t("publish.editor.preview")}
            </button>
          ) : (
            <button
              type="button"
              onClick={startEdit}
              className="flex items-center gap-1.5 rounded-[9px] border border-white/[0.1] bg-white/[0.05] px-2.5 py-1.5 text-[11.5px] font-semibold text-ink-soft transition-colors hover:bg-white/[0.1]"
            >
              <Pencil size={13} strokeWidth={2} />
              {t("publish.editor.edit")}
            </button>
          )}
          {/* Regenerate (#700). Once drafts exist, Prepare is gone — it lives in the
              empty state — so a comment written before the evidence manifest, or
              before a case was re-run, had no way back to a current one. Refused for
              a PUBLISHED comment: it is already on the work item, and re-publishing a
              rebuilt one posts a second comment rather than replacing the first. */}
          <button
            type="button"
            onClick={onRegenerate}
            disabled={regenerating || editing || comment.status === "published"}
            title={
              comment.status === "published"
                ? t("publish.regeneratePublishedHint")
                : t("publish.regenerateHint")
            }
            className="flex items-center gap-1.5 rounded-[9px] border border-white/[0.1] bg-white/[0.05] px-2.5 py-1.5 text-[11.5px] font-semibold text-ink-soft transition-colors hover:bg-white/[0.1] disabled:cursor-not-allowed disabled:opacity-40"
            data-testid="comment-regenerate"
          >
            {regenerating ? (
              <Spinner size={13} />
            ) : (
              <RefreshCw size={13} strokeWidth={2} />
            )}
            {regenerating ? t("publish.regenerating") : t("publish.regenerate")}
          </button>
          <Button variant="glass" size="sm" onClick={onPublish} disabled={publishing || editing || regenerating}>
            {t("publish.publish")}
          </Button>
        </div>

        {editing ? (
          <div className="flex flex-col gap-2.5 p-3 md:p-[14px_18px]">
            {previewing ? (
              <div className="min-h-[160px] rounded-[11px] border border-white/[0.08] bg-white/[0.02] p-3">
                {draft.trim() ? (
                  <MarkdownLite text={draft} />
                ) : (
                  <span className="text-[12.5px] text-ink-dim">{t("publish.editor.empty")}</span>
                )}
              </div>
            ) : (
              <textarea
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                spellCheck={false}
                className="min-h-[200px] w-full resize-y rounded-[11px] border border-white/[0.1] bg-white/[0.03] p-3 font-mono text-[12.5px] leading-[1.6] text-ink outline-none focus:border-[rgba(139,92,246,.5)]"
              />
            )}
            <div className="flex items-center justify-end gap-2">
              <Button variant="glass" size="sm" onClick={cancelEdit} disabled={saving}>
                {t("publish.editor.cancel")}
              </Button>
              <Button variant="primary" size="sm" onClick={save} disabled={saving || draft === comment.body}>
                {saving ? (
                  <>
                    <Spinner size={13} className="border-white/40 border-t-white" /> {t("publish.editor.saving")}
                  </>
                ) : (
                  t("publish.editor.save")
                )}
              </Button>
            </div>
          </div>
        ) : (
          <div className="p-3 md:p-[14px_18px]">
            <MarkdownLite text={comment.body} />
          </div>
        )}

        {comment.errorMessage && (
          <div className="px-[18px] pb-2 text-[12px] text-danger-soft">{comment.errorMessage}</div>
        )}
        <div className="flex flex-wrap gap-2 p-[0_18px_14px]">
          {/* The real files this comment will attach on publish (#696). There used to
              be two hardcoded `evidence.zip` / `trace.zip` chips here with nothing
              behind either — a fake attachment is worse than none, because a reviewer
              goes looking for it in the work item. */}
          {comment.attachments.map((file) => (
            <AttachmentChip
              key={`${file.caseCode}-${file.filename}`}
              label={`${file.caseCode} · ${file.filename}`}
              hint={humanSize(file.sizeBytes)}
            />
          ))}
          {comment.status === "draft" && comment.attachments.length > 0 && (
            <span className="rounded-[9px] border border-white/[0.09] bg-white/[0.05] px-2.5 py-1.5 text-[11.5px] text-faint">
              {t("publish.attachOnPublish", { count: comment.attachments.length })}
            </span>
          )}
          {comment.targetStatus && (
            <span className="rounded-[9px] border border-white/[0.09] bg-white/[0.05] px-2.5 py-1.5 text-[11.5px] text-ink-soft">
              → {comment.targetStatus}
            </span>
          )}
        </div>
      </GlassCard>
    </motion.div>
  );
}

function AttachmentChip({ label, hint }: { label: string; hint?: string }) {
  return (
    <span className="flex items-center gap-1.5 rounded-[9px] border border-white/[0.09] bg-white/[0.05] px-2.5 py-1.5 text-[11.5px] text-ink-soft">
      <FileText size={13} color="#a78bfa" strokeWidth={2} />
      {label}
      {hint && <span className="text-faint">{hint}</span>}
    </span>
  );
}

/** Sizes a reviewer can read — the chip's whole job is "is this the big video or the
 * small screenshot?". */
function humanSize(bytes: number): string {
  if (bytes <= 0) return "";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}
