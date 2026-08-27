import { Plus, Trash2 } from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { GlassCard } from "@/components/ui/GlassCard";
import { Button } from "@/components/ui/Button";
import { Select } from "@/components/ui/Dropdown";
import { ToggleRow } from "@/components/settings/ToggleRow";
import { PROVIDER_META } from "@/components/settings/providerMeta";
import { useProviders } from "@/hooks/queries";
import type {
  EnvironmentCfg,
  ProjectConfigOut,
  ProjectConfigUpdate,
  ProjectRepo,
  ProviderKind,
  TestAccountIn,
} from "@/types/api";
import { inputCls, labelCls } from "./formStyles";
import { ReposManager } from "./ReposManager";

/**
 * Reusable project settings form. Lets the user configure the project-specific
 * runtime values downstream automation needs (application URL, connections,
 * repos, test accounts, environments, extra config) so generated Playwright
 * specs run with little to no manual editing. Passwords are write-only: blank
 * preserves the securely stored secret. Scope-agnostic — the caller supplies the
 * loaded `config`, the `onSave` handler, and (optionally) the manual-login
 * widget via `renderManualLogin`, so it serves both a user's own project and the
 * admin shared-workspace settings page.
 *
 * `readOnly` renders the same values with no way to change them (#587): every
 * field disabled, every add/remove control and Save gone. It presents a decision
 * made elsewhere; it does not enforce it. The API refuses the write under the
 * same flag, because a read-only screen over a writable endpoint is the #512
 * defect in a new place.
 */
export function ProjectSettingsForm({
  config,
  saving,
  onSave,
  renderManualLogin,
  readOnly = false,
  hideConnections = false,
}: {
  config: ProjectConfigOut;
  saving: boolean;
  onSave: (patch: ProjectConfigUpdate) => void;
  renderManualLogin?: (hasBaseUrl: boolean) => ReactNode;
  /** Show the configuration, offer no way to change it (#587). */
  readOnly?: boolean;
  /** Drop the provider-connections card. Set by the project's Connection tab
   *  (#732), which owns the three connection roles itself — two pickers for the
   *  same binding on one screen is two places to change one thing. The admin
   *  shared-workspace page keeps them: it has no Connection tab. */
  hideConnections?: boolean;
}) {
  const { t } = useTranslation("projects");
  const { data: providers } = useProviders();
  // `Select` and `ToggleRow` are div-based and take no `disabled` prop, so a
  // non-interactive shell is the complete block for them: with no focusable
  // element inside, there is no keyboard path around `pointer-events: none`.
  // Native inputs get a real `disabled` instead — that one has a keyboard path.
  const roShell = readOnly ? "pointer-events-none opacity-60" : "";

  const [baseUrl, setBaseUrl] = useState("");
  const [repos, setRepos] = useState<ProjectRepo[]>([]);
  const [accounts, setAccounts] = useState<(TestAccountIn & { hasPassword: boolean })[]>([]);
  const [environments, setEnvironments] = useState<EnvironmentCfg[]>([]);
  const [extra, setExtra] = useState<{ k: string; v: string }[]>([]);
  const [manualAuth, setManualAuth] = useState(false);
  const [workItemConnectionId, setWorkItemConnectionId] = useState<number | null>(null);
  const [repositoryConnectionId, setRepositoryConnectionId] = useState<number | null>(null);

  // Work-item vs repository connections, from the grouped provider catalog.
  const connOption = (c: { id: number; kind: ProviderKind; name: string }) => ({
    value: String(c.id),
    label: `${PROVIDER_META[c.kind].name} · ${c.name}`,
  });
  const allConnections = (providers ?? []).flatMap((g) => g.connections);
  const workItemOptions = allConnections
    .filter((c) => c.categories.includes("work_item"))
    .map(connOption);
  const repositoryConnections = allConnections.filter((c) => c.categories.includes("repository"));
  const repositoryOptions = repositoryConnections.map(connOption);
  const repoConn = repositoryConnections.find((c) => c.id === repositoryConnectionId) ?? null;

  useEffect(() => {
    if (!config) return;
    setBaseUrl(config.baseUrl ?? "");
    setManualAuth(config.manualAuth ?? false);
    setWorkItemConnectionId(config.workItemConnectionId ?? null);
    setRepositoryConnectionId(config.repositoryConnectionId ?? null);
    setRepos(config.repos ?? []);
    setAccounts(
      (config.testAccounts ?? []).map((a) => ({
        role: a.role,
        username: a.username,
        password: "",
        notes: a.notes,
        hasPassword: a.hasPassword,
      })),
    );
    setEnvironments(config.environments ?? []);
    setExtra(Object.entries(config.extra ?? {}).map(([k, v]) => ({ k, v: String(v) })));
  }, [config]);

  const handleSave = () =>
    onSave({
      baseUrl,
      repos,
      testAccounts: accounts.map(({ role, username, password, notes }) => ({
        role,
        username,
        password,
        notes,
      })),
      environments,
      extra: Object.fromEntries(extra.filter((e) => e.k).map((e) => [e.k, e.v])),
      manualAuth,
      workItemConnectionId,
      repositoryConnectionId,
    });

  return (
    <div className="flex flex-col gap-3.5">
      {hideConnections ? null : (
      <GlassCard className="p-5">
        <div className="mb-1 text-[14px] font-bold">{t("settingsForm.providerConnections")}</div>
        <p className="mb-4 text-[12.5px] leading-relaxed text-ink-dim">
          {t("settingsForm.providerConnectionsDesc")}
        </p>
        <div className="grid max-w-[560px] grid-cols-1 gap-4 md:grid-cols-2">
          <div className={roShell}>
            <label className={labelCls}>{t("settingsForm.workItemProvider")}</label>
            <Select
              value={workItemConnectionId != null ? String(workItemConnectionId) : null}
              options={workItemOptions}
              placeholder={t("settingsForm.selectConnection")}
              onChange={(v) => setWorkItemConnectionId(v ? Number(v) : null)}
              emptyLabel={t("settingsForm.noWorkItemConnections")}
            />
          </div>
          <div className={roShell}>
            <label className={labelCls}>{t("settingsForm.repositoryProvider")}</label>
            <Select
              value={repositoryConnectionId != null ? String(repositoryConnectionId) : null}
              options={repositoryOptions}
              placeholder={t("settingsForm.selectConnection")}
              onChange={(v) => setRepositoryConnectionId(v ? Number(v) : null)}
              emptyLabel={t("settingsForm.noRepositoryConnections")}
            />
          </div>
        </div>
      </GlassCard>
      )}

      <GlassCard className="p-5">
        <div className="mb-1 text-[14px] font-bold">{t("settingsForm.application")}</div>
        <p className="mb-4 text-[12.5px] leading-relaxed text-ink-dim">
          {t("settingsForm.applicationDesc")}
        </p>
        <div className="max-w-[420px]">
          <label className={labelCls}>{t("settingsForm.baseUrl")}</label>
          <input
            className={inputCls}
            placeholder="https://staging.example.com"
            value={baseUrl}
            disabled={readOnly}
            onChange={(e) => setBaseUrl(e.target.value)}
          />
        </div>
      </GlassCard>

      <GlassCard className="p-5">
        <div className={roShell}>
        <ToggleRow
          title={t("settingsForm.manualLoginTitle")}
          description={t("settingsForm.manualLoginDesc")}
          checked={manualAuth}
          onChange={setManualAuth}
          bordered={false}
        />
        </div>
        <p className="mt-1 text-[12.5px] leading-relaxed text-ink-dim">
          {t("settingsForm.manualLoginNote")}
        </p>
        {manualAuth && renderManualLogin?.(baseUrl.trim().length > 0)}
      </GlassCard>

      <ReposManager
        repoConnectionId={repoConn?.id ?? null}
        repoConnectionName={repoConn?.name ?? ""}
        repos={repos}
        setRepos={setRepos}
        readOnly={readOnly}
      />

      <GlassCard className="p-5">
        <div className="mb-1 flex items-center gap-2">
          <div className="flex-1 text-[14px] font-bold">{t("settingsForm.testAccounts")}</div>
          {readOnly ? null : (
            <Button
              variant="glass"
              onClick={() =>
                setAccounts((a) => [...a, { role: "", username: "", password: "", notes: "", hasPassword: false }])
              }
            >
              <Plus size={14} strokeWidth={2.4} /> {t("settingsForm.addAccount")}
            </Button>
          )}
        </div>
        <p className="mb-4 text-[12.5px] leading-relaxed text-ink-dim">
          {t("settingsForm.testAccountsDesc")}
        </p>
        {accounts.length === 0 && (
          <div className="text-[12.5px] text-[#6c6c7e]">{t("settingsForm.noAccounts")}</div>
        )}
        <div className="flex flex-col gap-3">
          {accounts.map((acct, i) => (
            <div key={i} className="grid grid-cols-1 items-end gap-2.5 md:grid-cols-[1fr_1fr_1fr_1.2fr_auto]">
              <div>
                <label className={labelCls}>{t("settingsForm.role")}</label>
                <input
                  className={inputCls}
                  placeholder={t("settingsForm.rolePlaceholder")}
                  value={acct.role}
                  disabled={readOnly}
                  onChange={(e) =>
                    setAccounts((a) => a.map((x, j) => (j === i ? { ...x, role: e.target.value } : x)))
                  }
                />
              </div>
              <div>
                <label className={labelCls}>{t("settingsForm.username")}</label>
                <input
                  className={inputCls}
                  placeholder="qa@example.com"
                  value={acct.username}
                  disabled={readOnly}
                  onChange={(e) =>
                    setAccounts((a) => a.map((x, j) => (j === i ? { ...x, username: e.target.value } : x)))
                  }
                />
              </div>
              <div>
                <label className={labelCls}>{t("settingsForm.password")}</label>
                <input
                  type="password"
                  className={inputCls}
                  placeholder={acct.hasPassword ? t("settingsForm.passwordUnchanged") : t("settingsForm.passwordNew")}
                  value={acct.password}
                  disabled={readOnly}
                  onChange={(e) =>
                    setAccounts((a) => a.map((x, j) => (j === i ? { ...x, password: e.target.value } : x)))
                  }
                />
              </div>
              <div>
                <label className={labelCls}>{t("settingsForm.notes")}</label>
                <input
                  className={inputCls}
                  placeholder={t("settingsForm.optional")}
                  value={acct.notes}
                  disabled={readOnly}
                  onChange={(e) =>
                    setAccounts((a) => a.map((x, j) => (j === i ? { ...x, notes: e.target.value } : x)))
                  }
                />
              </div>
              {readOnly ? null : (
              <button
                onClick={() => setAccounts((a) => a.filter((_, j) => j !== i))}
                className="mb-0.5 flex h-[38px] w-[38px] items-center justify-center rounded-[10px] border border-white/[0.08] bg-white/[0.03] text-[#e06c75] hover:bg-white/[0.06]"
                title={t("settingsForm.removeAccount")}
              >
                <Trash2 size={15} strokeWidth={2.1} />
              </button>
              )}
            </div>
          ))}
        </div>
      </GlassCard>

      <GlassCard className="p-5">
        <div className="mb-1 flex items-center gap-2">
          <div className="flex-1 text-[14px] font-bold">{t("settingsForm.environments")}</div>
          {readOnly ? null : (
            <Button
              variant="glass"
              onClick={() => setEnvironments((e) => [...e, { name: "", baseUrl: "", notes: "" }])}
            >
              <Plus size={14} strokeWidth={2.4} /> {t("settingsForm.addEnvironment")}
            </Button>
          )}
        </div>
        <p className="mb-4 text-[12.5px] leading-relaxed text-ink-dim">
          {t("settingsForm.environmentsDesc")}
        </p>
        {environments.length === 0 && (
          <div className="text-[12.5px] text-[#6c6c7e]">{t("settingsForm.noEnvironments")}</div>
        )}
        <div className="flex flex-col gap-3">
          {environments.map((env, i) => (
            <div key={i} className="grid grid-cols-1 items-end gap-2.5 md:grid-cols-[1fr_1.4fr_1fr_auto]">
              <div>
                <label className={labelCls}>{t("settingsForm.name")}</label>
                <input
                  className={inputCls}
                  placeholder={t("settingsForm.namePlaceholder")}
                  value={env.name}
                  disabled={readOnly}
                  onChange={(e) =>
                    setEnvironments((v) => v.map((x, j) => (j === i ? { ...x, name: e.target.value } : x)))
                  }
                />
              </div>
              <div>
                <label className={labelCls}>{t("settingsForm.baseUrl")}</label>
                <input
                  className={inputCls}
                  placeholder="https://staging.example.com"
                  value={env.baseUrl}
                  disabled={readOnly}
                  onChange={(e) =>
                    setEnvironments((v) => v.map((x, j) => (j === i ? { ...x, baseUrl: e.target.value } : x)))
                  }
                />
              </div>
              <div>
                <label className={labelCls}>{t("settingsForm.notes")}</label>
                <input
                  className={inputCls}
                  placeholder={t("settingsForm.optional")}
                  value={env.notes}
                  disabled={readOnly}
                  onChange={(e) =>
                    setEnvironments((v) => v.map((x, j) => (j === i ? { ...x, notes: e.target.value } : x)))
                  }
                />
              </div>
              {readOnly ? null : (
              <button
                onClick={() => setEnvironments((v) => v.filter((_, j) => j !== i))}
                className="mb-0.5 flex h-[38px] w-[38px] items-center justify-center rounded-[10px] border border-white/[0.08] bg-white/[0.03] text-[#e06c75] hover:bg-white/[0.06]"
                title={t("settingsForm.removeEnvironment")}
              >
                <Trash2 size={15} strokeWidth={2.1} />
              </button>
              )}
            </div>
          ))}
        </div>
      </GlassCard>

      <GlassCard className="p-5">
        <div className="mb-1 flex items-center gap-2">
          <div className="flex-1 text-[14px] font-bold">{t("settingsForm.additionalSettings")}</div>
          {readOnly ? null : (
            <Button variant="glass" onClick={() => setExtra((x) => [...x, { k: "", v: "" }])}>
              <Plus size={14} strokeWidth={2.4} /> {t("settingsForm.addValue")}
            </Button>
          )}
        </div>
        <p className="mb-4 text-[12.5px] leading-relaxed text-ink-dim">
          {t("settingsForm.additionalSettingsDesc")}
        </p>
        <div className="flex flex-col gap-2.5">
          {extra.map((row, i) => (
            <div key={i} className="grid grid-cols-1 items-center gap-2.5 md:grid-cols-[1fr_1.4fr_auto]">
              <input
                className={inputCls}
                placeholder={t("settingsForm.keyPlaceholder")}
                value={row.k}
                disabled={readOnly}
                onChange={(e) => setExtra((x) => x.map((r, j) => (j === i ? { ...r, k: e.target.value } : r)))}
              />
              <input
                className={inputCls}
                placeholder={t("settingsForm.valuePlaceholder")}
                value={row.v}
                disabled={readOnly}
                onChange={(e) => setExtra((x) => x.map((r, j) => (j === i ? { ...r, v: e.target.value } : r)))}
              />
              {readOnly ? null : (
              <button
                onClick={() => setExtra((x) => x.filter((_, j) => j !== i))}
                className="flex h-[38px] w-[38px] items-center justify-center rounded-[10px] border border-white/[0.08] bg-white/[0.03] text-[#e06c75] hover:bg-white/[0.06]"
                title={t("settingsForm.removeValue")}
              >
                <Trash2 size={15} strokeWidth={2.1} />
              </button>
              )}
            </div>
          ))}
        </div>
      </GlassCard>

      {readOnly ? null : (
        <div className="flex justify-end">
          <Button
            variant="primary"
            size="lg"
            onClick={handleSave}
            disabled={saving}
            className="w-full md:w-auto"
          >
            {saving ? t("settingsForm.saving") : t("settingsForm.save")}
          </Button>
        </div>
      )}
    </div>
  );
}
