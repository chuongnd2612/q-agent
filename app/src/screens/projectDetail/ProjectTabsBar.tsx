import { useTranslation } from "react-i18next";
import { type ProjectTab } from "@/store/ui";

// Only the tabs that are genuinely views OF this project (#693). `tickets` and
// `runs` used to sit here and navigated straight out to the GLOBAL /tickets and
// /runs screens — so they read as "this project's tickets", then dropped the user
// into an unfiltered list with no way back to the tab they clicked. Both already
// have their own sidebar entries, which is where a global list belongs.
export const TABS: { id: ProjectTab; labelKey: string }[] = [
  { id: "overview", labelKey: "tabs.overview" },
  { id: "knowledge", labelKey: "tabs.knowledge" },
  { id: "settings", labelKey: "tabs.settings" },
];

/** Project detail tab bar. Highlights the active tab; each click is delegated to
 * `onSelect` (the screen routes tickets/runs away and query-syncs the rest). */
export function ProjectTabsBar({
  active,
  onSelect,
}: {
  active: ProjectTab;
  onSelect: (id: ProjectTab) => void;
}) {
  const { t } = useTranslation("projects");
  return (
    <div className="mb-[18px] flex flex-wrap gap-2 border-b border-white/[0.06] pb-4">
      {TABS.map((tab) => {
        const isActive = active === tab.id;
        return (
          <button
            key={tab.id}
            onClick={() => onSelect(tab.id)}
            className="cursor-pointer whitespace-nowrap rounded-[11px] border-none px-[15px] py-[9px] text-[13px] font-semibold"
            style={
              isActive
                ? {
                    background: "linear-gradient(135deg,rgba(139,92,246,.24),rgba(99,102,241,.12))",
                    color: "#fff",
                    boxShadow: "inset 0 0 0 1px rgba(139,92,246,.3)",
                  }
                : { background: "rgba(255,255,255,.04)", color: "#a0a0b2" }
            }
          >
            {t(tab.labelKey)}
          </button>
        );
      })}
    </div>
  );
}
