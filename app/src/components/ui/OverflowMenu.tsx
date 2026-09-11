import { AnimatePresence, motion } from "framer-motion";
import { MoreHorizontal } from "lucide-react";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { useTranslation } from "react-i18next";

const MENU_WIDTH = 190;

export interface OverflowItem {
  key: string;
  label: string;
  icon?: ReactNode;
  onClick: () => void;
  disabled?: boolean;
}

/**
 * A compact "⋯" overflow menu for secondary toolbar actions. Portalled to
 * `document.body` with fixed positioning anchored to the trigger (per the
 * project's floating-overlay rule — a toolbar sits inside transformed/animated
 * ancestors that would otherwise trap the menu's z-index). Closes on
 * outside-click / Escape / selection. Mirrors `RunActionsMenu`'s pattern,
 * generalized to a caller-supplied item list.
 */
export function OverflowMenu({ items, title }: { items: OverflowItem[]; title?: string }) {
  const { t } = useTranslation("commands");
  const [open, setOpen] = useState(false);
  const [pos, setPos] = useState<{ top: number; left: number } | null>(null);
  const btnRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (!open) return;
    const place = () => {
      const el = btnRef.current;
      if (!el) return;
      const r = el.getBoundingClientRect();
      setPos({
        top: r.bottom + 6,
        left: Math.max(12, Math.min(r.right - MENU_WIDTH, window.innerWidth - MENU_WIDTH - 12)),
      });
    };
    place();
    const onDown = (e: MouseEvent) => {
      const t = e.target as Node;
      if (btnRef.current?.contains(t)) return;
      if ((e.target as HTMLElement).closest?.("[data-overflow-menu]")) return;
      setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        setOpen(false);
      }
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
  }, [open]);

  return (
    <>
      <button
        ref={btnRef}
        type="button"
        onClick={() => setOpen((o) => !o)}
        title={title ?? t("overflow.moreActions")}
        className="flex h-[30px] w-[30px] shrink-0 items-center justify-center rounded-[9px] border border-bd2 bg-card2 text-ink-soft transition-colors hover:bg-card3"
        style={open ? { background: "var(--card3)" } : undefined}
      >
        <MoreHorizontal size={15} />
      </button>

      {createPortal(
        <AnimatePresence>
          {open && pos && (
            <motion.div
              data-overflow-menu
              initial={{ opacity: 0, y: -6, scale: 0.98 }}
              animate={{ opacity: 1, y: 0, scale: 1 }}
              exit={{ opacity: 0, y: -6, scale: 0.98 }}
              transition={{ duration: 0.14 }}
              className="fixed z-[1000] overflow-hidden rounded-[14px] border border-bd2 bg-pop p-1.5 shadow-pop"
              style={{ top: pos.top, left: pos.left, width: MENU_WIDTH }}
              onMouseDown={(e) => e.stopPropagation()}
            >
              {items.map((it) => (
                <button
                  key={it.key}
                  type="button"
                  disabled={it.disabled}
                  onClick={() => {
                    setOpen(false);
                    it.onClick();
                  }}
                  className="flex w-full items-center gap-2.5 rounded-[10px] px-2.5 py-2 text-left text-[12.5px] font-semibold text-ink-soft hover:bg-card3 disabled:opacity-40 disabled:hover:bg-transparent"
                >
                  {it.icon && <span className="flex h-3.5 w-3.5 shrink-0 items-center justify-center">{it.icon}</span>}
                  {it.label}
                </button>
              ))}
            </motion.div>
          )}
        </AnimatePresence>,
        document.body,
      )}
    </>
  );
}
