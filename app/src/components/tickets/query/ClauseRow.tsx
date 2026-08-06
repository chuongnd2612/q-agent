/**
 * One condition: a field picker, an operator picker, a value control, remove.
 *
 * Ported from EmeHub's `components/query/ClauseRow.tsx`, rebuilt on this repo's
 * `Select` / `MultiSelect` (which already portal their panel to `document.body`
 * with fixed positioning, per the frontend convention) instead of the hub's
 * `Dropdown`.
 *
 * **Nothing in here issues a request.** Every handler edits the draft and stops
 * — selecting a field must not run a query. See `QueryBuilder` for why.
 *
 * The labels and the allowed operators come from `./model`, never from a second
 * table here: one definition, and the same one that decides whether Apply is
 * enabled.
 */

import { X } from "lucide-react";
import { useTranslation } from "react-i18next";
import { MultiSelect, Select } from "@/components/ui/Dropdown";
import { cn } from "@/lib/cn";
import {
  FIELD_ORDER,
  FIELDS,
  operatorsFor,
  takesList,
  withField,
  withOperator,
  type ClauseField,
  type ClauseOperator,
  type QueryClause,
  type QueryProblem,
} from "./model";

/** The picker values for one field, or `[]` meaning "let them type". */
export interface FieldOptions {
  workItemType: string[];
  state: string[];
  assignee: string[];
  areaPath: string[];
  sprint: string[];
  priority: string[];
  epic: string[];
  title: string[];
}

export interface ClauseRowProps {
  clause: QueryClause;
  index: number;
  options: FieldOptions;
  problems: QueryProblem[];
  /** True on the first row only — the column labels are printed once. */
  showLabels: boolean;
  onChange: (clause: QueryClause) => void;
  onRemove: () => void;
  /** Enter in a free-text field applies; a filter box ignoring Enter feels broken. */
  onSubmit: () => void;
}

export function ClauseRow({
  clause,
  index,
  options,
  problems,
  showLabels,
  onChange,
  onRemove,
  onSubmit,
}: ClauseRowProps) {
  const { t } = useTranslation("tickets");
  const values = options[clause.field] ?? [];
  const list = takesList(clause.operator);
  // Where our rows carry no source for a field, the control degrades to a plain
  // input rather than to an empty dropdown that looks broken. `title` is always
  // free text (it is a substring match, not a value from a set); the others fall
  // back only when the caller has no ticket carrying that field yet.
  const freeText = clause.field === "title" || values.length === 0;
  const help = t(`builder.help.${clause.field}`, { defaultValue: "" });
  const asOptions = values.map((value) => ({ value, label: value }));

  const setValues = (next: string[]) => onChange({ ...clause, values: next });

  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex flex-wrap items-start gap-2">
        <Column
          testId={`clause-${index}-field`}
          label={showLabels ? t("builder.colField") : undefined}
          className="w-[168px]"
        >
          <Select
            value={clause.field}
            allowClear={false}
            fullWidth
            options={FIELD_ORDER.map((field) => ({ value: field, label: t(`builder.field.${field}`) }))}
            placeholder={t("builder.colField")}
            onChange={(field) => field && onChange(withField(field as ClauseField))}
          />
        </Column>

        <Column
          testId={`clause-${index}-operator`}
          label={showLabels ? t("builder.colIs") : undefined}
          className="w-[150px]"
        >
          <Select
            value={clause.operator}
            allowClear={false}
            fullWidth
            options={operatorsFor(clause.field).map((op) => ({
              value: op,
              label: t(`builder.operator.${op}`),
            }))}
            placeholder={t("builder.colIs")}
            onChange={(op) => op && onChange(withOperator(clause, op as ClauseOperator))}
          />
        </Column>

        <Column
          testId={`clause-${index}-value`}
          label={showLabels ? t("builder.colValue") : undefined}
          className="min-w-[220px] flex-1"
        >
          {list && !freeText ? (
            <MultiSelect
              values={clause.values.filter(Boolean)}
              options={asOptions}
              placeholder={t("builder.pickValues")}
              fullWidth
              onChange={setValues}
            />
          ) : list ? (
            <FreeTextList values={clause.values.filter(Boolean)} onChange={setValues} />
          ) : freeText ? (
            <input
              value={clause.values[0] ?? ""}
              onChange={(e) => setValues([e.target.value])}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  onSubmit();
                }
              }}
              placeholder={t("builder.typeValue")}
              aria-label={t("builder.valueAria", { field: t(`builder.field.${clause.field}`) })}
              className="h-9 w-full rounded-[11px] border border-white/[0.09] bg-white/[0.05] px-3 text-[12.5px] font-semibold text-ink outline-none placeholder:font-normal placeholder:text-ink-dim focus:border-[rgba(139,92,246,.45)]"
            />
          ) : (
            <Select
              value={clause.values[0] || null}
              options={asOptions}
              placeholder={t("builder.pickValue")}
              fullWidth
              onChange={(value) => setValues([value ?? ""])}
            />
          )}
        </Column>

        <div className={cn("flex", showLabels && "pt-[19px]")}>
          <button
            type="button"
            onClick={onRemove}
            aria-label={t("builder.removeCondition", { field: t(`builder.field.${clause.field}`) })}
            className="flex h-9 w-9 cursor-pointer items-center justify-center rounded-[11px] border border-white/[0.09] bg-white/[0.05] text-ink-dim transition-colors hover:border-red-500/40 hover:bg-red-500/10 hover:text-red-400"
          >
            <X size={14} strokeWidth={2.4} />
          </button>
        </div>
      </div>

      {/* The hint earns its place only where there is nothing to pick from —
          nobody knows a title is matched as a substring unless it is said. */}
      {help && freeText && <p className="m-0 pl-0.5 text-[11.5px] text-ink-dim">{help}</p>}

      {problems.map((problem) => (
        <p key={problem.key + problem.clauseIndex} className="m-0 pl-0.5 text-[11.5px] text-[#fbbf24]">
          {t(`builder.problem.${problem.key}`, {
            ...problem.values,
            field: problem.values?.field
              ? t(`builder.field.${problem.values.field}`)
              : undefined,
            operator: problem.values?.operator
              ? t(`builder.operator.${problem.values.operator}`)
              : undefined,
          })}
        </p>
      ))}
    </div>
  );
}

function Column({
  label,
  className,
  testId,
  children,
}: {
  label?: string;
  className?: string;
  /** Stable hook for Playwright — the styling classes are not a contract. */
  testId: string;
  children: React.ReactNode;
}) {
  return (
    <div data-testid={testId} className={cn("flex flex-col gap-[5px]", className)}>
      {label && (
        <span className="text-[9.5px] font-bold uppercase tracking-[.11em] text-ink-dim">
          {label}
        </span>
      )}
      {children}
    </div>
  );
}

/**
 * The `is any of` control when the field has no options to pick from: chosen
 * values as removable chips, plus an input that adds one on Enter.
 *
 * Only reachable for a list field whose column is empty across every one of the
 * caller's tickets — rare, but the alternative is a disabled multi-select that
 * looks like a bug.
 */
function FreeTextList({
  values,
  onChange,
}: {
  values: string[];
  onChange: (values: string[]) => void;
}) {
  const { t } = useTranslation("tickets");
  const add = (raw: string) => {
    const trimmed = raw.trim();
    if (trimmed && !values.includes(trimmed)) onChange([...values, trimmed]);
  };
  return (
    <div className="flex flex-col gap-2">
      {values.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {values.map((value) => (
            <button
              key={value}
              type="button"
              onClick={() => onChange(values.filter((v) => v !== value))}
              aria-label={t("builder.removeValue", { value })}
              className="cursor-pointer rounded-[8px] border border-[rgba(139,92,246,.35)] bg-[rgba(139,92,246,.18)] px-2 py-1 text-[11.5px] font-semibold text-ink"
            >
              {value} ×
            </button>
          ))}
        </div>
      )}
      <input
        placeholder={t("builder.valueThenEnter")}
        aria-label={t("builder.addValue")}
        onKeyDown={(e) => {
          if (e.key !== "Enter") return;
          e.preventDefault();
          add(e.currentTarget.value);
          e.currentTarget.value = "";
        }}
        className="h-9 w-full rounded-[11px] border border-white/[0.09] bg-white/[0.05] px-3 text-[12.5px] font-semibold text-ink outline-none placeholder:font-normal placeholder:text-ink-dim focus:border-[rgba(139,92,246,.45)]"
      />
    </div>
  );
}
