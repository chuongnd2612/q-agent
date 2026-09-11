import type { ReactNode } from "react";
import i18n from "@/i18n";

/** Colour maps shared across screens (ticket status, priority, approval, exec).
 * Human labels are localized via the `status` i18n namespace and resolved
 * through the i18next singleton (consumers already call `useTranslation`, so
 * they re-render on language switch).
 *
 * Every value here is a `var(--token)` rather than a hex (#784): these strings
 * are handed to inline `style`, so the browser resolves them per element and
 * they follow `data-mode` / `data-accent` for free. A frozen `#6ee7b7` would be
 * a light-mode contrast failure that nothing in the type system could catch. */

export const statusColors: Record<string, [string, string]> = {
  "Ready for QA": ["var(--ok)", "var(--okTint)"],
  "In Progress": ["var(--warn)", "var(--warnTint)"],
  Blocked: ["var(--danger)", "var(--dangerTint)"],
  Done: ["var(--ok)", "var(--okTint)"],
};

const approvalColor: Record<string, [string, string]> = {
  pending: ["var(--warn)", "var(--warnTint)"],
  approved: ["var(--ok)", "var(--okTint)"],
  rejected: ["var(--danger)", "var(--dangerTint)"],
};

/** `[color, label, bg]` for a test-case approval state. */
export function approvalStyle(approval: string): [string, string, string] {
  const [color, bg] = approvalColor[approval] ?? approvalColor.pending;
  return [color, i18n.t(`status:approval.${approval}`, { defaultValue: approval }), bg];
}

const execColor: Record<string, string> = {
  pending: "var(--paused)",
  running: "var(--warn)",
  pass: "var(--ok)",
  fail: "var(--danger)",
  skipped: "var(--paused)",
};

/** `[color, label]` for an execution result status. */
export function execStyle(status: string): [string, string] {
  return [execColor[status] ?? execColor.pending, i18n.t(`status:exec.${status}`, { defaultValue: status })];
}

/**
 * Visual token ([color, label]) for a confirmed product defect — a failed case
 * whose `failureClass === "product_defect"`. Deliberately fuchsia (`--defect`),
 * NOT the script-fail red (`--danger`), so a genuine product bug reads distinctly
 * from a plain test failure. Kept in sync with the Automation slice's
 * product-defect hue — which is now the same token, not a matching literal.
 */
export function productDefectStyle(): [string, string] {
  return ["var(--defect)", i18n.t("status:productDefect")];
}

export function priorityColor(p: string): string {
  return p === "High" ? "var(--danger)" : p === "Medium" ? "var(--warn)" : "var(--neutral)";
}
export function priorityBg(p: string): string {
  return p === "High"
    ? "var(--dangerTint)"
    : p === "Medium"
      ? "var(--warnTint)"
      : "var(--neutralTint)";
}

/** Provider glyph + brand colour. Brand marks, so they deliberately do NOT
 * darken in light mode — an Azure DevOps blue that shifts per theme stops
 * being the provider's colour. */
export const providerGlyph: Record<string, [string, string]> = {
  ado: ["A", "var(--azure)"],
  jira: ["J", "var(--jira)"],
  github: ["G", "var(--github)"],
};

interface PillProps {
  children: ReactNode;
  color: string;
  bg: string;
  dot?: boolean;
}

/** Small rounded status pill. */
export function Pill({ children, color, bg, dot }: PillProps) {
  return (
    <span
      className="inline-flex items-center gap-1.5 rounded-full px-2.5 py-[3px] text-[11px] font-bold"
      style={{ color, background: bg }}
    >
      {dot && <span className="h-1.5 w-1.5 rounded-full" style={{ background: color }} />}
      {children}
    </span>
  );
}

export function StatusBadge({ status }: { status: string }) {
  const [color, bg] = statusColors[status] ?? ["var(--neutral)", "var(--neutralTint)"];
  return (
    <Pill color={color} bg={bg}>
      {status}
    </Pill>
  );
}
