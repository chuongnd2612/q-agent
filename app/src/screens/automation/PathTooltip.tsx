import { useCallback, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";

/**
 * Hover/focus tooltip for a truncated file path.
 *
 * The tooltip is **portalled to `document.body`** with `position: fixed`, anchored
 * to the trigger's bounding rect (see CLAUDE.md): the file list lives inside a
 * `GlassCard`, which is a `motion.div` whose hover transform creates a stacking
 * context — a plain absolutely-positioned child would be trapped underneath the
 * code panel regardless of `z-index`. The surface is deliberately **opaque** (no
 * `backdrop-filter`), since it floats over the animated shell.
 *
 * Not a Framer Motion element on purpose: a fade would need `AnimatePresence` as
 * the direct parent inside the portal, and a path label doesn't earn it.
 */
export function PathTooltip({ label, children }: { label: string; children: ReactNode }) {
  const [rect, setRect] = useState<{ top: number; left: number } | null>(null);

  const show = useCallback((el: HTMLElement | null) => {
    if (!el) return;
    const r = el.getBoundingClientRect();
    // Anchor to the trigger's right edge (the list is a narrow sidebar column,
    // so a tooltip below would cover the next row), clamped into the viewport.
    setRect({
      top: Math.max(8, Math.min(r.top, window.innerHeight - 40)),
      left: Math.max(8, Math.min(r.right + 8, window.innerWidth - 330)),
    });
  }, []);
  const hide = useCallback(() => setRect(null), []);

  return (
    // A real box, not `display: contents`: a contents wrapper measures as an
    // empty 0×0 rect, which would pin the tooltip to the viewport corner.
    <div
      className="flex flex-col"
      onPointerEnter={(e) => show(e.currentTarget)}
      onPointerLeave={hide}
      onFocus={(e) => show(e.currentTarget)}
      onBlur={hide}
      onClick={hide}
    >
      {children}
      {rect != null &&
        createPortal(
          <div
            role="tooltip"
            className="pointer-events-none fixed z-[80] max-w-[320px] truncate rounded-lg border border-white/10 px-2.5 py-1.5 font-mono text-[11px] text-ink-soft shadow-lg"
            // Opaque background — never backdrop-filter over animated content.
            style={{ top: rect.top, left: rect.left, background: "#12121a" }}
          >
            {label}
          </div>,
          document.body,
        )}
    </div>
  );
}
