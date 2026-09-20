import { AlertTriangle } from "lucide-react";
import { effectiveSpecStatus, PRODUCT_DEFECT_HUE, SPEC_STATUS_DOT } from "./specStatus";

/**
 * Left-list status indicator for a spec. Reflects the authoritative `spec.status`
 * (falling back to the latest execution result), with a heal-in-flight override.
 * Product defects render a distinct fuchsia AlertTriangle instead of a dot.
 */
export function SpecStatusDot({
  specStatus,
  execStatus,
  healing,
}: {
  specStatus?: string;
  execStatus?: string;
  healing?: boolean;
}) {
  const status = effectiveSpecStatus(specStatus, execStatus);
  if (status === "product_defect") {
    return (
      <AlertTriangle size={13} strokeWidth={2.4} className="shrink-0" style={{ color: PRODUCT_DEFECT_HUE }} />
    );
  }
  const running = healing || status === "running";
  const color = running ? "var(--warn)" : SPEC_STATUS_DOT[status];
  return (
    <span
      className={`h-[7px] w-[7px] shrink-0 rounded-full ${running ? "animate-pulse" : ""}`}
      style={{ background: color }}
    />
  );
}
