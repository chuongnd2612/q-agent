import { AnimatePresence, motion } from "framer-motion";
import { createPortal } from "react-dom";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";
import {
  Check,
  ChevronLeft,
  ChevronRight,
  Plus,
  RefreshCw,
  Search,
  SlidersHorizontal,
  Trash2,
  X,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { Button } from "@/components/ui/Button";
import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import { DropdownShell, MultiSelect, Select } from "@/components/ui/Dropdown";
import { StatusBadge, priorityColor, providerGlyph } from "@/components/ui/badges";
import { EmptyState, ErrorState } from "@/components/ui/misc";
import { PROVIDER_META, PROVIDER_ORDER } from "@/components/settings/providerMeta";
import { SyncTicketsModal } from "@/components/tickets/SyncTicketsModal";
import { QueryBuilder, describeQuery } from "@/components/tickets/query/QueryBuilder";
import {
  compileQuery,
  emptyQuery,
  validateQuery,
  type TicketQuery,
} from "@/components/tickets/query/model";
import {
  deleteSavedQuery,
  loadSavedQueries,
  presetsFor,
  saveQuery,
  type SavedQuery,
} from "@/components/tickets/query/queryStore";
import {
  useConnectionSprints,
  useConnectionWorkItemMetadata,
  useDeleteTicket,
  useDeleteTickets,
  useProviders,
  useTicketFilterOptions,
  useTickets,
} from "@/hooks/queries";
import { toast } from "@/lib/toast";
import { useAuth } from "@/store/auth";
import { useUI } from "@/store/ui";
import type { ConnectionOut, ProviderKind, TicketFilters, TicketOut } from "@/types/api";

const PRIORITY_OPTIONS = ["High", "Medium", "Low"].map((p) => ({ value: p, label: p }));

const PAGE_SIZE = 10;

function initials(name: string): string {
  return name
    .split(" ")
    .map((p) => p[0])
    .filter(Boolean)
    .slice(0, 2)
    .join("")
    .toUpperCase();
}

/** Primary left-most filter-bar pill: provider glyph + "Provider · connection".
 * Dropdown lists every work-item connection, grouped by provider. */
function ConnectionSelect({
  groups,
  value,
  onChange,
}: {
  groups: { kind: ProviderKind; connections: ConnectionOut[] }[];
  value: number | null;
  onChange: (id: number) => void;
}) {
  const { t } = useTranslation("tickets");
  const selected = groups.flatMap((g) => g.connections).find((c) => c.id === value) ?? null;
  const meta = selected ? PROVIDER_META[selected.kind] : null;

  const label = selected && meta ? (
    <span className="flex min-w-0 items-center gap-2">
      <span
        className="flex h-5 w-5 shrink-0 items-center justify-center rounded-[6px] text-[10.5px] font-black"
        style={{ background: meta.color, color: meta.glyphColor }}
      >
        {meta.glyph}
      </span>
      <span className="truncate">
        {meta.name} &middot; {selected.name}
      </span>
    </span>
  ) : (
    t("selectConnection")
  );

  return (
    <DropdownShell active={!!selected} label={label} minWidth={240}>
      {(close) => (
        <>
          {groups.length === 0 && (
            <div className="px-3 py-4 text-center text-[12px] text-ink-dim">
              {t("noWorkItemConnections")}
            </div>
          )}
          {groups.map((g) => (
            <div key={g.kind} className="mb-1 last:mb-0">
              <div className="px-2.5 pt-2 pb-1 text-[10.5px] font-bold uppercase tracking-wide text-ink-dim">
                {PROVIDER_META[g.kind].name}
              </div>
              {g.connections.map((c) => {
                const on = c.id === value;
                return (
                  <button
                    key={c.id}
                    type="button"
                    onClick={() => {
                      onChange(c.id);
                      close();
                    }}
                    className="flex w-full cursor-pointer items-center gap-2.5 rounded-[10px] px-2.5 py-2 text-left text-[13px] hover:bg-white/[0.06] data-[on=true]:bg-[rgba(139,92,246,.16)]"
                    data-on={on}
                  >
                    <span className="flex h-4 w-4 shrink-0 items-center justify-center">
                      {on && <Check size={13} className="text-violet" strokeWidth={3} />}
                    </span>
                    <span className="min-w-0 flex-1 truncate">{c.name}</span>
                  </button>
                );
              })}
            </div>
          ))}
        </>
      )}
    </DropdownShell>
  );
}

export function Tickets() {
  const { t } = useTranslation("tickets");
  const { t: tCommon } = useTranslation("common");
  const ticketSearch = useUI((s) => s.ticketSearch);
  const setTicketSearch = useUI((s) => s.setTicketSearch);
  const selected = useUI((s) => s.selected);
  const toggleSelected = useUI((s) => s.toggleSelected);
  const setSelected = useUI((s) => s.setSelected);
  const clearSelected = useUI((s) => s.clearSelected);
  const openCreateRun = useUI((s) => s.openCreateRun);
  const navigate = useNavigate();
  const selectedSprint = useUI((s) => s.selectedSprint);
  const setSelectedSprint = useUI((s) => s.setSelectedSprint);
  const areaPath = useUI((s) => s.areaPath);
  const setAreaPath = useUI((s) => s.setAreaPath);
  const states = useUI((s) => s.states);
  const setStates = useUI((s) => s.setStates);
  const workItemTypes = useUI((s) => s.workItemTypes);
  const setWorkItemTypes = useUI((s) => s.setWorkItemTypes);
  const ticketPriority = useUI((s) => s.ticketPriority);
  const setTicketPriority = useUI((s) => s.setTicketPriority);
  const ticketEpic = useUI((s) => s.ticketEpic);
  const setTicketEpic = useUI((s) => s.setTicketEpic);
  const ticketPage = useUI((s) => s.ticketPage);
  const setTicketPage = useUI((s) => s.setTicketPage);
  const ticketConnectionId = useUI((s) => s.ticketConnectionId);
  const setTicketConnectionId = useUI((s) => s.setTicketConnectionId);

  // Connection scoping the ticket list, metadata, sprints + sync (ADR 0006).
  // Options are every connection with the work-item capability (ado/jira);
  // default to the first connected one, else the first available.
  const { data: providers } = useProviders();
  const workItemConnections = useMemo(
    () =>
      (providers ?? [])
        .flatMap((g) => g.connections)
        .filter((c) => c.categories.includes("work_item")),
    [providers],
  );
  const connectionGroups = useMemo(
    () =>
      PROVIDER_ORDER.map((kind) => ({
        kind,
        connections: workItemConnections.filter((c) => c.kind === kind),
      })).filter((g) => g.connections.length > 0),
    [workItemConnections],
  );
  const defaultConnId =
    workItemConnections.find((c) => c.connected)?.id ?? workItemConnections[0]?.id ?? null;
  useEffect(() => {
    if (ticketConnectionId == null && defaultConnId != null) {
      setTicketConnectionId(defaultConnId);
    }
  }, [ticketConnectionId, defaultConnId, setTicketConnectionId]);
  const connectionId = ticketConnectionId ?? defaultConnId;
  const selectedConn = workItemConnections.find((c) => c.id === connectionId) ?? null;
  const isJira = selectedConn?.kind === "jira";
  const isAdo = selectedConn?.kind === "ado";
  const { data: sprints } = useConnectionSprints(connectionId);
  const { data: metadata } = useConnectionWorkItemMetadata(connectionId);

  // "Assigned to me" resolves against the authenticated user (ADR 0007).
  const user = useAuth((s) => s.user);
  const userName = user ? `${user.firstName ?? ""} ${user.lastName ?? ""}`.trim() : "";

  // The query builder (#517): dropdown values read off the caller's own rows,
  // plus whether these tickets are EmeHub's to manage. No hub token and no
  // provider call, so it resolves with the hub down and on a mirrored connection
  // that holds no PAT — the two situations it exists for.
  const { data: filterOptions } = useTicketFilterOptions(connectionId, selectedConn?.kind ?? null);
  const hubManaged = filterOptions?.hubManaged === true;
  const [builderOpen, setBuilderOpen] = useState(false);
  const [draft, setDraft] = useState<TicketQuery>(emptyQuery);
  const [appliedQuery, setAppliedQuery] = useState<TicketQuery | null>(null);
  const [savedQueries, setSavedQueries] = useState<SavedQuery[]>(loadSavedQueries);
  const presets = useMemo(() => presetsFor(filterOptions, userName), [filterOptions, userName]);

  // Combine every active filter into the ticket query. An applied builder query
  // REPLACES the flat filters rather than intersecting with them: two filter
  // surfaces silently ANDed would produce a list neither of them describes.
  // Nothing is removed — Reset drops the query and the flat rail is back.
  const flatFilters: TicketFilters = {
    sprint: selectedSprint?.name,
    areaPath: isAdo ? areaPath || undefined : undefined,
    states: states.length ? states.join(",") : undefined,
    workItemTypes: workItemTypes.length ? workItemTypes.join(",") : undefined,
    priority: ticketPriority || undefined,
    epic: isJira ? ticketEpic || undefined : undefined,
  };
  const compiled = appliedQuery ? compileQuery(appliedQuery) : null;
  const filters: TicketFilters = {
    connectionId: connectionId ?? undefined,
    providerKind: selectedConn?.kind,
    ...(compiled ?? flatFilters),
    // The search box keeps driving `q` unless the query names `title`, in which
    // case the applied query wins — one parameter, one owner.
    q: compiled?.q ?? (ticketSearch || undefined),
    page: ticketPage,
    pageSize: PAGE_SIZE,
  };
  const { data: ticketsPage, isLoading, isError, refetch } = useTickets(filters);
  const tickets = ticketsPage?.items ?? [];
  const total = ticketsPage?.total ?? 0;
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const [syncOpen, setSyncOpen] = useState(false);

  // Apply on press: nothing above runs a query until one of these fires.
  const applyQuery = () => {
    if (validateQuery(draft).length > 0) return;
    setAppliedQuery(draft);
    setTicketPage(1);
  };
  const resetQuery = () => {
    setDraft(emptyQuery());
    setAppliedQuery(null);
    setTicketPage(1);
  };

  const selCount = useMemo(() => Object.values(selected).filter(Boolean).length, [selected]);

  // Local delete (LOCAL only — never calls the provider; a re-sync restores tickets).
  const deleteTicket = useDeleteTicket();
  const deleteTickets = useDeleteTickets();
  // The ticket queued for single-row delete confirmation, and the bulk-delete flag.
  const [confirmTicket, setConfirmTicket] = useState<TicketOut | null>(null);
  const [confirmBulk, setConfirmBulk] = useState(false);

  const onConfirmDeleteTicket = () => {
    if (!confirmTicket) return;
    const id = confirmTicket.externalId;
    deleteTicket.mutate(id, {
      onSuccess: () => {
        if (selected[id]) toggleSelected(id); // keep the selection count accurate
        setConfirmTicket(null);
        toast.success(t("toast.removed"), { description: t("toast.removedDesc", { id }) });
      },
      onError: (e) =>
        toast.error(t("toast.removeFailed"), {
          description: e instanceof Error ? e.message : undefined,
        }),
    });
  };

  const onConfirmDeleteSelected = () => {
    const ids = Object.keys(selected).filter((k) => selected[k]);
    deleteTickets.mutate(ids, {
      onSuccess: (res) => {
        clearSelected();
        setConfirmBulk(false);
        toast.success(t("toast.removedCount", { count: res.deleted }), {
          description: t("toast.removedCountDesc"),
        });
      },
      onError: (e) =>
        toast.error(t("toast.removeFailedMulti"), {
          description: e instanceof Error ? e.message : undefined,
        }),
    });
  };

  const sprintOptions = (sprints ?? []).map((s) => ({ value: s.path, label: s.name }));
  const areaOptions = (metadata?.areaPaths ?? []).map((a) => ({ value: a.path, label: a.name, hint: a.path }));
  const stateOptions = (metadata?.states ?? []).map((s) => ({ value: s, label: s }));
  const typeOptions = (metadata?.workItemTypes ?? []).map((t) => ({ value: t, label: t }));
  const epicOptions = (metadata?.epics ?? []).map((e) => ({ value: e.key, label: e.name }));

  const onPickSprint = (path: string | null) => {
    const sprint = path ? (sprints ?? []).find((s) => s.path === path) : null;
    setSelectedSprint(sprint ? { name: sprint.name, path: sprint.path } : null);
  };

  const selectAssigned = () => {
    if (!userName) return;
    const ids = tickets.filter((t) => t.assignee === userName).map((t) => t.externalId);
    setSelected(ids);
  };

  const syncSourceLabel = selectedConn
    ? `${PROVIDER_META[selectedConn.kind].name}${selectedConn.name ? ` · ${selectedConn.name}` : ""}`
    : t("yourProvider");

  return (
    <div className="px-1 pb-10 pt-0.5">
      <div className="mb-4 flex items-end justify-between">
        <div>
          {/* Where these tickets come from. In hub-managed mode this line is
              carrying weight: the Sync control is gone, and without a sentence
              saying EmeHub keeps the list current its absence is a mystery. */}
          <div className="mb-[5px] text-[13px] font-medium text-ink-dim">
            {(selectedConn ? `${PROVIDER_META[selectedConn.kind].name} · ${selectedConn.name}` : t("noConnection")) +
              ` · ${hubManaged ? t("managedByHub") : t("syncedAgo")}`}
          </div>
          <h1 className="m-0 text-[24px] font-black tracking-tight md:text-[28px]">{t("title")}</h1>
        </div>
      </div>

      <div className="glass mb-4 flex flex-col gap-[10px] rounded-2xl p-[12px_14px]">
        {/* Row 1 — connection · search · view pills, actions pinned right. On
            mobile the row stacks (flex-col) into groups; the pills + actions
            group uses `md:contents` so on desktop they flatten back into the
            single wrapping row (unchanged) with the actions pinned right. */}
        <div className="flex flex-col gap-[9px] md:flex-row md:flex-wrap md:items-center">
          <ConnectionSelect groups={connectionGroups} value={connectionId} onChange={setTicketConnectionId} />

          <div className="flex h-9 w-full items-center gap-2 rounded-[11px] border border-white/[0.08] bg-white/[0.04] px-3 md:max-w-[320px] md:min-w-[180px] md:flex-1">
            <Search size={14} color="#7a7a8c" strokeWidth={2} />
            <input
              value={ticketSearch}
              onChange={(e) => setTicketSearch(e.target.value)}
              placeholder={t("searchPlaceholder")}
              className="flex-1 border-none bg-transparent text-[13px] text-ink outline-none"
            />
          </div>

          <div className="flex items-center gap-[9px] md:contents">
            <div className="ml-auto flex items-center gap-[9px]">
              {selCount > 0 && (
                <Button
                  variant="danger"
                  className="hidden md:inline-flex"
                  onClick={() => setConfirmBulk(true)}
                >
                  <Trash2 size={13} />
                  {t("removeSelected", { n: selCount })}
                </Button>
              )}
              {/* Hidden when the hub manages these tickets (#517). Sync is the
                  pre-integration path: it needs a local provider PAT, and a
                  mirrored hub connection holds none by design (#501/#514), so
                  the button could only ever fail. The row is flex with a gap,
                  so removing it closes up rather than leaving a hole; the
                  header line above says who keeps the list current instead. */}
              {!hubManaged && (
                <Button variant="glass" onClick={() => setSyncOpen(true)}>
                  <RefreshCw size={13} />
                  {t("sync")}
                </Button>
              )}
              <Button variant="primary" className="hidden md:inline-flex" onClick={openCreateRun}>
                <Plus size={14} strokeWidth={2.3} />
                {t("createRun")} {selCount > 0 && `(${selCount})`}
              </Button>
            </div>
          </div>
        </div>

        {/* Row 2 — attribute filters. A single horizontal-scroll rail on mobile
            (chips never shrink); wraps normally from `md` up.

            While a builder query is in force the flat chips are replaced by a
            summary of it: leaving both visible would show two filter surfaces
            where only one is driving the list. Reset brings the chips back. */}
        <div className="flex items-center gap-[9px] overflow-x-auto border-t border-white/[0.06] pt-[10px] scrollbar-none [&>*]:shrink-0 md:flex-wrap">
          <button
            type="button"
            onClick={() => setBuilderOpen((open) => !open)}
            data-on={builderOpen || appliedQuery !== null}
            aria-expanded={builderOpen}
            className="flex cursor-pointer items-center gap-2 rounded-[11px] border border-white/[0.09] bg-white/[0.05] px-[13px] py-2 text-[12.5px] font-semibold text-[#dcdce4] transition-colors hover:bg-white/[0.1] data-[on=true]:border-[rgba(139,92,246,.35)] data-[on=true]:bg-[rgba(139,92,246,.2)] data-[on=true]:text-white"
          >
            <SlidersHorizontal size={13} />
            {t("builder.toggle")}
          </button>

          {appliedQuery !== null ? (
            <>
              <span className="min-w-0 max-w-[520px] truncate rounded-[11px] border border-[rgba(139,92,246,.3)] bg-[rgba(139,92,246,.14)] px-[13px] py-2 text-[12.5px] font-semibold text-ink">
                {describeQuery(appliedQuery, t)}
              </span>
              <button
                type="button"
                onClick={resetQuery}
                className="cursor-pointer rounded-[11px] border border-white/[0.09] bg-white/[0.05] px-[13px] py-2 text-[12.5px] font-semibold text-[#dcdce4] hover:bg-white/[0.1]"
              >
                {t("builder.clearQuery")}
              </button>
            </>
          ) : (
            <>
          <Select
            value={selectedSprint?.path ?? null}
            options={sprintOptions}
            placeholder={t("filters.sprint")}
            onChange={onPickSprint}
            emptyLabel={t("filters.noSprintsFound")}
          />
          {isJira && (
            <Select
              value={ticketEpic}
              options={epicOptions}
              placeholder={t("filters.epic")}
              onChange={setTicketEpic}
              emptyLabel={t("filters.noEpics")}
            />
          )}
          {isAdo && (
            <Select
              value={areaPath}
              options={areaOptions}
              placeholder={t("filters.areaPath")}
              onChange={setAreaPath}
              emptyLabel={t("filters.noAreaPaths")}
            />
          )}
          <MultiSelect
            values={states}
            options={stateOptions}
            placeholder={isJira ? t("filters.status") : t("filters.state")}
            onChange={setStates}
          />
          <MultiSelect
            values={workItemTypes}
            options={typeOptions}
            placeholder={isJira ? t("filters.issueType") : t("filters.workItemType")}
            onChange={setWorkItemTypes}
          />
          <Select
            value={ticketPriority}
            options={PRIORITY_OPTIONS}
            placeholder={t("filters.priority")}
            onChange={setTicketPriority}
          />
            </>
          )}
          {/* A selection action, not a filter — it stays put in both modes. */}
          {userName && (
            <button
              onClick={selectAssigned}
              className="cursor-pointer rounded-[11px] border border-white/[0.09] bg-white/[0.05] px-[13px] py-2 text-[12.5px] font-semibold text-[#dcdce4] hover:bg-white/[0.1]"
            >
              {t("selectMyAssigned")}
            </button>
          )}
        </div>

        {/* Row 3 — the query builder, when opened. Inside the same glass card so
            it reads as an expansion of the filter bar rather than a new screen. */}
        {builderOpen && (
          <div className="border-t border-white/[0.06] pt-[12px]">
            <QueryBuilder
              draft={draft}
              onDraftChange={setDraft}
              applied={appliedQuery}
              options={filterOptions}
              presets={presets}
              saved={savedQueries}
              onSave={(name) => setSavedQueries(saveQuery(name, draft))}
              onDeleteSaved={(id) => setSavedQueries(deleteSavedQuery(id))}
              onApply={applyQuery}
              onReset={resetQuery}
              busy={isLoading}
            />
          </div>
        )}
      </div>

      {isLoading ? (
        <div className="flex flex-col gap-[10px]">
          {Array.from({ length: 5 }).map((_, i) => (
            <div key={i} className="glass h-[64px] animate-pulse rounded-2xl" />
          ))}
        </div>
      ) : isError ? (
        // A failed load is NOT an empty list (#491) — saying "no tickets found"
        // here would send the user off to change filters that are working fine.
        <ErrorState
          title={tCommon("loadFailed.title")}
          body={tCommon("loadFailed.body")}
          retryLabel={tCommon("loadFailed.retry")}
          onRetry={() => void refetch()}
        />
      ) : !tickets.length ? (
        <EmptyState
          icon={<Search size={28} color="#8b8b9e" strokeWidth={1.6} />}
          title={t("empty.title")}
          body={t("empty.body")}
        />
      ) : (
        <>
          <div className="flex flex-col gap-[10px]">
            {tickets.map((tk, i) => (
              <TicketRow
                key={tk.externalId}
                ticket={tk}
                selected={!!selected[tk.externalId]}
                onToggle={() => toggleSelected(tk.externalId)}
                onOpen={() => navigate(`/tickets/${encodeURIComponent(tk.externalId)}`)}
                onRequestDelete={() => setConfirmTicket(tk)}
                index={i}
              />
            ))}
          </div>

          <div className="mt-4 flex items-center justify-between text-[12.5px] text-ink-dim">
            <span>{t("count", { count: total })}</span>
            <div className="flex items-center gap-3">
              <button
                onClick={() => setTicketPage(Math.max(1, ticketPage - 1))}
                disabled={ticketPage <= 1}
                className="flex cursor-pointer items-center gap-1 rounded-[10px] border border-white/[0.09] bg-white/[0.05] px-3 py-1.5 text-[12.5px] font-semibold text-[#dcdce4] hover:bg-white/[0.1] disabled:cursor-not-allowed disabled:opacity-40"
              >
                <ChevronLeft size={14} />
                {t("pagination.prev")}
              </button>
              <span className="font-medium text-ink">
                {t("pagination.page", { page: ticketPage, total: totalPages })}
              </span>
              <button
                onClick={() => setTicketPage(Math.min(totalPages, ticketPage + 1))}
                disabled={ticketPage >= totalPages}
                className="flex cursor-pointer items-center gap-1 rounded-[10px] border border-white/[0.09] bg-white/[0.05] px-3 py-1.5 text-[12.5px] font-semibold text-[#dcdce4] hover:bg-white/[0.1] disabled:cursor-not-allowed disabled:opacity-40"
              >
                {t("pagination.next")}
                <ChevronRight size={14} />
              </button>
            </div>
          </div>
        </>
      )}

      {syncOpen && (
        <SyncTicketsModal
          connectionId={connectionId}
          providerKind={selectedConn?.kind}
          configuredProject={selectedConn?.config.project}
          sourceLabel={syncSourceLabel}
          onClose={() => setSyncOpen(false)}
        />
      )}

      <MobileSelectionBar
        count={selCount}
        onCreateRun={openCreateRun}
        onRemove={() => setConfirmBulk(true)}
        onClear={clearSelected}
      />

      <ConfirmDialog
        open={!!confirmTicket}
        title={t("confirmRemove.title")}
        message={confirmTicket ? t("confirmRemove.message", { id: confirmTicket.externalId }) : ""}
        confirmLabel={t("confirmRemove.confirmLabel")}
        danger
        loading={deleteTicket.isPending}
        onConfirm={onConfirmDeleteTicket}
        onClose={() => setConfirmTicket(null)}
      />

      <ConfirmDialog
        open={confirmBulk}
        title={t("confirmRemoveBulk.title", { count: selCount })}
        message={t("confirmRemoveBulk.message", { count: selCount })}
        confirmLabel={t("confirmRemoveBulk.confirmLabel", { n: selCount })}
        danger
        loading={deleteTickets.isPending}
        onConfirm={onConfirmDeleteSelected}
        onClose={() => setConfirmBulk(false)}
      />
    </div>
  );
}

function TicketRow({
  ticket,
  selected,
  onToggle,
  onOpen,
  onRequestDelete,
  index,
}: {
  ticket: TicketOut;
  selected: boolean;
  onToggle: () => void;
  onOpen: () => void;
  onRequestDelete: () => void;
  index: number;
}) {
  const { t } = useTranslation("tickets");
  const [glyph, glyphColor] = providerGlyph[ticket.providerKind] ?? ["?", "#8b8b9e"];
  // Trash affordance — stops propagation so it never opens the ticket.
  const deleteButton = (
    <button
      type="button"
      aria-label={t("row.removeAria", { id: ticket.externalId })}
      title={t("row.removeLocally")}
      onClick={(e) => {
        e.stopPropagation();
        onRequestDelete();
      }}
      className="flex h-8 w-8 shrink-0 cursor-pointer items-center justify-center rounded-[9px] border border-white/[0.08] bg-white/[0.03] text-ink-dim transition-colors hover:border-red-500/40 hover:bg-red-500/10 hover:text-red-400"
    >
      <Trash2 size={14} />
    </button>
  );
  const checkboxOn = (
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="#fff" strokeWidth="3.2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M20 6 9 17l-5-5" />
    </svg>
  );
  return (
    <motion.div
      initial={{ opacity: 0, y: 16 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.45, delay: Math.min(index * 0.03, 0.24), ease: "easeOut" }}
      // Lift the row and expand its shadow on hover — matches the /runs rows.
      whileHover={{
        y: -4,
        zIndex: 10,
        boxShadow: "0 22px 48px -22px rgba(139,92,246,.5), 0 0 26px -12px rgba(34,211,238,.3)",
        transition: { duration: 0.25, ease: [0.2, 0.8, 0.2, 1] },
      }}
      className="glass relative rounded-2xl transition-colors hover:border-[rgba(139,92,246,.28)]"
      style={{ borderColor: selected ? "rgba(139,92,246,.5)" : undefined }}
    >
      {/* Desktop row — unchanged single-line layout. */}
      <div className="hidden items-center gap-[15px] p-[15px_18px] md:flex">
        <div
          onClick={onToggle}
          className="flex h-[18px] w-[18px] shrink-0 cursor-pointer items-center justify-center rounded-[6px] border transition-colors"
          style={{
            background: selected ? "linear-gradient(135deg,#8b5cf6,#6366f1)" : "rgba(255,255,255,.04)",
            borderColor: selected ? "transparent" : "rgba(255,255,255,.18)",
          }}
        >
          {selected && checkboxOn}
        </div>

        <div
          className="flex h-[34px] w-[34px] shrink-0 items-center justify-center rounded-[10px] text-[14px] font-black"
          style={{ color: glyphColor, background: `${glyphColor}26` }}
        >
          {glyph}
        </div>

        <div className="min-w-0 flex-1 cursor-pointer" onClick={onOpen}>
          <div className="mb-[3px] flex items-center gap-[9px]">
            <span className="font-mono text-[11.5px] font-semibold text-violet">{ticket.externalId}</span>
            <span className="text-[10.5px] text-[#7a7a8c]">{ticket.sprint}</span>
          </div>
          <div className="overflow-hidden text-ellipsis whitespace-nowrap text-[14.5px] font-semibold">
            {ticket.title}
          </div>
        </div>

        <StatusBadge status={ticket.status} />

        <span className="w-[74px] shrink-0 text-right text-[11px] text-ink-dim">
          {t("row.ac", { n: ticket.acCount })} &middot; <span style={{ color: priorityColor(ticket.priority) }}>{ticket.priority}</span>
        </span>

        <div className="accent-gradient flex h-[26px] w-[26px] shrink-0 items-center justify-center rounded-[8px] text-[10.5px] font-bold text-white">
          {initials(ticket.assignee)}
        </div>

        <Button variant="glass" size="sm" onClick={onOpen} className="shrink-0">
          {t("row.details")}
        </Button>

        {deleteButton}
      </div>

      {/* Mobile card — checkbox + tappable body (glyph/id/priority, title, status·sprint·AC). */}
      <div className="flex items-start gap-3 p-[14px_16px] md:hidden">
        <div
          onClick={onToggle}
          className="mt-0.5 flex h-6 w-6 shrink-0 cursor-pointer items-center justify-center rounded-[7px] border transition-colors"
          style={{
            background: selected ? "linear-gradient(135deg,#8b5cf6,#6366f1)" : "rgba(255,255,255,.04)",
            borderColor: selected ? "transparent" : "rgba(255,255,255,.18)",
          }}
        >
          {selected && checkboxOn}
        </div>

        <div className="min-w-0 flex-1 cursor-pointer" onClick={onOpen}>
          <div className="mb-[6px] flex items-center gap-[8px]">
            <div
              className="flex h-6 w-6 shrink-0 items-center justify-center rounded-[7px] text-[12px] font-black"
              style={{ color: glyphColor, background: `${glyphColor}26` }}
            >
              {glyph}
            </div>
            <span className="font-mono text-[11.5px] font-semibold text-violet">{ticket.externalId}</span>
            <span
              className="ml-auto shrink-0 text-[11px] font-semibold"
              style={{ color: priorityColor(ticket.priority) }}
            >
              {ticket.priority}
            </span>
          </div>
          <div className="mb-[6px] text-[14px] leading-snug font-semibold">{ticket.title}</div>
          <div className="flex flex-wrap items-center gap-[7px] text-[11px] text-ink-dim">
            <StatusBadge status={ticket.status} />
            <span>{ticket.sprint}</span>
            <span>&middot; {t("row.ac", { n: ticket.acCount })}</span>
          </div>
        </div>

        {deleteButton}
      </div>
    </motion.div>
  );
}

/**
 * Floating selection bar shown only on phones (below `md`) once one or more
 * tickets are selected — the mobile stand-in for the desktop toolbar's
 * "Create Run" + "Remove N selected" buttons. Styled to match the Runs board's
 * {@link RunBulkBar}: a compact centred glass bar with an "N selected" chip and
 * icon actions (create-run · remove · clear), portalled to `document.body` per
 * the floating-overlay convention.
 */
function MobileSelectionBar({
  count,
  onCreateRun,
  onRemove,
  onClear,
}: {
  count: number;
  onCreateRun: () => void;
  onRemove: () => void;
  onClear: () => void;
}) {
  const { t } = useTranslation("tickets");
  return createPortal(
    <AnimatePresence>
      {count > 0 && (
        <motion.div
          initial={{ opacity: 0, y: 90, x: "-50%" }}
          animate={{ opacity: 1, y: 0, x: "-50%" }}
          exit={{ opacity: 0, y: 90, x: "-50%" }}
          transition={{ type: "spring", stiffness: 420, damping: 34 }}
          className="fixed bottom-[calc(18px+env(safe-area-inset-bottom))] left-1/2 z-[900] flex items-center gap-1 rounded-[16px] border border-white/[0.12] py-2 pl-2 pr-2.5 shadow-[0_30px_70px_-20px_rgba(0,0,0,.85)] md:hidden"
          style={{ background: "rgb(26,26,34)" }}
        >
          <span className="mr-1 flex items-center gap-2 rounded-[11px] bg-[rgba(139,92,246,.18)] py-1.5 pl-2 pr-3 text-[12.5px] font-semibold text-ink">
            <span className="flex h-5 min-w-5 items-center justify-center rounded-full bg-violet px-1.5 text-[11px] font-bold text-white">
              {count}
            </span>
            {t("mobileBar.selected")}
          </span>
          <button
            type="button"
            title={t("mobileBar.createRun")}
            onClick={onCreateRun}
            className="flex h-9 items-center gap-1.5 whitespace-nowrap rounded-[11px] bg-[rgba(139,92,246,.9)] px-3 text-[13px] font-bold text-white transition-colors hover:bg-[rgba(139,92,246,1)]"
          >
            <Plus size={15} strokeWidth={2.4} />
            {t("mobileBar.createRun")}
          </button>
          <button
            type="button"
            title={t("mobileBar.removeSelected")}
            aria-label={t("mobileBar.removeSelectedAria", { n: count })}
            onClick={onRemove}
            className="flex h-9 w-9 items-center justify-center rounded-[11px] text-[#fb7185] transition-colors hover:bg-[rgba(251,113,133,.14)]"
          >
            <Trash2 size={16} strokeWidth={2} />
          </button>
          <div className="mx-0.5 h-6 w-px bg-white/[0.1]" />
          <button
            type="button"
            title={t("mobileBar.clearSelection")}
            onClick={onClear}
            className="flex h-9 w-9 items-center justify-center rounded-[11px] text-ink-soft transition-colors hover:bg-white/[0.08] hover:text-ink"
          >
            <X size={16} strokeWidth={2} />
          </button>
        </motion.div>
      )}
    </AnimatePresence>,
    document.body,
  );
}
