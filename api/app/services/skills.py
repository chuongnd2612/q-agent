"""Dedicated Q-Agent skill loader.

Each Claude CLI action is guided by a dedicated skill under ``settings.skills_dir``
(``skills/<name>/SKILL.md`` + optional ``templates/``). We inject the skill's
SKILL.md as the Claude system prompt so every AI action follows that skill's
methodology and quality rules, while the caller's prompt still pins the exact
(JSON) output shape the backend parses.

Skill names map 1:1 to pipeline actions — see :data:`SKILLS`.
"""

from __future__ import annotations

from functools import lru_cache

from app.config import settings
from app.logging import logger

# Canonical skill names (folders under skills/). Referenced by services so a
# typo fails loudly in one place rather than silently skipping a skill.
REQUIREMENT_ANALYST = "requirement-analyst"
TEST_CASE_GENERATOR = "test-case-generator"
TEST_CASE_REVIEWER = "test-case-reviewer"
AUTOMATION_PLANNER = "automation-planner"
PAGE_OBJECT_AUTHOR = "page-object-author"
AUTOMATION_GENERATOR = "automation-generator"
AUTOMATION_REVIEWER = "automation-reviewer"
EXECUTION_ANALYZER = "execution-analyzer"
REPORT_GENERATOR = "report-generator"
TICKET_COMMENT_GENERATOR = "ticket-comment-generator"
SCREENSHOT_ANNOTATOR = "screenshot-annotator"
PROJECT_BOOTSTRAP = "project-bootstrap"

SKILLS = {
    REQUIREMENT_ANALYST,
    TEST_CASE_GENERATOR,
    TEST_CASE_REVIEWER,
    AUTOMATION_PLANNER,
    PAGE_OBJECT_AUTHOR,
    AUTOMATION_GENERATOR,
    AUTOMATION_REVIEWER,
    EXECUTION_ANALYZER,
    REPORT_GENERATOR,
    TICKET_COMMENT_GENERATOR,
    SCREENSHOT_ANNOTATOR,
    PROJECT_BOOTSTRAP,
}


@lru_cache(maxsize=32)
def load_skill(name: str, include_template: bool = False) -> str | None:
    """Return a skill's SKILL.md (optionally + its template) as a system prompt.

    Args:
        name: skill folder name under ``settings.skills_dir``.
        include_template: also append the skill's ``templates/*`` files. Leave
            False for JSON-output actions (the markdown template would fight the
            required JSON shape); enable for prose actions (e.g. ticket comments).

    Returns:
        The composed skill text, or None if the skill is not present on disk.
    """
    skill_dir = settings.skills_dir / name
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.exists():
        logger.warning("Skill '{}' not found at {} — proceeding without it", name, skill_md)
        return None

    parts = [
        "You are operating under the dedicated Q-Agent skill below. Follow its "
        "workflow, coverage rules and quality rules precisely.\n",
        skill_md.read_text(encoding="utf-8"),
    ]
    if include_template:
        template_dir = skill_dir / "templates"
        if template_dir.is_dir():
            for tmpl in sorted(template_dir.glob("*")):
                if tmpl.is_file():
                    parts.append(f"\n--- Reference template: {tmpl.name} ---\n")
                    parts.append(tmpl.read_text(encoding="utf-8"))
    return "\n".join(parts)
