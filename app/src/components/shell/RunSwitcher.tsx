import { Check } from "lucide-react";
import { useEffect, useState, type RefObject } from "react";
import { createPortal } from "react-dom";
import { useTranslation } from "react-i18next";
import { useLocation, useNavigate } from "react-router-dom";
import { useRuns } from "@/hooks/queries";
import { cn } from "@/lib/cn";

const PANEL_WIDTH = 250;

/**
 * The "Switch run" dropdown (design frame A2, `.rswitch`). Controlled via props so
 * each trigger owns its own instance — both the run-context header button and the
 * workspace sidebar's run card open a menu anchored to the control that was
 * clicked. Portalled to `document.body` with fixed positioning anchored
 * below-right of the trigger so ancestor backdrop-filter/transform stacking
 * contexts can't trap it.
 *
 * Selecting a run navigates to that run **on the same pipeline sub-screen**
 * (e.g. from `/runs/12/review` to `/runs/34/review`), so the user keeps their
 * place. Closes on select, outside click, or Escape.
 */
export function RunSwitcher({
  open,
  onClose,
  anchorRef,
  runId,
}: {
  open: boolean;
  onClose: () => void;
  anchorRef: RefObject<HTMLButtonElement | null>;
  runId: number;
}) {
  const { data: runs } = useRuns();
  const navigate = useNavigate();
  const { t } = useTranslation("nav");
  const { pathname } = useLocation();
  const [pos, setPos] = useState<{ top: number; left: number } | null>(null);

  // Sub-screen segment after the run id (e.g. "review"), preserved on switch.
  // Matches the nested run route and the pre-#728 flat one.
  const seg =
    pathname.match(/^(?:\/projects\/[^/]+)?\/runs\/\d+\/(.+)$/)?.[1] ?? "";

  useEffect(() => {
    if (!open) return;
    const place = () => {
      const el = anchorRef.current;
      if (!el) return;
      const r = el.getBoundingClientRect();
      setPos({
        top: r.bottom + 6,
        left: Math.max(12, Math.min(r.right - PANEL_WIDTH, window.innerWidth - PANEL_WIDTH - 12)),
      });
    };
    place();
    const onDown = (e: MouseEvent) => {
      const t = e.target as Node;
      if (anchorRef.current?.contains(t)) return;
      if ((e.target as HTMLElement).closest?.("[data-run-switcher]")) return;
      onClose();
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    const reposition = () => place();
    window.addEventListener("mousedown", onDown);
    window.addEventListener("keydown", onKey);
    window.addEventListener("scroll", reposition, true);
    window.addEventListener("resize", reposition);
    return () => {
      window.removeEventListener("mousedown", onDown);
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("scroll", reposition, true);
      window.removeEventListener("resize", reposition);
    };
  }, [open, anchorRef, onClose]);

  if (!open || !pos) return null;

  // Switching runs can cross projects, so the target's own stamped project
  // decides the path (#727) — not the project currently in the URL. Sending a
  // Claims Portal run to a Surency Platform URL is exactly the misattribution
  // containment exists to prevent.
  const select = (id: number) => {
    const target = runs?.find((r) => r.id === id);
    const project = target?.projectGuid;
    const base = project
      ? `/projects/${encodeURIComponent(project)}/runs/${id}`
      : `/runs/${id}`;
    navigate(`${base}${seg ? `/${seg}` : ""}`);
    onClose();
  };

  return createPortal(
    <div
      data-run-switcher
      className="fixed z-[1000] rounded-[14px] border border-white/[0.12] p-[7px] shadow-[0_30px_70px_-20px_#000]"
      style={{ top: pos.top, left: pos.left, width: PANEL_WIDTH, background: "rgba(24,24,32,.97)" }}
    >
      <div className="px-[9px] pb-[5px] pt-2 text-[9px] font-bold tracking-[0.1em] text-ink-dim">
        {t("run.switchHint")}
      </div>
      {runs?.map((r) => {
        const on = r.id === runId;
        return (
          <button
            key={r.id}
            type="button"
            onClick={() => select(r.id)}
            className={cn(
              "flex w-full items-center gap-2.5 rounded-[10px] p-[9px] text-left hover:bg-white/[0.06]",
              on && "hover:bg-transparent",
            )}
            style={
              on
                ? {
                    background: "rgba(139,92,246,.16)",
                    boxShadow: "inset 0 0 0 1px rgba(139,92,246,.3)",
                  }
                : undefined
            }
          >
            <span
              className="flex h-6 w-6 shrink-0 items-center justify-center rounded-[7px]"
              style={{ background: on ? "linear-gradient(135deg,#8b5cf6,#6366f1)" : "rgba(255,255,255,.08)" }}
            >
              <svg
                width="12"
                height="12"
                viewBox="0 0 24 24"
                fill="none"
                stroke={on ? "#fff" : "#8b8b9e"}
                strokeWidth="2"
              >
                <rect x="3" y="4" width="18" height="4" rx="1.2" />
                <rect x="3" y="10" width="18" height="4" rx="1.2" />
              </svg>
            </span>
            <span className="min-w-0 flex-1">
              <span
                className="block font-mono text-[10.5px] font-semibold"
                style={{ color: on ? "#c4b5fd" : "#a78bfa" }}
              >
                {r.code}
              </span>
              <span
                className="block truncate text-[11px]"
                style={{ color: on ? "#c7c7d4" : "#8b8b9e" }}
              >
                {r.name}
              </span>
            </span>
            {on && <Check size={13} strokeWidth={3} style={{ color: "#6ee7b7" }} />}
          </button>
        );
      })}
    </div>,
    document.body,
  );
}
