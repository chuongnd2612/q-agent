import { Redo2, Trash2, Undo2 } from "lucide-react";
import { useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Button } from "@/components/ui/Button";
import { Spinner } from "@/components/ui/misc";
import type { AnnotationTool } from "@/store/ui";
import type { AnnotationShape } from "@/types/api";

/** Every shape's stroke. Matches `AnnotationShape.color`'s server-side default, so
 * what is drawn here is what `services/annotate.py` burns into the image. */
const STROKE = "#f43f5e";

/** Below this (in image-relative units) a drag is a slip, not a shape. Without it
 * a click with a drag tool selected leaves an invisible zero-size shape that the
 * renderer faithfully draws as nothing. */
const MIN_DRAG = 0.005;

type Point = { x: number; y: number };

/**
 * The drawing surface for screenshot annotations (#695).
 *
 * Before this, the tool palette was a mock: selecting a tool highlighted it and did
 * nothing else, because the screenshot was a bare `<img>` with no pointer handlers
 * and no shape state — and **Save** posted one hardcoded shape at the image centre
 * whatever was selected and wherever the user clicked. So saving *appeared* to work
 * and produced an annotated image with a single mark in the middle, which reads as a
 * rendering bug rather than a missing feature. The server half
 * (`AnnotationShape` + `services/annotate.py`) has been complete all along.
 *
 * Three things here are decisions rather than detail:
 *
 * * **Coordinates are image-relative (0–1), never pixels.** The server renders at the
 *   screenshot's native resolution while the browser shows it scaled to the panel, so
 *   pixels would land somewhere else in the saved file. Every shape is stored as a
 *   fraction of the displayed box, which is the same fraction of the real one.
 * * **The overlay is one SVG with `viewBox="0 0 1 1"` and `preserveAspectRatio="none"`.**
 *   That makes the 0–1 coordinate space literal: no per-shape arithmetic, and it stays
 *   correct through every resize without a single measurement.
 * * **Nothing is sent until Save.** Drawing is local; the pending count is visible, and
 *   undo/clear operate on the draft. An annotation is a deliberate act, and a stray
 *   drag must not mutate stored evidence.
 */
export function AnnotationCanvas({
  src,
  alt,
  tool,
  saving,
  onSave,
}: {
  src: string;
  alt: string;
  tool: AnnotationTool;
  saving: boolean;
  onSave: (shapes: AnnotationShape[]) => void;
}) {
  const { t } = useTranslation("pipeline");
  const surface = useRef<HTMLDivElement>(null);
  const [shapes, setShapes] = useState<AnnotationShape[]>([]);
  const [undone, setUndone] = useState<AnnotationShape[]>([]);
  const [drag, setDrag] = useState<{ from: Point; to: Point } | null>(null);

  /** Pointer position as a fraction of the surface, clamped to it — a drag that
   * leaves the image still ends on the edge rather than off-canvas. */
  const pointAt = (e: React.PointerEvent): Point => {
    const box = surface.current!.getBoundingClientRect();
    return {
      x: Math.min(1, Math.max(0, (e.clientX - box.left) / box.width)),
      y: Math.min(1, Math.max(0, (e.clientY - box.top) / box.height)),
    };
  };

  const push = (shape: AnnotationShape) => {
    setShapes((current) => [...current, shape]);
    // A new shape ends the redo chain — keeping it would let Redo resurrect a shape
    // from a branch the user has already abandoned.
    setUndone([]);
  };

  const onPointerDown = (e: React.PointerEvent) => {
    if (tool === "cursor") return;
    e.preventDefault();
    const at = pointAt(e);
    if (tool === "text") {
      const text = window.prompt(t("evidence.annotate.textPrompt")) ?? "";
      if (text.trim()) push({ tool: "text", x: at.x, y: at.y, text: text.trim(), color: STROKE });
      return;
    }
    // Capture the pointer so a drag that leaves the image still completes here
    // rather than being lost to whatever it passes over.
    e.currentTarget.setPointerCapture?.(e.pointerId);
    setDrag({ from: at, to: at });
  };

  const onPointerMove = (e: React.PointerEvent) => {
    if (!drag) return;
    setDrag({ from: drag.from, to: pointAt(e) });
  };

  const onPointerUp = (e: React.PointerEvent) => {
    if (!drag) return;
    const to = pointAt(e);
    const { from } = drag;
    setDrag(null);
    if (Math.abs(to.x - from.x) < MIN_DRAG && Math.abs(to.y - from.y) < MIN_DRAG) return;
    if (tool === "arrow") {
      push({ tool: "arrow", x: from.x, y: from.y, x2: to.x, y2: to.y, color: STROKE });
      return;
    }
    push({
      tool,
      x: Math.min(from.x, to.x),
      y: Math.min(from.y, to.y),
      w: Math.abs(to.x - from.x),
      h: Math.abs(to.y - from.y),
      color: STROKE,
    });
  };

  const undo = () => {
    setShapes((current) => {
      if (!current.length) return current;
      setUndone((u) => [...u, current[current.length - 1]]);
      return current.slice(0, -1);
    });
  };
  const redo = () => {
    setUndone((current) => {
      if (!current.length) return current;
      setShapes((s) => [...s, current[current.length - 1]]);
      return current.slice(0, -1);
    });
  };
  const clear = () => {
    setShapes([]);
    setUndone([]);
  };

  const preview: AnnotationShape | null = !drag
    ? null
    : tool === "arrow"
      ? { tool: "arrow", x: drag.from.x, y: drag.from.y, x2: drag.to.x, y2: drag.to.y }
      : {
          tool,
          x: Math.min(drag.from.x, drag.to.x),
          y: Math.min(drag.from.y, drag.to.y),
          w: Math.abs(drag.to.x - drag.from.x),
          h: Math.abs(drag.to.y - drag.from.y),
        };

  return (
    <div>
      <div
        ref={surface}
        className="relative overflow-hidden rounded-b-[14px]"
        style={{ cursor: tool === "cursor" ? "default" : "crosshair", touchAction: "none" }}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        data-testid="annotation-surface"
      >
        {/* `draggable={false}`: the browser's native image drag would otherwise
            hijack the very gesture this surface exists to read. */}
        <img src={src} alt={alt} className="block w-full select-none" draggable={false} />
        <svg
          viewBox="0 0 1 1"
          preserveAspectRatio="none"
          className="pointer-events-none absolute inset-0 h-full w-full"
        >
          {shapes.map((shape, i) => (
            <Shape key={i} shape={shape} />
          ))}
          {preview && <Shape shape={preview} preview />}
        </svg>
      </div>

      <div className="mt-3.5 flex flex-wrap items-center gap-2.5 text-[12.5px] text-ink-dim">
        <span className="font-semibold text-[#c4b5fd]">{t("evidence.toolLabel")}</span>
        {t(`evidence.tools.${tool}`)}
        <span className="text-faint" data-testid="annotation-pending">
          {t("evidence.annotate.pending", { count: shapes.length })}
        </span>
        <div className="ml-auto flex flex-wrap items-center gap-2">
          <Button variant="glass" size="sm" onClick={undo} disabled={!shapes.length}>
            <Undo2 size={13} strokeWidth={2.2} /> {t("evidence.annotate.undo")}
          </Button>
          <Button variant="glass" size="sm" onClick={redo} disabled={!undone.length}>
            <Redo2 size={13} strokeWidth={2.2} /> {t("evidence.annotate.redo")}
          </Button>
          <Button variant="glass" size="sm" onClick={clear} disabled={!shapes.length}>
            <Trash2 size={13} strokeWidth={2.2} /> {t("evidence.annotate.clear")}
          </Button>
          <Button
            variant="primary"
            size="sm"
            onClick={() => onSave(shapes)}
            // Saving nothing would burn an "annotated" copy with no annotations on it
            // and flip the Annotated/Original toggle on for a picture identical to the
            // original — a state that looks broken and explains nothing.
            disabled={saving || !shapes.length}
            data-testid="annotation-save"
          >
            {saving ? <Spinner size={13} /> : null}
            {t("evidence.saveAnnotation")}
          </Button>
        </div>
      </div>
      {!shapes.length && tool === "cursor" && (
        <div className="mt-1.5 text-[11.5px] text-faint">{t("evidence.annotate.hint")}</div>
      )}
    </div>
  );
}

/** One shape in the 0–1 overlay space. Deliberately mirrors what
 * `services/annotate.py` draws, so the preview is not a different picture from the
 * saved file: an outline for rectangle/circle, a translucent fill for highlight, a
 * line with a head for arrow, and a label for text. */
function Shape({ shape, preview = false }: { shape: AnnotationShape; preview?: boolean }) {
  const color = shape.color ?? STROKE;
  // `non-scaling-stroke` makes strokeWidth a SCREEN width, not a user-space one — so
  // this is 2.5px, and the 0-1 fraction it would otherwise be renders as literally
  // nothing (which is exactly how the first version of this shipped invisible
  // rectangles, arrows and circles while the filled highlight looked fine).
  const stroke = { stroke: color, strokeWidth: 2.5, vectorEffect: "non-scaling-stroke" as const };
  // Dashes are in screen units too, for the same reason.
  const dashed = preview ? { strokeDasharray: "6 4" } : {};

  if (shape.tool === "rectangle" || shape.tool === "highlight") {
    const isHighlight = shape.tool === "highlight";
    return (
      <rect
        x={shape.x}
        y={shape.y}
        width={shape.w ?? 0}
        height={shape.h ?? 0}
        fill={isHighlight ? color : "none"}
        fillOpacity={isHighlight ? 0.28 : 0}
        {...stroke}
        {...dashed}
      />
    );
  }
  if (shape.tool === "circle") {
    const w = shape.w ?? 0;
    const h = shape.h ?? 0;
    return (
      <ellipse
        cx={shape.x + w / 2}
        cy={shape.y + h / 2}
        rx={w / 2}
        ry={h / 2}
        fill="none"
        {...stroke}
        {...dashed}
      />
    );
  }
  if (shape.tool === "arrow") {
    const x2 = shape.x2 ?? shape.x;
    const y2 = shape.y2 ?? shape.y;
    const angle = Math.atan2(y2 - shape.y, x2 - shape.x);
    const head = 0.035;
    const spread = 0.5;
    return (
      <g>
        <line x1={shape.x} y1={shape.y} x2={x2} y2={y2} {...stroke} {...dashed} />
        <line
          x1={x2}
          y1={y2}
          x2={x2 - head * Math.cos(angle - spread)}
          y2={y2 - head * Math.sin(angle - spread)}
          {...stroke}
        />
        <line
          x1={x2}
          y1={y2}
          x2={x2 - head * Math.cos(angle + spread)}
          y2={y2 - head * Math.sin(angle + spread)}
          {...stroke}
        />
      </g>
    );
  }
  if (shape.tool === "text") {
    return (
      <text
        x={shape.x}
        y={shape.y}
        fill={color}
        // The overlay is 1 unit tall, so a readable label is a small fraction of it.
        fontSize={0.028}
        fontWeight={700}
        // A dark halo so a label stays readable over a light or busy screenshot.
        // Screen units again (see `stroke` above) — as a fraction it swamped the text.
        style={{
          paintOrder: "stroke",
          stroke: "rgba(0,0,0,.7)",
          strokeWidth: 3,
          vectorEffect: "non-scaling-stroke",
        }}
      >
        {shape.text}
      </text>
    );
  }
  return null;
}
