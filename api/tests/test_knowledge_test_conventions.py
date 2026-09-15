"""The existing suite's test conventions, carried through the KB (#872).

#868 and #870 show the model the team's real files, but both read a local
checkout. This is the channel that survives without one — a remote-only repo, a
KB built elsewhere, an agent-dispatched run — so the normalization has to be
hostile-input-proof and the rendering has to be unmistakably about the TEAM's
suite rather than the automation project.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.services import knowledge_service, prompts
from app.services.knowledge_service import _normalise_test_conventions as normalise

CONVENTIONS = {
    "spec_roots": ["apps/web/e2e"],
    "spec_naming": "kebab-case files, test titles start with the ticket id",
    "structure": "one describe per screen, one test per acceptance criterion",
    "assertion_style": "expect(locator).toBeVisible(), never assert on raw text",
    "data": "fixtures/users.json, one account per role",
}


# --------------------------------------------------------------- normalization


def test_a_well_formed_object_survives_intact():
    assert normalise(dict(CONVENTIONS)) == CONVENTIONS


def test_unknown_keys_are_dropped_and_blank_fields_omitted():
    """Absent and blank must be the same thing, so rendering can test truthiness."""
    out = normalise(
        {"spec_naming": "  kebab-case  ", "assertion_style": "   ", "coverage": "97%"}
    )
    assert out == {"spec_naming": "kebab-case"}


def test_a_list_valued_field_is_joined_rather_than_dropped():
    """The model answers a prose field with bullets often enough to handle it."""
    out = normalise({"structure": ["one describe per screen", "", "one test per AC"]})
    assert out == {"structure": "one describe per screen; one test per AC"}


def test_values_are_bounded_so_a_chatty_model_cannot_bloat_every_prompt():
    """These land in generation, authoring AND planning prompts — they must be capped."""
    out = normalise({"structure": "x" * 5000, "spec_roots": [f"e2e/{i}" for i in range(50)]})
    assert len(out["structure"]) == knowledge_service._CONVENTION_CHARS
    assert len(out["spec_roots"]) == knowledge_service._SPEC_ROOTS_MAX


def test_a_non_object_answer_degrades_to_nothing():
    """Prose, null, or a list instead of an object: no crash, no half-filled block."""
    for raw in ("the team uses Playwright", None, [], 7):
        assert normalise(raw) == {}


def test_the_build_payload_carries_and_normalizes_the_field(monkeypatch):
    monkeypatch.setattr(
        knowledge_service,
        "run_json",
        lambda *a, **k: {"stack": ["React"], "test_conventions": {**CONVENTIONS, "junk": "x"}},
    )
    payload = knowledge_service.build_knowledge_payload("P", "Azure DevOps", "org/web", "Playwright")
    assert payload["knowledge"]["test_conventions"] == CONVENTIONS


def test_a_build_that_omits_the_field_yields_an_empty_object(monkeypatch):
    """A KB built before #872 (or by a project with no tests) renders nothing."""
    monkeypatch.setattr(knowledge_service, "run_json", lambda *a, **k: {"stack": ["React"]})
    payload = knowledge_service.build_knowledge_payload("P", "Azure DevOps", "org/web", "Playwright")
    assert payload["knowledge"]["test_conventions"] == {}


# --------------------------------------------------------------- rendering


def test_the_rendered_line_names_the_teams_own_suite():
    block = prompts.render_project_context({"projectKey": "P", "testConventions": CONVENTIONS})
    line = next(l for l in block.splitlines() if "already writes" in l)
    assert "apps/web/e2e" in line
    assert "kebab-case files" in line
    assert "one describe per screen" in line
    assert "fixtures/users.json" in line
    # The distinction that keeps this from being mistaken for the automation project's
    # own conventions — which are what the AUTOMATION PLAN block describes.
    assert "not this automation project" in line


def test_a_partial_object_renders_only_what_it_has():
    block = prompts.render_project_context(
        {"projectKey": "P", "testConventions": {"assertion_style": "expect(locator)"}}
    )
    line = next(l for l in block.splitlines() if "already writes" in l)
    assert "assertions: expect(locator)" in line
    assert "naming" not in line and "specs live in" not in line


def test_nothing_is_rendered_without_conventions():
    for context in (
        {"projectKey": "P"},
        {"projectKey": "P", "testConventions": {}},
        {"projectKey": "P", "testConventions": None},
    ):
        assert "already writes" not in prompts.render_project_context(context)


# --------------------------------------------------------------- the artifact


def test_the_json_artifact_mirrors_the_field(db_session):
    """knowledge.json is what the skill's own contract says downstream reads."""
    from app.models.knowledge import ProjectKnowledge

    row = ProjectKnowledge(
        key="Surency Platform",
        project_key="Surency Platform",
        name="Surency Platform",
        repo="web",
        framework="Playwright",
        knowledge={"stack": ["React"], "test_conventions": CONVENTIONS},
        confidence=80,
        version="v1",
    )
    out_dir = Path(knowledge_service.write_knowledge_files(row))
    doc = json.loads((out_dir / "knowledge.json").read_text(encoding="utf-8"))
    assert doc["test_conventions"] == CONVENTIONS
