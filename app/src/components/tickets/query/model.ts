/**
 * The ticket query model — clauses, the capability matrix, validation, and the
 * compiler down to `GET /tickets` params (#517).
 *
 * Ported from EmeHub's `app/src/data/ticketQuery.ts`, with one deliberate and
 * large difference: **the matrix describes what OUR endpoint can run**, not what
 * a provider can. EmeHub compiles a query to WIQL / JQL / a GitHub search string,
 * so it can offer `is not`, date ranges and an area-path tree. `GET /tickets`
 * takes a flat set of equality-ish params:
 *
 *     status · assignee · sprint · areaPath · states · workItemTypes ·
 *     priority · epic · q · connectionId · providerKind · page · pageSize
 *
 * A field or operator outside that set would have to be **silently dropped** at
 * compile time — and a dropped condition returns *more* tickets than were asked
 * for, which is the worst way for a filter to fail. So the matrix below is
 * narrow on purpose, and every clause it permits compiles to a real parameter.
 *
 * ## What a query is
 *
 * A flat list of clauses plus one global `match`. No nesting, no per-clause
 * conjunction — the same limit the hub carries, for the same reason.
 */

import type { TicketFilters } from "@/types/api";

export type ClauseField =
  | "workItemType"
  | "state"
  | "assignee"
  | "areaPath"
  | "sprint"
  | "priority"
  | "epic"
  | "title";

export type ClauseOperator = "is" | "in" | "under" | "contains";

export type MatchMode = "all" | "any";

export interface QueryClause {
  field: ClauseField;
  operator: ClauseOperator;
  values: string[];
}

export interface TicketQuery {
  clauses: QueryClause[];
  /** `all` joins with AND. `any` is only runnable within a single field — see
   * {@link validateQuery}. */
  match: MatchMode;
}

/** The only operator that takes more than one value. */
export const takesList = (operator: ClauseOperator): boolean => operator === "in";

/* ── the capability matrix ───────────────────────────────────────────────── */

/**
 * What each field can be filtered with, and which query param it compiles to.
 *
 * `param` is the single source of truth for the compiler *and* for the
 * "one condition per field" rule: two clauses that write the same non-list
 * param would have the second silently overwrite the first.
 *
 * `list` marks the two params the backend reads as a comma-separated set
 * (`states` → `status IN (…)`, `workItemTypes` → `work_item_type IN (…)`).
 * Those are the only fields where several values can be asked for at once, and
 * therefore the only place an OR exists in this dialect at all.
 */
export const FIELDS: Record<
  ClauseField,
  { operators: ClauseOperator[]; param: keyof TicketFilters; list: boolean }
> = {
  workItemType: { operators: ["is", "in"], param: "workItemTypes", list: true },
  state: { operators: ["is", "in"], param: "states", list: true },
  assignee: { operators: ["is"], param: "assignee", list: false },
  areaPath: { operators: ["under"], param: "areaPath", list: false },
  sprint: { operators: ["is"], param: "sprint", list: false },
  priority: { operators: ["is"], param: "priority", list: false },
  epic: { operators: ["is"], param: "epic", list: false },
  title: { operators: ["contains"], param: "q", list: false },
};

/** The fields to offer, in the order to offer them. */
export const FIELD_ORDER = Object.keys(FIELDS) as ClauseField[];

export const operatorsFor = (field: ClauseField): ClauseOperator[] =>
  FIELDS[field]?.operators ?? [];

/* ── construction ────────────────────────────────────────────────────────── */

export const newClause = (field: ClauseField = "state"): QueryClause => ({
  field,
  operator: operatorsFor(field)[0] ?? "is",
  values: [""],
});

export const emptyQuery = (): TicketQuery => ({ clauses: [newClause()], match: "all" });

const filled = (clause: QueryClause): string[] =>
  clause.values.map((value) => value.trim()).filter((value) => value !== "");

/**
 * Clauses with at least one non-blank value — what actually gets compiled.
 *
 * A half-typed clause must not become `state=''`, which matches nothing and
 * reads as "there is no work" rather than as unfinished input.
 */
export const effectiveClauses = (query: TicketQuery): QueryClause[] =>
  query.clauses.filter((clause) => filled(clause).length > 0);

/**
 * `clause` moved onto `field` — operator reset to the first the matrix allows,
 * values cleared.
 *
 * Keeping the old operator would leave e.g. a `sprint` / `under` pair that
 * validation then has to reject, which is worse than silently picking the sane
 * one.
 */
export const withField = (field: ClauseField): QueryClause => newClause(field);

/** `clause` moved onto `operator`, remapping values across the list boundary. */
export function withOperator(clause: QueryClause, operator: ClauseOperator): QueryClause {
  const wasList = takesList(clause.operator);
  const isList = takesList(operator);
  if (wasList === isList) return { ...clause, operator };
  const values = isList ? filled(clause) : [filled(clause)[0] ?? ""];
  return { ...clause, operator, values: values.length > 0 ? values : [""] };
}

/* ── validation ──────────────────────────────────────────────────────────── */

export interface QueryProblem {
  /** An i18n key under `tickets:builder.problem`. */
  key: string;
  values?: Record<string, string | number>;
  /** Which clause the message belongs to, so the UI prints it under that row. */
  clauseIndex: number | null;
}

/**
 * Every problem with `query`; empty means it can be applied.
 *
 * The interesting rules, both consequences of compiling to flat params:
 *
 * 1. **One condition per single-value field.** `assignee is A` and
 *    `assignee is B` both write `?assignee=`, so the second would win silently.
 *    (`state` / `workItemType` are exempt: they compile to a CSV set, so several
 *    clauses union rather than overwrite.)
 * 2. **`Any` needs one field.** Separate params are ANDed by the backend, full
 *    stop. Within one field, `is any of` already *is* the OR. So `any` across
 *    two different fields is a query we cannot run, and saying so beats
 *    compiling it as AND and returning fewer tickets than were asked for.
 */
export function validateQuery(query: TicketQuery): QueryProblem[] {
  const problems: QueryProblem[] = [];
  const add = (
    key: string,
    clauseIndex: number | null = null,
    values?: Record<string, string | number>,
  ) => problems.push({ key, clauseIndex, values });

  if (query.clauses.length === 0) add("noConditions");

  const seenSingle = new Map<ClauseField, number>();
  query.clauses.forEach((clause, index) => {
    const spec = FIELDS[clause.field];
    if (!spec) {
      add("unknownField", index, { field: String(clause.field) });
      return;
    }
    if (!spec.operators.includes(clause.operator)) {
      add("badOperator", index, { field: clause.field, operator: clause.operator });
      return;
    }
    if (filled(clause).length === 0) {
      add("needsValue", index, { field: clause.field });
    } else if (filled(clause).length !== clause.values.length) {
      add("blankValue", index, { field: clause.field });
    }
    if (!takesList(clause.operator) && clause.values.length > 1) {
      add("oneValueOnly", index, { field: clause.field });
    }
    if (!spec.list && filled(clause).length > 0) {
      const first = seenSingle.get(clause.field);
      if (first !== undefined) add("fieldTwice", index, { field: clause.field });
      else seenSingle.set(clause.field, index);
    }
  });

  const fieldsUsed = new Set(effectiveClauses(query).map((clause) => clause.field));
  if (query.match === "any" && fieldsUsed.size > 1) add("anyNeedsOneField");

  return problems;
}

export const problemsForClause = (problems: QueryProblem[], index: number): QueryProblem[] =>
  problems.filter((problem) => problem.clauseIndex === index);

export const generalProblems = (problems: QueryProblem[]): QueryProblem[] =>
  problems.filter((problem) => problem.clauseIndex === null);

/* ── the compiler ────────────────────────────────────────────────────────── */

/**
 * `query` as `GET /tickets` parameters.
 *
 * Only ever called on a query {@link validateQuery} accepted, so it can assume
 * one clause per single-value field. List fields union across clauses, so
 * `state is Blocked` + `state is any of Done, Ready` becomes
 * `states=Blocked,Done,Ready` — the union, which is what "match all" means once
 * every condition names the same column.
 *
 * Scoping params (`connectionId`, `providerKind`, `page`, `pageSize`) are the
 * screen's, not the query's, and are merged in by the caller.
 */
export function compileQuery(query: TicketQuery): TicketFilters {
  const out: TicketFilters = {};
  const lists = new Map<keyof TicketFilters, string[]>();

  for (const clause of effectiveClauses(query)) {
    const spec = FIELDS[clause.field];
    if (!spec) continue;
    const values = filled(clause);
    if (spec.list) {
      const current = lists.get(spec.param) ?? [];
      for (const value of values) if (!current.includes(value)) current.push(value);
      lists.set(spec.param, current);
    } else {
      // A single-value param: validation has already refused a second clause on
      // the same field, so first-wins here is a formality rather than a policy.
      if (out[spec.param] === undefined) {
        (out as Record<string, string>)[spec.param] = values[0];
      }
    }
  }

  for (const [param, values] of lists) {
    (out as Record<string, string>)[param] = values.join(",");
  }
  return out;
}

/** How many clauses differ between the draft and what was last applied. */
export function countChanges(draft: TicketQuery, applied: TicketQuery | null): number {
  if (applied === null) return effectiveClauses(draft).length;
  if (JSON.stringify(draft) === JSON.stringify(applied)) return 0;
  const before = applied.clauses.map((clause) => JSON.stringify(clause));
  const after = draft.clauses.map((clause) => JSON.stringify(clause));
  const changed =
    after.filter((clause) => !before.includes(clause)).length +
    before.filter((clause) => !after.includes(clause)).length;
  // A match flip changes nothing clause-by-clause but changes the query.
  return Math.max(changed, draft.match !== applied.match ? 1 : 0);
}
