/**
 * The ticket query builder — clause rows over a draft, applied on press (#517).
 *
 * Ported from EmeHub's `components/query/QueryBuilder.tsx` so a user moving
 * between the two apps meets the same tool, and arranged around the same rule.
 *
 * ## Apply on press
 *
 * **Selecting a field, an operator or a value must not run a query.** Every edit
 * lands in the draft; only `Apply` hands it to the caller. The split is made
 * legible rather than left implicit: an unapplied-changes pill, an Apply that
 * enables only when there is something to apply, and Reset.
 *
 * ## Validation
 *
 * An invalid draft **disables Apply and says why, under the offending row**,
 * from the same `validateQuery` the compiler assumes has already passed. Unlike
 * the hub there is no server-side twin to drift from: `compileQuery` only ever
 * runs on a query this validated.
 *
 * ## Where the values come from
 *
 * `GET /tickets/filter-options` — a `SELECT DISTINCT` over the caller's own
 * ticket rows. Not the provider (a mirrored hub connection holds no PAT, #501)
 * and not the hub (its metadata endpoints are hub-audience only). The panel says
 * so, because the tradeoff is real: a value absent from the mirrored set is not
 * offered.
 */

import { Plus } from "lucide-react";
import { useMemo } from "react";
import { useTranslation } from "react-i18next";
import { Button } from "@/components/ui/Button";
import { cn } from "@/lib/cn";
import type { TicketFilterOptions } from "@/types/api";
import { ClauseRow, type FieldOptions } from "./ClauseRow";
import { SavedQueries } from "./SavedQueries";
import {
  countChanges,
  effectiveClauses,
  generalProblems,
  newClause,
  problemsForClause,
  validateQuery,
  type QueryClause,
  type TicketQuery,
} from "./model";
import type { Preset, SavedQuery } from "./queryStore";

/** The endpoint's flat lists, mapped onto the clause fields that consume them. */
export function fieldOptionsFrom(options: TicketFilterOptions | undefined): FieldOptions {
  return {
    workItemType: options?.workItemTypes ?? [],
    state: options?.states ?? [],
    assignee: options?.assignees ?? [],
    areaPath: options?.areaPaths ?? [],
    sprint: options?.sprints ?? [],
    epic: options?.epics ?? [],
    priority: options?.priorities ?? [],
    // Always free text: `q` is a substring match over title and id, not a value
    // drawn from a set.
    title: [],
  };
}

export interface QueryBuilderProps {
  draft: TicketQuery;
  onDraftChange: (query: TicketQuery) => void;
  /** The query currently in force, or null before anything has been applied. */
  applied: TicketQuery | null;
  options: TicketFilterOptions | undefined;
  presets: Preset[];
  saved: SavedQuery[];
  onSave: (name: string) => void;
  onDeleteSaved: (id: string) => void;
  onApply: () => void;
  onReset: () => void;
  /** True while the applied query is running — Apply says so and locks. */
  busy?: boolean;
}

export function QueryBuilder({
  draft,
  onDraftChange,
  applied,
  options,
  presets,
  saved,
  onSave,
  onDeleteSaved,
  onApply,
  onReset,
  busy = false,
}: QueryBuilderProps) {
  const { t } = useTranslation("tickets");
  const problems = useMemo(() => validateQuery(draft), [draft]);
  const general = generalProblems(problems);
  const valid = problems.length === 0;
  const changes = countChanges(draft, applied);
  const canApply = valid && !busy && (applied === null || changes > 0);
  const fieldOptions = useMemo(() => fieldOptionsFrom(options), [options]);

  const setClause = (index: number, clause: QueryClause) =>
    onDraftChange({
      ...draft,
      clauses: draft.clauses.map((c, i) => (i === index ? clause : c)),
    });

  const removeClause = (index: number) =>
    onDraftChange({ ...draft, clauses: draft.clauses.filter((_, i) => i !== index) });

  const addClause = () => onDraftChange({ ...draft, clauses: [...draft.clauses, newClause()] });

  const submit = () => {
    if (canApply) onApply();
  };

  return (
    <div
      data-testid="query-builder"
      className="flex flex-col gap-3.5"
      onKeyDown={(e) => {
        // ⌘↵ / Ctrl+↵ applies. Stopped here so the shell's global shortcuts do
        // not also fire from a panel that has nothing to do with them.
        if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
          e.preventDefault();
          e.stopPropagation();
          submit();
        }
      }}
    >
      <SavedQueries
        presets={presets}
        saved={saved}
        draft={draft}
        onLoad={onDraftChange}
        onSave={onSave}
        onDelete={onDeleteSaved}
        canSave={valid}
      />

      <div className="flex flex-wrap items-center gap-3 border-t border-white/[0.06] pt-3">
        <span className="text-[10.5px] font-bold uppercase tracking-[.11em] text-ink-dim">
          {t("builder.match")}
        </span>
        <div className="flex items-center gap-1 rounded-[11px] border border-white/[0.09] bg-white/[0.04] p-1">
          {(["all", "any"] as const).map((mode) => (
            <button
              key={mode}
              type="button"
              onClick={() => onDraftChange({ ...draft, match: mode })}
              data-on={draft.match === mode}
              className="cursor-pointer rounded-[8px] px-2.5 py-1 text-[11.5px] font-semibold text-ink-dim transition-colors data-[on=true]:bg-[rgba(139,92,246,.22)] data-[on=true]:text-ink"
            >
              {t(`builder.match_${mode}`)}
            </button>
          ))}
        </div>
        <span className="ml-auto max-w-full text-[11.5px] text-ink-dim">
          {t("builder.optionsSource", { count: options?.ticketCount ?? 0 })}
        </span>
      </div>

      <div className="flex flex-col gap-3">
        {draft.clauses.map((clause, index) => (
          <ClauseRow
            key={index}
            clause={clause}
            index={index}
            options={fieldOptions}
            problems={problemsForClause(problems, index)}
            showLabels={index === 0}
            onChange={(next) => setClause(index, next)}
            onRemove={() => removeClause(index)}
            onSubmit={submit}
          />
        ))}
      </div>

      {general.map((problem) => (
        <p key={problem.key} className="m-0 text-[11.5px] text-[#fbbf24]">
          {t(`builder.problem.${problem.key}`, problem.values)}
        </p>
      ))}

      <div>
        <button
          type="button"
          onClick={addClause}
          className="inline-flex cursor-pointer items-center gap-1.5 rounded-[11px] border border-white/[0.09] bg-white/[0.05] px-3 py-2 text-[12.5px] font-semibold text-[#dcdce4] transition-colors hover:bg-white/[0.1]"
        >
          <Plus size={13} strokeWidth={2.4} />
          {t("builder.addCondition")}
        </button>
      </div>

      <div className="flex flex-wrap items-center gap-3 border-t border-white/[0.06] pt-3.5">
        <Button variant="primary" onClick={submit} disabled={!canApply}>
          {busy ? t("builder.running") : t("builder.apply")}
        </Button>
        <Button variant="glass" onClick={onReset} disabled={busy}>
          {t("builder.reset")}
        </Button>

        {/* The draft/applied split, said out loud. Without this line an edit that
            has not been applied looks exactly like one that has. */}
        {changes > 0 && applied !== null && (
          <span className="rounded-[8px] border border-[rgba(251,191,36,.3)] bg-[rgba(251,191,36,.14)] px-2 py-1 text-[11px] font-semibold text-[#fbbf24]">
            {t("builder.unapplied", { count: changes })}
          </span>
        )}

        <span className={cn("min-w-0 flex-1 truncate text-[11.5px] text-ink-dim")}>
          {describeQuery(applied ?? draft, t)}
        </span>
      </div>
    </div>
  );
}

/**
 * The query as a person would say it — `state is any of Done, Blocked · …`.
 *
 * Deliberately lossy: prose for a confirmation line, never something to compile
 * back from.
 */
export function describeQuery(
  query: TicketQuery,
  t: (key: string, values?: Record<string, unknown>) => string,
): string {
  const parts = effectiveClauses(query).map((clause) => {
    const values = clause.values.filter((value) => value.trim() !== "");
    return `${t(`builder.field.${clause.field}`)} ${t(`builder.operator.${clause.operator}`)} ${values.join(", ")}`;
  });
  if (parts.length === 0) return t("builder.describeEverything");
  return parts.join(query.match === "all" ? " · " : ` ${t("builder.or")} `);
}
