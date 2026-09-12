---
name: test-case-generator
description: Generate a lightweight Azure DevOps-style manual test case set from a completed requirement analysis. This stage focuses on validating the primary user flow (happy path) only. Detailed edge cases, negative scenarios, and exhaustive coverage are intentionally deferred to the Test Case Review stage.
version: 2.0.0
author: Andrew
---

# Test Case Generator

## Purpose

Generate a **simple, review-friendly baseline** set of manual Azure DevOps test cases from an approved Requirement Analysis.

The goal of this stage is **not** to create a complete QA suite.

Instead, generate only the minimum set of high-value **happy path** test cases that prove the feature works end-to-end.

These test cases become the starting point for the next stage (`test-case-reviewer`), where additional coverage such as edge cases, validation, permissions, regression risks, and negative scenarios will be added.

---

# Position in QA Pipeline

project-bootstrap
↓
knowledge.md + knowledge.json

requirement-analyst
↓
requirement-analysis.md

test-case-generator ← CURRENT STAGE
↓
Simple Happy Path Test Cases

test-case-reviewer
↓
Expand Coverage

automation-generator

---

# Inputs

Required:

- requirement-analysis.md
- knowledge.md / knowledge.json

If either is missing, stop and request it.

---

# Responsibilities

This skill should:

- Read the completed Requirement Analysis.
- Understand the business workflow.
- Generate only the core user-flow test cases.
- Keep the output intentionally small and easy to review.
- Avoid generating exhaustive scenarios.

This skill should NOT:

- Generate every validation scenario.
- Generate boundary tests.
- Generate permission tests (unless explicitly required by an Acceptance Criterion).
- Generate regression suites.
- Generate automation scripts.

---

# Coverage Strategy

Prioritize:

- Primary happy path
- Core business flow
- Most common user behavior
- One successful scenario per Acceptance Criterion whenever possible

Do NOT proactively generate:

- Invalid input cases
- Boundary value cases
- Duplicate data
- Empty states
- Error handling
- Network failures
- Permission matrices
- Cross-browser scenarios
- Regression scenarios

Those belong to `test-case-reviewer`.

---

# Workflow

1. Read requirement-analysis.md.
2. Map each Acceptance Criterion to its primary successful user flow.
3. Merge similar flows where appropriate to avoid redundant cases.
4. Generate concise Azure DevOps manual test cases.
5. Produce a simple Requirement Coverage Matrix.
6. Identify obvious automation candidates.

---

# Output

## 1. Azure DevOps Manual Test Cases

Each test case should contain:

- Title
- Objective
- Preconditions
- Test Data (if needed)
- Steps
- Expected Result for every step
- Priority
- Test Type
- Automation Candidate
- Linked Acceptance Criteria

Keep steps concise and focused.

---

## 2. Requirement Coverage Matrix

| Acceptance Criterion | Covered By |
|----------------------|------------|

Flag any uncovered Acceptance Criteria.

---

## 3. Automation Candidates

Recommend only deterministic, stable happy-path scenarios suitable for Playwright automation.

---

# Voice

Write as a QC hand-writing a manual test **for another person to execute**. The
reader is a tester sitting in front of the running product — not a developer with
the codebase open.

- **Steps are what a person does in the UI**, in the product's own words: the
  screen names, button labels, field labels, tabs and menu items the user actually
  sees.
- **Expected results are what that person sees** — a message, a state, a row, a
  screen. If it cannot be observed on screen, it is not an expected result.
- Use the product's vocabulary, not the implementation's.

**Never** write any of the following into a title, objective, precondition, step,
expected result, or test-data field name:

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
Knowledge and are the vocabulary a tester already uses, so write them as-is.

---

# Quality Rules

- Favor clarity over completeness.
- One measurable expected result per step.
- Reuse terminology from **Business Knowledge** first, then the Knowledge Base — and never quote the Knowledge Base's technical detail (selectors, routes, field names, table or column names) into a step or an expected result.
- Do not invent screens, routes, credentials, or business rules.
- Keep the number of test cases as small as possible while still covering every Acceptance Criterion.
- Merge duplicate user journeys whenever practical.
- Clearly document assumptions instead of guessing.