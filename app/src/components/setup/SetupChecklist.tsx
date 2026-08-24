import { ArrowRight, Check, CircleDashed, ExternalLink } from "lucide-react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";
import { GlassCard } from "@/components/ui/GlassCard";
import { Button } from "@/components/ui/Button";
import { useHubWebUrl, useReadiness } from "@/hooks/queries";
import { fixRoute, SETUP_ICONS } from "./setupItems";
import type { ReadinessItem } from "@/types/api";

/**
 * The full setup checklist (#643) — every prerequisite and its state, so a new
 * account can see the whole path to a working run in one place instead of
 * discovering it one failure at a time (#640).
 *
 * Unlike `SetupBlockers` this shows *optional* unmet items too, marked as such:
 * "you could pair a Local Agent to run on your machine" is useful information,
 * while presenting it as a failure is not.
 */
export function SetupChecklist() {
  const { t } = useTranslation("common");
  const { data: readiness } = useReadiness();
  if (!readiness) return null;

  const done = readiness.items.filter((i) => i.ready).length;

  return (
    <GlassCard className="p-5">
      <div className="mb-4 flex items-end justify-between gap-3">
        <div>
          <div className="text-[15px] font-bold">{t("setup.checklist.title")}</div>
          <div className="mt-1 text-[12.5px] text-muted">
            {readiness.ready
              ? t("setup.checklist.allSet")
              : t("setup.checklist.subtitle")}
          </div>
        </div>
        <div className="shrink-0 text-[12px] font-semibold text-ink-dim">
          {t("setup.checklist.progress", { done, total: readiness.items.length })}
        </div>
      </div>
      <div className="flex flex-col gap-2">
        {readiness.items.map((item) => (
          <ChecklistRow key={item.key} item={item} />
        ))}
      </div>
    </GlassCard>
  );
}

function ChecklistRow({ item }: { item: ReadinessItem }) {
  const { t } = useTranslation("common");
  const navigate = useNavigate();
  const hubWebUrl = useHubWebUrl();
  const Icon = SETUP_ICONS[item.key] ?? CircleDashed;
  const route = fixRoute(item.fix);
  const hubLink = item.fix === "hub" ? hubWebUrl : null;

  return (
    <div className="flex flex-wrap items-center gap-2.5 rounded-[12px] border border-white/[0.06] bg-white/[0.02] px-3 py-2.5">
      <span
        className="flex h-[22px] w-[22px] shrink-0 items-center justify-center rounded-full"
        style={
          // A managed item gets the neutral mark, not a tick: Q-Agent did not
          // verify it, and a green tick would claim a check it never ran (#651).
          item.ready && !item.managed
            ? { background: "rgba(16,185,129,.18)", color: "#6ee7b7" }
            : { background: "rgba(255,255,255,.06)", color: "#7a7a8c" }
        }
      >
        {item.ready && !item.managed ? (
          <Check size={13} strokeWidth={2.8} />
        ) : (
          <Icon size={12} strokeWidth={2.2} />
        )}
      </span>
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-[13px] font-semibold">{t(`setup.items.${item.key}.title`)}</span>
          {/* An unmet item that blocks nothing right now is labelled optional
              rather than left looking like a failure. */}
          {item.managed ? (
            <span className="rounded-full bg-white/[0.07] px-2 py-[1px] text-[10.5px] font-semibold text-faint">
              {t("setup.managed")}
            </span>
          ) : (
            !item.ready &&
            !item.required && (
              <span className="rounded-full bg-white/[0.07] px-2 py-[1px] text-[10.5px] font-semibold text-faint">
                {t("setup.optional")}
              </span>
            )
          )}
        </div>
        <div className="mt-0.5 text-[12px] text-ink-dim">
          {item.detail || t(`setup.items.${item.key}.why`)}
        </div>
      </div>
      {(!item.ready || item.managed) &&
        (route ? (
          <Button variant="glass" size="sm" onClick={() => navigate(route)}>
            {t(`setup.fix.${item.fix}`)} <ArrowRight size={12} strokeWidth={2.4} />
          </Button>
        ) : hubLink ? (
          <Button variant="glass" size="sm" onClick={() => window.open(hubLink, "_blank", "noopener")}>
            {t("setup.fix.hub")} <ExternalLink size={12} strokeWidth={2.4} />
          </Button>
        ) : (
          <span className="text-[11.5px] text-faint">{t("setup.fix.hubNoLink")}</span>
        ))}
    </div>
  );
}
