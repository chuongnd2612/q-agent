import { Menu } from "lucide-react";
import { useTranslation } from "react-i18next";
import { useLocation } from "react-router-dom";
import { AiActivityIndicator } from "@/components/shell/AiActivityIndicator";
import { ClaudeStatsButton } from "@/components/shell/ClaudeStatsButton";
import { useUI } from "@/store/ui";

/** In-run sub-route (null seg = index/overview) → key under `nav:mobile.stage`. */
/** Pick the `nav:mobile.global.<key>` bucket for a global (non-run) route. */
function globalTitleKey(pathname: string): string {
  const map: [RegExp, string][] = [
    [/^\/$/, "dashboard"],
    [/^\/projects\/[^/]+/, "project"],
    [/^\/projects/, "projects"],
    [/^\/tickets\/[^/]+/, "ticket"],
    [/^\/tickets/, "tickets"],
    [/^\/runs/, "runs"],
    [/^\/reports/, "reports"],
    [/^\/audit/, "audit"],
    [/^\/settings\/users/, "users"],
    [/^\/settings\/claude-credentials/, "claudeCreds"],
    [/^\/settings\/shared-workspace/, "sharedWorkspace"],
    [/^\/settings/, "settings"],
    [/^\/getting-started/, "gettingStarted"],
    [/^\/local-agent/, "localAgent"],
    [/^\/profile/, "profile"],
  ];
  return map.find(([re]) => re.test(pathname))?.[1] ?? "fallback";
}

/**
 * The compact top bar shown below the `md` breakpoint in place of the desktop
 * sidebar + top bar: hamburger (opens the nav drawer) · centered title/subtitle.
 *
 * No in-run variant any more (#734). It used to retitle itself to the run's
 * current stage and swap its right action for "exit run"; a run is now a
 * full-screen overlay with its own top bar, portalled OVER this frame, so that
 * branch could never be seen — dead code that still had to be kept in sync with
 * the stage list. See MOBILE_SPEC §1a.
 */
export function MobileTopBar() {
  const { pathname } = useLocation();
  const { t } = useTranslation("nav");
  const openDrawer = useUI((s) => s.openDrawer);

  const gKey = globalTitleKey(pathname);
  const title = t(`mobile.global.${gKey}.t`);
  const subtitle = t(`mobile.global.${gKey}.s`);

  return (
    <header
      className="glass-strong z-20 flex shrink-0 items-center gap-3 rounded-[16px] px-3.5 py-2.5"
    >
      <button
        onClick={openDrawer}
        aria-label={t("aria.openNav")}
        className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl border border-white/[0.08] bg-white/[0.05] text-ink-soft transition-colors active:bg-white/[0.12]"
      >
        <Menu size={19} strokeWidth={2.1} />
      </button>

      <div className="min-w-0 flex-1 text-center">
        <div className="truncate text-[15.5px] font-extrabold tracking-tight">{title}</div>
        {subtitle && <div className="truncate text-[10.5px] font-medium text-[#7a7a8c]">{subtitle}</div>}
      </div>

      <AiActivityIndicator />
      <ClaudeStatsButton />

    </header>
  );
}
