import i18n from "@/i18n";
import type { ProviderKind } from "@/types/api";

/** Provider brand name — intentionally NOT localized (proper nouns, ADR 0011). */
export const providerLabel: Record<ProviderKind, string> = {
  ado: "Azure DevOps",
  jira: "Jira",
  github: "GitHub",
};

/** Stable keys for the cosmetic build steps shown in the AI knowledge-build
 * overlay; the overlay resolves each against the `status:knowledgeSteps.*`
 * catalog. Order is the display order. */
export const KNOWLEDGE_STEPS = [
  "connecting",
  "reading",
  "architecture",
  "stack",
  "findingTests",
  "pageObjects",
  "conventions",
  "building",
  "optimizing",
  "ready",
] as const;

/** [label, color, bg, dot] for a knowledge status pill. Label is localized via
 * the `status` i18n namespace (resolved through the i18next singleton).
 *
 * The colours are theme tokens, not literals (#844): the previous hex pairs were
 * the dark-mode ramp, so on the light backdrop the pill rendered pale-on-pale —
 * the "not indexed" pill in the project header measured under 2.5:1. Each status
 * maps onto the semantic token the theme layer already defines for it, and text
 * and dot share one token because the light column darkens the pair together. */
export function knowledgeStatusStyle(status: string): [string, string, string, string] {
  if (status === "indexed") return [i18n.t("status:knowledge.indexed"), "var(--ok)", "var(--okTint)", "var(--ok)"];
  if (status === "indexing") return [i18n.t("status:knowledge.indexing"), "var(--p)", "var(--pt)", "var(--p)"];
  if (status === "stale") return [i18n.t("status:knowledge.stale"), "var(--warn)", "var(--warnTint)", "var(--warn)"];
  if (status === "error") return [i18n.t("status:knowledge.error"), "var(--danger)", "var(--dangerTint)", "var(--danger)"];
  return [i18n.t("status:knowledge.none"), "var(--neutral)", "var(--neutralTint)", "var(--neutral)"];
}

export function confidenceColor(c: number): string {
  return c >= 90 ? "#6ee7b7" : c >= 60 ? "#fbbf24" : "#8b93a7";
}
