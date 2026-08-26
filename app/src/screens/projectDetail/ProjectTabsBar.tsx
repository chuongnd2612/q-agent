import { useTranslation } from "react-i18next";
import { PROJECT_TABS, type ProjectTab } from "./projectTabs";

/** Project detail tab bar. Highlights the active tab; each click is delegated to
 * `onSelect`, which navigates to that tab's own route (ADR 0015 slice 2).
 *
 * `tickets` and `runs` are back after #693 removed them. The bug then was not
 * that they existed — it was that they navigated *out* of the project into the
 * unfiltered global lists, so a tab labelled "this project's tickets" showed
 * every ticket in the workspace with no way back. Under containment they render
 * the project's own rows, in place. */
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
      {PROJECT_TABS.map((tab) => {
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
