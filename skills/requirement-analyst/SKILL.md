---
name: requirement-analyst
description: Analyze an Azure DevOps or Jira ticket into a structured Requirement Analysis (business objective, functional/non-functional requirements, business rules, AC breakdown, edge cases, risks, test-scope + coverage plan) before any test cases are written. Use when the user pastes a ticket, says "analyze this requirement", "break down this ticket", or before running test-case-generator. Requires the Project Knowledge Base from project-bootstrap.
version: 1.0.0
author: Andrew
---

# Requirement Analyst

## Purpose

Transform a raw Azure DevOps or Jira ticket into a structured **Requirement Analysis** that
becomes the foundation for all downstream QA work. This skill *understands* requirements — it
does **not** generate test cases or automation.

The output gives `test-case-generator` enough structured information to produce comprehensive,
review-ready test cases without having to reinterpret the original ticket.

## Position in the QA Pipeline

```
project-bootstrap
        ↓ knowledge.md + knowledge.json
[requirement-analyst]  ← you are here
        ↓ requirement-analysis.md
test-case-generator → test-case-reviewer
        → automation-generator → automation-reviewer → execution-analyzer
        → screenshot-annotator / ticket-comment-generator / report-generator
```

## When to Use

- A new ticket needs analysis before test cases are written.
- Acceptance criteria are ambiguous, incomplete, or contradictory and must be interrogated.
- The user asks to "analyze", "break down", or "understand" a requirement/ticket.

## Inputs / Prerequisites

Required:
- Ticket Title, Description, Acceptance Criteria
- Ticket Comments and Attachments (if available)
- Linked Pull Requests (optional)
- **Project Knowledge Base** — `knowledge.md` + `knowledge.json` from `project-bootstrap`

> The analysis now receives the **enriched KB context** injected into the prompt — business
> entities, domain, application routes, and base URL. Reuse that real terminology and those
> entities/routes as-is; never reinterpret or invent them.
>
> If the Project Knowledge Base does not exist, run **project-bootstrap** first. Reuse its
> business entities, roles, workflows, and glossary — never invent domain concepts.

## Workflow

1. **Requirement understanding** — identify the business objective, user goal, functional and
   non-functional requirements, scope, and out-of-scope items.
2. **Acceptance Criteria analysis** — for each AC: explain its meaning, identify dependencies,
   detect ambiguity, flag missing information, and state the expected behavior.
3. **Business rules** — extract explicit and implicit rules; categorize as Validation, Workflow,
   Permission, Data, or Calculation rules.
4. **Edge cases** — boundary conditions, empty states, invalid inputs, unexpected actions,
   error and recovery scenarios.
5. **Risks** — missing/contradictory requirements, technical, business, and regression risks;
   assign a risk level.
6. **Domain mapping** — map the ticket to the KB's business entities, routes, and workflows,
   reusing KB terminology and the exact project terminology from the enriched context.
7. **Test scope recommendation** — recommend which testing types apply (Functional, Regression,
   Validation, Permission, UI, API, Integration).
8. **Requirement coverage plan** — map each AC to proposed testing areas; highlight covered vs
   uncovered requirements and suggest additional scenarios.

## Voice

This analysis is not read only by you. Its **business rules** and **validation
rules** are carried almost verbatim into the manual test cases that
`test-case-generator` writes for a human tester to execute, so a rule worded in
implementation terms leaks straight into a test step.

Write every rule as a statement about the **product and the person using it**, in
the vocabulary of **Business Knowledge** and the ticket — not about the code that
implements it. Keep selectors, routes, endpoints, payload and column names, HTTP
status codes and code identifiers out of the rule text; if a technical detail is
genuinely necessary to understand a risk or a dependency, put it under Risks,
Assumptions or Domain Mapping, where it stays out of the case text.

- BAD — `expirationDate must be set before POST /brokers/agencies returns 201`
- GOOD — `An agency cannot be saved until its next expiration date is filled in`

Domain words that are the product's own name for a thing — `eClaims`, `FSA_Card`,
`COBRA` — are vocabulary, not leakage, and should be reused as-is.

## Output

Populate `templates/requirement-analysis.md` with: Executive Summary, Business Objective,
Functional Requirements, Non-functional Requirements, Business Rules, Acceptance Criteria
Breakdown, Edge Cases, Risks, Assumptions, Missing Information, Recommended Test Scope, and the
Requirement Coverage Plan.

## Quality Rules

- **Do not** generate test cases or automation code — that is downstream work.
- Clearly distinguish **facts** (stated in the ticket/KB) from **assumptions** (inferred).
- Reuse business terminology from **Business Knowledge** first, then the Project Knowledge Base; never invent domain concepts.
- Word every business rule and validation rule in business terms (see **Voice**) — they become test-case text downstream.
- **Identify ambiguity instead of guessing** — put unknowns under Missing Information.
- Assign risk levels; make expected behavior explicit for every AC.

## Handoff / Success Criteria

The Requirement Analysis is consumed by **test-case-generator**. Success means a test-case author
(human or AI) can build complete Azure DevOps-style test cases directly from
`requirement-analysis.md` without re-reading or reinterpreting the original ticket.
