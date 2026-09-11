/**
 * Appearance state — theme mode, brand accent, and the ambient depth controls
 * (#783, part of #782).
 *
 * Ported from EmeHub's `store/appearance.ts` so the two apps in the suite offer
 * the same choices under the same names. Persisted to localStorage under the
 * `qagent.*` convention, so the workspace looks the same on the next visit.
 *
 * **This store holds appearance ONLY.** Navigation lives in the URL and UI-only
 * state in `store/ui.ts` (CLAUDE.md › Routing & navigation). It is deliberately
 * separate from `store/ui.ts`: appearance is persisted and global, that one is
 * ephemeral and per-screen.
 *
 * Appearance is a **per-app** choice, not inherited from the hub over SSO — see
 * #782. Nothing here touches the API.
 */

import { create } from "zustand";
import { persist } from "zustand/middleware";

export type Mode = "dark" | "light";
export type Accent = "red" | "purple" | "cyan" | "steel";

/** The four brand accents, as offered by Settings › Appearance › Brand colour. */
export const ACCENTS: { key: Accent; label: string; hex: number }[] = [
  { key: "red", label: "EMESOFT Red", hex: 0xe1172b },
  { key: "purple", label: "Agent Purple", hex: 0x8b5cf6 },
  { key: "cyan", label: "Signal Cyan", hex: 0x22d3ee },
  { key: "steel", label: "Metallic Steel", hex: 0xb4becd },
];

export interface AppearanceState {
  mode: Mode;
  accent: Accent;
  /** Ambient bloom opacity, 0–100 step 5. */
  ambient: number;
  /** 3D constellation field on/off — tears down / re-creates the WebGL scene. */
  fx3d: boolean;
  /** Depth on hover — gates ALL pointer tilt. */
  tilt: boolean;
  setMode: (mode: Mode) => void;
  toggleMode: () => void;
  setAccent: (accent: Accent) => void;
  setAmbient: (ambient: number) => void;
  setFx3d: (fx3d: boolean) => void;
  setTilt: (tilt: boolean) => void;
}

export const useAppearance = create<AppearanceState>()(
  persist(
    (set) => ({
      mode: "dark",
      // PURPLE, deliberately — #782 ships EMESOFT Red as the default, but only
      // in the cleanup slice (#792), once the hard-coded purple literals across
      // the components are gone. Until then a red default would paint red
      // chrome onto a still-purple app. See the note in `index.css` § 3.
      accent: "purple",
      ambient: 85,
      fx3d: true,
      tilt: true,
      setMode: (mode) => set({ mode }),
      toggleMode: () =>
        set((s) => ({ mode: s.mode === "light" ? "dark" : "light" })),
      setAccent: (accent) => set({ accent }),
      setAmbient: (ambient) => set({ ambient }),
      setFx3d: (fx3d) => set({ fx3d }),
      setTilt: (tilt) => set({ tilt }),
    }),
    { name: "qagent.appearance" },
  ),
);

/** The three.js palette hex for the current accent (see `background/palette.ts`). */
export function accentHex(accent: Accent): number {
  return ACCENTS.find((a) => a.key === accent)?.hex ?? 0x8b5cf6;
}
