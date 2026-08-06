import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useLocation } from "react-router-dom";
import { LanguageSwitcher } from "@/components/shell/LanguageSwitcher";
import { GlassCard } from "@/components/ui/GlassCard";
import { Select } from "@/components/ui/Dropdown";
import { Spinner } from "@/components/ui/misc";
import { ClaudeCredentialsCard } from "@/components/settings/ClaudeCredentialsCard";
import { CollapsibleSection } from "@/components/settings/CollapsibleSection";
import { HubConnections } from "@/components/settings/HubConnections";
import { ProviderGroup } from "@/components/settings/ProviderGroup";
import { PROVIDER_META, PROVIDER_ORDER } from "@/components/settings/providerMeta";
import { ToggleRow } from "@/components/settings/ToggleRow";
import { useProviders, useSettings, useUpdateSettings } from "@/hooks/queries";
import { AI_MODEL_OPTIONS } from "@/lib/models";
import type { AuthoringLogVerbosity, AuthoringMode, ExecutionTarget, HealMode, ProviderGroupOut, ProviderKind, SettingsOut } from "@/types/api";

/** A never-configured provider: the backend catalog omits it (fresh machine),
 * so synthesize an empty group the user can add a first connection under. */
const emptyGroup = (kind: ProviderKind): ProviderGroupOut => ({
  kind,
  categories: PROVIDER_META[kind].categories,
  name: PROVIDER_META[kind].name,
  connectionCount: 0,
  connectedCount: 0,
  connections: [],
});

/** AI actions that accept a per-skill model override — mirrors the backend
 * skills that are actually invoked with `skill=` (see api skills.SKILLS +
 * claude_cli._resolve_model). `haikuDefault` marks the mechanical actions that
 * fall back to Haiku when left on "inherit". */
const TUNABLE_SKILLS: { id: string; label: string; haikuDefault?: boolean }[] = [
  { id: "test-case-generator", label: "Analyze + generate test cases" },
  { id: "test-case-reviewer", label: "Review & expand coverage" },
  { id: "automation-generator", label: "Generate Playwright spec" },
  { id: "automation-reviewer", label: "Review Playwright spec" },
  { id: "project-bootstrap", label: "Build project knowledge base" },
  { id: "execution-analyzer", label: "Classify run failures", haikuDefault: true },
  { id: "screenshot-annotator", label: "Annotate screenshots", haikuDefault: true },
  { id: "ticket-comment-generator", label: "Summarize ticket comments", haikuDefault: true },
];

export function Settings() {
  const { data: providers, isLoading: providersLoading } = useProviders();
  const { data: settings, isLoading: settingsLoading } = useSettings();
  const updateSettings = useUpdateSettings();
  const location = useLocation();
  const { t: tNav } = useTranslation("nav");
  const { t } = useTranslation("settings");

  // Settings edit as a local draft — controls mutate `draft`, never the server,
  // until the user hits "Save changes". `settings` only changes on save (cache
  // is updated via setQueryData), so re-syncing the draft to it is safe.
  const [draft, setDraft] = useState<SettingsOut | null>(null);
  useEffect(() => {
    if (settings) setDraft(settings);
  }, [settings]);
  const set = (patch: Partial<SettingsOut>) => setDraft((d) => (d ? { ...d, ...patch } : d));
  // Per-action model override: set one, or clear it (null = inherit default/global).
  const setSkillModel = (skillId: string, model: string | null) => {
    if (!draft) return;
    const next = { ...draft.skillModels };
    if (model) next[skillId] = model;
    else delete next[skillId];
    set({ skillModels: next });
  };
  const dirty = Boolean(draft && settings && JSON.stringify(draft) !== JSON.stringify(settings));
  const save = () => {
    if (draft && dirty) updateSettings.mutate(draft);
  };
  const discard = () => {
    if (settings) setDraft(settings);
  };

  // Deep-link support for in-page anchors — e.g. the AI status popover's
  // "Manage Claude account" button (`/settings#claude-account`) and the
  // Execution screen's target chip (`/settings#execution`). Sections default
  // open, so the target's content is visible after scrolling to it.
  useEffect(() => {
    if (!location.hash) return;
    document.getElementById(location.hash.slice(1))?.scrollIntoView({ behavior: "smooth", block: "start" });
  }, [location.hash]);

  // Always render one group per known kind — synthesize an empty group when the
  // backend catalog omits it, so a fresh machine can still add connections.
  const groupFor = (kind: ProviderKind): ProviderGroupOut =>
    providers?.find((g) => g.kind === kind) ?? emptyGroup(kind);

  return (
    <div className="max-w-[900px] px-1 pb-10 pt-0.5">
      <div className="mb-[22px]">
        <div className="mb-[5px] text-[13px] font-medium text-muted">{t("header.workspace")}</div>
        <h1 className="m-0 text-[28px] font-black tracking-tight">{t("header.title")}</h1>
      </div>

      <CollapsibleSection title={t("sections.providerConnections")}>
        {providersLoading ? (
          <div className="flex justify-center py-10">
            <Spinner />
          </div>
        ) : (
          <div className="flex flex-col gap-3.5">
            {PROVIDER_ORDER.map((kind) => (
              <ProviderGroup key={kind} group={groupFor(kind)} />
            ))}
            {/* Hub-owned connections, read-only (#501). Rendered *after* the
                local groups and self-hiding when there are none, so the local
                picker above is unaffected by anything the hub does or doesn't
                do — and with the flag off this renders nothing at all. */}
            <HubConnections />
          </div>
        )}
      </CollapsibleSection>

      <CollapsibleSection title={t("sections.profile")}>
        <GlassCard lift className="p-[22px]">
          <div className="flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
            <div className="text-[12px] text-muted">
              {t("profile.description")}
            </div>
            <Link
              to="/profile"
              className="w-full shrink-0 rounded-[11px] border border-white/[0.1] bg-white/[0.05] px-[15px] py-[10px] text-center text-[13px] font-semibold text-ink transition-colors hover:bg-white/[0.1] md:w-auto"
            >
              {t("profile.manage")}
            </Link>
          </div>
        </GlassCard>
      </CollapsibleSection>

      <CollapsibleSection title={t("sections.defaultExecution")} id="execution">
        <GlassCard lift className="p-[22px]">
          {settingsLoading || !draft ? (
            <div className="flex justify-center py-10">
              <Spinner />
            </div>
          ) : (
            <>
              <div className="flex flex-col gap-2.5 border-b border-white/[0.06] py-[13px] md:flex-row md:items-center md:justify-between">
                <div>
                  <div className="text-[14px] font-semibold">{t("execution.target.title")}</div>
                  <div className="text-[12px] text-muted">
                    {t("execution.target.description")}
                  </div>
                </div>
                <div className="w-full md:w-[170px]">
                  <Select
                    value={draft.executionTarget}
                    onChange={(v) => v && set({ executionTarget: v as ExecutionTarget })}
                    placeholder={t("execution.target.placeholder")}
                    allowClear={false}
                    options={[
                      { value: "server", label: t("execution.target.server") },
                      { value: "local-agent", label: t("execution.target.localAgent") },
                    ]}
                  />
                </div>
              </div>
              <div className="flex flex-col gap-2.5 border-b border-white/[0.06] py-[13px] md:flex-row md:items-center md:justify-between">
                <div>
                  <div className="text-[14px] font-semibold">{t("execution.authoring.title")}</div>
                  <div className="text-[12px] text-muted">
                    {t("execution.authoring.description")}
                  </div>
                </div>
                <div className="w-full md:w-[170px]">
                  <Select
                    value={draft.authoringMode}
                    onChange={(v) => v && set({ authoringMode: v as AuthoringMode })}
                    placeholder={t("execution.authoring.placeholder")}
                    allowClear={false}
                    options={[
                      { value: "blind", label: t("execution.authoring.blind") },
                      { value: "live-harness", label: t("execution.authoring.liveHarness") },
                    ]}
                  />
                </div>
              </div>
              <div className="flex flex-col gap-2.5 border-b border-white/[0.06] py-[13px] md:flex-row md:items-center md:justify-between">
                <div>
                  <div className="text-[14px] font-semibold">{t("execution.heal.title")}</div>
                  <div className="text-[12px] text-muted">
                    {t("execution.heal.description")}
                  </div>
                </div>
                <div className="w-full md:w-[170px]">
                  <Select
                    value={draft.healMode}
                    onChange={(v) => v && set({ healMode: v as HealMode })}
                    placeholder={t("execution.heal.placeholder")}
                    allowClear={false}
                    options={[
                      { value: "classic", label: t("execution.heal.classic") },
                      { value: "live-harness", label: t("execution.heal.liveHarness") },
                    ]}
                  />
                </div>
              </div>
              <div className="flex flex-col gap-2.5 border-b border-white/[0.06] py-[13px] md:flex-row md:items-center md:justify-between">
                <div>
                  <div className="text-[14px] font-semibold">{t("execution.liveBudget.title")}</div>
                  <div className="text-[12px] text-muted">
                    {t("execution.liveBudget.description")}
                  </div>
                </div>
                <div className="flex items-center gap-2">
                  <span className="text-[13px] font-semibold text-muted">$</span>
                  <input
                    type="number"
                    min={0.5}
                    step={0.5}
                    value={draft.authoringCostBudgetUsd}
                    onChange={(e) => set({ authoringCostBudgetUsd: Number(e.target.value) })}
                    className="w-[100px] rounded-[11px] border border-white/[0.09] bg-white/[0.04] px-[13px] py-[10px] text-[13px] text-ink outline-none focus:border-[rgba(139,92,246,.5)]"
                  />
                </div>
              </div>
              <div className="flex flex-col gap-2.5 border-b border-white/[0.06] py-[13px] md:flex-row md:items-center md:justify-between">
                <div>
                  <div className="text-[14px] font-semibold">{t("execution.authoringLog.title")}</div>
                  <div className="text-[12px] text-muted">
                    {t("execution.authoringLog.description")}
                  </div>
                </div>
                <div className="w-full md:w-[170px]">
                  <Select
                    value={draft.authoringLogVerbosity}
                    onChange={(v) => v && set({ authoringLogVerbosity: v as AuthoringLogVerbosity })}
                    placeholder={t("execution.authoringLog.placeholder")}
                    allowClear={false}
                    options={[
                      { value: "concise", label: t("execution.authoringLog.concise") },
                      { value: "verbose", label: t("execution.authoringLog.verbose") },
                    ]}
                  />
                </div>
              </div>
              <div className="flex flex-col gap-2.5 border-b border-white/[0.06] py-[13px] md:flex-row md:items-center md:justify-between">
                <div>
                  <div className="text-[14px] font-semibold">{t("execution.parallel.title")}</div>
                  <div className="text-[12px] text-muted">
                    {t("execution.parallel.description", { n: draft.parallel })}
                  </div>
                </div>
                <div className="flex items-center gap-3">
                  <input
                    type="range"
                    min={1}
                    max={8}
                    value={draft.parallel}
                    onChange={(e) => set({ parallel: Number(e.target.value) })}
                    className="w-full accent-[#8b5cf6] md:w-[150px]"
                  />
                  <span className="w-5 text-center font-mono text-[14px] font-bold">{draft.parallel}</span>
                </div>
              </div>
              <div className="flex flex-col gap-2.5 border-b border-white/[0.06] py-[13px] md:flex-row md:items-center md:justify-between">
                <div>
                  <div className="text-[14px] font-semibold">{t("execution.maxCases.title")}</div>
                  <div className="text-[12px] text-muted">
                    {t("execution.maxCases.description", { n: draft.maxCasesPerTicket })}
                  </div>
                </div>
                <div className="flex items-center gap-3">
                  <input
                    type="range"
                    min={1}
                    max={20}
                    value={draft.maxCasesPerTicket}
                    onChange={(e) => set({ maxCasesPerTicket: Number(e.target.value) })}
                    className="w-full accent-[#8b5cf6] md:w-[150px]"
                  />
                  <span className="w-5 text-center font-mono text-[14px] font-bold">
                    {draft.maxCasesPerTicket}
                  </span>
                </div>
              </div>
              <ToggleRow
                title={t("execution.retryFlaky.title")}
                description={t("execution.retryFlaky.description")}
                checked={draft.retryFlaky}
                onChange={(v) => set({ retryFlaky: v })}
              />
              <ToggleRow
                title={t("execution.screenshotOnFail.title")}
                description={t("execution.screenshotOnFail.description")}
                checked={draft.screenshotOnFail}
                onChange={(v) => set({ screenshotOnFail: v })}
              />
              <ToggleRow
                title={t("execution.autoAnnotate.title")}
                description={t("execution.autoAnnotate.description")}
                checked={draft.autoAnnotate}
                onChange={(v) => set({ autoAnnotate: v })}
              />
              <ToggleRow
                title={t("execution.video.title")}
                description={t("execution.video.description")}
                checked={draft.video}
                onChange={(v) => set({ video: v })}
              />
              <ToggleRow
                title={t("execution.headless.title")}
                description={t("execution.headless.description")}
                checked={draft.headless}
                onChange={(v) => set({ headless: v })}
                bordered={false}
              />
            </>
          )}
        </GlassCard>
      </CollapsibleSection>

      <CollapsibleSection title={t("sections.specGate")}>
        <GlassCard lift className="p-[22px]">
          {settingsLoading || !draft ? (
            <div className="flex justify-center py-10">
              <Spinner />
            </div>
          ) : (
            <ToggleRow
              title={t("specGate.title")}
              description={t("specGate.description")}
              checked={draft.gateEnabled}
              onChange={(v) => set({ gateEnabled: v })}
              bordered={false}
            />
          )}
        </GlassCard>
      </CollapsibleSection>

      <CollapsibleSection title={t("sections.aiModel")}>
        <GlassCard lift className="p-[22px]">
          {settingsLoading || !draft ? (
            <div className="flex justify-center py-10">
              <Spinner />
            </div>
          ) : (
            <div className="flex flex-col gap-2">
              <span className="text-[12px] font-semibold text-[#9494a6]">{t("aiModel.model.label")}</span>
              <Select
                value={draft.claudeModel}
                onChange={(v) => v && set({ claudeModel: v })}
                placeholder={t("aiModel.model.placeholder")}
                allowClear={false}
                options={[
                  { value: "claude-opus-4-8", label: t("aiModel.model.opus") },
                  { value: "claude-sonnet-5", label: t("aiModel.model.sonnet") },
                  { value: "claude-haiku-4-5-20251001", label: t("aiModel.model.haiku") },
                ]}
              />
              <span className="text-[12px] text-muted">
                {t("aiModel.model.hint")}
              </span>

              <span className="mt-4 text-[12px] font-semibold text-[#9494a6]">
                {t("aiModel.budget.label")}
              </span>
              <input
                type="number"
                min={0}
                value={draft.weeklyTokenBudget}
                onChange={(e) => set({ weeklyTokenBudget: Number(e.target.value) })}
                className="rounded-[11px] border border-white/[0.09] bg-white/[0.04] px-[13px] py-[10px] text-[13px] text-ink outline-none focus:border-[rgba(139,92,246,.5)]"
              />
              <span className="text-[12px] text-muted">
                {t("aiModel.budget.hint")}
              </span>
            </div>
          )}
        </GlassCard>
      </CollapsibleSection>

      <CollapsibleSection title={t("sections.perAction")}>
        <GlassCard lift className="p-[22px]">
          {settingsLoading || !draft ? (
            <div className="flex justify-center py-10">
              <Spinner />
            </div>
          ) : (
            <div className="flex flex-col gap-2">
              <span className="text-[12px] font-semibold text-[#9494a6]">{t("perAction.concurrency.label")}</span>
              <Select
                value={String(draft.aiPipelineWorkers)}
                onChange={(v) => v != null && set({ aiPipelineWorkers: Number(v) })}
                placeholder={t("perAction.concurrency.placeholder")}
                allowClear={false}
                options={[
                  { value: "0", label: t("perAction.concurrency.auto") },
                  { value: "1", label: t("perAction.concurrency.sequential") },
                  { value: "2", label: "2" },
                  { value: "3", label: "3" },
                  { value: "4", label: "4" },
                ]}
              />
              <span className="text-[12px] text-muted">
                {t("perAction.concurrency.hint")}
              </span>

              <span className="mt-4 text-[12px] font-semibold text-[#9494a6]">
                {t("perAction.overrides.label")}
              </span>
              <span className="mb-1 text-[12px] text-muted">
                {t("perAction.overrides.hint")}
              </span>
              <div className="flex flex-col divide-y divide-white/[0.06]">
                {TUNABLE_SKILLS.map((skill) => (
                  <div key={skill.id} className="flex flex-col gap-2 py-2.5 md:flex-row md:items-center md:justify-between md:gap-4">
                    <div className="min-w-0">
                      <div className="text-[13px] text-ink">{t(`perAction.skills.${skill.id}`, skill.label)}</div>
                      <div className="text-[11px] text-muted">
                        {t("perAction.default", {
                          model: skill.haikuDefault ? t("perAction.defaultHaiku") : t("perAction.defaultInherit"),
                        })}
                      </div>
                    </div>
                    <div className="w-full md:w-[220px] md:shrink-0">
                      <Select
                        value={draft.skillModels[skill.id] ?? ""}
                        onChange={(v) => setSkillModel(skill.id, v ?? null)}
                        placeholder={t("perAction.overrides.placeholder")}
                        allowClear
                        options={AI_MODEL_OPTIONS}
                      />
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}
        </GlassCard>
      </CollapsibleSection>

      <CollapsibleSection title={t("sections.claudeAccount")} id="claude-account">
        <GlassCard lift className="p-[22px]">
          <ClaudeCredentialsCard />
        </GlassCard>
      </CollapsibleSection>

      <CollapsibleSection title={t("sections.interface")}>
        <GlassCard lift className="p-[22px]">
          <div className="flex items-center justify-between border-b border-white/[0.06] py-[13px]">
            <div>
              <div className="text-[14px] font-semibold">{tNav("language.label")}</div>
              <div className="text-[12px] text-muted">{tNav("language.description")}</div>
            </div>
            <LanguageSwitcher />
          </div>
          {settingsLoading || !draft ? (
            <div className="flex justify-center py-10">
              <Spinner />
            </div>
          ) : (
            <ToggleRow
              title={t("interface.background.title")}
              description={t("interface.background.description")}
              checked={draft.neuralBackground}
              onChange={(v) => set({ neuralBackground: v })}
              bordered={false}
            />
          )}
        </GlassCard>
      </CollapsibleSection>

      {/* Sticky action bar — only while there are unsaved edits. Opaque bg (no
          backdrop-filter) since it layers over the animated 3D background. */}
      {dirty && (
        <div className="pointer-events-none fixed inset-x-0 bottom-0 z-40 flex justify-center px-4 pb-6">
          <div className="pointer-events-auto flex items-center gap-3 rounded-[16px] border border-white/[0.12] bg-[#16161f] px-5 py-3 shadow-[0_16px_48px_rgba(0,0,0,0.55)]">
            <span className="text-[13px] font-medium text-muted">{t("common:unsavedChanges")}</span>
            <button
              onClick={discard}
              disabled={updateSettings.isPending}
              className="rounded-[11px] border border-white/[0.1] bg-white/[0.05] px-[15px] py-[9px] text-[13px] font-semibold text-ink transition-colors hover:bg-white/[0.1] disabled:opacity-50"
            >
              {t("common:discard")}
            </button>
            <button
              onClick={save}
              disabled={updateSettings.isPending}
              className="rounded-[11px] bg-gradient-to-br from-[#8b5cf6] to-[#6366f1] px-[17px] py-[9px] text-[13px] font-semibold text-white transition-opacity hover:opacity-90 disabled:opacity-50"
            >
              {updateSettings.isPending ? t("status.saving") : t("common:save")}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
