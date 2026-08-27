import { Search } from "lucide-react";
import { useTranslation } from "react-i18next";
import { AiActivityIndicator } from "@/components/shell/AiActivityIndicator";
import { ClaudeStatsButton } from "@/components/shell/ClaudeStatsButton";
import { LanguageSwitcher } from "@/components/shell/LanguageSwitcher";
import { useUI } from "@/store/ui";

/**
 * The global header. Under ADR 0015 (#729) it lost three things:
 *
 * - the **run-context bar** — it swapped the whole header out on a run route,
 *   which was the header half of "workspace mode"; a run is an overlay now and
 *   the shell never changes mode. (`RunContextHeader` is deleted, #734.)
 * - the **project pill** — it was a project switcher,
 *   and there is deliberately no quick switcher: you move between projects
 *   through the sidebar tree or the Projects list.
 * - the global **New Run** button — a run can only be created from inside a
 *   project, so it cannot exist without a project and a provider.
 */
export function TopBar() {
  const openPalette = useUI((s) => s.openPalette);
  const { t } = useTranslation("nav");

  return (
    <header className="glass-strong flex h-[56px] shrink-0 items-center gap-3.5 rounded-[18px] px-[18px]">
      <button
        data-tour="topbar-search"
        onClick={openPalette}
        className="flex h-[38px] max-w-[420px] flex-1 cursor-text items-center gap-2.5 rounded-xl border border-white/[0.07] bg-white/[0.04] px-3.5 text-[#7a7a8c] hover:border-[rgba(139,92,246,.4)]"
      >
        <Search size={15} strokeWidth={2} />
        <span className="flex-1 text-left text-[13px]">{t("topbar.searchPlaceholder")}</span>
        <span className="rounded-md border border-white/[0.08] bg-white/[0.06] px-[7px] py-0.5 font-mono text-[11px]">
          &#8984;K
        </span>
      </button>

      <div className="ml-auto flex items-center gap-2">
        <LanguageSwitcher />
        <AiActivityIndicator />
        <ClaudeStatsButton />
      </div>
    </header>
  );
}
