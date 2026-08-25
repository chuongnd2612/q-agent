"""Tests for evidence grouping and Pillow-based annotation."""

from __future__ import annotations

from pathlib import Path

from PIL import Image


def _make_png(path: Path, size: tuple[int, int] = (100, 80)) -> None:
    Image.new("RGB", size, color=(10, 20, 30)).save(path, format="PNG")


def _seed_execution(db_session, run_id: int = 1):
    from app.models.execution import Execution, ExecutionResult
    from app.models.run import Run
    from app.models.ticket import Ticket

    run = Run(id=run_id, code="RUN-1", name="Run 1", status="evidence")
    db_session.add(run)

    ticket = Ticket(external_id="SUR-1", provider_kind="ado", title="Login works")
    db_session.add(ticket)
    db_session.flush()

    execution = Execution(run_id=run_id, status="done", total=2, passed=1, failed=1)
    db_session.add(execution)
    db_session.flush()

    pass_result = ExecutionResult(
        execution_id=execution.id,
        test_case_id=1,
        ticket_external_id="SUR-1",
        case_code="TC-01",
        title="Case 1",
        status="pass",
        duration_ms=500,
    )
    fail_result = ExecutionResult(
        execution_id=execution.id,
        test_case_id=2,
        ticket_external_id="SUR-1",
        case_code="TC-02",
        title="Case 2",
        status="fail",
        duration_ms=700,
        error_message="assertion failed",
    )
    db_session.add_all([pass_result, fail_result])
    db_session.flush()

    return execution, fail_result


def test_render_annotations_creates_output_file(tmp_path: Path):
    from app.schemas import AnnotationShape
    from app.services.annotate import render_annotations

    src = tmp_path / "shot.png"
    _make_png(src)
    dst = tmp_path / "shot-annotated.png"

    shapes = [
        AnnotationShape(tool="rectangle", x=5, y=5, w=20, h=15, color="#f43f5e"),
        AnnotationShape(tool="arrow", x=0, y=0, x2=50, y2=40, color="#22c55e"),
        AnnotationShape(tool="circle", x=10, y=10, w=30, h=30, color="#3b82f6"),
        AnnotationShape(tool="highlight", x=0, y=0, w=100, h=10, color="#eab308"),
        AnnotationShape(tool="text", x=2, y=2, text="broken here", color="#000000"),
    ]

    result_path = render_annotations(src, shapes, dst)

    assert result_path == dst
    assert dst.exists()
    with Image.open(dst) as img:
        assert img.size == (100, 80)


def test_get_run_evidence_groups_by_ticket(client, db_session):
    execution, _fail_result = _seed_execution(db_session)
    db_session.commit()

    resp = client.get("/runs/1/evidence")
    assert resp.status_code == 200
    body = resp.json()

    assert len(body["tickets"]) == 1
    ticket_summary = body["tickets"][0]
    assert ticket_summary["id"] == "SUR-1"
    assert ticket_summary["pass"] == 1
    assert ticket_summary["fail"] == 1
    assert ticket_summary["provGlyph"] == "AD"
    assert ticket_summary["statusLabel"] == "Failed"

    assert "SUR-1" in body["byTicket"]
    assert len(body["byTicket"]["SUR-1"]) == 2


def test_get_result_evidence_returns_list(client, db_session):
    from app.models.execution import Evidence

    _execution, fail_result = _seed_execution(db_session)
    evidence = Evidence(
        result_id=fail_result.id,
        kind="screenshot",
        path="shot.png",
        filename="shot.png",
        size_bytes=123,
    )
    db_session.add(evidence)
    db_session.commit()

    resp = client.get(f"/results/{fail_result.id}/evidence")
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) == 1
    assert items[0]["kind"] == "screenshot"
    assert items[0]["annotated"] is False
    # Served path is scope-prefixed (ADR 0009 §5): run 1 has no owner -> shared.
    assert items[0]["path"] == "shared/evidence/shot.png"


def test_annotate_evidence_endpoint(client, db_session, workspace_dir: Path):
    from app.config import get_settings
    from app.models.execution import Evidence
    from app.services.workspace_scope import scoped_evidence_dir

    _execution, fail_result = _seed_execution(db_session)

    settings = get_settings()
    # Run 1 has no owner (auth disabled in tests) -> the shared namespace.
    src_relpath = "run1/SUR-1/shot.png"
    src_path = scoped_evidence_dir(None) / src_relpath
    src_path.parent.mkdir(parents=True, exist_ok=True)
    _make_png(src_path)

    evidence = Evidence(
        result_id=fail_result.id,
        kind="screenshot",
        path=src_relpath,
        filename="shot.png",
        size_bytes=src_path.stat().st_size,
    )
    db_session.add(evidence)
    db_session.commit()
    db_session.refresh(evidence)

    resp = client.post(
        f"/evidence/{evidence.id}/annotate",
        json={"shapes": [{"tool": "rectangle", "x": 1, "y": 1, "w": 10, "h": 10, "color": "#ff0000"}]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["annotated"] is True
    assert body["path"].endswith("-annotated.png")
    # The served path is scope-prefixed (ADR 0009 §5): "shared/evidence/...".
    assert body["path"].startswith("shared/evidence/")

    annotated_path = settings.workspace_dir / body["path"]
    assert annotated_path.exists()


def test_ticket_passed_only_when_all_approved_cases_pass(client, db_session):
    """A ticket is 'Passed' in Evidence only when every approved automatable
    case's script ran and passed — one passing case out of two is 'Pending'."""
    from app.models.execution import Execution, ExecutionResult
    from app.models.run import Run
    from app.models.testcase import TestCase
    from app.models.ticket import Ticket

    run = Run(id=88, code="RUN-88", name="Run 88", status="evidence")
    db_session.add(run)
    db_session.add(Ticket(external_id="SUR-9", provider_kind="ado", title="Broker list"))
    # Two approved, automatable cases on the ticket.
    for code in ("TC-01", "TC-02"):
        db_session.add(TestCase(run_id=88, ticket_external_id="SUR-9", code=code,
                                title=code, approval="approved", automation="Playwright"))
    execution = Execution(run_id=88, status="done", total=1, passed=1, failed=0)
    db_session.add(execution)
    db_session.flush()
    # Only ONE of the two approved cases actually ran (and passed).
    db_session.add(ExecutionResult(execution_id=execution.id, test_case_id=1,
                                   ticket_external_id="SUR-9", case_code="TC-01",
                                   title="TC-01", status="pass", duration_ms=100))
    db_session.commit()

    summary = client.get("/runs/88/evidence").json()["tickets"][0]
    assert summary["approved"] == 2
    assert summary["pass"] == 1
    assert summary["statusLabel"] == "Pending"  # not Passed — TC-02 hasn't passed

    # Run the second approved case successfully → now the ticket is Passed.
    db_session.add(ExecutionResult(execution_id=execution.id, test_case_id=2,
                                   ticket_external_id="SUR-9", case_code="TC-02",
                                   title="TC-02", status="pass", duration_ms=120))
    db_session.commit()
    db_session.expire_all()  # shared test session: force the endpoint to reload results
    summary2 = client.get("/runs/88/evidence").json()["tickets"][0]
    assert summary2["pass"] == 2
    assert summary2["statusLabel"] == "Passed"


# --------------------------------------------------------------- auto-annotation


def _seed_failed_screenshot(db_session):
    """A failed result with a real screenshot Evidence on disk."""
    from app.models.execution import Evidence
    from app.services.workspace_scope import scoped_evidence_dir

    _execution, fail_result = _seed_execution(db_session, run_id=77)
    fail_result.error_message = 'expect(locator).toContainText("Activate") — not found'
    db_session.flush()

    rel = "RUN-77/SUR-1/TC-02/test-failed-1.png"
    # Run 77 has no owner (auth disabled in tests) -> the shared namespace.
    abs_path = scoped_evidence_dir(None) / rel
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (400, 300), (240, 240, 245)).save(abs_path)
    ev = Evidence(result_id=fail_result.id, kind="screenshot", path=rel,
                  filename="test-failed-1.png", size_bytes=abs_path.stat().st_size)
    db_session.add(ev)
    db_session.commit()
    db_session.refresh(ev)
    # A Run object for the service signature.
    from app.models.run import Run
    run = db_session.get(Run, 77)
    return run, fail_result, ev


def test_auto_annotate_burns_shapes_and_stores_diagnosis(db_session, monkeypatch):
    from app.services import claude_cli, evidence_analysis
    from app.services.workspace_scope import scoped_evidence_dir

    run, result, ev = _seed_failed_screenshot(db_session)
    monkeypatch.setattr(
        claude_cli, "run_prompt",
        lambda *a, **k: '{"diagnosis":"The Activate action is missing from the menu",'
        '"shapes":[{"tool":"rectangle","x":10,"y":20,"w":40,"h":12,"color":"#f43f5e"},'
        '{"tool":"text","x":10,"y":6,"text":"missing option","color":"#f43f5e"}]}',
    )

    evidence_analysis.auto_annotate_result(db_session, run, result)
    db_session.refresh(ev)

    assert ev.annotated is True
    assert ev.meta["autoAnnotated"] is True
    assert "Activate action is missing" in ev.meta["diagnosis"]
    assert (scoped_evidence_dir(run.owner_id) / ev.meta["annotatedPath"]).exists()


def test_auto_annotate_falls_back_to_caption_on_bad_json(db_session, monkeypatch):
    from app.services import claude_cli, evidence_analysis
    from app.services.workspace_scope import scoped_evidence_dir

    run, result, ev = _seed_failed_screenshot(db_session)
    monkeypatch.setattr(claude_cli, "run_prompt", lambda *a, **k: "sorry, I cannot help")

    evidence_analysis.auto_annotate_result(db_session, run, result)
    db_session.refresh(ev)

    assert ev.annotated is True
    assert ev.meta["diagnosis"]  # falls back to the error message
    assert (scoped_evidence_dir(run.owner_id) / ev.meta["annotatedPath"]).exists()


def test_auto_annotate_endpoint(client, db_session, monkeypatch):
    from app.models.execution import Evidence
    from app.services import claude_cli

    _run, _result, ev = _seed_failed_screenshot(db_session)
    monkeypatch.setattr(
        claude_cli, "run_prompt",
        lambda *a, **k: '{"diagnosis":"Broken selector","shapes":[]}',
    )

    resp = client.post(f"/evidence/{ev.id}/auto-annotate")
    assert resp.status_code == 200
    body = resp.json()
    assert body["annotated"] is True
    assert body["meta"]["diagnosis"] == "Broken selector"

    vid = Evidence(result_id=ev.result_id, kind="video", path="x.webm", filename="x.webm")
    db_session.add(vid)
    db_session.commit()
    assert client.post(f"/evidence/{vid.id}/auto-annotate").status_code == 400
    assert client.post("/evidence/999999/auto-annotate").status_code == 404


# ---------------------------------------------------------------------------
# Where the marks actually land (#695)
# ---------------------------------------------------------------------------
#
# The tests above assert an output file exists and has the right dimensions. That is
# what let three callers disagree about the coordinate space for months while every
# annotation drew in the wrong place: the SPA sent fractions (0-1),
# `evidence_analysis` asked Claude for percent (0-100), and the renderer read absolute
# pixels — so a "centre" mark landed at pixel (0,0) and an auto-annotation squeezed
# into the top-left 100x100 corner.
#
# So these assert POSITION, and each carries the negative control that matters: the
# region where the *wrong* interpretation would have drawn must be untouched.

_WHITE = (255, 255, 255)


def _canvas(path: Path, size: tuple[int, int] = (400, 400)) -> Path:
    Image.new("RGB", size, _WHITE).save(path)
    return path


#: `_seed_failed_screenshot`'s canvas colour — the "blank" for the auto-annotate
#: assertions, which run against that fixture rather than a white page.
_SEEDED = (240, 240, 245)


def _painted(img: Image.Image, x: int, y: int, blank: tuple[int, int, int] = _WHITE) -> bool:
    """Is this pixel no longer the blank canvas?"""
    return img.convert("RGB").getpixel((x, y)) != blank


def _painted_in(
    img: Image.Image,
    box: tuple[int, int, int, int],
    blank: tuple[int, int, int] = _WHITE,
) -> int:
    """How many pixels inside ``box`` (l, t, r, b) are painted.

    Crops first and reads the whole region in one go: `getpixel` per coordinate on a
    2000px image is slow enough to dominate the suite, and converting inside the loop
    (as the first version did) re-converts the entire image per pixel.
    """
    left, top, right, bottom = box
    region = img.convert("RGB").crop((left, top, right, bottom))
    return sum(1 for pixel in region.getdata() if pixel != blank)


def _painted_ratio(img: Image.Image) -> float:
    """Fraction of the whole image that is painted."""
    rgb = img.convert("RGB")
    data = list(rgb.getdata())
    return sum(1 for pixel in data if pixel != _WHITE) / len(data)


def test_shapes_are_placed_by_fraction_of_the_image_not_by_pixel(tmp_path: Path):
    """A rectangle over the middle quarter must be drawn in the middle."""
    from app.schemas import AnnotationShape
    from app.services.annotate import render_annotations

    src = _canvas(tmp_path / "shot.png")
    dst = tmp_path / "out.png"

    render_annotations(
        src,
        [AnnotationShape(tool="rectangle", x=0.5, y=0.5, w=0.25, h=0.25)],
        dst,
    )

    with Image.open(dst) as img:
        # Its top-left corner is at (200, 200) — the outline passes through there.
        assert _painted_in(img, (195, 195, 210, 210)) > 0, "nothing drawn at the fraction"
        # The negative control: pixel-space would have put this 0.5px from the origin,
        # i.e. in this corner. Nothing may be there.
        assert _painted_in(img, (0, 0, 40, 40)) == 0, "drawn as pixels, not fractions"


def test_a_full_width_highlight_reaches_both_edges(tmp_path: Path):
    """`w=1.0` means the whole width, whatever the image's resolution."""
    from app.schemas import AnnotationShape
    from app.services.annotate import render_annotations

    src = _canvas(tmp_path / "shot.png")
    dst = tmp_path / "out.png"

    render_annotations(src, [AnnotationShape(tool="highlight", x=0, y=0, w=1.0, h=0.1)], dst)

    with Image.open(dst) as img:
        assert _painted(img, 2, 20) and _painted(img, 397, 20), "bar does not span the width"
        assert not _painted(img, 200, 300), "bar is taller than the fraction it named"


def test_an_arrow_ends_where_it_was_pointed(tmp_path: Path):
    """The head is the whole point of an arrow — it must land on the target."""
    from app.schemas import AnnotationShape
    from app.services.annotate import render_annotations

    src = _canvas(tmp_path / "shot.png")
    dst = tmp_path / "out.png"

    render_annotations(
        src, [AnnotationShape(tool="arrow", x=0.1, y=0.1, x2=0.8, y2=0.8)], dst
    )

    with Image.open(dst) as img:
        assert _painted_in(img, (305, 305, 335, 335)) > 0, "no arrowhead near (320, 320)"
        assert _painted_in(img, (360, 40, 400, 80)) == 0, "drew somewhere it was not pointed"


def test_a_mark_stays_proportional_on_a_high_resolution_capture(tmp_path: Path):
    """The same fractions on a 5x larger screenshot must cover the same *area*.

    A retina capture is the case where a pixel-space renderer looks almost right on
    the developer's screenshot and badly wrong on the customer's.
    """
    from app.schemas import AnnotationShape
    from app.services.annotate import render_annotations

    shape = AnnotationShape(tool="highlight", x=0.25, y=0.25, w=0.5, h=0.5)
    small = tmp_path / "small-out.png"
    large = tmp_path / "large-out.png"
    render_annotations(_canvas(tmp_path / "small.png", (400, 400)), [shape], small)
    render_annotations(_canvas(tmp_path / "large.png", (1200, 1200)), [shape], large)

    with Image.open(small) as img:
        small_ratio = _painted_ratio(img)
    with Image.open(large) as img:
        large_ratio = _painted_ratio(img)

    assert abs(small_ratio - large_ratio) < 0.02, (small_ratio, large_ratio)
    assert small_ratio > 0.2, "the highlight covered almost nothing"


def test_the_stroke_thickens_with_the_image(tmp_path: Path):
    """A 4px outline is assertive at 800px and invisible at 2560px."""
    from app.schemas import AnnotationShape
    from app.services.annotate import render_annotations

    shape = AnnotationShape(tool="rectangle", x=0.2, y=0.2, w=0.6, h=0.6)
    small, large = tmp_path / "s.png", tmp_path / "l.png"
    render_annotations(_canvas(tmp_path / "a.png", (400, 400)), [shape], small)
    render_annotations(_canvas(tmp_path / "b.png", (1200, 1200)), [shape], large)

    # Walk across the left edge of each rectangle and count the outline's thickness.
    with Image.open(small) as img:
        thin = _painted_in(img, (70, 200, 100, 201))
    with Image.open(large) as img:
        thick = _painted_in(img, (220, 600, 280, 601))

    assert thick > thin, (thin, thick)


# ---------------------------------------------------------------------------
# The model speaks percent; the renderer speaks fractions (#695)
# ---------------------------------------------------------------------------


def test_model_percentages_are_converted_to_fractions():
    """The prompt asks for 0-100 because a vision model reasons in percent reliably.

    Converting at this one boundary is what stops a third coordinate space existing.
    It used to convert to *pixels*, which was correct against the old pixel renderer
    and is now one multiplication too many.
    """
    from app.services.evidence_analysis import _pct_to_fraction

    converted = _pct_to_fraction(
        {"tool": "rectangle", "x": 10, "y": 20, "w": 40, "h": 12, "color": "#f43f5e"}
    )

    assert converted["x"] == 0.1
    assert converted["y"] == 0.2
    assert converted["w"] == 0.4
    assert converted["h"] == 0.12
    assert converted["color"] == "#f43f5e", "non-coordinate fields must survive intact"
    assert converted["tool"] == "rectangle"
    # Out of range and unparseable values are clamped/zeroed rather than raising: one
    # misplaced shape is a better outcome than losing the whole annotation.
    assert _pct_to_fraction({"tool": "rectangle", "x": 250})["x"] == 1.0
    assert _pct_to_fraction({"tool": "rectangle", "x": "10%"})["x"] == 0.0


def test_auto_annotation_lands_where_the_model_pointed(db_session, monkeypatch):
    """End-to-end: percent from Claude, fractions to the renderer, marks in the middle.

    The old path fed 10-50 *percent* to a pixel renderer, so every auto-annotation
    ended up crushed into the top-left corner of the screenshot regardless of what
    the model actually identified.
    """
    from app.services import claude_cli, evidence_analysis
    from app.services.workspace_scope import scoped_evidence_dir

    run, result, ev = _seed_failed_screenshot(db_session)
    monkeypatch.setattr(
        claude_cli,
        "run_prompt",
        lambda *a, **k: '{"diagnosis":"Filter chip is missing",'
        '"shapes":[{"tool":"highlight","x":40,"y":40,"w":20,"h":20,"color":"#f43f5e"}]}',
    )

    evidence_analysis.auto_annotate_result(db_session, run, result)
    db_session.refresh(ev)

    annotated = scoped_evidence_dir(run.owner_id) / ev.meta["annotatedPath"]
    with Image.open(annotated) as img:
        width, height = img.size
        middle = _painted_in(
            img,
            (int(width * 0.45), int(height * 0.45), int(width * 0.55), int(height * 0.55)),
            _SEEDED,
        )
        corner = _painted_in(
            img, (0, 0, max(2, int(width * 0.1)), max(2, int(height * 0.1))), _SEEDED
        )

    assert middle > 0, "the model pointed at the middle and nothing was drawn there"
    assert corner == 0, "percent was fed to the renderer as pixels again"


def test_the_caption_fallback_spans_the_full_width(db_session, monkeypatch):
    """The one path that USED to be right, for the wrong reason.

    It emitted pixels (`w=image_width`) into a pixel renderer, so it looked correct
    and hid the disagreement everywhere else. Now it emits `w=1.0`, and it has to keep
    reaching the right-hand edge.
    """
    from app.services import claude_cli, evidence_analysis
    from app.services.workspace_scope import scoped_evidence_dir

    run, result, ev = _seed_failed_screenshot(db_session)
    monkeypatch.setattr(claude_cli, "run_prompt", lambda *a, **k: "sorry, I cannot help")

    evidence_analysis.auto_annotate_result(db_session, run, result)
    db_session.refresh(ev)

    annotated = scoped_evidence_dir(run.owner_id) / ev.meta["annotatedPath"]
    with Image.open(annotated) as img:
        width, _height = img.size
        assert _painted(img, 1, 1, _SEEDED), "caption bar missing at the left edge"
        assert _painted(img, width - 2, 1, _SEEDED), "caption bar does not reach the right edge"
