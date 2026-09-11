// Slider with an optional tracked label and a mono readout (#784). Added for
// Settings › Appearance's ambient-bloom control (#786).
//
// Ported from EmeHub's `components/ui/Range.tsx`. The native `<input type=range>`
// sits transparent on top of a painted track, because the UA track cannot carry
// a gradient fill consistently across browsers — so the visible track is two
// spans and the input contributes only the thumb and the interaction.
//
// The filled portion's `width` is the one inline style here: it is a computed
// value, not a colour.

import { cn } from "@/lib/cn";

export interface RangeProps {
  value: number;
  onChange: (value: number) => void;
  min?: number;
  max?: number;
  step?: number;
  /** Small tracked uppercase label above the track. */
  label?: string;
  /** Mono readout on the right, e.g. "85%" or "2". */
  readout?: string;
  className?: string;
  "aria-label"?: string;
}

export function Range({
  value,
  onChange,
  min = 0,
  max = 100,
  step = 1,
  label,
  readout,
  className,
  "aria-label": ariaLabel,
}: RangeProps) {
  const pct = max === min ? 0 : ((value - min) / (max - min)) * 100;

  return (
    <div className={cn("flex flex-col gap-2", className)}>
      {(label || readout) && (
        <div className="flex items-center justify-between">
          {label && (
            <span className="text-[9.5px] font-bold tracking-[.11em] text-label">{label}</span>
          )}
          {readout && (
            <span className="font-mono text-[11.5px] font-semibold text-txt3">{readout}</span>
          )}
        </div>
      )}
      <div className="relative flex h-5 items-center">
        <span className="absolute inset-x-0 h-[6px] rounded-full bg-inset" />
        <span
          className="accent-gradient absolute left-0 h-[6px] rounded-full"
          style={{ width: `${pct}%` }}
        />
        <input
          type="range"
          min={min}
          max={max}
          step={step}
          value={value}
          aria-label={ariaLabel ?? label}
          onChange={(e) => onChange(Number(e.target.value))}
          className={cn(
            "relative z-10 h-5 w-full cursor-pointer appearance-none bg-transparent",
            "[&::-webkit-slider-thumb]:size-[16px] [&::-webkit-slider-thumb]:appearance-none",
            "[&::-webkit-slider-thumb]:rounded-full [&::-webkit-slider-thumb]:bg-p",
            "[&::-webkit-slider-thumb]:shadow-primary",
            "[&::-moz-range-thumb]:size-[16px] [&::-moz-range-thumb]:rounded-full",
            "[&::-moz-range-thumb]:border-0 [&::-moz-range-thumb]:bg-p",
          )}
        />
      </div>
    </div>
  );
}
