import { forwardRef, type ButtonHTMLAttributes, type MouseEvent } from "react";
import { cn } from "@/lib/cn";
import { useMagnetic } from "@/hooks/useMagnetic";

type Variant = "primary" | "glass" | "ghost" | "white" | "success" | "danger";
type Size = "sm" | "md" | "lg";

interface Props extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant;
  size?: Size;
}

const base =
  "inline-flex items-center justify-center gap-2 rounded-xl font-semibold transition-[filter,background,border-color] cursor-pointer border select-none disabled:opacity-50 disabled:cursor-not-allowed";

/**
 * Every variant reads tokens, never a hex (#784), so the family follows both
 * `data-accent` and `data-mode`.
 *
 * Two are worth a note:
 *  - `primary` puts `text-p-on` on the accent gradient rather than `text-white`.
 *    White is right on purple and red, and unreadable on Metallic Steel; `--pOn`
 *    is the token that knows which.
 *  - `white` is the *inverted* button, not a white one — `bg-txt text-bg` keeps
 *    it near-white on dark and near-black on light, which is what "inverted"
 *    meant all along. A literal `bg-white` would vanish into a light page.
 */
const variants: Record<Variant, string> = {
  primary:
    "border-transparent text-p-on accent-gradient hover:brightness-110 shadow-[0_8px_22px_-8px_var(--pglow)]",
  glass: "border-bd2 bg-card2 text-txt3 hover:bg-card3",
  ghost: "border-transparent bg-transparent text-txt4 hover:bg-card3",
  white: "border-transparent bg-txt text-bg font-bold hover:brightness-95",
  success: "border-ok/30 bg-ok-tint text-ok hover:bg-ok/20",
  danger: "border-danger/30 bg-danger-tint text-danger hover:bg-danger/20",
};

const sizes: Record<Size, string> = {
  sm: "h-8 px-3 text-[12px]",
  md: "h-[38px] px-4 text-[13px]",
  lg: "h-11 px-5 text-[14px]",
};

/** Shared button matching the design's button family, with a subtle magnetic
 * lean toward the cursor on hover (springs back on leave). */
export const Button = forwardRef<HTMLButtonElement, Props>(function Button(
  { variant = "glass", size = "md", className, onMouseMove, onMouseLeave, ...rest },
  ref,
) {
  const mag = useMagnetic<HTMLButtonElement>();

  const setRef = (node: HTMLButtonElement | null) => {
    mag.ref.current = node;
    if (typeof ref === "function") ref(node);
    else if (ref) ref.current = node;
  };

  const handleMove = (e: MouseEvent<HTMLButtonElement>) => {
    mag.onMouseMove(e);
    onMouseMove?.(e);
  };
  const handleLeave = (e: MouseEvent<HTMLButtonElement>) => {
    mag.onMouseLeave();
    onMouseLeave?.(e);
  };

  return (
    <button
      ref={setRef}
      onMouseMove={handleMove}
      onMouseLeave={handleLeave}
      className={cn(base, variants[variant], sizes[size], className)}
      {...rest}
    />
  );
});
