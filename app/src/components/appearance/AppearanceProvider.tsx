// Stamps `data-mode` and `data-accent` on the app root so the token layer in
// `index.css` resolves. Everything downstream reads tokens, never a hex.
//
// The attributes go on `document.documentElement` and NOT on the app's root div:
// dropdowns, popovers, modals and the command palette portal to `document.body`
// (CLAUDE.md › Frontend), which sits outside the React tree — anything scoped to
// the app div would leave every portalled overlay on the previous theme.

import { useLayoutEffect, type ReactNode } from "react";
import { useAppearance } from "@/store/appearance";

/**
 * Apply the persisted appearance to `<html>` immediately, before React mounts.
 *
 * Called from `main.tsx` rather than left to the provider's effect: `body` paints
 * `var(--bg)` as soon as the stylesheet lands, which is the *dark* default until
 * something says otherwise. A light-mode user would get a dark flash for the
 * whole of boot. Zustand's `persist` with the default localStorage backend
 * rehydrates synchronously, so `getState()` here already holds their choice.
 */
export function stampAppearance() {
  const { mode, accent } = useAppearance.getState();
  const root = document.documentElement;
  root.setAttribute("data-mode", mode);
  root.setAttribute("data-accent", accent);
}

export function AppearanceProvider({ children }: { children: ReactNode }) {
  const mode = useAppearance((s) => s.mode);
  const accent = useAppearance((s) => s.accent);

  // Layout effect, not a plain effect: it runs before paint, so a mode change
  // never shows one frame of the old theme.
  useLayoutEffect(() => {
    const root = document.documentElement;
    root.setAttribute("data-mode", mode);
    root.setAttribute("data-accent", accent);
  }, [mode, accent]);

  return <>{children}</>;
}
