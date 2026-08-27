import { motion } from "framer-motion";
import { Outlet, useLocation } from "react-router-dom";
import { GlobalSidebar } from "@/components/shell/GlobalSidebar";
import { TopBar } from "@/components/shell/TopBar";
import { MobileTopBar } from "@/components/shell/MobileTopBar";
import { MobileDrawer } from "@/components/shell/MobileDrawer";
import { CursorLight } from "@/components/effects/CursorLight";
import { ClickRipples } from "@/components/effects/ClickRipples";
import { useIsMobile } from "@/hooks/useIsMobile";

/**
 * One keyed fade-in per route. No AnimatePresence/exit + `mode="wait"`: that ran
 * exit-then-enter (felt delayed) and let the incoming page paint at full opacity
 * for a frame before `initial` applied (a flash, then a second fade). A plain
 * keyed motion.div applies `initial` inline on first paint, so the page fades in
 * once, immediately.
 */
function RouteContent() {
  const location = useLocation();
  return (
    <motion.div
      key={location.pathname}
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.24, ease: "easeOut" }}
      className="h-full"
    >
      <Outlet />
    </motion.div>
  );
}

/**
 * The persistent frame. On desktop (`md` and up): sidebar + top bar + scrollable
 * content. Below `md` the sidebar collapses into a slide-in drawer and the
 * desktop chrome is replaced by the compact mobile top bar, all inside a single
 * 480px-capped column. See MOBILE_SPEC §2.
 *
 * There is ONE sidebar now (ADR 0015): the shell no longer swaps into a "run
 * workspace" mode, because a run is a full-screen overlay portalled over this
 * frame rather than a screen inside it. The `Sidebar` brancher, `RunSidebar` and
 * the in-run `MobileStepperRail` are gone with it (#734).
 */
export function AppLayout() {
  const isMobile = useIsMobile();

  if (isMobile) {
    return (
      <div className="relative z-[2] mx-auto flex h-[100dvh] w-full max-w-[480px] flex-col gap-2 p-2">
        <MobileTopBar />
        <main className="min-h-0 flex-1 overflow-y-auto overflow-x-hidden rounded-[16px]">
          <RouteContent />
        </main>
        <MobileDrawer />
      </div>
    );
  }

  return (
    <div className="relative z-[2] flex h-screen w-screen gap-3.5 p-3.5">
      <GlobalSidebar />
      <div className="flex min-w-0 flex-1 flex-col gap-3.5">
        <TopBar />
        <main className="min-h-0 flex-1 overflow-y-auto rounded-[20px]">
          <RouteContent />
        </main>
      </div>
      <CursorLight />
      <ClickRipples />
    </div>
  );
}
