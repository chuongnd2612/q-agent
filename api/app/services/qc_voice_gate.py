"""QC-voice gate for generated manual test cases.

A generated test case has to read as a QC hand-wrote it for another person to
execute: what a human *does* in the UI, and what that human *sees*. What keeps
leaking into it instead is the grounding material — selectors, routes, table
names, camelCase field names — because the model is handed a code-derived
Knowledge Base and cheerfully quotes it.

The skills (``skills/test-case-generator``, ``skills/test-case-reviewer``) now
ask for the right voice. That is necessary and not sufficient: a prompt
instruction drifts. It holds, then a model update or a longer context quietly
erodes it, and nobody notices until a customer reads a step that says
``assert [data-testid="save-btn"] is enabled``. This module is the part that
does not drift — a deterministic, DB-free scan over the case's own text, with a
fixture corpus behind it (``api/tests/test_qc_voice_gate.py``) so "is the voice
right?" is a number rather than a hope.

Shape is deliberately the same as :mod:`app.services.placeholder_gate`: pure
functions over plain dicts, named rules, no session, no I/O.

**The allow-list is the load-bearing design detail.** A product's own vocabulary
is frequently identifier-shaped — ``eClaims``, ``FSA_Card``, ``COBRA`` — and a
camelCase rule with no exemption will reject *correct* test cases, at which
point the gate gets switched off within a week and protects nothing. So the
caller seeds ``allowed_terms`` from the project's business glossary
(``BusinessFact.term`` where ``category="glossary"`` — see
:func:`allowed_terms_from_facts`), and those terms are masked out of the text
before any rule runs. The gate itself never touches the database, so it stays
usable from a test, a CLI, or a prompt-time check with no session at hand.

This module is a **library only**. Wiring it into the generation pipeline is a
separate change (#829), on purpose: a false-positive problem in the gate must
not be able to block the skills work.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Sequence

# --------------------------------------------------------------------- rules
#
# Each rule is (name, pattern). Order is PRIORITY order, not cosmetic: the scan
# records the character span of every match and a later rule may not claim a
# span that an earlier one already owns. That is what makes attribution
# deterministic — ``[data-testid="save-btn"]`` is reported as
# ``css_xpath_selector`` and never as ``code_identifier``, so a test can assert
# the *named* rule rather than merely "something rejected it".

#: Braced/angled substitution markers the model leaves behind when it has no
#: real value: ``{{email}}``, ``${BASE_URL}``, ``<USER NAME>``, ``%USERNAME%``.
#: The angle form is SHOUTING-only so it can never eat ordinary prose.
_TEMPLATE_VAR = [
    re.compile(r"\{\{[^}\n]*\}\}"),
    re.compile(r"\$\{[^}\n]*\}"),
    re.compile(r"<[A-Z_][A-Z0-9_ ]*>"),
    re.compile(r"%[A-Za-z_][A-Za-z0-9_]*%"),
]

_UUID = [
    re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"),
]

#: Opaque machine identifiers. Narrow on purpose — a bare number is a quantity,
#: a price or a day count far more often than it is a primary key, so a number
#: only counts when it is *labelled* as an id, is long-hex, or is a ``#1234``
#: reference.
_INTERNAL_ID = [
    # "id: 8842", "ID = 42", "user id #7788", "record ID 90210"
    re.compile(r"(?i)\b(?:[a-z][a-z0-9]*\s+)?id\b\s*(?:[:=#]|\bis\b|\bof\b)?\s*#?\d{2,}"),
    # 16+ hex characters: an object id / hash, never a thing a person types.
    re.compile(r"\b[0-9a-f]{16,}\b"),
    re.compile(r"(?i)\bObjectId\s*\(\s*[^)]*\)"),
    # A bare "#8842" record reference.
    re.compile(r"(?<![\w#-])#\d{3,}\b"),
]

#: CSS / XPath / Playwright locator syntax.
_CSS_XPATH_SELECTOR = [
    re.compile(r"\[[A-Za-z][\w:-]*\s*(?:[~^|*$]?=)\s*[^\]\n]*\]"),  # [data-testid="x"], [role=button]
    re.compile(r"(?<![\w&:#])#[A-Za-z][\w-]{2,}\b"),  # #login-submit, #email
    re.compile(
        r"\b(?:div|span|button|input|form|table|tbody|thead|td|tr|ul|ol|li|label|select|"
        r"textarea|section|nav|header|footer|h[1-6])(?:\.[\w-]{2,}|#[\w-]{2,}|\[[^\]\n]+\])+"
    ),  # button.primary, div#root, input[name=email]
    re.compile(r"(?<![:\w])//(?:[A-Za-z*]|\*)[\w\-*\[\]@='\"()./]*"),  # //button[@id='save']
    re.compile(r"(?i)\b(?:css|xpath|text)\s*=\s*[^\s,;]+"),  # css=..., xpath=...
    re.compile(r"(?i)\b(?:data-testid|data-test|aria-label|test-?id)\b"),
]

#: Routes, endpoints and URLs. The leading slash must be preceded by
#: start-of-string/space/quote/paren and followed by a letter, which is what
#: keeps ordinary English out: "and/or", "N/A", "Yes/No", "12/05", "US/Pacific"
#: and "Save / Cancel" all fail to match.
_API_PATH = [
    re.compile(r"(?i)\b(?:GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+/[\w\-/{}:.$]*"),
    re.compile(r"(?i)\bhttps?://\S+"),
    re.compile(r"(?<![\w:/.])/[A-Za-z][\w\-]*(?:/[\w\-{}:.$]+)*"),
]

#: HTTP status codes. Heavily narrowed (see the module note in the PR): a bare
#: ``404`` is flagged ONLY when a status-ish word or a reason phrase sits next
#: to it, because "enter 200" and "expect 404 items" are ordinary test data.
_HTTP_STATUS = [
    re.compile(
        r"(?i)\b(?:HTTP|status(?:\s+code)?|response(?:\s+code)?|error\s+code)"
        r"\s*(?:of\s+|with\s+|is\s+|=|:)?\s*[1-5]\d{2}\b"
    ),
    re.compile(
        r"\b[1-5]\d{2}\s+(?:OK|Created|Accepted|No\s+Content|Moved\s+Permanently|Found|"
        r"Bad\s+Request|Unauthorized|Forbidden|Not\s+Found|Method\s+Not\s+Allowed|Conflict|"
        r"Gone|Unprocessable\s+\w+|Too\s+Many\s+Requests|Internal\s+Server\s+Error|"
        r"Bad\s+Gateway|Service\s+Unavailable)\b"
    ),
    re.compile(r"\bHTTP/\d(?:\.\d)?\b"),
]

#: Code identifiers: camelCase, snake_case, PascalCase, backticked, and calls.
#: PascalCase requires two capitalised runs, so "User Management" (two words) is
#: untouched while "UserManagement" is caught. Product vocabulary that is
#: legitimately identifier-shaped is handled by the allow-list, not by loosening
#: these.
_CODE_IDENTIFIER = [
    re.compile(r"`[^`\n]+`"),  # `userId`
    re.compile(r"\b[A-Za-z_]\w*(?:\.\w+)*\((?:|[^)\n\s]+)\)"),  # login(); page.click('#x')
    # Any underscore-joined identifier — snake_case, SCREAMING_SNAKE, and the
    # mixed FSA_Card form. An underscore inside a word is essentially never
    # ordinary English, so this is safe to keep broad; the glossary allow-list is
    # what rescues a domain word that legitimately looks like this.
    re.compile(r"\b[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+\b"),  # snake_case
    re.compile(r"\b[a-z]+\d*[A-Z][A-Za-z0-9]*\b"),  # camelCase
    re.compile(r"\b[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+)+\b"),  # PascalCase
]

#: Database artifacts. Also heavily narrowed: "the table shows three rows" is
#: exactly how a QC describes an on-screen grid, so the word "table" alone is
#: never enough — it takes SQL shape, a quoted/identifier-shaped name, or an
#: explicitly *database* noun phrase.
_DB_ARTIFACT = [
    # Uppercase-only on purpose: "Select a status from the dropdown" and "Delete
    # from the list" are exactly how a QC writes a UI step, so a case-insensitive
    # SELECT…FROM / DELETE FROM would reject correct cases constantly. Leaked SQL
    # arrives in SQL casing.
    re.compile(r"\bSELECT\s+[\w*,.\s]+\s+FROM\s+[\w.\"'`]+"),
    re.compile(r"\bINSERT\s+INTO\s+[\w.\"'`]+"),
    re.compile(r"\bUPDATE\s+[\w.\"'`]+\s+SET\b"),
    re.compile(r"\bDELETE\s+FROM\s+[\w.\"'`]+"),
    re.compile(r"(?i)\b(?:database|db)\s+(?:table|row|record|column|entry|field)\b"),
    re.compile(r"(?i)\b(?:table|column|schema)\s+[\"'`][\w.]+[\"'`]"),
    re.compile(r"(?i)\b(?:table|column)\s+named\s+\S+"),
    re.compile(r"(?i)\bin\s+the\s+[a-z][a-z0-9]*_[a-z0-9_]+\s+(?:table|column)\b"),
    re.compile(r"(?i)\b(?:primary|foreign)\s+key\b"),
    re.compile(r"(?i)\bstored\s+procedure\b"),
]

#: Plain technical nouns a QC writing for another person would not use. Kept to
#: words with no ordinary-UI meaning — "cookie" and "token" are excluded because
#: a cookie banner and an emailed token are both things a user genuinely sees.
_TECH_NOUN = [
    re.compile(
        r"(?i)\b(?:endpoint|payload|request\s+body|response\s+body|query\s+string|"
        r"webhook|middleware|backend|back-end|front-end|selector|locator|"
        r"stack\s+trace|console\s+log|network\s+tab|http\s+header|request\s+header|"
        r"api\s+call|rest\s+api|graphql|websocket|regex|mock\s+server|stub(?:bed)?\s+response)\b"
    ),
    re.compile(r"\b(?:API|JSON|XML|DOM|SQL|CSS|XPath|JWT|HTTP|HTTPS|CRUD|ORM|UUID|GUID)\b"),
]

#: The rule table, in priority order. ``check_case``/``check_text`` iterate it.
RULES: tuple[tuple[str, list[re.Pattern[str]]], ...] = (
    ("template_var", _TEMPLATE_VAR),
    ("uuid", _UUID),
    ("css_xpath_selector", _CSS_XPATH_SELECTOR),
    ("api_path", _API_PATH),
    ("db_artifact", _DB_ARTIFACT),
    ("http_status", _HTTP_STATUS),
    ("internal_id", _INTERNAL_ID),
    ("code_identifier", _CODE_IDENTIFIER),
    ("tech_noun", _TECH_NOUN),
)

#: Every rule name, for callers that want to enumerate or selectively disable.
RULE_NAMES: tuple[str, ...] = tuple(name for name, _ in RULES)

#: Identifier-shaped words that are ubiquitous product/QA English rather than
#: leaked code, and would otherwise be caught by ``code_identifier`` in almost
#: every real case. This is a floor, not a substitute for the project glossary.
BASELINE_ALLOWED_TERMS: tuple[str, ...] = (
    "JavaScript",
    "TypeScript",
    "PowerPoint",
    "SharePoint",
    "PayPal",
    "iPhone",
    "iPad",
    "macOS",
    "iOS",
    "eSign",
    "eMail",
    "Playwright",
    "Q-Agent",
    "drop-down",
)


def allowed_terms_from_facts(facts: Iterable[Any]) -> list[str]:
    """Extract the glossary vocabulary a project is allowed to use verbatim.

    The gate must stay DB-free, so this takes *rows*, not a session: the caller
    queries ``BusinessFact`` for the project and hands the result over. Both ORM
    objects (``fact.category`` / ``fact.term``) and plain dicts
    (``{"category": ..., "term": ...}``) are accepted, so a test can pass
    literals and the eventual pipeline wiring (#829) can pass model instances.

    Only ``category == "glossary"`` rows contribute: a glossary term is the
    product's own name for a thing and is therefore legitimate in a step, while
    a ``rule``/``flow`` fact's ``term`` is a subject line, not vocabulary.
    Excluded facts are skipped — an excluded fact is out of context everywhere.

    Args:
        facts: ``BusinessFact`` rows (or dicts of the same shape) for one project.

    Returns:
        The distinct, non-empty glossary terms, in first-seen order. Suitable to
        pass straight to :func:`check_case` as ``allowed_terms``.
    """
    out: list[str] = []
    seen: set[str] = set()
    for fact in facts or []:
        if isinstance(fact, dict):
            category = str(fact.get("category") or "")
            term = str(fact.get("term") or "")
            excluded = bool(fact.get("excluded"))
        else:
            category = str(getattr(fact, "category", "") or "")
            term = str(getattr(fact, "term", "") or "")
            excluded = bool(getattr(fact, "excluded", False))
        if excluded or category != "glossary":
            continue
        term = term.strip()
        key = term.casefold()
        if term and key not in seen:
            seen.add(key)
            out.append(term)
    return out


def _mask_allowed(text: str, allowed_terms: Sequence[str]) -> str:
    """Blank out the project's own vocabulary before any rule sees the text.

    Each allowed term is replaced, case-insensitively, by an equal-length run of
    a neutral filler character. Equal length matters: every finding reports the
    matched substring sliced out of the ORIGINAL text, so the masked copy must
    stay character-aligned with it.

    Masking (rather than post-filtering findings) is what makes a compound leak
    behave correctly: in ``the eClaims userId field`` the glossary term
    ``eClaims`` disappears and ``userId`` is still reported, which a
    "drop findings equal to an allowed term" approach would get right only by
    accident.

    Args:
        text: The original field text.
        allowed_terms: Vocabulary that is exempt (glossary + baseline).

    Returns:
        A same-length copy of ``text`` with the allowed terms blanked.
    """
    if not text or not allowed_terms:
        return text
    masked = text
    # Longest first, so "FSA Card Number" wins over "FSA" and a short term can
    # never chop a longer one into a fragment that then trips a rule.
    for term in sorted({t.strip() for t in allowed_terms if t and t.strip()}, key=len, reverse=True):
        pattern = re.compile(r"(?<![\w-])" + re.escape(term) + r"(?![\w-])", re.IGNORECASE)
        masked = pattern.sub(lambda m: "\u00b7" * (m.end() - m.start()), masked)
    return masked


def check_text(
    text: str,
    field: str = "text",
    allowed_terms: Sequence[str] | None = None,
    disabled_rules: Iterable[str] | None = None,
) -> list[dict]:
    """Scan one string for QC-voice violations.

    Rules are applied in :data:`RULES` priority order and matches claim their
    character span, so the most specific rule wins and each offending substring
    is reported exactly once under exactly one rule name.

    Args:
        text: The string to scan (a title, a step action, an expected result…).
        field: The field path reported in each finding, e.g. ``"steps[2].e"``.
        allowed_terms: Project vocabulary that is exempt. Merged with
            :data:`BASELINE_ALLOWED_TERMS`. Pass the project's glossary terms
            (see :func:`allowed_terms_from_facts`).
        disabled_rules: Rule names to skip entirely. Exists so a caller can turn
            off a rule that misfires on their domain without losing the rest of
            the gate — and so the test corpus can prove a fixture is rejected by
            the rule it claims to exercise, by disabling that rule and watching
            the fixture pass.

    Returns:
        A list of ``{"field": str, "rule": str, "match": str}`` dicts, ordered by
        rule priority then by position. Empty when the text is clean.
    """
    if not text or not str(text).strip():
        return []
    original = str(text)
    skip = {str(name) for name in (disabled_rules or ())}
    terms = list(BASELINE_ALLOWED_TERMS) + list(allowed_terms or ())
    masked = _mask_allowed(original, terms)

    claimed: list[tuple[int, int]] = []
    findings: list[dict] = []
    for rule_name, patterns in RULES:
        if rule_name in skip:
            continue
        for pattern in patterns:
            for match in pattern.finditer(masked):
                start, end = match.span()
                if start == end:
                    continue
                if any(start < c_end and c_start < end for c_start, c_end in claimed):
                    continue
                claimed.append((start, end))
                findings.append(
                    {"field": field, "rule": rule_name, "match": original[start:end].strip()}
                )
    return findings


#: The case fields the gate reads. ``value`` in ``testData`` is deliberately NOT
#: scanned: test data values are literally the strings a tester types, and they
#: legitimately include emails, ids and codes.
_SCALAR_FIELDS: tuple[str, ...] = ("title", "objective", "precondition")


def check_case(
    case: dict,
    allowed_terms: Sequence[str] | None = None,
    disabled_rules: Iterable[str] | None = None,
) -> dict:
    """Gate one generated test case on QC voice.

    Reads ``title``, ``objective``, ``precondition``, every ``steps[].a``, every
    ``steps[].e`` and every ``testData[].field``. A case is rejected if any one
    of those carries a selector, route, endpoint, status code, identifier,
    database artifact, template variable or bare technical noun — i.e. anything
    the person executing the test could not act on.

    Args:
        case: The case dict as the generator returns it — ``{"title": str,
            "objective": str, "precondition": str, "steps": [{"a": str, "e":
            str}], "testData": [{"field": str, "value": str}]}``. Missing or
            oddly-typed keys are tolerated and skipped, never raised on: the
            gate's job is to judge text, not to validate the schema.
        allowed_terms: The project's own vocabulary, exempt from every rule.
            Seed it from the business glossary via
            :func:`allowed_terms_from_facts` — without it, ``code_identifier``
            rejects correct cases that use domain words like ``eClaims`` or
            ``FSA_Card``, and the gate gets switched off.
        disabled_rules: Rule names to skip (see :func:`check_text`).

    Returns:
        ``{"outcome": "pass" | "reject", "findings": [{"field", "rule",
        "match"}]}``. ``findings`` is empty exactly when ``outcome == "pass"``.
    """
    case = case or {}
    findings: list[dict] = []

    for name in _SCALAR_FIELDS:
        value = case.get(name)
        if isinstance(value, str):
            findings += check_text(value, name, allowed_terms, disabled_rules)

    steps = case.get("steps")
    if isinstance(steps, list):
        for index, step in enumerate(steps):
            if not isinstance(step, dict):
                continue
            for key in ("a", "e"):
                value = step.get(key)
                if isinstance(value, str):
                    findings += check_text(
                        value, f"steps[{index}].{key}", allowed_terms, disabled_rules
                    )

    test_data = case.get("testData")
    if isinstance(test_data, list):
        for index, entry in enumerate(test_data):
            if not isinstance(entry, dict):
                continue
            value = entry.get("field")
            if isinstance(value, str):
                findings += check_text(
                    value, f"testData[{index}].field", allowed_terms, disabled_rules
                )

    return {"outcome": "reject" if findings else "pass", "findings": findings}
