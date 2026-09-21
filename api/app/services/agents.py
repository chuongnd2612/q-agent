"""Claude Code agent definitions (#888, part of #887).

The sibling of :mod:`app.services.skills`, for the three browser-driving roles.
Where a *skill* is prose composed into ``--append-system-prompt`` for a one-shot
call, an *agent* is a real Claude Code agent selected with ``--agent`` — it owns
its own tool policy and multi-turn loop, which is what the browser roles need.

Definitions live in ``settings.agents_dir`` as ``<name>.md`` with YAML
frontmatter (``name`` / ``description``), mirroring the layout the Playwright
"Test Agents" scaffold emits so upstream revisions can be diffed straight in.

They are passed to the CLI **inline** via ``--agents '{"<name>": {...}}'`` rather
than discovered from disk, because neither discovery location works here:
``.claude/`` is gitignored in this repo, and an agentic run ``cwd``s into a
throwaway authoring workspace. Inline keeps the definitions tracked in git and
independent of where the CLI happens to run.
"""

from __future__ import annotations

import json
from functools import lru_cache

from app.config import settings
from app.logging import logger

# Canonical agent names (files under agents/). Referenced by services so a typo
# fails loudly in one place rather than silently running without the agent.
PLAYWRIGHT_TEST_PLANNER = "playwright-test-planner"
PLAYWRIGHT_TEST_GENERATOR = "playwright-test-generator"
PLAYWRIGHT_TEST_HEALER = "playwright-test-healer"

AGENTS = {
    PLAYWRIGHT_TEST_PLANNER,
    PLAYWRIGHT_TEST_GENERATOR,
    PLAYWRIGHT_TEST_HEALER,
}


def _split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Split a ``---`` YAML frontmatter header off an agent definition.

    Only the flat ``key: value`` pairs these files use are parsed — pulling in a
    YAML dependency to read two fields would not earn its keep. A file without
    frontmatter is returned whole as the body.

    Args:
        text: The raw ``<name>.md`` contents.

    Returns:
        ``(frontmatter, body)``.
    """
    if not text.startswith("---"):
        return {}, text.strip()
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text.strip()
    meta: dict[str, str] = {}
    for line in parts[1].splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip():
            meta[key.strip()] = value.strip().strip("'\"")
    return meta, parts[2].strip()


@lru_cache(maxsize=8)
def load_agent(name: str) -> dict[str, str] | None:
    """Return an agent definition as ``{"description", "prompt"}``.

    Args:
        name: agent file stem under ``settings.agents_dir``.

    Returns:
        The definition, or None when the file is absent — callers fall back to
        their skill path rather than failing the run.
    """
    path = settings.agents_dir / f"{name}.md"
    if not path.exists():
        logger.warning("Agent '{}' not found at {} — proceeding without it", name, path)
        return None
    meta, body = _split_frontmatter(path.read_text(encoding="utf-8"))
    if not body:
        logger.warning("Agent '{}' at {} has no body — ignoring", name, path)
        return None
    return {"description": meta.get("description", name), "prompt": body}


def agents_json(name: str) -> str | None:
    """Render one agent as the JSON ``--agents`` expects, or None if absent.

    Shape: ``{"<name>": {"description": ..., "prompt": ...}}`` — the CLI selects
    which of them to run with ``--agent <name>``.
    """
    definition = load_agent(name)
    if definition is None:
        return None
    return json.dumps({name: definition})
