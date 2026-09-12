"""Fixture corpus for the QC-voice gate (:mod:`app.services.qc_voice_gate`).

The point of the gate is that it does not drift, and the only thing that proves
that is a corpus. Two halves:

- **KNOWN_BAD** — strings that have actually leaked (or would): each is asserted
  rejected *by the rule it is filed under*, not merely "rejected". A test that
  only checks ``outcome == "reject"`` cannot tell a working rule from a
  different rule that happened to fire on the same sentence, and that is exactly
  how a rule rots unnoticed.
- **KNOWN_GOOD** — real QC English, much of it lifted verbatim from the repo's
  own demo case bank (:mod:`app.services.sample_data`). A false positive here is
  worse than a missing rule: the first correct case the gate rejects is the day
  somebody switches it off.

And the negative control: :func:`test_disabling_a_rule_lets_its_bad_fixture_pass`
disables one rule at a time and asserts that rule's fixtures then pass. Without
it, a fixture "passing the gate" could mean the rule works or could mean the
fixture never exercised it.
"""

from __future__ import annotations

import pytest

from app.services import qc_voice_gate as gate

# ------------------------------------------------------------------ known bad
#
# One entry per rule. Drawn from the shapes that really appear in this repo's
# generated/indexed material — ``#login-submit`` and ``[data-testid=member-search]``
# are literally the selectors in ``tests/test_knowledge.py`` /
# ``tests/test_hub_knowledge.py``; ``/admin/groups/:id`` is the route shape the
# Knowledge Base block carries into the prompt.

KNOWN_BAD: dict[str, list[str]] = {
    "uuid": [
        "Open the agency with id 3f2504e0-4f89-11d3-9a0c-0305e82c3301.",
        "The response shows 550e8400-e29b-41d4-a716-446655440000.",
        "Precondition: a member exists with guid 123E4567-E89B-12D3-A456-426614174000.",
    ],
    "internal_id": [
        "Open broker agency id: 8842.",
        "Check the record ID = 90210 is updated.",
        "The row for #12345 is highlighted.",
        "Look up 9f86d081884c7d65 in the store.",
    ],
    "css_xpath_selector": [
        'Assert [data-testid="save-btn"] is enabled.',
        "Click #login-submit.",
        "Verify button.primary is visible.",
        "Select //button[@id='save'] and click it.",
        "The element [data-testid=member-search] is focused.",
        "Use css=.agency-row to find the row.",
    ],
    "api_path": [
        "Navigate to /admin/users and check the list.",
        "Send GET /api/v1/agencies and confirm the list returns.",
        "Open /admin/groups/:id for the selected group.",
        "Browse to https://app.example.com/brokers and sign in.",
        "POST /brokers/agencies with the new agency details.",
    ],
    "http_status": [
        "The API returns status code 403 for a non-admin.",
        "Expect HTTP 404 when the agency is missing.",
        "The server replies 500 Internal Server Error.",
        "Response code: 201 confirms creation.",
    ],
    "code_identifier": [
        "Set userId to the admin account.",
        "Confirm the broker_agency_id value is stored.",
        "The BrokerAgencyList component renders the rows.",
        "Check that `expirationDate` is not empty.",
        "Call submitForm() and wait.",
        "Assert isActive stays true after saving.",
    ],
    "db_artifact": [
        "Run SELECT name FROM broker_agency to confirm.",
        "INSERT INTO agencies the new record.",
        "Verify the database row is created.",
        "Check the primary key is unchanged.",
        "Execute the stored procedure to seed data.",
    ],
    "tech_noun": [
        "Confirm the endpoint is reachable.",
        "Inspect the JSON payload returned.",
        "The selector resolves to one element.",
        "Check the network tab for the request.",
        "The DOM updates after saving.",
        "Send the request body with the agency name.",
    ],
    "template_var": [
        "Log in as {{admin_user}}.",
        "Navigate to ${BASE_URL} and sign in.",
        "Enter <USER NAME> in the field.",
        "Use %USERNAME% for the login.",
    ],
}

#: Flattened ``(rule, text)`` pairs, so every fixture is its own test id.
BAD_CASES: list[tuple[str, str]] = [
    (rule, text) for rule, texts in KNOWN_BAD.items() for text in texts
]

# ----------------------------------------------------------------- known good
#
# The first block is lifted verbatim from ``sample_data.CASE_BANK`` — the demo
# cases the product itself ships and shows to users. If the gate rejects one of
# those, the gate is wrong. The rest are the ordinary-English shapes most likely
# to trip an over-eager rule: "and/or" style slashes, bare numbers next to
# status-ish words, "the table shows…", "Select … from …", "8 a.m.".

KNOWN_GOOD: list[str] = [
    # --- verbatim from the shipped demo case bank
    "Broker Management loads with two tabs, Broker Agencies active by default",
    "Navigate to the Brokers section.",
    "Broker Management screen is displayed.",
    "Agency row shows name, number, type, next expiration, status + actions",
    "Inspect a row.",
    "Shows name, number, type, expiration, status, ellipsis.",
    "Open the ellipsis menu on an Active row.",
    "Search filters by agency name or number",
    "Empty-state message shown.",
    "Clicking Deactivate opens a confirmation dialog",
    "Agency Inactive; toast appears.",
    "Cancel closes the dialog with no change",
    "Reminder email sent 30 days before expiration",
    "Run the daily reminder job.",
    "Account timezone US/Pacific.",
    "Sends at 8am local.",
    "Signed in as a Surency Internal Admin.",
    # --- the target voice, including the issue's own before/after "after"
    "Open the User Management screen and check that the Save button can be clicked",
    "Select a status from the dropdown and confirm only matching agencies remain.",
    "Delete from the list and confirm the row disappears.",
    "The table shows three rows, one per agency.",
    "Update the set of selected agencies and save.",
    # --- shapes that would trip a careless rule
    "Enter 200 in the Amount field.",
    "The list shows 404 items in total.",
    "Choose Save / Cancel in the confirmation banner.",
    "Enter N/A when the value is unknown.",
    "Check the Yes/No toggle is off.",
    "The renewal date shows 12/05/2026.",
    "Click Confirm (the primary button).",
    "Sign in at 8 a.m. and check the dashboard.",
    "Review the Terms and Conditions, then accept.",
    "The Save button is disabled until a name is entered.",
    "Password reset email arrives within 5 minutes.",
    "Upload a receipt and confirm it appears in the list.",
]


@pytest.mark.parametrize(("rule", "text"), BAD_CASES, ids=[f"{r}:{t[:40]}" for r, t in BAD_CASES])
def test_known_bad_text_is_rejected_by_its_named_rule(rule: str, text: str) -> None:
    """Every known-bad fixture is rejected, and by the rule it is filed under."""
    result = gate.check_case({"title": text})
    assert result["outcome"] == "reject", f"{text!r} was not rejected at all"
    hit_rules = {finding["rule"] for finding in result["findings"]}
    assert rule in hit_rules, f"{text!r} rejected by {sorted(hit_rules)}, expected {rule}"


@pytest.mark.parametrize("text", KNOWN_GOOD, ids=[t[:48] for t in KNOWN_GOOD])
def test_known_good_text_passes(text: str) -> None:
    """Real QC English passes untouched — a false positive kills the gate."""
    result = gate.check_case({"title": text})
    assert result["outcome"] == "pass", f"false positive on {text!r}: {result['findings']}"


@pytest.mark.parametrize("rule", sorted(KNOWN_BAD))
def test_disabling_a_rule_lets_its_bad_fixture_pass(rule: str) -> None:
    """The negative control: the fixtures really do exercise the rules they name.

    Disable one rule and its own fixtures must stop being rejected *by that rule*
    — and the ones that carry nothing else must pass outright. Without this, a
    corpus can be green while a rule's pattern never matched anything, because
    some other rule quietly covered every fixture.
    """
    survivors: list[str] = []
    for text in KNOWN_BAD[rule]:
        result = gate.check_case({"title": text}, disabled_rules=[rule])
        hit_rules = {finding["rule"] for finding in result["findings"]}
        assert rule not in hit_rules, f"{rule} still fired on {text!r} while disabled"
        if result["outcome"] == "pass":
            survivors.append(text)
    # At least one fixture per rule must be *clean* apart from that rule, so the
    # disable flips the whole verdict and not just one finding.
    assert survivors, f"no fixture for {rule} passes with only {rule} disabled"


def test_disabling_a_rule_leaves_the_other_rules_working() -> None:
    """Disabling one rule is surgical — the rest of the gate still rejects."""
    text = "Click #login-submit and confirm userId is set."
    assert gate.check_case({"title": text})["outcome"] == "reject"
    result = gate.check_case({"title": text}, disabled_rules=["css_xpath_selector"])
    rules = {finding["rule"] for finding in result["findings"]}
    assert result["outcome"] == "reject"
    assert rules == {"code_identifier"}


def test_a_clean_case_passes_across_every_scanned_field() -> None:
    """A whole well-written case passes — every field, not just the title."""
    case = {
        "title": "Broker Management loads with two tabs, Broker Agencies active by default",
        "objective": "Confirm an admin can reach the broker list and see both tabs.",
        "precondition": "Signed in as a Surency Internal Admin.",
        "testData": [{"field": "Agency name", "value": "Northwind Benefits"}],
        "steps": [
            {"a": "Navigate to the Brokers section.", "e": "Broker Management screen is displayed."},
            {"a": "Observe the tab bar default selection.", "e": "Broker Agencies is active."},
        ],
    }
    result = gate.check_case(case)
    assert result["outcome"] == "pass"
    assert result["findings"] == []


@pytest.mark.parametrize(
    ("case", "expected_field"),
    [
        ({"title": "Open /admin/users"}, "title"),
        ({"objective": "Prove the endpoint answers."}, "objective"),
        ({"precondition": "A row exists with id: 8842."}, "precondition"),
        ({"steps": [{"a": "Click #login-submit.", "e": "ok"}]}, "steps[0].a"),
        ({"steps": [{"a": "ok", "e": "ok"}, {"a": "ok", "e": "Returns HTTP 201."}]}, "steps[1].e"),
        ({"testData": [{"field": "userId", "value": "admin"}]}, "testData[0].field"),
    ],
)
def test_every_scanned_field_is_actually_scanned(case: dict, expected_field: str) -> None:
    """Each field the gate claims to read is read, and reported by its own path."""
    result = gate.check_case(case)
    assert result["outcome"] == "reject"
    assert [finding["field"] for finding in result["findings"]] == [expected_field]


def test_test_data_values_are_not_scanned() -> None:
    """``testData[].value`` is what a tester types — ids and emails belong there."""
    case = {"testData": [{"field": "Member email", "value": "qa_user+1@example.com"}]}
    assert gate.check_case(case)["outcome"] == "pass"


# ------------------------------------------------------------- the allow-list


def _glossary(*terms: str) -> list[dict]:
    return [{"category": "glossary", "term": term} for term in terms]


def test_glossary_terms_exempt_the_products_own_vocabulary() -> None:
    """The control the whole design turns on.

    A domain word that happens to look like an identifier passes *because it is
    in the glossary*, while a genuine leaked identifier in the same sentence is
    still rejected. Both halves matter: an allow-list that exempts everything is
    the same as no gate.
    """
    terms = gate.allowed_terms_from_facts(_glossary("eClaims", "FSA_Card"))
    assert terms == ["eClaims", "FSA_Card"]

    good = {"title": "Submit an eClaims request with the FSA_Card selected."}
    assert gate.check_case(good, allowed_terms=terms)["outcome"] == "pass"

    # Negative control: the same sentence WITHOUT the glossary is rejected, so
    # the pass above is the allow-list doing work, not the rules failing to fire.
    ungated = gate.check_case(good)
    assert ungated["outcome"] == "reject"
    assert {f["match"] for f in ungated["findings"]} == {"eClaims", "FSA_Card"}

    # And a leaked identifier that is NOT glossary vocabulary still fails.
    leaked = gate.check_case({"title": "Set userId on the eClaims request."}, allowed_terms=terms)
    assert leaked["outcome"] == "reject"
    assert [f["match"] for f in leaked["findings"]] == ["userId"]


def test_allowed_term_does_not_shadow_an_adjacent_violation() -> None:
    """Masking is per-term, so a glossary word next to a leak hides only itself."""
    terms = gate.allowed_terms_from_facts(_glossary("eClaims"))
    result = gate.check_case({"title": "The eClaims userId field is shown."}, allowed_terms=terms)
    assert [f["match"] for f in result["findings"]] == ["userId"]


def test_allowed_terms_match_case_insensitively_but_not_as_substrings() -> None:
    """``eclaims`` is the same word; ``eClaimsRouter`` is not."""
    terms = ["eClaims"]
    assert gate.check_case({"title": "Open the eclaims inbox."}, allowed_terms=terms)["outcome"] == "pass"
    result = gate.check_case({"title": "Open eClaimsRouter."}, allowed_terms=terms)
    assert result["outcome"] == "reject"
    assert result["findings"][0]["match"] == "eClaimsRouter"


def test_allowed_terms_from_facts_takes_only_live_glossary_rows() -> None:
    """Rules/flows are subjects, not vocabulary; excluded facts are out of context."""
    facts = [
        {"category": "glossary", "term": "eClaims"},
        {"category": "glossary", "term": "eClaims"},  # duplicate
        {"category": "rule", "term": "ClaimApproval"},
        {"category": "flow", "term": "OnboardingFlow"},
        {"category": "glossary", "term": "COBRA", "excluded": True},
        {"category": "glossary", "term": "   "},
    ]
    assert gate.allowed_terms_from_facts(facts) == ["eClaims"]


def test_allowed_terms_from_facts_accepts_orm_style_rows() -> None:
    """Attribute access works too, so the pipeline can hand over model rows."""

    class _Row:
        def __init__(self, category: str, term: str, excluded: bool = False) -> None:
            self.category = category
            self.term = term
            self.excluded = excluded

    rows = [_Row("glossary", "FSA_Card"), _Row("glossary", "HRA", True), _Row("rule", "Eligibility")]
    assert gate.allowed_terms_from_facts(rows) == ["FSA_Card"]


def test_allowed_terms_from_facts_tolerates_nothing() -> None:
    assert gate.allowed_terms_from_facts([]) == []
    assert gate.allowed_terms_from_facts(None) == []  # type: ignore[arg-type]


# ------------------------------------------------------------------- plumbing


def test_the_issues_before_and_after_pair_behave_as_documented() -> None:
    """The exact pair written into the skills, pinned here so it cannot rot."""
    before = 'Navigate to /admin/users and assert [data-testid="save-btn"] is enabled'
    after = "Open the User Management screen and check that the Save button can be clicked"

    rejected = gate.check_case({"steps": [{"a": before, "e": "The form saves."}]})
    assert rejected["outcome"] == "reject"
    assert {f["rule"] for f in rejected["findings"]} >= {"api_path", "css_xpath_selector"}

    assert gate.check_case({"steps": [{"a": after, "e": "The form saves."}]})["outcome"] == "pass"


def test_findings_carry_field_rule_and_match() -> None:
    """The finding shape #829 will render — asserted per key, never as a whole dict."""
    result = gate.check_case({"steps": [{"a": "Click #login-submit.", "e": "ok"}]})
    finding = result["findings"][0]
    assert finding["field"] == "steps[0].a"
    assert finding["rule"] == "css_xpath_selector"
    assert finding["match"] == "#login-submit"


def test_rule_names_cover_the_documented_rule_set() -> None:
    """The nine named rules the issue specifies, and no silent extras."""
    assert set(gate.RULE_NAMES) == set(KNOWN_BAD)
    assert len(gate.RULE_NAMES) == len(set(gate.RULE_NAMES))


def test_empty_and_malformed_cases_are_tolerated_not_raised_on() -> None:
    """The gate judges text; schema validation is somebody else's job."""
    for case in (
        {},
        {"title": None, "steps": None, "testData": None},
        {"steps": ["not a dict", {"a": 7, "e": None}]},
        {"testData": [None, {"value": "only a value"}]},
        {"title": "   "},
    ):
        assert gate.check_case(case) == {"outcome": "pass", "findings": []}
    assert gate.check_case(None)["outcome"] == "pass"  # type: ignore[arg-type]


def test_check_text_reports_the_field_path_it_is_given() -> None:
    findings = gate.check_text("Open /admin/users", field="steps[3].a")
    assert [f["field"] for f in findings] == ["steps[3].a"]


def test_a_span_is_claimed_by_exactly_one_rule() -> None:
    """Priority order means no double-reporting of the same substring."""
    findings = gate.check_text('Assert [data-testid="save-btn"] is enabled')
    matches = [f["match"] for f in findings]
    assert matches == ['[data-testid="save-btn"]']
    assert findings[0]["rule"] == "css_xpath_selector"
