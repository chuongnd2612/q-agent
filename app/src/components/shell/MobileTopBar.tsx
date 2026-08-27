import { Menu, X } from "lucide-react";
import { useTranslation } from "react-i18next";
import { useLocation, useNavigate } from "react-router-dom";
import { AiActivityIndicator } from "@/components/shell/AiActivityIndicator";
import { ClaudeStatsButton } from "@/components/shell/ClaudeStatsButton";
import { useRunRouteId } from "@/hooks/useRunRouteId";
import { useRun } from "@/hooks/queries";
import { useUI } from "@/store/ui";

/** In-run sub-route (null seg = index/overview) → key under `nav:mobile.stage`. */
const RUN_STAGE_KEY: Record<string, string> = {
  "": "overview",
  review: "review",
  sync: "sync",
  automation: "automation",
  execution: "execution",
  evidence: "evidence",
  comment: "comment",
};

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
 * sidebar + top bar: hamburger (opens the nav drawer) · centered title/subtitle
 * · a single right action. On a global screen the action is "+ new run"; inside
 * a run it becomes "✕ exit run" (back to Dashboard). See MOBILE_SPEC §1a.
 */
export function MobileTopBar() {
  const navigate = useNavigate();
  const { pathname } = useLocation();
  const { t } = useTranslation("nav");
  const runId = useRunRouteId();
  const { data: run } = useRun(runId ?? null);
  const openDrawer = useUI((s) => s.openDrawer);

  const inRun = runId != null;
  const seg = pathname.match(/^\/runs\/\d+(?:\/(\w+))?/)?.[1] ?? "";
  const gKey = globalTitleKey(pathname);
  const title = inRun
    ? t(`mobile.stage.${RUN_STAGE_KEY[seg] ?? "fallback"}`)
    : t(`mobile.global.${gKey}.t`);
  const subtitle = inRun
    ? run
      ? `${run.code} · ${run.name}`
      : t("run.loading")
    : t(`mobile.global.${gKey}.s`);

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

      {/* No global "new run" action any more (#729): a run is created only from
          inside a project, so it can never exist without one. Inside a run the
          slot is still the exit. */}
      {inRun && (
        <button
          onClick={() => navigate("/")}
          aria-label={t("aria.exitRun")}
          className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl border border-white/[0.08] bg-white/[0.05] text-[#c7c7d4] transition-colors active:bg-white/[0.12]"
        >
          <X size={18} strokeWidth={2.2} />
        </button>
      )}
    </header>
  );
}
