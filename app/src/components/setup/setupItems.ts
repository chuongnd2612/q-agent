import { Cpu, KeyRound, Link2, LogIn, Globe } from "lucide-react";
import type { ComponentType } from "react";
import type { ReadinessFix, ReadinessItem } from "@/types/api";

/**
 * Presentation for each readiness item (#643). The server decides *whether* an
 * item is met and *who owns the fix*; this table decides how it looks and where
 * "Fix" navigates — routing is the frontend's business, which is why the API
 * returns stable `fix` keys instead of URLs.
 */
export const SETUP_ICONS: Record<string, ComponentType<{ size?: number; strokeWidth?: number; className?: string }>> = {
  claudeCredential: KeyRound,
  providerConnection: Link2,
  localAgent: Cpu,
  projectBaseUrl: Globe,
  capturedLogin: LogIn,
};

/**
 * Where "Fix" goes for a given owner. `hub` returns null: EmeHub lives on another
 * origin and its URL is only known at runtime (`useHubWebUrl`), so the caller
 * supplies it — and when it can't, the item still explains itself instead of
 * offering a dead button.
 */
export function fixRoute(fix: ReadinessFix): string | null {
  switch (fix) {
    case "settings":
      return "/settings";
    case "project":
      return "/projects";
    case "install-agent":
      return "/local-agent";
    default:
      return null;
  }
}

/**
 * Items that actually block work: unmet, relevant under current settings, and
 * not owned by somewhere we cannot see.
 *
 * The `managed` exclusion is belt-and-braces with the server (which already
 * reports such an item as not required): a blocker must never be raised on a
 * state Q-Agent did not verify (#651).
 */
export function blockers(items: ReadinessItem[] | undefined): ReadinessItem[] {
  return (items ?? []).filter((i) => i.required && !i.ready && !i.managed);
}

/** The blocker for a specific key, or null — for a pre-flight check on one action. */
export function blockerFor(items: ReadinessItem[] | undefined, key: string): ReadinessItem | null {
  return blockers(items).find((i) => i.key === key) ?? null;
}
