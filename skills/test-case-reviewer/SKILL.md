---
name: test-case-reviewer
description: Review generated manual test cases for coverage, correctness, clarity, and traceability against the requirement analysis and Project Knowledge Base, and produce a severity-rated review report with an Approve / Approve-with-changes / Reject verdict. Use AFTER test-case-generator, when the user says "review the test cases", "check test coverage", or "are these test cases good". Does NOT rewrite the test cases unless asked.
version: 1.0.0
author: Andrew
---

# Test Case Reviewer

## Purpose

Independently review the manual test cases produced by `test-case-generator` and decide whether
they are complete, correct, and traceable enough to proceed to automation. The output is a
**severity-rated review report** with a clear verdict — not a rewrite of the cases.

## Position in the QA Pipeline

```
project-bootstrap → requirement-analyst → test-case-generator
        ↓ ADO test cases + coverage matrix
[test-case-reviewer]  ← you are here
        ↓ review report (verdict)
automation-generator → automation-reviewer → execution-analyzer
        → screenshot-annotator / ticket-comment-generator / report-generator
```

## When to Use

- Test cases exist (from `test-case-generator`) and need sign-off before automation.
- The user asks to review test cases or check coverage.

## Inputs / Prerequisites

- The generated **test cases** and their **Requirement Coverage Matrix**.
- **`requirement-analysis.md`** — the ground truth for coverage and expected behavior.
- **`knowledge.md`** — for terminology, roles, and format expectations.

## Review Dimensions

Evaluate every test case (and the set as a whole) against:

1. **Coverage** — is every Acceptance Criterion and business rule from the analysis covered?
2. **Correctness** — do expected results match the requirement analysis (not assumptions)?
3. **Clarity / reproducibility** — can another engineer execute the steps without guessing?
4. **Test data adequacy** — is data specified, valid, and sufficient for each case?
5. **Duplication / overlap** — are there redundant or overlapping cases?
6. **Traceability** — does each case link to an AC, and each AC to ≥1 case?
7. **ADO format compliance** — are all required fields present and well-formed?
8. **Automation-candidate correctness** — are the right cases flagged (stable, deterministic)?
9. **QC voice** — are the cases written for a person using the product, free of selectors,
   routes, status codes, database artifacts and code identifiers? See **Voice** below.

## Severity Levels

- **Critical** — a required AC/business rule is completely untested, or an expected result is wrong.
- **Major** — meaningful coverage gap, incorrect data, or a case that cannot be executed as written.
- **Minor** — clarity, wording, or minor format issues that don't block execution.
- **Nit** — style/consistency polish.

## Workflow

1. Load the test cases, coverage matrix, `requirement-analysis.md`, and `knowledge.md`.
2. Check each Review Dimension; for each issue, cite the **Test Case ID** and the **AC** involved.
3. Build a coverage gap list from the analysis (every AC and business rule).
4. Rate each finding by severity and give a concrete, actionable recommendation.
5. Decide the verdict and write the report using `templates/review-report.md`.

## Output

A review report following `templates/review-report.md`: verdict (Approve / Approve-with-changes /
Reject), findings table, coverage matrix with gaps, missing-scenario list, approved items, and the
next step.

> **Q-Agent pipeline mode.** In the automated Q-Agent pipeline the generator emits only the
> happy-path set on purpose, so here you are explicitly asked to ALSO produce the deferred
> coverage: after listing the gaps, generate the additional negative, invalid-input, boundary,
> permission, empty-state and error-handling test cases that fill them (without duplicating the
> happy-path cases). The calling prompt pins the exact JSON shape to return.

## Voice

Test cases are read and executed by a person with the product open in front of
them, not by a developer with the codebase open. This applies **twice** here: it
is a review dimension you judge the existing cases against, and it binds the
additional cases you generate in Q-Agent pipeline mode.

- **Steps are what a person does in the UI**, in the product's own words — screen
  names, button labels, field labels, tabs, menu items.
- **Expected results are what that person sees** — a message, a state, a row, a
  screen. If it cannot be observed on screen, it is not an expected result.
- Use the product's vocabulary from **Business Knowledge** first, then the
  Knowledge Base — and never quote the Knowledge Base's technical detail
  (selectors, routes, field names, table or column names) into a step.

**Never** write any of the following into a title, objective, precondition, step,
expected result, or test-data field name — and raise a **Minor** finding (or
**Major**, where it makes the step unexecutable) when an existing case does:

- CSS / XPath / test-id selectors — `#login-submit`, `[data-testid="save-btn"]`, `button.primary`
- Routes, endpoints, or URLs — `/admin/users`, `GET /api/v1/agencies`, `/admin/groups/:id`
- Request or response payloads, and the field names inside them
- HTTP status codes — "returns 403", "HTTP 404", "201 Created"
- Database artifacts — tables, columns, rows, SQL statements, stored procedures, keys
- IDs and internal identifiers — UUIDs, `id: 8842`, record numbers, hashes
- Code identifiers in any casing — `userId`, `broker_agency_id`, `BrokerAgencyList`, `submitForm()`
- Bare technical nouns — endpoint, payload, selector, locator, DOM, API, webhook, network tab
- Unresolved template placeholders — `{{admin_user}}`, `${BASE_URL}`, `<USER NAME>`

Before and after:

- BAD — `Navigate to /admin/users and assert [data-testid="save-btn"] is enabled`
- GOOD — `Open the User Management screen and check that the Save button can be clicked`

The one legitimate exception is a domain word that is genuinely the product's own
name for a thing — `eClaims`, `FSA_Card`, `COBRA`. Those come from Business
Knowledge and are the vocabulary a tester already uses, so they are correct as-is
and must not be flagged.

## Quality Rules

- Cite the specific **Test Case ID** and **Acceptance Criterion** for every finding.
- Verify against the **requirement analysis**, not against assumptions.
- Findings must be **specific and actionable** — no vague "improve coverage" notes.
- Do not rewrite the test cases unless explicitly asked; recommend changes instead.
- Judge every case against **Voice** above, and write any case you generate in that voice.

## Handoff

Approved cases proceed to `automation-generator`. Rejected or changed cases go back to
`test-case-generator` for revision. The verdict and gaps also feed `report-generator`.
