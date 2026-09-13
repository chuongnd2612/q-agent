import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { Check, Eye, EyeOff, PencilLine, Plus, Sparkles, X } from "lucide-react";
import { Button } from "@/components/ui/Button";
import {
  useBusinessFacts,
  useCorrectBusinessFact,
  useCreateBusinessFact,
  useUpdateBusinessFact,
} from "@/hooks/queries";
import { toast } from "@/lib/toast";
import type { BusinessFactCategory, BusinessFactOut } from "@/types/api";
import { SkeletonList, useSkeleton } from "@/components/ui/Skeleton";

/**
 * Project → Business Knowledge → **the fact overlay** (#827, epic #813, ADR 0016 §5).
 *
 * ## Three affordances, not a blob editor
 *
 * Ingested content is immutable. This panel never edits a distilled fact in
 * place; it writes rows *around* it:
 *
 * - **Correct** — a new pinned row supersedes the ingested one, and the
 *   original stays in the list, **struck through**, labelled *superseded by
 *   your correction*. That is the whole point: a disagreement between the team
 *   and the source document is a fact about the project, and an in-place edit
 *   would delete it. The strike-through is the feature, not decoration.
 * - **Add** — a fact the documents never stated. Deliberately *not* pinned:
 *   pinning marks a row as overriding a source, and an addition overrides
 *   nothing. It survives a re-sync either way.
 * - **Exclude / restore** — out of context, still on the row, one click back.
 *   The "that wiki page is wrong but I am not deleting it" case, per fact.
 *
 * ## Precedence is the server's, and it is shown rather than explained
 *
 * The list arrives in ADR 0016 §5 order (pinned corrections, then additions,
 * then ingested facts) and is rendered in exactly that order, so the ladder is
 * legible from the page instead of from a paragraph nobody reads. The per-row
 * origin chip names the layer.
 *
 * ## Opaque surface, tokens only
 *
 * Same reasoning as `BusinessTab`: text-heavy at 11-13px over the shell's
 * animated background, so `bg-pop` (opaque in both modes) and appearance tokens
 * throughout — no `rgba()`, no hex, so the panel follows `data-mode` and
 * `data-accent` (#783/#784, and the light-mode regression #844).
 *
 * ## Local editor state
 *
 * Which row is being corrected is `useState`: a half-typed correction is a
 * transient composer, not an addressable view, and a `?correct=` that reopened
 * it on reload would be a URL that lies.
 */

/** Category → its label key. Listed so the i18n gate can see every key used. */
const CATEGORIES: BusinessFactCategory[] = [
  "glossary",
  "rule",
  "flow",
  "actor",
  "constraint",
  "acceptance-norm",
];

/** What the composer is doing. `null` = closed. */
type Editor =
  | { mode: "add" }
  | { mode: "correct"; fact: BusinessFactOut }
  | { mode: "edit"; fact: BusinessFactOut };

/* The facts list is unpaginated; four rows is one screenful of the panel
   rather than a count picked by eye (#750). */
const BUSINESS_FACT_ROWS = 4;

export function BusinessFactsPanel({ projectGuid }: { projectGuid: string | null }) {
  const { t } = useTranslation("projects");
  const facts = useBusinessFacts(projectGuid);
  const create = useCreateBusinessFact(projectGuid);
  const correct = useCorrectBusinessFact(projectGuid);
  const update = useUpdateBusinessFact(projectGuid);

  const [editor, setEditor] = useState<Editor | null>(null);

  const rows = useMemo(() => facts.data ?? [], [facts.data]);
  const showSkeleton = useSkeleton(facts.isLoading);
  const busy = create.isPending || correct.isPending || update.isPending;

  /** Which correction superseded a given fact, so the row can name it. */
  const supersedingIds = useMemo(
    () => new Set(rows.map((row) => row.supersededBy).filter((id): id is number => !!id)),
    [rows],
  );

  const toggleExcluded = (row: BusinessFactOut) =>
    update.mutate(
      { id: row.id, body: { excluded: !row.excluded } },
      {
        onSuccess: (next) =>
          toast.success(
            t(
              next.excluded
                ? "businessTab.facts.excludedToast"
                : "businessTab.facts.includedToast",
              { term: next.term },
            ),
          ),
        onError: (e) =>
          toast.error(e instanceof Error ? e.message : t("businessTab.facts.saveError")),
      },
    );

  const submit = (draft: {
    category: BusinessFactCategory;
    term: string;
    statement: string;
    detail: string;
  }) => {
    const onError = (e: unknown) =>
      toast.error(e instanceof Error ? e.message : t("businessTab.facts.saveError"));
    if (editor?.mode === "correct") {
      correct.mutate(
        { id: editor.fact.id, body: { statement: draft.statement, detail: draft.detail } },
        {
          onSuccess: () => {
            toast.success(t("businessTab.facts.correctedToast", { term: editor.fact.term }));
            setEditor(null);
          },
          onError,
        },
      );
      return;
    }
    if (editor?.mode === "edit") {
      update.mutate(
        { id: editor.fact.id, body: { statement: draft.statement, detail: draft.detail } },
        {
          onSuccess: () => {
            toast.success(t("businessTab.facts.savedToast", { term: editor.fact.term }));
            setEditor(null);
          },
          onError,
        },
      );
      return;
    }
    create.mutate(draft, {
      onSuccess: () => {
        toast.success(t("businessTab.facts.addedToast", { term: draft.term }));
        setEditor(null);
      },
      onError,
    });
  };

  if (!projectGuid) return null;

  return (
    <section
      className="overflow-hidden rounded-2xl border border-bd2 bg-pop"
      data-testid="business-facts-panel"
    >
      <header className="flex flex-wrap items-start gap-x-3 gap-y-2 border-b border-bd3 px-5 py-4">
        <div className="min-w-[240px] flex-1">
          <div className="flex items-center gap-2">
            <Sparkles size={16} className="shrink-0 text-p" strokeWidth={2.2} />
            <h2 className="m-0 text-[15px] font-bold text-txt">
              {t("businessTab.facts.title")}
            </h2>
            {rows.length > 0 && (
              <span className="text-[11.5px] font-semibold text-faint">
                {t("businessTab.facts.count", { count: rows.length })}
              </span>
            )}
          </div>
          <p className="m-0 mt-1.5 max-w-[62ch] text-[12.5px] leading-relaxed text-txt4">
            {t("businessTab.facts.subtitle")}
          </p>
        </div>
        <Button
          variant={editor?.mode === "add" ? "ghost" : "primary"}
          size="sm"
          onClick={() => setEditor(editor?.mode === "add" ? null : { mode: "add" })}
          data-testid="business-fact-add-toggle"
        >
          {editor?.mode === "add" ? (
            <X size={14} strokeWidth={2.4} />
          ) : (
            <Plus size={14} strokeWidth={2.4} />
          )}
          {editor?.mode === "add" ? t("businessTab.cancel") : t("businessTab.facts.addFact")}
        </Button>
      </header>

      {editor !== null && (
        <FactEditor
          key={editor.mode === "add" ? "add" : `${editor.mode}-${editor.fact.id}`}
          editor={editor}
          busy={busy}
          onSubmit={submit}
          onCancel={() => setEditor(null)}
        />
      )}

      {showSkeleton ? (
        <SkeletonList count={BUSINESS_FACT_ROWS} rowHeight={56} className="px-5 py-4" />
      ) : rows.length === 0 ? (
        <p
          className="m-0 px-5 py-10 text-center text-[13px] leading-relaxed text-txt4"
          data-testid="business-facts-empty"
        >
          {t("businessTab.facts.empty")}
        </p>
      ) : (
        <ul className="m-0 flex list-none flex-col p-0" data-testid="business-fact-list">
          {rows.map((row) => (
            <FactRow
              key={row.id}
              row={row}
              isSuperseding={supersedingIds.has(row.id)}
              busy={busy}
              onCorrect={() => setEditor({ mode: "correct", fact: row })}
              onEdit={() => setEditor({ mode: "edit", fact: row })}
              onToggle={() => toggleExcluded(row)}
            />
          ))}
        </ul>
      )}
    </section>
  );
}

/** Origin → [label key, text token, tint token] — the ADR 0016 §5 layer, named. */
function originStyle(row: BusinessFactOut): [string, string, string] {
  if (row.origin === "manual" && row.pinned) {
    return ["businessTab.facts.originCorrection", "text-p", "bg-pt"];
  }
  if (row.origin === "manual") {
    return ["businessTab.facts.originAddition", "text-info", "bg-info-tint"];
  }
  return ["businessTab.facts.originIngested", "text-txt4", "bg-card2"];
}

function FactRow({
  row,
  isSuperseding,
  busy,
  onCorrect,
  onEdit,
  onToggle,
}: {
  row: BusinessFactOut;
  isSuperseding: boolean;
  busy: boolean;
  onCorrect: () => void;
  onEdit: () => void;
  onToggle: () => void;
}) {
  const { t } = useTranslation("projects");
  const [originKey, originText, originTint] = originStyle(row);
  const superseded = row.supersededBy !== null;

  return (
    <li
      className="flex flex-wrap items-start gap-x-3 gap-y-2 border-b border-bd3 px-5 py-3.5 last:border-b-0"
      data-testid="business-fact-row"
      data-fact-id={row.id}
      data-superseded={superseded ? "1" : undefined}
    >
      <div className="min-w-[240px] flex-1">
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
          <span className="rounded-md bg-card2 px-1.5 py-0.5 text-[10px] font-bold tracking-wide text-label uppercase">
            {t(`businessTab.facts.categories.${row.category}`)}
          </span>
          <span
            className={`text-[13px] font-semibold ${superseded ? "text-txt4" : "text-txt"}`}
          >
            {row.term}
          </span>
          <span
            className={`rounded-md px-1.5 py-0.5 text-[10px] font-bold ${originText} ${originTint}`}
            data-testid="business-fact-origin"
          >
            {t(originKey)}
          </span>
          {row.revision > 1 && (
            <span className="text-[10.5px] text-faint">
              {t("businessTab.facts.revision", { count: row.revision })}
            </span>
          )}
        </div>

        <p
          className={`m-0 mt-1 max-w-[76ch] text-[12.5px] leading-relaxed ${
            superseded ? "text-txt4 line-through decoration-danger/70" : "text-txt2"
          }`}
          data-testid="business-fact-statement"
        >
          {row.statement}
        </p>

        {row.detail && !superseded && (
          <p className="m-0 mt-1 max-w-[76ch] text-[11.5px] leading-snug text-faint">
            {row.detail}
          </p>
        )}

        {superseded && (
          <p
            className="m-0 mt-1 text-[11px] font-semibold leading-snug text-p"
            data-testid="business-fact-superseded"
          >
            {t("businessTab.facts.supersededBy")}
          </p>
        )}
        {row.excluded && (
          <p className="m-0 mt-1 text-[11px] leading-snug text-faint">
            {t("businessTab.facts.excludedHint")}
          </p>
        )}
      </div>

      <div className="flex items-center gap-2">
        {row.excluded && (
          <span className="rounded-md bg-neutral-tint px-2 py-0.5 text-[10.5px] font-bold text-neutral">
            {t("businessTab.excluded")}
          </span>
        )}
        {isSuperseding && (
          <span className="rounded-md bg-pt px-2 py-0.5 text-[10.5px] font-bold text-p">
            {t("businessTab.facts.winning")}
          </span>
        )}
        {row.origin === "manual" && !superseded && (
          <button
            onClick={onEdit}
            disabled={busy}
            title={t("businessTab.facts.edit")}
            aria-label={t("businessTab.facts.edit")}
            data-testid="business-fact-edit"
            className="cursor-pointer rounded-lg p-1.5 text-txt4 hover:bg-card3 hover:text-txt disabled:opacity-50"
          >
            <PencilLine size={15} />
          </button>
        )}
        {!superseded && (
          <button
            onClick={onCorrect}
            disabled={busy}
            title={t("businessTab.facts.correct")}
            aria-label={t("businessTab.facts.correct")}
            data-testid="business-fact-correct"
            className="cursor-pointer rounded-lg p-1.5 text-txt4 hover:bg-pt hover:text-p disabled:opacity-50"
          >
            <Check size={15} />
          </button>
        )}
        <button
          onClick={onToggle}
          disabled={busy}
          title={t(row.excluded ? "businessTab.include" : "businessTab.exclude")}
          aria-label={t(row.excluded ? "businessTab.include" : "businessTab.exclude")}
          data-testid="business-fact-exclude"
          className="cursor-pointer rounded-lg p-1.5 text-txt4 hover:bg-card3 hover:text-txt disabled:opacity-50"
        >
          {row.excluded ? <Eye size={15} /> : <EyeOff size={15} />}
        </button>
      </div>
    </li>
  );
}

/**
 * One composer for all three writes.
 *
 * A correction and an edit take no `category`/`term`: a correction inherits both
 * from the fact it supersedes (changing the term would make it collide with a
 * *different* distilled fact on the next re-sync — an addition wearing a
 * correction's clothes), and an edit is about the sentence, not the subject.
 */
function FactEditor({
  editor,
  busy,
  onSubmit,
  onCancel,
}: {
  editor: Editor;
  busy: boolean;
  onSubmit: (draft: {
    category: BusinessFactCategory;
    term: string;
    statement: string;
    detail: string;
  }) => void;
  onCancel: () => void;
}) {
  const { t } = useTranslation("projects");
  const existing = editor.mode === "add" ? null : editor.fact;
  const [category, setCategory] = useState<BusinessFactCategory>(existing?.category ?? "rule");
  const [term, setTerm] = useState(existing?.term ?? "");
  const [statement, setStatement] = useState(
    editor.mode === "edit" ? (existing?.statement ?? "") : "",
  );
  const [detail, setDetail] = useState(editor.mode === "edit" ? (existing?.detail ?? "") : "");

  const canSubmit = term.trim().length > 0 && statement.trim().length > 0 && !busy;

  const field =
    "w-full rounded-lg border border-bd2 bg-field px-3 py-2 text-[12.5px] text-txt placeholder:text-faint focus:border-p focus:outline-none";

  return (
    <form
      className="flex flex-col gap-2.5 border-b border-bd3 bg-card px-5 py-4"
      data-testid="business-fact-editor"
      onSubmit={(e) => {
        e.preventDefault();
        if (!canSubmit) return;
        onSubmit({
          category,
          term: term.trim(),
          statement: statement.trim(),
          detail: detail.trim(),
        });
      }}
    >
      <p className="m-0 max-w-[70ch] text-[12px] leading-relaxed text-txt4">
        {editor.mode === "correct"
          ? t("businessTab.facts.correctHint", { term: existing?.term ?? "" })
          : editor.mode === "edit"
            ? t("businessTab.facts.editHint")
            : t("businessTab.facts.addHint")}
      </p>

      {editor.mode === "correct" && existing && (
        <p className="m-0 max-w-[76ch] text-[12px] leading-relaxed text-txt4 line-through decoration-danger/70">
          {existing.statement}
        </p>
      )}

      {editor.mode === "add" && (
        <div className="flex flex-wrap gap-2">
          <select
            value={category}
            onChange={(e) => setCategory(e.target.value as BusinessFactCategory)}
            aria-label={t("businessTab.facts.categoryLabel")}
            data-testid="business-fact-category"
            className={`${field} max-w-[200px]`}
          >
            {CATEGORIES.map((value) => (
              <option key={value} value={value}>
                {t(`businessTab.facts.categories.${value}`)}
              </option>
            ))}
          </select>
          <input
            value={term}
            onChange={(e) => setTerm(e.target.value)}
            placeholder={t("businessTab.facts.termPlaceholder")}
            aria-label={t("businessTab.facts.termLabel")}
            data-testid="business-fact-term"
            className={`${field} max-w-[300px] flex-1`}
          />
        </div>
      )}

      <textarea
        value={statement}
        onChange={(e) => setStatement(e.target.value)}
        rows={2}
        placeholder={t("businessTab.facts.statementPlaceholder")}
        aria-label={t("businessTab.facts.statementLabel")}
        data-testid="business-fact-statement-input"
        className={field}
      />
      <input
        value={detail}
        onChange={(e) => setDetail(e.target.value)}
        placeholder={t("businessTab.facts.detailPlaceholder")}
        aria-label={t("businessTab.facts.detailLabel")}
        data-testid="business-fact-detail-input"
        className={field}
      />

      <div className="flex items-center gap-2">
        <Button type="submit" variant="primary" size="sm" disabled={!canSubmit}>
          {editor.mode === "correct"
            ? t("businessTab.facts.saveCorrection")
            : t("businessTab.facts.save")}
        </Button>
        <Button type="button" variant="ghost" size="sm" onClick={onCancel}>
          {t("businessTab.cancel")}
        </Button>
      </div>
    </form>
  );
}
