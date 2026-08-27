import { AnimatePresence, motion } from "framer-motion";
import { LogOut, Sparkles, User, UserRound, X } from "lucide-react";
import { createPortal } from "react-dom";
import { useTranslation } from "react-i18next";
import { useLocation, useNavigate } from "react-router-dom";
import { cn } from "@/lib/cn";
import {
  ADMIN_NAV,
  PRIMARY_NAV,
  SECONDARY_NAV,
  activeNavPath,
  type NavItem,
} from "@/components/shell/navConfig";
import { SidebarProjectTree } from "@/components/shell/SidebarProjectTree";
import { useHubDataEnabled } from "@/hooks/queries";
import { useLogout } from "@/hooks/useLogout";
import { useAuth } from "@/store/auth";
import { useUI } from "@/store/ui";

/**
 * The left slide-in navigation drawer — the mobile presentation of the desktop
 * `GlobalSidebar`. It presents the WORKSPACE nav, the project tree (#729) and
 * the ADMIN group for admins.
 *
 * There is no in-run mode any more (#734): a run is a full-screen overlay with
 * its own stepper, so the drawer no longer carries a run-context card or an
 * "All of Q-Agent" exit. Navigation is still URL-driven — this is a responsive
 * presentation of the same routes, opened via `ui.drawerOpen`. See MOBILE_SPEC
 * §1b.
 */
export function MobileDrawer() {
  const open = useUI((s) => s.drawerOpen);
  const closeDrawer = useUI((s) => s.closeDrawer);
  const navigate = useNavigate();
  const { t } = useTranslation("nav");
  const { pathname } = useLocation();
  const user = useAuth((s) => s.user);
  const isAdmin = user?.role === "admin";
  // The ADMIN section is Q-Agent administering ITSELF — users, the Claude
  // credential, the shared workspace, the audit trail. Under hub management
  // EmeHub owns all of that (#651 for the credential, SSO/JIT for users), so
  // showing it here offers settings that either do nothing or fight the hub.
  //
  // Gated on `resolved` as well: the flag arrives from `/health`, and rendering a
  // RESTRICTED section for a moment before hiding it is worse than showing it a
  // beat late — so it appears only once we know the hub does NOT own it.
  const { enabled: hubManaged, resolved: hubResolved } = useHubDataEnabled();
  const showAdmin = isAdmin && hubResolved && !hubManaged;
  const logout = useLogout();

  const displayName = user ? `${user.firstName ?? ""} ${user.lastName ?? ""}`.trim() : "";
  const initials = user
    ? `${user.firstName?.[0] ?? ""}${user.lastName?.[0] ?? ""}`.toUpperCase()
    : "";

  const allNav = [...PRIMARY_NAV, ...SECONDARY_NAV, ...(showAdmin ? ADMIN_NAV : [])];
  const activePath = activeNavPath(allNav, pathname);

  const go = (path: string) => {
    closeDrawer();
    navigate(path);
  };

  const renderItem = (n: NavItem) => {
    const active = n.path === activePath;
    const Icon = n.icon;
    return (
      <button
        key={n.path}
        onClick={() => go(n.path)}
        className={cn(
          "flex w-full items-center gap-[13px] rounded-xl px-3 py-[11px] text-left text-[14px] font-semibold transition-colors",
          active ? "text-white" : "text-ink-dim active:bg-white/[0.06]",
        )}
        style={
          active
            ? { background: "linear-gradient(135deg,rgba(139,92,246,.9),rgba(99,102,241,.75))" }
            : undefined
        }
      >
        <span className="flex w-[18px] justify-center">
          <Icon size={18} strokeWidth={2} />
        </span>
        <span className="flex-1">{t(`items.${n.key}`)}</span>
      </button>
    );
  };

  return createPortal(
    <AnimatePresence>
      {open && (
        <motion.div
          key="drawer-scrim"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.22 }}
          onClick={closeDrawer}
          className="fixed inset-0 z-[80] md:hidden"
          style={{ background: "rgba(4,4,8,.62)", backdropFilter: "blur(2px)" }}
        >
          <motion.aside
            key="drawer-panel"
            initial={{ x: "-100%" }}
            animate={{ x: 0 }}
            exit={{ x: "-100%" }}
            transition={{ duration: 0.3, ease: [0.2, 0.8, 0.2, 1] }}
            onClick={(e) => e.stopPropagation()}
            className="absolute bottom-0 left-0 top-0 flex w-[82%] max-w-[320px] flex-col overflow-y-auto border-r border-white/[0.08] p-4"
            style={{
              background: "rgba(15,15,22,.97)",
              backdropFilter: "blur(30px)",
              boxShadow: "30px 0 70px -20px rgba(0,0,0,.8)",
            }}
          >
            {/* Brand header */}
            <div className="mb-4 flex items-center gap-[11px]">
              <div className="accent-gradient flex h-9 w-9 items-center justify-center rounded-[11px] shadow-[0_6px_18px_-4px_rgba(139,92,246,.7)]">
                <Sparkles size={19} color="#fff" strokeWidth={2.2} />
              </div>
              <div className="flex-1">
                <div className="text-[17px] font-black leading-tight tracking-tight">Q&#8209;Agent</div>
                <div className="text-[10px] font-medium tracking-[0.04em] text-[#7a7a8c]">
                  {t("brand.tagline")}
                </div>
              </div>
              <button
                onClick={closeDrawer}
                aria-label={t("aria.closeNav")}
                className="flex h-8 w-8 items-center justify-center rounded-[10px] bg-white/[0.05] text-ink-dim active:bg-white/[0.12]"
              >
                <X size={16} strokeWidth={2.2} />
              </button>
            </div>

            {/* Nav groups */}
            <div className="px-1 pb-2 text-[10px] font-semibold tracking-[0.11em] text-[#5c5c6e]">
              {t("sections.workspace")}
            </div>
            <nav className="flex flex-col gap-0.5">
              {PRIMARY_NAV.map(renderItem)}
              {/* Same tree component as the desktop rail, so the two
                  presentations of the nav cannot drift (#729). */}
              <SidebarProjectTree variant="mobile" onNavigate={closeDrawer} />
              <hr className="mx-1 my-2 border-0 border-t border-white/[0.06]" />
              {SECONDARY_NAV.map(renderItem)}
            </nav>

            {showAdmin && (
              <>
                <div className="flex items-center gap-2 px-1 pb-2 pt-4">
                  <span className="text-[10px] font-semibold tracking-[0.11em] text-[#5c5c6e]">{t("sections.admin")}</span>
                  <span className="rounded-full border border-white/10 bg-white/[0.04] px-[7px] py-[1.5px] text-[8.5px] font-bold uppercase tracking-[0.07em] text-[#7a7a8c]">
                    {t("sections.restricted")}
                  </span>
                </div>
                <nav className="flex flex-col gap-0.5">{ADMIN_NAV.map(renderItem)}</nav>
              </>
            )}

            {/* Profile + logout */}
            <div className="mt-auto flex items-center gap-2.5 border-t border-white/[0.06] pt-3">
              <button
                onClick={() => go("/profile")}
                className="flex min-w-0 flex-1 items-center gap-2.5 rounded-2xl px-1.5 py-1.5 text-left active:bg-white/[0.05]"
              >
                <div className="h-8 w-8 shrink-0 rounded-[10px]">
                  {initials ? (
                    <div
                      className="flex h-full w-full items-center justify-center rounded-[10px] text-[13px] font-bold text-white"
                      style={{ background: "linear-gradient(135deg,#f59e0b,#f43f5e)" }}
                    >
                      {initials}
                    </div>
                  ) : (
                    <div className="flex h-full w-full items-center justify-center rounded-[10px] bg-white/[0.08] text-[#9494a6]">
                      <User size={16} strokeWidth={2} />
                    </div>
                  )}
                </div>
                <div className="min-w-0 flex-1">
                  <div className="truncate text-[12.5px] font-semibold">
                    {displayName || t("account.setIdentity")}
                  </div>
                  <div className="truncate text-[10.5px] capitalize text-[#7a7a8c]">
                    {user?.role || t("account.settingsProfile")}
                  </div>
                </div>
                <UserRound size={16} strokeWidth={2} className="shrink-0 text-ink-dim" />
              </button>
              <button
                onClick={logout}
                aria-label={t("aria.logout")}
                className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-rose-500/10 text-rose-300 active:bg-rose-500/20"
              >
                <LogOut size={16} strokeWidth={2} />
              </button>
            </div>
          </motion.aside>
        </motion.div>
      )}
    </AnimatePresence>,
    document.body,
  );
}
