import { motion } from "framer-motion";
import { ChevronRight } from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";

/**
 * A Settings section whose body collapses/expands with a height animation, keeping
 * the page's small-caps section-label style. The header (label + a rotating
 * chevron) toggles it.
 *
 * **Collapsed by default**, matching EmeHub, so the long Settings page opens as a
 * scannable index instead of a wall. The body stays mounted while collapsed, so
 * the settings draft and in-page anchors survive.
 *
 * `defaultOpen` is also honoured *after* mount: when it flips to true the section
 * opens. That is what keeps deep links working — `/settings#claude-account` (the
 * AI popover) and `/settings#execution` (the Execution screen's target chip) would
 * otherwise scroll to a collapsed heading and show nothing. It never force-closes,
 * so a section the user opened by hand stays open.
 */
export function CollapsibleSection({
  title,
  id,
  defaultOpen = false,
  onOpenChange,
  children,
}: {
  title: string;
  /** Anchor id for deep-links (kept on the section wrapper). */
  id?: string;
  defaultOpen?: boolean;
  /** Notified whenever the open state changes — lets a caller defer work (e.g. a
   * query) until its body is actually visible. Optional and purely additive. */
  onOpenChange?: (open: boolean) => void;
  children: ReactNode;
}) {
  const [open, setOpen] = useState(defaultOpen);

  // Open when the caller starts asking for it — a hash arriving after mount, e.g.
  // clicking "Manage Claude account" while already on Settings. Deliberately
  // one-way: it must not slam shut a section the user opened.
  useEffect(() => {
    if (defaultOpen) setOpen(true);
  }, [defaultOpen]);
  useEffect(() => {
    onOpenChange?.(open);
    // `onOpenChange` is intentionally not a dependency: callers commonly pass an
    // inline arrow, and re-firing on every render would defeat the point.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);
  // The height-collapse animation needs `overflow: hidden`, but that clips a
  // child card's hover-lift shadow/glow (#430) once a section is open. Keep it
  // hidden while collapsed/animating and switch to `visible` only after the
  // open animation settles, so an open section no longer crops child shadows.
  const [overflow, setOverflow] = useState<"hidden" | "visible">(defaultOpen ? "visible" : "hidden");
  return (
    <section id={id} className="mt-[26px] first:mt-0">
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
        className="group mb-3 flex w-full items-center gap-1.5 text-left text-[12px] font-bold tracking-[0.08em] text-[#6c6c7e] transition-colors hover:text-[#9494a6]"
      >
        <motion.span
          initial={false}
          animate={{ rotate: open ? 90 : 0 }}
          transition={{ duration: 0.2, ease: "easeOut" }}
          className="flex text-[#5c5c6e] transition-colors group-hover:text-[#9494a6]"
        >
          <ChevronRight size={13} strokeWidth={2.6} />
        </motion.span>
        {title}
      </button>
      <motion.div
        initial={false}
        animate={{ height: open ? "auto" : 0, opacity: open ? 1 : 0 }}
        transition={{ duration: 0.28, ease: [0.2, 0.8, 0.2, 1] }}
        // Clip during the transition; reveal (visible) once fully open so child
        // hover-lift shadows aren't cropped.
        onAnimationStart={() => setOverflow("hidden")}
        onAnimationComplete={() => setOverflow(open ? "visible" : "hidden")}
        style={{ overflow }}
      >
        {children}
      </motion.div>
    </section>
  );
}
