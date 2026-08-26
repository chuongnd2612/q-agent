"""The Claude models Q-Agent offers, and what they cost — one table (#715).

There used to be two hardcoded tables, in `ai_usage_service` (labels + context windows)
and `claude_usage_reader` (prices), plus a third in the SPA. They drifted, and every way
they drifted was silent:

* Two **1M**-context models were labelled **200K**, so the AI chip reported
  "Claude Opus 4.8 · 200K ctx" about a model with five times that.
* **Sonnet 5 was priced at $3/$15 instead of $2/$10** — every Sonnet cost the product
  displayed, in the run breakdown, the weekly budget and the per-model table, was
  overstated by half.
* The Haiku id carried an invented date suffix (`claude-haiku-4-5-20251001`). Current
  ids are complete as-is, and a suffixed id silently matches nothing the CLI reports.
* **Claude Opus 5 was absent entirely** — and `_PRICES`' own rule is that a model absent
  from the table contributes **zero** cost, so work on the current flagship was reported
  as free.

The last one is why this is one table rather than two kept in step by discipline: a
model missing from the pricing half does not raise, it just quietly costs nothing.

Prices are US dollars per million tokens, from Anthropic's published rates. Cache reads
are ~0.1x the input price and cache writes ~1.25x (the 5-minute TTL the CLI uses), so
they are derived rather than typed out — three of the four rows had them keyed off a
wrong input price before.
"""

from __future__ import annotations

from typing import NamedTuple

__all__ = ["ALIASES", "MODELS", "ClaudeModel", "canonical", "labels", "options", "prices"]


class ClaudeModel(NamedTuple):
    """One offered model. ``id`` is the exact string the CLI and API accept."""

    id: str
    label: str
    #: Context window, as a person reads it ("1M", "200K").
    context: str
    #: US dollars per million tokens.
    input_price: float
    output_price: float
    #: One line for the model dropdown, explaining when to pick it.
    hint: str


#: Offered models, best-first — the order the dropdown shows.
MODELS: tuple[ClaudeModel, ...] = (
    ClaudeModel("claude-opus-5", "Claude Opus 5", "1M", 5.0, 25.0, "highest quality"),
    ClaudeModel("claude-opus-4-8", "Claude Opus 4.8", "1M", 5.0, 25.0, "previous flagship"),
    ClaudeModel("claude-sonnet-5", "Claude Sonnet 5", "1M", 2.0, 10.0, "balanced"),
    ClaudeModel("claude-haiku-4-5", "Claude Haiku 4.5", "200K", 1.0, 5.0, "fastest"),
)

#: Ids that used to be written into usage rows and settings, mapped to the id they are
#: now. Retiring `claude-haiku-4-5-20251001` without this would silently reprice every
#: historical Haiku call to **zero** — the absent-means-free rule turning a rename into
#: a quiet restatement of past cost.
ALIASES: dict[str, str] = {"claude-haiku-4-5-20251001": "claude-haiku-4-5"}


def canonical(model_id: str) -> str:
    """The current id for ``model_id``, resolving retired spellings."""
    return ALIASES.get(model_id, model_id)


#: Cache pricing multipliers on the input rate (5-minute TTL, the CLI's default).
_CACHE_READ_RATIO = 0.1
_CACHE_WRITE_RATIO = 1.25


def labels() -> dict[str, tuple[str, str]]:
    """``{model id: (label, context window)}`` for presentation, aliases included."""
    table = {m.id: (m.label, m.context) for m in MODELS}
    for old, new in ALIASES.items():
        if new in table:
            table[old] = table[new]
    return table


def prices() -> dict[str, dict[str, float]]:
    """``{model id: {input, output, cacheRead, cacheWrite}}`` in $/MTok."""
    table = {
        m.id: {
            "input": m.input_price,
            "output": m.output_price,
            "cacheRead": round(m.input_price * _CACHE_READ_RATIO, 4),
            "cacheWrite": round(m.input_price * _CACHE_WRITE_RATIO, 4),
        }
        for m in MODELS
    }
    # Historical rows keep their price; see ALIASES.
    for old, new in ALIASES.items():
        if new in table:
            table[old] = table[new]
    return table


def options() -> list[dict[str, str]]:
    """The model dropdown's options, for the SPA."""
    return [{"value": m.id, "label": f"{m.label.removeprefix('Claude ')} — {m.hint}"} for m in MODELS]
