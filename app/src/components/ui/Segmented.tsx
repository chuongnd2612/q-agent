// Segmented control — a small inset track of mutually-exclusive options
// (#784). Added for Settings › Appearance's Dark | Light switch (#786), but
// deliberately generic: it is the right control anywhere a toolbar picks one of
// two or three modes.
//
// Ported from EmeHub's `components/ui/Segmented.tsx` so the two apps offer the
// same control under the same API.

import type { ReactNode } from "react";
import { cn } from "@/lib/cn";

export interface SegmentedOption<T extends string> {
  value: T;
  label: string;
  icon?: ReactNode;
}

export interface SegmentedProps<T extends string> {
  options: SegmentedOption<T>[];
  value: T;
  onChange: (value: T) => void;
  /**
   * `tint` — active segment is the accent tint + border.
   * `solid` — active segment is the accent gradient with its glow.
   */
  variant?: "tint" | "solid";
  className?: string;
}

export function Segmented<T extends string>({
  options,
  value,
  onChange,
  variant = "tint",
  className,
}: SegmentedProps<T>) {
  return (
    <div
      role="tablist"
      className={cn(
        "inline-flex gap-1 rounded-[13px] border border-bd2 bg-inset p-1",
        className,
      )}
    >
      {options.map((o) => {
        const active = o.value === value;
        return (
          <button
            key={o.value}
            type="button"
            role="tab"
            aria-selected={active}
            onClick={() => onChange(o.value)}
            className={cn(
              "inline-flex cursor-pointer items-center justify-center gap-2 rounded-[9px] px-4 py-[7px]",
              "text-[12.5px] font-bold transition-[background-color,color,box-shadow] duration-200",
              !active && "text-txt4 hover:bg-card3 hover:text-txt2",
              active &&
                (variant === "solid"
                  ? // `text-p-on`, not `text-white`: white is unreadable on the
                    // Metallic Steel accent and in light mode.
                    "accent-gradient text-p-on shadow-primary"
                  : "border border-pb bg-pt text-ps-text"),
            )}
          >
            {o.icon}
            {o.label}
          </button>
        );
      })}
    </div>
  );
}
