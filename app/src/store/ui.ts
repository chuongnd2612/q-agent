/**
 * UI-only client state (Zustand). Server state lives in TanStack Query and
 * navigation lives in the URL (react-router, see ADR 0003) — this store holds
 * only ephemeral UI state: modal/panel open-state, selections, search/filter and
 * in-progress edit drafts.
 */

import { create } from "zustand";
import type { TestDatum, TestStep } from "@/types/api";

export type TicketFilter = "all" | "ready" | "mine" | "sprint";
export type RunFilter = "all" | "active" | "review" | "completed" | "failed";
export type ProjectTab = "overview" | "knowledge" | "tickets" | "runs" | "settings";
export type EvidenceTab = "screenshot" | "video" | "trace" | "console" | "network";
export type AnnotationTool = "cursor" | "rectangle" | "arrow" | "highlight" | "circle" | "text";

export interface CaseDraft {
  title: string;
  precondition: string;
  steps: TestStep[];
  testData: TestDatum[];
}

interface UIState {
  // Project Knowledge build overlay (cosmetic step animation over the real build)
  knowledgeBuilding: boolean;
  buildProjectName: string;
  knowledgeStep: number;
  startKnowledgeBuild: (name: string) => void;
  tickKnowledgeStep: (max: number) => void;
  endKnowledgeBuild: () => void;

  // command palette
  paletteOpen: boolean;
  paletteQuery: string;
  openPalette: () => void;
  closePalette: () => void;
  togglePalette: () => void;
  setPaletteQuery: (q: string) => void;

  // mobile navigation drawer (replaces both sidebars below the `md` breakpoint)
  drawerOpen: boolean;
  openDrawer: () => void;
  closeDrawer: () => void;

  // AI chat panel (Automation screen — edit the selected spec conversationally)
  chatOpen: boolean;
  openChat: () => void;
  closeChat: () => void;
  toggleChat: () => void;

  // tickets page
  selected: Record<string, boolean>;
  ticketSearch: string;
  ticketFilter: TicketFilter;
  /** The work-item connection scoping the ticket list, metadata, sprints + sync
   * (ADR 0006). Null until Tickets.tsx resolves a default. */
  ticketConnectionId: number | null;
  /** The real sprint chosen in the picker (name + provider-native path/id). */
  selectedSprint: { name: string; path: string } | null;
  /** Additional ADO query filters. */
  areaPath: string | null;
  states: string[];
  workItemTypes: string[];
  ticketPriority: string | null;
  /** Jira epic key. */
  ticketEpic: string | null;
  /** 1-based current page of the Tickets list. */
  ticketPage: number;
  toggleSelected: (id: string) => void;
  setSelected: (ids: string[]) => void;
  clearSelected: () => void;
  setTicketSearch: (q: string) => void;
  setTicketFilter: (f: TicketFilter) => void;
  setTicketConnectionId: (id: number | null) => void;
  setSelectedSprint: (s: { name: string; path: string } | null) => void;
  setAreaPath: (p: string | null) => void;
  setStates: (s: string[]) => void;
  setWorkItemTypes: (t: string[]) => void;
  setTicketPriority: (p: string | null) => void;
  setTicketEpic: (e: string | null) => void;
  setTicketPage: (p: number) => void;

  // runs page — status filter tab + multi-select for bulk actions
  runFilter: RunFilter;
  runSel: Record<number, boolean>;
  setRunFilter: (f: RunFilter) => void;
  toggleRunSel: (id: number) => void;
  clearRunSel: () => void;

  // create-run modal
  createRunOpen: boolean;
  runScope: "single" | "selected" | "assigned" | "sprint";
  runBrowser: string;
  runEnv: string;
  runWorkers: number;
  runRetry: number;
  openCreateRun: () => void;
  closeCreateRun: () => void;
  setRunField: <K extends keyof RunFormFields>(key: K, value: RunFormFields[K]) => void;

  // review
  expandedCase: number | null;
  editingCase: number | null;
  draft: CaseDraft | null;
  reviewSel: Record<number, boolean>;
  toggleReviewSel: (caseId: number) => void;
  clearReviewSel: () => void;
  toggleCase: (caseId: number) => void;
  startEdit: (caseId: number, draft: CaseDraft) => void;
  updateDraft: (patch: Partial<CaseDraft>) => void;
  cancelEdit: () => void;

  // evidence
  evidenceTab: EvidenceTab;
  tool: AnnotationTool;
  setEvidenceTab: (t: EvidenceTab) => void;
  setTool: (t: AnnotationTool) => void;
}

interface RunFormFields {
  runScope: UIState["runScope"];
  runBrowser: string;
  runEnv: string;
  runWorkers: number;
  runRetry: number;
}

export const useUI = create<UIState>((set) => ({
  knowledgeBuilding: false,
  buildProjectName: "",
  knowledgeStep: 0,
  startKnowledgeBuild: (name) =>
    set({ knowledgeBuilding: true, buildProjectName: name, knowledgeStep: 0 }),
  tickKnowledgeStep: (max) => set((s) => ({ knowledgeStep: Math.min(s.knowledgeStep + 1, max) })),
  endKnowledgeBuild: () => set({ knowledgeBuilding: false, knowledgeStep: 0 }),

  paletteOpen: false,
  paletteQuery: "",
  openPalette: () => set({ paletteOpen: true, paletteQuery: "" }),
  closePalette: () => set({ paletteOpen: false }),
  togglePalette: () => set((s) => ({ paletteOpen: !s.paletteOpen, paletteQuery: "" })),
  setPaletteQuery: (q) => set({ paletteQuery: q }),

  drawerOpen: false,
  openDrawer: () => set({ drawerOpen: true }),
  closeDrawer: () => set({ drawerOpen: false }),

  chatOpen: false,
  openChat: () => set({ chatOpen: true }),
  closeChat: () => set({ chatOpen: false }),
  toggleChat: () => set((s) => ({ chatOpen: !s.chatOpen })),

  selected: {},
  ticketSearch: "",
  ticketFilter: "all",
  ticketConnectionId: null,
  selectedSprint: null,
  areaPath: null,
  states: [],
  workItemTypes: [],
  ticketPriority: null,
  ticketEpic: null,
  ticketPage: 1,
  toggleSelected: (id) => set((s) => ({ selected: { ...s.selected, [id]: !s.selected[id] } })),
  setSelected: (ids) => set({ selected: Object.fromEntries(ids.map((id) => [id, true])) }),
  clearSelected: () => set({ selected: {} }),
  setTicketSearch: (q) => set({ ticketSearch: q, ticketPage: 1 }),
  setTicketFilter: (f) => set({ ticketFilter: f, ticketPage: 1 }),
  setTicketConnectionId: (id) => set({ ticketConnectionId: id, ticketPage: 1 }),
  setSelectedSprint: (s) => set({ selectedSprint: s, ticketPage: 1 }),
  setAreaPath: (p) => set({ areaPath: p, ticketPage: 1 }),
  setStates: (s) => set({ states: s, ticketPage: 1 }),
  setWorkItemTypes: (t) => set({ workItemTypes: t, ticketPage: 1 }),
  setTicketPriority: (p) => set({ ticketPriority: p, ticketPage: 1 }),
  setTicketEpic: (e) => set({ ticketEpic: e, ticketPage: 1 }),
  setTicketPage: (p) => set({ ticketPage: p }),

  runFilter: "all",
  runSel: {},
  setRunFilter: (f) => set({ runFilter: f }),
  toggleRunSel: (id) => set((s) => ({ runSel: { ...s.runSel, [id]: !s.runSel[id] } })),
  clearRunSel: () => set({ runSel: {} }),

  createRunOpen: false,
  runScope: "selected",
  runBrowser: "chromium",
  runEnv: "",
  runWorkers: 4,
  runRetry: 2,
  openCreateRun: () => set({ createRunOpen: true }),
  closeCreateRun: () => set({ createRunOpen: false }),
  setRunField: (key, value) => set({ [key]: value } as Partial<UIState>),

  expandedCase: null,
  editingCase: null,
  draft: null,
  reviewSel: {},
  toggleReviewSel: (caseId) =>
    set((s) => ({ reviewSel: { ...s.reviewSel, [caseId]: !s.reviewSel[caseId] } })),
  clearReviewSel: () => set({ reviewSel: {} }),
  toggleCase: (caseId) => set((s) => ({ expandedCase: s.expandedCase === caseId ? null : caseId })),
  startEdit: (caseId, draft) => set({ editingCase: caseId, expandedCase: caseId, draft }),
  updateDraft: (patch) => set((s) => (s.draft ? { draft: { ...s.draft, ...patch } } : {})),
  cancelEdit: () => set({ editingCase: null, draft: null }),

  evidenceTab: "screenshot",
  tool: "cursor",
  setEvidenceTab: (t) => set({ evidenceTab: t }),
  setTool: (t) => set({ tool: t }),
}));
