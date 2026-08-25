"""Screenshot annotation via Pillow.

Burns reviewer-drawn shapes (rectangle / arrow / highlight / circle / text) onto
a copy of a captured screenshot PNG. Real Pillow rendering only (ADR 0001) — no
simulated/placeholder output.

**Coordinates are FRACTIONS of the image (0–1), not pixels** (#695). One space, and
this module is where it is converted, because the callers cannot agree on pixels: the
browser shows the screenshot scaled to a panel, so a reviewer's drag only ever knows
what *fraction* of the image it covered — it has no idea of the native resolution.

That disagreement is the bug this docstring exists to prevent recurring. Three callers
each used a different space, and every one of them drew in the wrong place:

* the SPA sent fractions (and, worse, one hardcoded ``{x: .5, y: .5}``),
* ``evidence_analysis`` asked Claude for **percent (0–100)** and passed it straight
  through,
* this renderer treated whatever arrived as **absolute pixels**.

So a "centre" mark landed at pixel (0,0) and an auto-annotation squeezed into the top
-left 100x100 corner. Fractions win because they are the only space every caller can
actually produce; anything else converts to them before it gets here.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from app.schemas import AnnotationShape

_HIGHLIGHT_ALPHA = 90
# All in fractions of the image's shorter side, so a mark reads the same on an
# 800px screenshot and on a 2560px retina capture.
_LINE_WIDTH_RATIO = 0.004
_MIN_LINE_WIDTH = 2
_FONT_RATIO = 0.028
_MIN_FONT_PX = 12
_ARROWHEAD_RATIO = 0.025


def _rgba(color: str, alpha: int = 255) -> tuple[int, int, int, int]:
    """Parse a `#rrggbb` (or `#rgb`) hex color string into an RGBA tuple."""
    value = color.lstrip("#")
    if len(value) == 3:
        value = "".join(ch * 2 for ch in value)
    r, g, b = (int(value[i : i + 2], 16) for i in (0, 2, 4))
    return (r, g, b, alpha)


def _draw_shape(
    draw: ImageDraw.ImageDraw, shape: AnnotationShape, size: tuple[int, int]
) -> None:
    """Draw one fractional shape onto an overlay of ``size`` pixels."""
    width, height = size
    color = _rgba(shape.color)
    x1, y1 = shape.x * width, shape.y * height
    line_width = _line_width(size)

    if shape.tool == "rectangle":
        x2, y2 = x1 + shape.w * width, y1 + shape.h * height
        draw.rectangle([x1, y1, x2, y2], outline=color, width=line_width)
    elif shape.tool == "circle":
        x2, y2 = x1 + shape.w * width, y1 + shape.h * height
        draw.ellipse([x1, y1, x2, y2], outline=color, width=line_width)
    elif shape.tool == "arrow":
        x2, y2 = shape.x2 * width, shape.y2 * height
        draw.line([x1, y1, x2, y2], fill=color, width=line_width)
        _draw_arrowhead(draw, x1, y1, x2, y2, color, size)
    elif shape.tool == "highlight":
        x2, y2 = x1 + shape.w * width, y1 + shape.h * height
        draw.rectangle([x1, y1, x2, y2], fill=_rgba(shape.color, _HIGHLIGHT_ALPHA))
    elif shape.tool == "text":
        draw.text((x1, y1), shape.text, fill=color, font=_font(size))


def _line_width(size: tuple[int, int]) -> int:
    """Stroke width in pixels, scaled to the image.

    A 4px outline is assertive on an 800px screenshot and nearly invisible on a
    2560px one, and a reviewer's mark has to survive being looked at on both.
    """
    return max(_MIN_LINE_WIDTH, round(min(size) * _LINE_WIDTH_RATIO))


def _font(size: tuple[int, int]) -> ImageFont.ImageFont | ImageFont.FreeTypeFont:
    """A label font scaled to the image, falling back to Pillow's bitmap default.

    ``load_default`` is ~11px whatever the screenshot's resolution, which on a
    retina capture is an unreadable smudge. ``size=`` needs Pillow >= 10.1, and a
    missing font is not a reason to fail an annotation.
    """
    wanted = max(_MIN_FONT_PX, round(min(size) * _FONT_RATIO))
    try:
        return ImageFont.load_default(size=wanted)
    except TypeError:  # pragma: no cover - Pillow < 10.1
        return ImageFont.load_default()


def _draw_arrowhead(
    draw: ImageDraw.ImageDraw,
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    color: tuple[int, int, int, int],
    size: tuple[int, int],
) -> None:
    """Draw a small filled triangle at (x2, y2) pointing away from (x1, y1)."""
    import math

    angle = math.atan2(y2 - y1, x2 - x1)
    length, spread = max(10.0, min(size) * _ARROWHEAD_RATIO), 0.5
    left = (
        x2 - length * math.cos(angle - spread),
        y2 - length * math.sin(angle - spread),
    )
    right = (
        x2 - length * math.cos(angle + spread),
        y2 - length * math.sin(angle + spread),
    )
    draw.polygon([(x2, y2), left, right], fill=color)


def render_annotations(src_path: Path | str, shapes: list[AnnotationShape], dst_path: Path | str) -> Path:
    """Burn ``shapes`` onto a copy of the PNG at ``src_path`` and save to ``dst_path``.

    Args:
        src_path: Path to the source screenshot (PNG).
        shapes: Annotation shapes to draw, in order. Coordinates are **fractions of
            the image** (0-1) — see the module docstring for why, and for what went
            wrong when three callers each assumed a different space.
        dst_path: Output path for the annotated PNG. Parent dirs are created.

    Returns:
        The resolved ``dst_path``.
    """
    src_path = Path(src_path)
    dst_path = Path(dst_path)
    dst_path.parent.mkdir(parents=True, exist_ok=True)

    base = Image.open(src_path).convert("RGBA")
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    for shape in shapes:
        _draw_shape(draw, shape, base.size)

    composed = Image.alpha_composite(base, overlay).convert("RGB")
    composed.save(dst_path, format="PNG")
    return dst_path
