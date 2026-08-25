"""Automatic failure-screenshot analysis + annotation.

When a test fails, this sends the failure screenshot (plus the Playwright error)
to Claude vision and asks for a short diagnosis and a few annotation shapes
(bounding box / arrow / highlight / caption) in percent coordinates. Those are
scaled to pixels and burned onto a copy of the screenshot via the existing Pillow
renderer (:func:`app.services.annotate.render_annotations`). The diagnosis and the
path to the annotated PNG are stored on the Evidence row's ``meta`` so the UI and
review comments can use it.

Best-effort throughout: any failure (no Claude, bad JSON, render error) leaves the
original screenshot untouched and never breaks the run.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from PIL import Image

from app.logging import logger
from app.models.execution import Evidence, ExecutionResult
from app.models.run import Run
from app.schemas import AnnotationShape
from app.services import claude_cli
from app.services.annotate import render_annotations
from app.services.skills import SCREENSHOT_ANNOTATOR
from app.services.workspace_scope import scoped_evidence_dir

_ALLOWED_TOOLS = {"rectangle", "arrow", "highlight", "circle", "text"}
_MAX_SHAPES = 4

_PROMPT = (
    "A Playwright end-to-end UI test FAILED. The failure screenshot is the image "
    "file `{name}` in this directory — open and look at it.\n\n"
    "Playwright error:\n{error}\n\n"
    "Analyse the screenshot together with the error and localise the most likely "
    "problem area on screen. Respond with ONLY compact JSON (no prose, no code "
    "fence) shaped exactly like:\n"
    '{{"diagnosis":"<=200 char plain-language likely root cause",'
    '"shapes":['
    '{{"tool":"rectangle","x":10,"y":20,"w":30,"h":8,"color":"#f43f5e"}},'
    '{{"tool":"arrow","x":50,"y":60,"x2":40,"y2":28,"color":"#f43f5e"}},'
    '{{"tool":"text","x":10,"y":12,"text":"short label","color":"#f43f5e"}}'
    "]}}\n\n"
    "All coordinates are PERCENT of image width/height (0-100). Draw a rectangle "
    "around the element that is wrong, missing or unexpected; optionally an arrow "
    "pointing at it and a short text label. Use at most 4 shapes. If you cannot "
    "localise it, return \"shapes\":[] but still give a diagnosis."
)


def _parse_json(raw: str) -> dict[str, Any] | None:
    """Tolerantly pull a JSON object out of the model's reply."""
    raw = (raw or "").strip()
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        pass
    start, end = raw.find("{"), raw.rfind("}")
    if start != -1 and end > start:
        try:
            obj = json.loads(raw[start : end + 1])
            return obj if isinstance(obj, dict) else None
        except json.JSONDecodeError:
            return None
    return None


def _pct_to_fraction(shape: dict[str, Any]) -> dict[str, Any]:
    """Rescale one model-authored shape from percent (0-100) to fraction (0-1).

    The prompt asks for percent because that is what a vision model reasons in
    reliably; the renderer takes fractions of the image (#695). This is the single
    boundary between the two, which is what keeps a third coordinate space from
    appearing — the image size is deliberately no longer a parameter, because a
    fraction does not depend on it.

    Unparseable values become 0 rather than raising: a model that returns "10%" as a
    string should cost one misplaced shape, not the whole annotation.
    """
    def fraction(value: Any) -> float:
        try:
            return max(0.0, min(100.0, float(value))) / 100.0
        except (TypeError, ValueError):
            return 0.0

    return {
        "tool": str(shape.get("tool", "")).lower(),
        "x": fraction(shape.get("x", 0)),
        "y": fraction(shape.get("y", 0)),
        "w": fraction(shape.get("w", 0)),
        "h": fraction(shape.get("h", 0)),
        "x2": fraction(shape.get("x2", 0)),
        "y2": fraction(shape.get("y2", 0)),
        "text": str(shape.get("text", ""))[:120],
        "color": str(shape.get("color", "#f43f5e") or "#f43f5e"),
    }


def _analyze(src: Path, error: str) -> dict[str, Any] | None:
    """Ask Claude (vision) for a diagnosis + annotation shapes. None on failure.

    Shapes come back as fractions of the image (#695) — the size is not a parameter
    because a fraction does not depend on it.
    """
    prompt = _PROMPT.format(name=src.name, error=(error or "(no error message)")[:1500])
    try:
        raw = claude_cli.run_prompt(
            prompt, cwd=src.parent, skill=SCREENSHOT_ANNOTATOR, label=f"Annotate: {src.name}"
        )
    except Exception as exc:  # noqa: BLE001 - best-effort
        logger.warning("evidence analysis: Claude call failed: {}", exc)
        return None
    data = _parse_json(raw)
    if data is None:
        return None
    diagnosis = str(data.get("diagnosis", "")).strip()[:400]
    shapes = [
        _pct_to_fraction(s)
        for s in (data.get("shapes") or [])
        if isinstance(s, dict) and str(s.get("tool", "")).lower() in _ALLOWED_TOOLS
    ]
    return {"diagnosis": diagnosis, "shapes": shapes[:_MAX_SHAPES]}


def _caption_shapes(diagnosis: str, w: int, h: int) -> list[dict[str, Any]]:
    """Fallback annotation when nothing localisable: a red caption bar up top.

    Fractions of the image, like every other shape (#695). This used to emit pixels
    (``w`` and a pixel bar height) into a renderer that also read pixels — which is
    why it was the ONE annotation path that looked right, and why the disagreement
    everywhere else went unnoticed for so long.
    """
    del w, h  # proportional now (#695); kept in the signature for the caller's call site
    return [
        {"tool": "highlight", "x": 0, "y": 0, "w": 1.0, "h": 0.06, "color": "#f43f5e"},
        {"tool": "text", "x": 0.012, "y": 0.012, "w": 0, "h": 0, "x2": 0, "y2": 0,
         "text": diagnosis[:110], "color": "#ffffff"},
    ]



def _owner_id_for_result(db, result: ExecutionResult | None) -> int | None:
    """Resolve the owning user id of the Run behind an ExecutionResult's execution.

    Returns ``None`` (shared namespace) when the result/execution is missing —
    matches the ``owned``/``get_owned_or_404`` bridge (#91) where an unowned
    row is always accessible.
    """
    if result is None or result.execution is None:
        return None
    run = db.get(Run, result.execution.run_id)
    return run.owner_id if run is not None else None


def annotate_screenshot(db, evidence: Evidence, error_message: str, *, force: bool = False) -> bool:
    """Analyse + annotate one screenshot Evidence row. Returns True if annotated.

    Stores ``meta.diagnosis`` + ``meta.annotatedPath`` and sets ``annotated``.
    Idempotent unless ``force`` (skips evidence already auto-annotated).

    ``evidence.path`` is stored relative to the scoped evidence root of the
    owning run (ADR 0009 §1/§5 — ``<RUN-CODE>/<ticket>/<case>/<file>``, see
    ``playwright_runner._store_evidence``), so ``src``/``dst`` are resolved
    under ``scoped_evidence_dir(<owner of the evidence's run>)`` rather than
    the legacy flat ``settings.evidence_dir``.
    """
    if evidence.kind != "screenshot":
        return False
    meta = dict(evidence.meta or {})
    if meta.get("autoAnnotated") and not force:
        return False
    evidence_root = scoped_evidence_dir(_owner_id_for_result(db, evidence.result))
    src = evidence_root / evidence.path
    if not src.exists():
        return False
    try:
        with Image.open(src) as im:
            w, h = im.size
    except Exception as exc:  # noqa: BLE001
        logger.warning("evidence analysis: cannot open {}: {}", src, exc)
        return False

    analysis = _analyze(src, error_message)
    diagnosis = (analysis or {}).get("diagnosis") or (error_message or "Test failed")[:200]
    shapes_data = (analysis or {}).get("shapes") or []
    if not shapes_data:
        shapes_data = _caption_shapes(diagnosis, w, h)

    try:
        shapes = [AnnotationShape(**s) for s in shapes_data]
        dst_rel = Path(evidence.path).with_name(Path(evidence.path).stem + "-annotated.png")
        dst = evidence_root / dst_rel
        render_annotations(src, shapes, dst)
    except Exception as exc:  # noqa: BLE001
        logger.warning("evidence analysis: render failed for {}: {}", src, exc)
        return False

    meta.update(
        {
            "autoAnnotated": True,
            "diagnosis": diagnosis,
            "annotatedPath": str(dst_rel).replace("\\", "/"),
        }
    )
    evidence.meta = meta
    evidence.annotated = True
    db.add(evidence)
    db.commit()
    logger.info("Auto-annotated failure screenshot {} ({} shapes)", evidence.path, len(shapes_data))
    return True


def auto_annotate_result(db, run, result) -> None:
    """Auto-annotate the failure screenshot of a failed ExecutionResult (best-effort)."""
    try:
        shot = next(
            (e for e in result.evidence
             if e.kind == "screenshot" and not (e.meta or {}).get("autoAnnotated")),
            None,
        )
        if shot is None:
            return
        annotate_screenshot(db, shot, result.error_message or "")
    except Exception as exc:  # noqa: BLE001 - never break the run
        logger.warning("auto_annotate_result failed for result {}: {}", getattr(result, "id", "?"), exc)
