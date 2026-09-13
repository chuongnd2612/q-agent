import { useEffect, useRef, useState, type CSSProperties } from "react";

/**
 * The one skeleton primitive (#750, slice 1 of #749).
 *
 * Before this, every loading placeholder in the app was a hand-rolled
 * `animate-pulse` div: four different surfaces, seven different radii, an
 * opacity blink instead of a shimmer, and five panels that rendered the word
 * "Loading…" instead of any placeholder at all. Everything now comes from here,
 * so the surface, the radius, the motion and the timing are decided once.
 *
 * The visual treatment lives in the `skeleton` utility in `index.css` (one
 * token-driven surface, one radius, the pre-existing `@keyframes shimmer`, and
 * a static pulse under `prefers-reduced-motion`). This module owns the *timing*,
 * which is the half callers kept getting wrong.
 */

/**
 * How long a load may take before a skeleton is painted at all. A placeholder
 * that flashes for 80ms is noise; below this threshold the user perceives the
 * response as instant and should simply see the content appear.
 */
export const SKELETON_DELAY_MS = 200;

/**
 * How long a skeleton stays on screen once it HAS been painted, even if the
 * data lands sooner. A placeholder that vanishes a frame after appearing reads
 * as a glitch rather than as progress.
 */
export const SKELETON_MIN_MS = 400;

/** Per-row shimmer offset in `SkeletonList`, so rows ripple instead of blinking in unison. */
const STAGGER_MS = 90;

/**
 * True once `ms` have elapsed since mount. Used to hold the first
 * `SKELETON_DELAY_MS` of every skeleton blank, so a fast response never flashes
 * a placeholder. Self-contained: a caller that renders a skeleton directly still
 * gets the delay without doing anything.
 */
function useAfterDelay(ms: number): boolean {
  const [elapsed, setElapsed] = useState(false);
  useEffect(() => {
    const id = window.setTimeout(() => setElapsed(true), ms);
    return () => window.clearTimeout(id);
  }, [ms]);
  return elapsed;
}

/**
 * Whether a skeleton should be rendered for a query that is `loading`.
 *
 * This is the other half of the timing contract and the half a leaf component
 * cannot implement on its own: it keeps returning `true` for a short while
 * *after* the data lands, so a skeleton that was actually painted survives
 * `SKELETON_MIN_MS` instead of disappearing mid-fade. A load that resolves
 * before `SKELETON_DELAY_MS` is never held, because nothing was ever shown.
 *
 * The delay itself is NOT applied here — the components apply it on mount — so
 * gating a component with this hook yields exactly one delay and one minimum.
 *
 * @param loading whether the underlying query is still on its first load
 * @returns whether to render the skeleton right now
 */
export function useSkeleton(loading: boolean): boolean {
  const startedAt = useRef<number | null>(null);
  const [holding, setHolding] = useState(false);

  if (loading && startedAt.current === null) startedAt.current = Date.now();

  useEffect(() => {
    if (loading) {
      setHolding(false);
      return;
    }
    const start = startedAt.current;
    startedAt.current = null;
    if (start === null) return;
    // Below the delay threshold nothing was ever painted, so there is nothing
    // to hold — releasing immediately is what keeps a fast load feeling fast.
    const remaining = start + SKELETON_DELAY_MS + SKELETON_MIN_MS - Date.now();
    if (Date.now() - start < SKELETON_DELAY_MS || remaining <= 0) {
      setHolding(false);
      return;
    }
    setHolding(true);
    const id = window.setTimeout(() => setHolding(false), remaining);
    return () => window.clearTimeout(id);
  }, [loading]);

  return loading || holding;
}

/**
 * The bare shimmering block, with no mount delay of its own. Internal, so that
 * `SkeletonText` / `SkeletonList` can gate their whole group once rather than
 * stacking a delay per child.
 */
function Block({
  className = "",
  style,
}: {
  className?: string;
  style?: CSSProperties;
}) {
  return <div aria-hidden className={`skeleton ${className}`} style={style} />;
}

/**
 * A single placeholder block. Size it with Tailwind height/width classes; the
 * surface, radius and motion are fixed by the primitive.
 *
 * @param className height/width/margin utilities for this block
 * @param style inline styles (used for shimmer stagger); rarely needed
 * @param testId optional `data-testid`, for driving the loading state in tests
 */
export function Skeleton({
  className = "",
  style,
  testId,
}: {
  className?: string;
  style?: CSSProperties;
  testId?: string;
}) {
  const visible = useAfterDelay(SKELETON_DELAY_MS);
  if (!visible) return null;
  return <div aria-hidden data-testid={testId} className={`skeleton ${className}`} style={style} />;
}

/**
 * A run of text lines, the last one short so it reads as a paragraph rather
 * than a stack of bars.
 *
 * @param lines how many lines to draw (default 3)
 * @param className extra utilities on the wrapping column
 */
export function SkeletonText({
  lines = 3,
  className = "",
}: {
  lines?: number;
  className?: string;
}) {
  const visible = useAfterDelay(SKELETON_DELAY_MS);
  if (!visible) return null;
  return (
    <div aria-hidden className={`flex flex-col gap-2 ${className}`}>
      {Array.from({ length: lines }).map((_, i) => (
        <Block
          key={i}
          className={`h-3 ${i === lines - 1 && lines > 1 ? "w-3/5" : "w-full"}`}
          style={{ animationDelay: `${i * STAGGER_MS}ms` }}
        />
      ))}
    </div>
  );
}

/**
 * A list of identical rows — the shape behind almost every list screen.
 *
 * `count` should mirror the screen's real page size (or the real number of
 * cards in a grid row), not a number picked to fill the viewport: the
 * placeholder is a promise about what is coming.
 *
 * @param count number of rows
 * @param rowHeight row height in px (default 64)
 * @param gap gap between rows in px (default 10)
 * @param className extra utilities on the wrapping container
 * @param testId optional `data-testid` on the container
 */
export function SkeletonList({
  count,
  rowHeight = 64,
  gap = 10,
  className = "",
  testId,
}: {
  count: number;
  rowHeight?: number;
  gap?: number;
  className?: string;
  testId?: string;
}) {
  const visible = useAfterDelay(SKELETON_DELAY_MS);
  if (!visible) return null;
  return (
    <div
      aria-hidden
      data-testid={testId}
      className={`flex flex-col ${className}`}
      style={{ gap: `${gap}px` }}
    >
      {Array.from({ length: count }).map((_, i) => (
        <Block
          key={i}
          style={{ height: `${rowHeight}px`, animationDelay: `${i * STAGGER_MS}ms` }}
        />
      ))}
    </div>
  );
}
