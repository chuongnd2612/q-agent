"""The QC comment template and its provider rendering (#703).

Two things are being defended, and they fail in different ways:

* **The template states only facts.** ENV / Status / OS / Browser are claims a reviewer
  acts on. "Operating system: Windows" on a run that happened in a Linux container
  sends someone chasing a platform difference that never existed, so an unknown field
  has to be *absent*, not guessed.
* **What reaches the provider is the provider's format.** The draft is Markdown — that
  is what the Publish screen renders and what a reviewer edits — and an Azure DevOps
  comment renders HTML. Posting the draft verbatim is why `**PASSED**` arrived as
  literal asterisks, and it went unnoticed because the draft looks right in Q-Agent
  and wrong only in the ticket.
"""

from __future__ import annotations

from app.services import comment_markup, comment_template


def _results():
    return [
        {
            "caseCode": "TC-01",
            "title": "Employees tab default filter",
            "status": "pass",
            "observation": "Loaded with Terminated excluded.",
            "screenshot": "TC-01-shot.png",
        },
        {
            "caseCode": "TC-02",
            "title": "Employer search",
            "status": "fail",
            "observation": "Search for '27G' returned employers that should not match.",
            "screenshot": "TC-02-shot.png",
        },
    ]


# --------------------------------------------------------------- the template


def test_the_header_block_is_scannable_and_in_qc_order():
    """Four lines, in the order QC writes them — that is how a reviewer decides in two
    seconds whether the report is even about their environment."""
    lines = comment_template.header_lines(
        env="QA", status="PASSED", operating_system="Windows", browser="Chrome"
    )

    assert lines == [
        "**ENV:** QA",
        "**Status:** PASSED",
        "**Operating system:** Windows",
        "**Browser:** Chrome",
    ]


def test_an_unknown_field_is_omitted_rather_than_guessed():
    """The whole reason the fields are assembled and not asked for.

    An agent-executed run happened on someone's laptop and the agent does not report
    its platform, so the OS line has to disappear rather than claim this container's.
    """
    lines = comment_template.header_lines(
        env="QA", status="PASSED", operating_system="", browser="Chrome"
    )

    assert not any("Operating system" in line for line in lines)
    assert "**ENV:** QA" in lines, "an unknown OS must not take the rest of the block with it"


def test_the_browser_is_named_the_way_a_person_names_it():
    """`Execution.browser` holds Playwright's engine id; a QC comment names a browser."""
    assert comment_template.browser_label("chromium") == "Chrome"
    assert comment_template.browser_label("webkit") == "Safari"
    assert comment_template.browser_label("") == ""


def test_every_case_gets_a_numbered_entry_with_its_screenshot_under_it():
    """Inline is the point of the sample: the reader sees the evidence without
    leaving the comment, and does not have to match filenames to findings by hand."""
    body = comment_template.build_body(
        assignee="gabe.gray",
        env="QA",
        status="FAILED",
        operating_system="Windows",
        browser="Chrome",
        results=_results(),
    )

    assert body.startswith("Hi @gabe.gray")
    assert "**Actual result:**" in body
    assert "1. **TC-01**" in body and "2. **TC-02**" in body
    # The screenshot follows its OWN item, not a list at the bottom.
    first, second = body.index("1. **TC-01**"), body.index("2. **TC-02**")
    assert first < body.index("![TC-01](TC-01-shot.png)") < second


def test_a_run_with_no_executed_cases_says_so():
    """An empty "Actual result" heading reads as a broken comment; the sentence reads
    as a fact about the run."""
    body = comment_template.build_body(
        assignee="", env="QA", status="FAILED", operating_system="", browser="", results=[]
    )

    assert "No test cases were executed" in body


def test_a_ticket_with_no_assignee_still_asks_for_the_status_change():
    """Addressing nobody as "@" is worse than not naming anyone."""
    body = comment_template.build_body(
        assignee="", env="QA", status="PASSED", operating_system="", browser="", results=[]
    )

    assert "@" not in body.splitlines()[0]
    assert "change the status" in body


# ------------------------------------------------------- rendering to a provider


def test_markdown_becomes_html_so_a_work_item_does_not_show_asterisks():
    """The bug in one line."""
    assert "<b>PASSED</b>" in comment_markup.to_html("**PASSED**")
    assert "**" not in comment_markup.to_html("**PASSED**")


def test_a_numbered_item_becomes_an_ordered_list_item():
    html = comment_markup.to_html("1. **TC-01** did a thing\n2. **TC-02** did another")

    assert html.count("<li>") == 2
    assert "<ol>" in html and "</ol>" in html


def test_an_image_is_rendered_with_the_url_the_file_landed_at():
    """Only the adapter knows where the upload went, so the template writes the
    filename and the mapping happens here."""
    html = comment_markup.to_html(
        "![TC-01](TC-01-shot.png)",
        image_src={"TC-01-shot.png": "https://dev.azure.com/org/_apis/wit/attachments/abc"},
    )

    assert '<img src="https://dev.azure.com/org/_apis/wit/attachments/abc"' in html


def test_an_image_with_no_uploaded_file_is_dropped_not_left_broken():
    """A broken-image icon in a ticket reads as "the evidence is gone", which is a
    worse lie than the line simply not being there."""
    html = comment_markup.to_html("![TC-01](TC-01-shot.png)", image_src={})

    assert "<img" not in html
    assert "TC-01-shot.png" not in html


def test_html_a_reviewer_typed_is_escaped_not_executed():
    """The edit box is free text, and it ends up in someone else's work item."""
    html = comment_markup.to_html("<script>alert(1)</script>")

    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_a_stray_underscore_is_not_turned_into_emphasis():
    """Why this is a small converter and not a Markdown library: test names are full of
    underscores, and a general renderer would eat them."""
    html = comment_markup.to_html("case_code_here passed")

    assert "case_code_here" in html
    assert "<em>" not in html


# ----------------------------------------- the structure, not just the markup
#
# The first version of the converter passed every assertion above and still rendered
# wrongly: each numbered item became its own single-item <ul> with the literal "1."
# printed next to a bullet, and the observation and screenshot broke OUT of the item
# into sibling blocks — pulling the evidence away from the finding it belongs to, which
# is the exact arrangement the template exists to avoid. Only looking at the rendered
# page showed it. These pin what that render has to be.


def test_the_numbered_list_is_one_ordered_list_not_one_per_item():
    html = comment_markup.to_html("1. first\n2. second\n3. third")

    assert html.count("<ol>") == 1, html
    assert html.count("<li>") == 3


def test_the_list_marker_is_not_printed_twice():
    """`<ol>` supplies the number; keeping the literal "1." prints it alongside."""
    html = comment_markup.to_html("1. **TC-01** did a thing")

    assert "1." not in html
    assert "<ol><li><b>TC-01</b> did a thing</li></ol>" == html


def test_an_observation_and_its_screenshot_stay_inside_their_item():
    """Indentation is structure: the evidence has to sit with its finding."""
    html = comment_markup.to_html(
        "1. **TC-01** did a thing\n   what it observed\n   ![TC-01](shot.png)\n2. **TC-02** next",
        image_src={"shot.png": "https://example.test/a"},
    )

    first_item = html[html.index("<li>") : html.index("</li>")]
    assert "what it observed" in first_item
    assert "<img" in first_item, "the screenshot broke out of its numbered item"


def test_nested_bullets_stay_under_the_case_they_describe():
    """The evidence manifest is two levels: a case, then its artifacts."""
    html = comment_markup.to_html("- TC-01 - PASS\n  - Screenshot: a.png\n- TC-02 - FAIL")

    first_item = html[html.index("<li>") : html.index("</li>")]
    assert "Screenshot: a.png" in first_item
    assert html.count("<li>") == 2, "a nested artifact became a case of its own"


def test_the_published_image_carries_a_real_src():
    """The provider's reader has no Q-Agent session, so the published form must be a
    URL their browser can load unaided — the opposite of the preview's deferred one."""
    html = comment_markup.to_html(
        "![TC-01](shot.png)", image_src={"shot.png": "https://dev.azure.com/org/attach/1"}
    )

    assert '<img src="https://dev.azure.com/org/attach/1"' in html
    assert "data-artifact" not in html


def test_the_preview_image_defers_its_source_to_the_client():
    """`/artifacts/**` needs a token that never leaves the browser's memory, and the
    URL needs the SPA's mount prefix. The server knows neither."""
    html = comment_markup.to_html(
        "![TC-01](shot.png)",
        image_src={"shot.png": "users/3/evidence/RUN-1/shot.png"},
        deferred=True,
    )

    assert 'data-artifact="users/3/evidence/RUN-1/shot.png"' in html
    assert "src=" not in html, "a bare path as src is what rendered as a broken image"
