/**
 * Saved and preset ticket queries (#517).
 *
 * Ported in shape from EmeHub's `data/savedQueries.ts`, but **not** in storage:
 * the hub persists these in `/ticket-queries`, and #517 is explicit that this
 * slice adds no new filtering backend beyond `GET /tickets/filter-options`. So a
 * user's own saved queries live in `localStorage`, per browser. The UI says so
 * rather than letting someone assume they follow them to another machine.
 *
 * ## Presets are built from the caller's own data, not shipped as constants
 *
 * A shipped preset like `state is Blocked` is a promise that "Blocked" is a
 * state some ticket has. Offer it where it isn't and the user clicks it, gets
 * nothing, and reads that as "there is no work" rather than as our mistake —
 * the very failure `GET /tickets/filter-options` exists to prevent. So a preset
 * is offered **only when every value it names is present in the live options**.
 */

import type { TicketFilterOptions } from "@/types/api";
import type { QueryClause, TicketQuery } from "./model";

const STORAGE_KEY = "qagent.ticketQueries.v1";

export interface SavedQuery {
  id: string;
  /** Preset rows carry an i18n key here instead; see {@link builtIn}. */
  name: string;
  query: TicketQuery;
  /** A derived preset: loadable, never deletable (there is nothing to delete). */
  builtIn: boolean;
}

/* ── the user's own, in localStorage ─────────────────────────────────────── */

/** Never throws: a corrupt or unavailable store yields an empty list, because a
 * saved-query strip is a convenience and must not take the screen down. */
export function loadSavedQueries(): SavedQuery[] {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return [];
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.filter(
      (entry): entry is SavedQuery =>
        !!entry &&
        typeof entry === "object" &&
        typeof (entry as SavedQuery).id === "string" &&
        typeof (entry as SavedQuery).name === "string" &&
        Array.isArray((entry as SavedQuery).query?.clauses),
    );
  } catch {
    return [];
  }
}

function write(queries: SavedQuery[]): SavedQuery[] {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(queries));
  } catch {
    /* storage disabled / full — the in-memory list still reflects the action */
  }
  return queries;
}

/** Saves under `name`, replacing any existing query of the same name. */
export function saveQuery(name: string, query: TicketQuery): SavedQuery[] {
  const trimmed = name.trim();
  const kept = loadSavedQueries().filter((saved) => saved.name !== trimmed);
  return write([
    ...kept,
    { id: `q-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`, name: trimmed, query, builtIn: false },
  ]);
}

export function deleteSavedQuery(id: string): SavedQuery[] {
  return write(loadSavedQueries().filter((saved) => saved.id !== id));
}

/* ── presets, derived from the live options ──────────────────────────────── */

/** A preset's identity — the i18n key under `tickets:builder.presets`. */
export type PresetKey = "assignedToMe" | "openBugs" | "highPriority" | "readyForQa";

export interface Preset {
  key: PresetKey;
  query: TicketQuery;
}

/**
 * The presets that can actually return something, given `options`.
 *
 * `userName` is the signed-in user's display name; "assigned to me" is dropped
 * entirely when no ticket is assigned to that exact string, rather than offered
 * as a button that always comes back empty.
 */
export function presetsFor(options: TicketFilterOptions | undefined, userName: string): Preset[] {
  if (!options) return [];
  const out: Preset[] = [];

  if (userName && options.assignees.includes(userName)) {
    out.push({
      key: "assignedToMe",
      query: { match: "all", clauses: [{ field: "assignee", operator: "is", values: [userName] }] },
    });
  }

  if (options.workItemTypes.includes("Bug")) {
    // "Open" is every state the data has except the terminal one, so the preset
    // stays right on a provider whose workflow we have never seen.
    const open = options.states.filter((state) => state.toLowerCase() !== "done");
    const clauses: QueryClause[] = [
      { field: "workItemType", operator: "is", values: ["Bug"] },
    ];
    if (open.length > 0 && open.length < options.states.length) {
      clauses.push({ field: "state", operator: "in", values: open });
    }
    out.push({ key: "openBugs", query: { match: "all", clauses } });
  }

  if (options.priorities.includes("High")) {
    out.push({
      key: "highPriority",
      query: { match: "all", clauses: [{ field: "priority", operator: "is", values: ["High"] }] },
    });
  }

  if (options.states.includes("Ready for QA")) {
    out.push({
      key: "readyForQa",
      query: {
        match: "all",
        clauses: [{ field: "state", operator: "is", values: ["Ready for QA"] }],
      },
    });
  }

  return out;
}
