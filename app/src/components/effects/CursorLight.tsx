import { useEffect } from "react";

/**
 * Cursor glow — a faithful port of the design prototype's `initAmbient` light.
 * A 520px radial violet light appended to <body> at z-index 1 (behind the app's
 * content, so it illuminates the glass panels), `mix-blend:screen`, eased toward
 * the pointer at 0.14/frame. Purely decorative. Renders nothing itself.
 */
export function CursorLight() {
  useEffect(() => {
    const light = document.createElement("div");
    light.setAttribute("data-cursor-light", "");
    light.style.cssText =
      "position:fixed;top:0;left:0;width:520px;height:520px;margin:-260px 0 0 -260px;" +
      "border-radius:50%;pointer-events:none;z-index:1;opacity:0;transition:opacity .6s ease;" +
      // Dimmer than the design prototype (.16 -> .10): the veil in `AppBackground`
      // damps this too, but the glow was the brightest thing on screen and drew the
      // eye away from whatever the pointer was actually over (#722).
      "background:radial-gradient(circle,rgba(139,92,246,.10),rgba(99,102,241,.04) 42%,transparent 70%);" +
      "will-change:transform;mix-blend-mode:screen";
    document.body.appendChild(light);

    let lx = window.innerWidth / 2;
    let ly = window.innerHeight / 2;
    let tx = lx;
    let ty = ly;
    let raf = 0;

    const onMove = (e: MouseEvent) => {
      tx = e.clientX;
      ty = e.clientY;
      light.style.opacity = "1";
    };
    const onLeave = () => {
      light.style.opacity = "0";
    };
    const tick = () => {
      raf = requestAnimationFrame(tick);
      if (document.hidden) return;
      lx += (tx - lx) * 0.14;
      ly += (ty - ly) * 0.14;
      light.style.transform = `translate3d(${lx}px,${ly}px,0)`;
    };

    window.addEventListener("mousemove", onMove);
    document.addEventListener("mouseleave", onLeave);
    tick();

    return () => {
      window.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseleave", onLeave);
      cancelAnimationFrame(raf);
      light.remove();
    };
  }, []);

  return null;
}
