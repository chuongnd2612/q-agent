import { motion } from "framer-motion";
import type { ReactNode } from "react";
import { cn } from "@/lib/cn";
import { useTilt } from "@/hooks/useTilt";

interface GlassCardProps {
  children: ReactNode;
  className?: string;
  hover?: boolean;
  /** stagger index for the fade-in-up entrance */
  index?: number;
  onClick?: () => void;
  style?: React.CSSProperties;
  /** Opt a card into the cursor-tracked 3D tilt (the hover brighten always
   *  applies). Defaults to false — only the Dashboard cards enable it.
   *  Mutually exclusive with `lift`. */
  tilt?: boolean;
  /** Opt a card into the hover-lift effect (raise + expanded shadow on hover,
   *  as on the /runs and /tickets rows). Defaults to false. */
  lift?: boolean;
}

/**
 * Frosted glass surface — the design's default panel. Cards brighten on hover.
 * Two opt-in hover motions are available and mutually exclusive: `tilt` (a
 * cursor-tracked 3D tilt pivoting from the top edge, used only on the Dashboard
 * — see `useTilt` / `design/tilt-effect.html`) and `lift` (a plain raise +
 * expanded shadow, matching the /runs & /tickets rows). Cards flagged `hover` or
 * given an `onClick` are interactive: they get the richer border-highlight +
 * cyan glow and a pointer cursor.
 */
export function GlassCard({
  children,
  className,
  hover,
  index = 0,
  onClick,
  style,
  tilt: tiltEnabled = false,
  lift: liftEnabled = false,
}: GlassCardProps) {
  const interactive = hover || Boolean(onClick);
  const tilt = useTilt();
  return (
    <motion.div
      initial={{ opacity: 0, y: 16 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.4, delay: Math.min(index * 0.04, 0.3), ease: "easeOut" }}
      whileHover={{
        // `lift` raises the card; otherwise no translate (a tilt card pivots
        // from the top and must not rise; a plain card just brightens its frame).
        ...(liftEnabled ? { y: -4 } : {}),
        zIndex: 20,
        ...(interactive ? { borderColor: "var(--pb)" } : {}),
        // Motion animates inline values, so these are var() compositions rather
        // than utilities. `--pglow` follows the accent; the cyan companion glow
        // stays the secondary highlight it has always been.
        boxShadow: liftEnabled
          ? "0 22px 48px -22px var(--pglow), 0 0 26px -12px var(--cyan)"
          : interactive
            ? "0 20px 45px -20px var(--pglow), 0 0 26px -10px var(--cyan)"
            : "0 18px 50px -22px var(--pglow)",
        transition: { duration: 0.25, ease: [0.2, 0.8, 0.2, 1] },
      }}
      onClick={onClick}
      onPointerMove={tiltEnabled ? tilt.onPointerMove : undefined}
      onPointerLeave={tiltEnabled ? tilt.onPointerLeave : undefined}
      // tilt.style provides the perspective + rotate/scale motion values; the
      // caller's `style` may override cosmetics but never the transform keys.
      style={{ ...(tiltEnabled ? tilt.style : undefined), ...style }}
      className={cn(
        "glass relative rounded-[20px]",
        hover && "cursor-pointer",
        onClick && "cursor-pointer",
        className,
      )}
    >
      {children}
    </motion.div>
  );
}
