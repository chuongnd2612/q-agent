import { useTranslation } from "react-i18next";
import { GlassCard } from "@/components/ui/GlassCard";
import { CollapsibleSection } from "@/components/settings/CollapsibleSection";
import { PLAN_GROUPS, type AutomationPlan, type PlanEntry } from "./specStatus";

/**
 * The ticket's REUSE / EXTEND / CREATE plan, shown beside the gate report (#544).
 *
 * The plan is decided **before** generation (doc §8/§24) and is what authorises the
 * spec's imports, so it is the artifact a reviewer reads to judge whether the reuse
 * decisions were sound. This slice deliberately surfaces the plan while authoring no
 * page objects yet (#545 does that) — the decisions are meant to be watched before
 * they are trusted.
 *
 * The counts row is the epic's own success metric ("how little new code is generated
 * while still fully covering the new test cases"), so it stays visible even while the
 * per-asset detail is collapsed — `CollapsibleSection` is collapsed by default since
 * #536.
 *
 * Renders nothing without a plan: a legacy spec has no `planReport` and must look
 * exactly as it did before this slice.
 */
export function PlanReport({ plan }: { plan: AutomationPlan | null }) {
  const { t } = useTranslation("pipeline");
  if (!plan) return null;

  const counts = plan.counts ?? {};
  const groups = PLAN_GROUPS.map((group) => ({ group, entries: plan[group] ?? [] })).filter(
    (g) => g.entries.length > 0,
  );

  return (
    <GlassCard className="mt-3 p-3">
      <div className="flex flex-wrap items-baseline gap-x-2.5 gap-y-1">
        <span className="text-[10.5px] font-semibold tracking-wider text-faint">
          {t("plan.title")}
        </span>
        {plan.feature && (
          <span className="min-w-0 truncate text-[12px] text-ink-soft">{plan.feature}</span>
        )}
      </div>
      <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
        {(["reuse", "extend", "create", "reuse-base"] as const).map((action) => (
          <CountChip key={action} action={action} count={counts[action] ?? 0} label={t(`plan.actions.${actionKey(action)}`)} />
        ))}
      </div>
      <p className="mt-1.5 text-[10.5px] leading-snug text-muted">{t("plan.pendingHint")}</p>
      <div className="mt-1">
        {groups.map(({ group, entries }) => (
          <CollapsibleSection key={group} title={`${t(`plan.groups.${group}`)} · ${entries.length}`}>
            <div className="mb-1 flex flex-col gap-1">
              {entries.map((entry) => (
                <PlanRow key={`${entry.action}:${entry.path || entry.name}`} entry={entry} />
              ))}
            </div>
          </CollapsibleSection>
        ))}
      </div>
    </GlassCard>
  );
}

/** i18n keys can't contain the hyphen in `reuse-base`; map it to a valid segment. */
function actionKey(action: string): string {
  return action === "reuse-base" ? "reuseBase" : action;
}

const ACTION_HUE: Record<string, string> = {
  reuse: "#34d399",
  extend: "#60a5fa",
  create: "#fbbf24",
  "reuse-base": "#a78bfa",
};

function CountChip({ action, count, label }: { action: string; count: number; label: string }) {
  const hue = ACTION_HUE[action] ?? "#8b8b9e";
  const active = count > 0;
  return (
    <span
      className="inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-[10.5px] font-medium"
      style={{
        background: active ? `${hue}1f` : "rgba(255,255,255,.04)",
        color: active ? hue : "#6c6c7e",
      }}
    >
      <span className="font-mono text-[11px]">{count}</span>
      {label}
    </span>
  );
}

function PlanRow({ entry }: { entry: PlanEntry }) {
  const { t } = useTranslation("pipeline");
  const hue = ACTION_HUE[entry.action] ?? "#8b8b9e";
  // An `extend` names methods that do NOT exist yet — the honest label matters,
  // because the generator is told to keep those steps inline until #545 authors them.
  const pending =
    entry.action === "extend"
      ? (entry.methods ?? []).filter((m) => !(entry.existingMethods ?? []).includes(m))
      : entry.action === "create"
        ? (entry.methods ?? [])
        : [];
  const existing = entry.action === "reuse" ? entry.methods ?? [] : entry.existingMethods ?? [];
  return (
    <div className="rounded-[10px] bg-white/[.03] px-2.5 py-1.5">
      <div className="flex items-center gap-2">
        <span
          className="shrink-0 rounded px-1.5 py-px text-[9.5px] font-semibold uppercase tracking-wider"
          style={{ background: `${hue}22`, color: hue }}
        >
          {t(`plan.actions.${actionKey(entry.action)}`)}
        </span>
        <span className="min-w-0 flex-1 truncate text-[12px] text-ink-soft">{entry.name}</span>
        {entry.path && (
          // A plain `title` rather than the portalled PathTooltip: this row is a
          // flex line inside an animated card, and the tooltip's wrapper div would
          // fight the layout for no gain on a path that is already short.
          <span
            title={entry.path}
            className="max-w-[48%] shrink-0 truncate font-mono text-[10.5px] text-faint"
          >
            {entry.path}
          </span>
        )}
      </div>
      {existing.length > 0 && (
        <div className="mt-0.5 truncate font-mono text-[10.5px] text-muted">{existing.join(", ")}</div>
      )}
      {pending.length > 0 && (
        <div className="mt-0.5 truncate font-mono text-[10.5px]" style={{ color: "#fbbf24" }}>
          {t("plan.notYetAuthored")}: {pending.join(", ")}
        </div>
      )}
      {entry.reason && <div className="mt-0.5 text-[10.5px] leading-snug text-muted">{entry.reason}</div>}
    </div>
  );
}
