import { CountUp } from "@/components/ui/CountUp";

export interface Kpi {
  label: string;
  value: string;
  /** Small caption under the figure — the trend/context line the KPI cards used
   *  to carry, kept rather than dropped when the grid was compressed (#733). */
  caption: string;
  color: string;
}

/**
 * The workspace KPIs, compressed from the old four-card grid into a header strip
 * (ADR 0015 §1). Same numbers, a fraction of the vertical space, so the project
 * comparison table is the first thing on the screen.
 *
 * Deliberately opaque-ish flat surfaces rather than `GlassCard`: these sit in the
 * page header over the animated shell backdrop, and `backdrop-filter` there is
 * what makes small text mushy (CLAUDE.md).
 */
export function KpiStrip({ items }: { items: Kpi[] }) {
  return (
    <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-5">
      {items.map((k) => (
        <div
          key={k.label}
          className="min-w-0 rounded-[14px] px-[15px] py-[11px]"
          style={{
            background: "rgba(255,255,255,.04)",
            border: "1px solid rgba(255,255,255,.07)",
          }}
        >
          <CountUp
            value={k.value}
            className="block text-[19px] font-black leading-none tracking-tight"
            style={{ color: k.color }}
          />
          <div className="mt-[3px] text-[10.5px] font-medium text-muted">{k.label}</div>
          <div className="mt-px truncate text-[10.5px] text-[#7a7a8c]">{k.caption}</div>
        </div>
      ))}
    </div>
  );
}
