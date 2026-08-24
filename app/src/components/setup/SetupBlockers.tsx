import { AlertTriangle, ArrowRight, ExternalLink } from "lucide-react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";
import { Button } from "@/components/ui/Button";
import { useHubWebUrl, useReadiness } from "@/hooks/queries";
import { blockers, fixRoute, SETUP_ICONS } from "./setupItems";

/**
 * The prerequisites that will make the action on this screen fail (#643).
 *
 * Shown only for items that are unmet AND required under the settings in force,
 * so a server-target user never sees "pair a Local Agent" — an alarm that fires
 * on things which don't matter is one users learn to scroll past.
 *
 * `only` narrows it to the items a given screen's action actually needs. A screen
 * that lists everything would report the Automation blockers on the Execution
 * page and vice versa, which makes both look broken.
 */
export function SetupBlockers({ only }: { only?: string[] }) {
  const { t } = useTranslation("common");
  const navigate = useNavigate();
  const hubWebUrl = useHubWebUrl();
  const { data: readiness } = useReadiness();

  let items = blockers(readiness?.items);
  if (only) items = items.filter((i) => only.includes(i.key));
  if (!items.length) return null;

  return (
    <div
      className="mb-3.5 rounded-[18px] border px-4 py-3.5"
      style={{ background: "rgba(251,191,36,.09)", borderColor: "rgba(251,191,36,.28)" }}
    >
      <div className="flex items-start gap-3">
        <AlertTriangle size={18} className="mt-[1px] shrink-0 text-warning-soft" strokeWidth={2.2} />
        <div className="min-w-0 flex-1">
          <div className="text-[13.5px] font-bold text-warning-soft">
            {t("setup.blockers.title", { count: items.length })}
          </div>
          <div className="mt-2 flex flex-col gap-2.5">
            {items.map((item) => {
              const Icon = SETUP_ICONS[item.key] ?? AlertTriangle;
              const route = fixRoute(item.fix);
              const hubLink = item.fix === "hub" ? hubWebUrl : null;
              return (
                <div key={item.key} className="flex flex-wrap items-center gap-2.5">
                  <Icon size={14} className="shrink-0 text-ink-dim" strokeWidth={2.1} />
                  <span className="text-[13px] font-semibold">{t(`setup.items.${item.key}.title`)}</span>
                  {/* The server's own words: it names the missing thing precisely,
                      and a second copy of that wording here would drift. */}
                  <span className="min-w-0 flex-1 text-[12px] text-ink-dim">{item.detail}</span>
                  {route ? (
                    <Button variant="glass" size="sm" onClick={() => navigate(route)}>
                      {t(`setup.fix.${item.fix}`)} <ArrowRight size={12} strokeWidth={2.4} />
                    </Button>
                  ) : hubLink ? (
                    <Button
                      variant="glass"
                      size="sm"
                      onClick={() => window.open(hubLink, "_blank", "noopener")}
                    >
                      {t("setup.fix.hub")} <ExternalLink size={12} strokeWidth={2.4} />
                    </Button>
                  ) : (
                    // Hub-managed with no known hub URL: say where it lives rather
                    // than render a button that goes nowhere.
                    <span className="text-[11.5px] text-faint">{t("setup.fix.hubNoLink")}</span>
                  )}
                </div>
              );
            })}
          </div>
        </div>
      </div>
    </div>
  );
}
