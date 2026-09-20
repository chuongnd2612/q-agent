import { FileCode } from "lucide-react";
import { useTranslation } from "react-i18next";
import { GlassCard } from "@/components/ui/GlassCard";
import type { AutomationSpecOut } from "@/types/api";
import { normalizeSpecStatus } from "./specStatus";
import { SpecStatusDot } from "./SpecStatusDot";
import type { HealProgress } from "./useAutomationEvents";

/**
 * Left "APPROVED SPECS" list. Renders a selectable row per spec with a status
 * dot; blocked specs get a dashed outline.
 */
export function SpecList({
  specs,
  selectedTestCaseId,
  resultStatusByCase,
  healProgress,
  onSelect,
}: {
  specs: AutomationSpecOut[];
  selectedTestCaseId: number | null;
  resultStatusByCase: Map<number, string>;
  healProgress: HealProgress | null;
  onSelect: (caseId: number) => void;
}) {
  const { t } = useTranslation("pipeline");
  return (
    <GlassCard className="p-2">
      <div className="hidden px-2.5 pb-1.5 pt-2 text-[10.5px] font-semibold tracking-wider text-faint md:block">
        {t("spec.list.approvedSpecs")}
      </div>
      {/* Desktop: vertical file list. */}
      <div className="hidden flex-col gap-0.5 md:flex">
        {specs.map((s) => {
          const active = selectedTestCaseId === s.testCaseId;
          const blocked = normalizeSpecStatus(s.status) === "blocked";
          return (
            <button
              key={s.id}
              onClick={() => onSelect(s.testCaseId)}
              className={`flex items-center gap-2 rounded-[10px] px-2.5 py-2 text-left hover:bg-card2 ${
                blocked ? "border border-dashed border-bd2" : ""
              }`}
              style={active ? { background: "var(--pt)" } : undefined}
            >
              <FileCode size={14} color={active ? "var(--psText)" : "var(--muted)"} />
              <span className="flex-1 truncate font-mono text-xs text-txt3">{s.filename}</span>
              <SpecStatusDot
                specStatus={s.status}
                execStatus={resultStatusByCase.get(s.testCaseId)}
                healing={
                  healProgress?.caseId === s.testCaseId &&
                  !["passed", "failed", "product_defect"].includes(healProgress?.phase ?? "")
                }
              />
            </button>
          );
        })}
      </div>
      {/* Mobile: horizontal-scroll file tabs (MOBILE_SPEC §4 pattern 7). */}
      <div className="scrollbar-none flex gap-2 overflow-x-auto p-1 md:hidden">
        {specs.map((s) => {
          const active = selectedTestCaseId === s.testCaseId;
          const blocked = normalizeSpecStatus(s.status) === "blocked";
          return (
            <button
              key={s.id}
              onClick={() => onSelect(s.testCaseId)}
              className={`flex shrink-0 items-center gap-1.5 whitespace-nowrap rounded-full border px-3 py-1.5 text-left text-[11.5px] ${
                blocked ? "border-dashed border-bd2" : "border-bd2"
              }`}
              style={active ? { background: "var(--pt)", borderColor: "var(--pb)" } : undefined}
            >
              <FileCode size={13} color={active ? "var(--psText)" : "var(--muted)"} />
              <span className="font-mono text-txt3">{s.filename}</span>
              <SpecStatusDot
                specStatus={s.status}
                execStatus={resultStatusByCase.get(s.testCaseId)}
                healing={
                  healProgress?.caseId === s.testCaseId &&
                  !["passed", "failed", "product_defect"].includes(healProgress?.phase ?? "")
                }
              />
            </button>
          );
        })}
      </div>
    </GlassCard>
  );
}
