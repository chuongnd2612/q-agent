"""The Claude model catalog (#715).

There were three hardcoded copies — labels + context windows in `ai_usage_service`,
prices in `claude_usage_reader`, and a dropdown list in the SPA — and every way they
drifted was silent:

* two **1M**-context models were labelled 200K, so the AI chip said
  "Claude Opus 4.8 · 200K ctx" about a model with five times that;
* **Sonnet 5 was priced at $3/$15 instead of $2/$10**, overstating every Sonnet cost the
  product displayed by half;
* the Haiku id carried an invented date suffix, and an id that matches nothing the CLI
  reports matches nothing;
* **Opus 5 was missing entirely** — and a model absent from the price table contributes
  **zero**, so the current flagship was reported as free.

That last one is why these tests exist at all: the failure mode of a gap is not an
error, it is a wrong number nobody questions.
"""

from __future__ import annotations

import pytest

from app.services import ai_usage_service, claude_usage_reader, model_catalog


def test_every_offered_model_has_a_price():
    """A model absent from the price table costs nothing, silently.

    That is the whole bug: adding a model to the dropdown and forgetting the price
    reports its work as free, and free looks like good news.
    """
    priced = claude_usage_reader._PRICES

    for model in model_catalog.MODELS:
        assert model.id in priced, f"{model.id} is offered but has no price"
        assert priced[model.id]["input"] > 0
        assert priced[model.id]["output"] > 0


def test_every_offered_model_has_a_label_and_context_window():
    labels = ai_usage_service.MODEL_LABELS

    for model in model_catalog.MODELS:
        assert model.id in labels, f"{model.id} is offered but the UI cannot name it"
        assert labels[model.id][1] in {"200K", "1M"}


def test_the_current_flagship_is_offered():
    """Opus 5 was absent from all three tables while being the model to use."""
    assert "claude-opus-5" in {m.id for m in model_catalog.MODELS}


@pytest.mark.parametrize(
    ("model_id", "context", "input_price", "output_price"),
    [
        ("claude-opus-5", "1M", 5.0, 25.0),
        ("claude-opus-4-8", "1M", 5.0, 25.0),
        ("claude-sonnet-5", "1M", 2.0, 10.0),
        ("claude-haiku-4-5", "200K", 1.0, 5.0),
    ],
)
def test_the_published_numbers(model_id, context, input_price, output_price):
    """Pinned against Anthropic's published rates, so a wrong one is a failing test
    rather than a plausible-looking invoice."""
    model = next(m for m in model_catalog.MODELS if m.id == model_id)

    assert (model.context, model.input_price, model.output_price) == (
        context,
        input_price,
        output_price,
    )


def test_no_model_id_carries_a_date_suffix():
    """Current ids are complete as-is. `claude-haiku-4-5-20251001` was invented, and an
    id the CLI never reports is an id that matches no usage row."""
    for model in model_catalog.MODELS:
        assert not model.id[-8:].isdigit(), f"{model.id} looks date-suffixed"


def test_a_retired_id_keeps_its_price_and_label():
    """Renaming must not reprice history.

    Usage rows written before the rename still carry the old id; dropping it from the
    table would restate every past Haiku call as free — a rename quietly rewriting what
    the product already told someone they spent.
    """
    legacy = "claude-haiku-4-5-20251001"

    assert claude_usage_reader._PRICES[legacy]["input"] == 1.0
    assert ai_usage_service.MODEL_LABELS[legacy] == ("Claude Haiku 4.5", "200K")
    assert model_catalog.canonical(legacy) == "claude-haiku-4-5"


def test_cache_prices_derive_from_the_model_s_own_input_price():
    """They used to be typed out per row, keyed off whatever input price that row had —
    so a wrong input price was wrong three more times."""
    prices = model_catalog.prices()

    for model in model_catalog.MODELS:
        row = prices[model.id]
        assert row["cacheRead"] == pytest.approx(model.input_price * 0.1)
        assert row["cacheWrite"] == pytest.approx(model.input_price * 1.25)


def test_the_dropdown_offers_exactly_what_the_server_prices():
    """The SPA's own copy is what made a third disagreement possible."""
    offered = {option["value"] for option in model_catalog.options()}

    assert offered == {m.id for m in model_catalog.MODELS}
    assert all(option["label"] for option in model_catalog.options())
