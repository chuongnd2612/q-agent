import type { ReactNode } from "react";
import { cn } from "@/lib/cn";

/** Claude brand mark (the eight-point "sunburst") in the brand orange. Shared
 * by every surface that visually tags something as Claude/Anthropic-related —
 * credential cards, the admin Claude credentials screen, the AI status
 * popover — so the glyph stays pixel-identical everywhere it appears. */
export function ClaudeLogo({ size = 16, color = "#D97757" }: { size?: number; color?: string }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill={color}>
      <path d="M12 2.4l2.6 6.6 6.9.4-5.3 4.4 1.8 6.7L12 17.3 6 20.9l1.8-6.7L2.5 9.4l6.9-.4z" />
    </svg>
  );
}

/** A spinning ring loader (matches the design's `spin` animation). */
export function Spinner({ size = 15, className }: { size?: number; className?: string }) {
  return (
    <span
      className={cn("inline-block shrink-0 rounded-full", className)}
      style={{
        width: size,
        height: size,
        border: "2px solid rgba(167,139,250,.3)",
        borderTopColor: "#a78bfa",
        animation: "spin .7s linear infinite",
      }}
    />
  );
}

/** SVG progress ring with a centered label. */
export function ProgressRing({
  value,
  size = 88,
  stroke = 11,
  label,
  sub,
}: {
  value: number;
  size?: number;
  stroke?: number;
  label?: ReactNode;
  sub?: ReactNode;
}) {
  const r = (120 - stroke) / 2;
  const c = 2 * Math.PI * r;
  const offset = c * (1 - Math.max(0, Math.min(100, value)) / 100);
  const gid = `ring-${Math.round(r)}-${stroke}`;
  return (
    <div className="relative shrink-0" style={{ width: size, height: size }}>
      <svg width={size} height={size} viewBox="0 0 120 120">
        <circle cx="60" cy="60" r={r} fill="none" stroke="rgba(255,255,255,.08)" strokeWidth={stroke} />
        <circle
          cx="60"
          cy="60"
          r={r}
          fill="none"
          stroke={`url(#${gid})`}
          strokeWidth={stroke}
          strokeLinecap="round"
          strokeDasharray={c}
          strokeDashoffset={offset}
          transform="rotate(-90 60 60)"
          style={{ transition: "stroke-dashoffset .4s ease" }}
        />
        <defs>
          <linearGradient id={gid} x1="0" y1="0" x2="1" y2="1">
            <stop offset="0" stopColor="#8b5cf6" />
            <stop offset="1" stopColor="#22d3ee" />
          </linearGradient>
        </defs>
      </svg>
      <div className="absolute inset-0 flex flex-col items-center justify-center text-center">
        {label ?? <span className="text-lg font-black">{Math.round(value)}%</span>}
        {sub}
      </div>
    </div>
  );
}

/** Elegant empty state. */
/**
 * "We couldn't load this" — the failure counterpart to {@link EmptyState} (#491).
 *
 * Every list screen used to fall through to `EmptyState` when its query
 * **failed**, so a broken backend was reported as a successful answer of
 * "nothing here" — `/tickets` said *"No tickets found — try a different filter"*
 * while all four of its requests were failing 502, sending the user off to debug
 * their own filters. A failure and an empty result are different facts and must
 * not share a screen.
 *
 * Deliberately the same shape/weight as `EmptyState` so screens can swap between
 * them without a layout jump, but with the danger accent and a Retry.
 */
export function ErrorState({
  title,
  body,
  onRetry,
  retryLabel,
}: {
  title: string;
  body: string;
  onRetry?: () => void;
  retryLabel: string;
}) {
  return (
    <div className="glass flex flex-col items-center rounded-[22px] px-8 py-14 text-center" role="alert">
      <div
        className="mb-5 flex h-[70px] w-[70px] items-center justify-center rounded-[22px]"
        style={{ background: "rgba(248,113,113,.10)" }}
      >
        <svg
          width="28"
          height="28"
          viewBox="0 0 24 24"
          fill="none"
          stroke="#f87171"
          strokeWidth="1.7"
          strokeLinecap="round"
          strokeLinejoin="round"
          aria-hidden="true"
        >
          <path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0Z" />
          <path d="M12 9v4M12 17h.01" />
        </svg>
      </div>
      <h2 className="m-0 mb-2 text-xl font-extrabold">{title}</h2>
      <p className="m-0 mb-5 max-w-[360px] text-[13.5px] leading-relaxed text-ink-dim">{body}</p>
      {onRetry ? (
        <button
          type="button"
          onClick={onRetry}
          className="flex h-[38px] items-center gap-2 rounded-xl border px-4 text-[13px] font-bold text-ink transition-colors hover:bg-white/[.07]"
          style={{ background: "rgba(255,255,255,.05)", borderColor: "rgba(255,255,255,.18)" }}
        >
          {retryLabel}
        </button>
      ) : null}
    </div>
  );
}

export function EmptyState({
  icon,
  title,
  body,
  action,
}: {
  icon: ReactNode;
  title: string;
  body: string;
  action?: ReactNode;
}) {
  return (
    <div className="glass flex flex-col items-center rounded-[22px] px-8 py-14 text-center">
      <div className="mb-5 flex h-[70px] w-[70px] items-center justify-center rounded-[22px] bg-white/[0.05]">
        {icon}
      </div>
      <h2 className="m-0 mb-2 text-xl font-extrabold">{title}</h2>
      <p className="m-0 mb-5 max-w-[360px] text-[13.5px] leading-relaxed text-ink-dim">{body}</p>
      {action}
    </div>
  );
}
