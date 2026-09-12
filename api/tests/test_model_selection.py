"""Per-action Claude model selection (app.services.claude_cli._resolve_model, #175)."""

from __future__ import annotations

from app.services import claude_cli, settings_store


def test_mechanical_skills_default_to_haiku(monkeypatch):
    monkeypatch.setattr(settings_store, "load_settings", lambda: {"claudeModel": "claude-sonnet-5"})
    # Small/mechanical actions default to the cheaper model...
    assert claude_cli._resolve_model("execution-analyzer") == "claude-haiku-4-5"
    assert claude_cli._resolve_model("screenshot-annotator") == "claude-haiku-4-5"
    # ...while heavy actions and the skill-less path inherit the global model.
    assert claude_cli._resolve_model("test-case-generator") == "claude-sonnet-5"
    assert claude_cli._resolve_model() == "claude-sonnet-5"


def test_settings_skill_override_beats_default_and_global(monkeypatch):
    monkeypatch.setattr(
        settings_store,
        "load_settings",
        lambda: {
            "claudeModel": "claude-sonnet-5",
            "skillModels": {
                "execution-analyzer": "claude-opus-4-8",  # override the Haiku default
                "test-case-generator": "claude-opus-4-8",  # override the global
            },
        },
    )
    assert claude_cli._resolve_model("execution-analyzer") == "claude-opus-4-8"
    assert claude_cli._resolve_model("test-case-generator") == "claude-opus-4-8"
    # A skill with no override/default still inherits the global model.
    assert claude_cli._resolve_model("automation-generator") == "claude-sonnet-5"
