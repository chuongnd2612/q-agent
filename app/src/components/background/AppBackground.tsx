import { NeuralBackground } from "@/components/background/NeuralBackground";
import { AmbientLayer } from "@/components/background/AmbientLayer";
import { useSettings } from "@/hooks/queries";

/**
 * How much the backdrop is damped before the app draws over it (#722).
 *
 * ONE lever, deliberately. The backdrop's brightness comes from a WebGL canvas, two
 * pulsing glow blobs, a drifting dust layer and a cursor light — turning each of their
 * opacity constants down would be four numbers nobody can reason about together, and
 * they would drift apart the next time one was touched. A single veil sits between the
 * whole backdrop and the content and damps all of it uniformly.
 *
 * The value is a readability decision, not a taste one: the constellation was bleeding
 * through panels far enough to compete with body text.
 */
const BACKDROP_VEIL = "rgba(9,9,14,.55)";

/**
 * Chooses the app backdrop based on the user's "3D background" setting. When on
 * (the default) it renders the animated WebGL {@link NeuralBackground} plus the
 * {@link AmbientLayer} atmospheric fog/cursor light; when off it renders a flat
 * dark fill so the shell keeps its solid backdrop without any GPU/animation
 * cost. Defaults to on while settings are still loading so the background
 * doesn't flash for the common case.
 */
export function AppBackground() {
  const { data: settings } = useSettings();
  const enabled = settings?.neuralBackground !== false;

  if (enabled) {
    return (
      <>
        <NeuralBackground />
        <AmbientLayer />
        {/* Above the canvas, the glows and the cursor light (all z-[1]); below the
            shell (z-[2]). That seam is the only place a single layer can damp the
            entire backdrop, cursor glow included, without touching any of them. */}
        <div
          className="pointer-events-none fixed inset-0 z-[1]"
          style={{ background: BACKDROP_VEIL }}
          aria-hidden
        />
      </>
    );
  }
  return <div className="fixed inset-0 z-0" style={{ background: "#0a0a0f" }} />;
}
