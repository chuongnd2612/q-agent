/**
 * three.js *scene* colours for the constellation background (#783, part of #782).
 *
 * These are numeric hex literals consumed by `new THREE.Color(...)`, not CSS.
 * They deliberately do NOT live in the token layer: **WebGL cannot read CSS
 * custom properties**, so a `var(--p)` is meaningless to a material. This
 * documented map is the single place scene colours are declared — no component
 * renders a raw hex.
 *
 * Declared here by #783; `NeuralBackground` is wired to it by #785.
 */

import type { Accent } from "@/store/appearance";

/** Accent tokens, "three.js hex" column. Mirrors `ACCENTS` in the store. */
export const ACCENT_HEX: Record<Accent, number> = {
  red: 0xe1172b,
  purple: 0x8b5cf6,
  cyan: 0x22d3ee,
  steel: 0xb4becd,
};

export interface ScenePalette {
  /** Second colour of the [accent, silver, steel] cycle. */
  silver: number;
  /** Third colour of the cycle. */
  steel: number;
  /** Edge (LineSegments) colour. */
  line: number;
  /** Base opacity of the node PointsMaterial. */
  nodeOpacity: number;
  /** Base opacity of the edge LineBasicMaterial. */
  lineOpacity: number;
  /** Dust twinkle opacity = base + swing · world. */
  dustBase: number;
  dustSwing: number;
}

/** Dark → additive glow on near-black. */
export const DARK_PALETTE: ScenePalette = {
  silver: 0xdfe4ec,
  steel: 0x7a8290,
  line: 0x8d97a8,
  nodeOpacity: 0.9,
  lineOpacity: 0.45,
  dustBase: 0.4,
  dustSwing: 0.32,
};

/**
 * Light → normal blending, darkened greys so the field reads on paper.
 *
 * Additive blending is the reason this is a separate palette rather than a
 * tweak: on a light background "add" saturates towards white, so the dark
 * palette does not merely look wrong in light mode, it disappears entirely.
 */
export const LIGHT_PALETTE: ScenePalette = {
  silver: 0x6b7280,
  steel: 0x99a1b2,
  line: 0x5a6472,
  nodeOpacity: 0.72,
  lineOpacity: 0.26,
  dustBase: 0.22,
  dustSwing: 0.18,
};

export function scenePalette(mode: "dark" | "light"): ScenePalette {
  return mode === "light" ? LIGHT_PALETTE : DARK_PALETTE;
}
