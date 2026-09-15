---
name: project-bootstrap
description: Learn a software project and build a reusable Project Knowledge Base (knowledge.md + knowledge.json) before any downstream QA work. Use this FIRST, once per project, whenever no Project Knowledge Base exists yet, or when the user asks to "index the codebase", "bootstrap the project", "learn this repo for QA", or before running requirement analysis, test-case generation, or automation generation.
version: 1.0.0
author: Andrew
---

# Project Bootstrap & Codebase Indexing

## Purpose

Learn a software project **before** generating any requirements analysis, test cases, or
automation, and persist that understanding as a reusable **Project Knowledge Base**.

Every other skill in this QA pipeline reads the Project Knowledge Base instead of
re-discovering the codebase. Running this skill once produces two synchronized artifacts:

- `knowledge.md` — human-readable knowledge base
- `knowledge.json` — machine-readable context for downstream AI agents

## Position in the QA Pipeline

```
[project-bootstrap]  ← you are here (run once per project)
        ↓ knowledge.md + knowledge.json
requirement-analyst → test-case-generator → test-case-reviewer
        → automation-generator → automation-reviewer → execution-analyzer
        → screenshot-annotator / ticket-comment-generator / report-generator
```

## When to Use

- No Project Knowledge Base exists for the target repository yet.
- The codebase changed materially (new framework, new automation project, restructured folders).
- A downstream skill reports that `knowledge.md` / `knowledge.json` is missing or stale.

Do **not** re-run on every ticket. The knowledge base is meant to be reused.

## Inputs

- Read all stuff needed for bootstrap from project code base from provided configuration, URL, PAT.
  - `README.md`, `docs/`, architecture documents
  - Existing automation project, if any (Playwright / Selenium / Cypress / WebdriverIO)
  - Package manifests (`package.json`, `*.csproj`, `pom.xml`, `requirements.txt`, etc.)
- **User-authored project configuration** (from the Project Details page), treated as
  authoritative: application base URL, per-environment URLs, configured test-account
  **roles/usernames** (never the passwords — those stay in the secure store), and any
  extra project-specific values.
- **A local checkout of the application source.** When a local repo path is configured it is
  used directly; otherwise the configured **remote repo URL** is cloned/pulled into the app
  workspace (`workspace/repos/<project>`) and used instead. When a checkout is available,
  traverse the actual source with your file tools to discover **real** routes,
  `data-testid`/selectors, page objects, fixtures and the authentication flow — do not infer them.

## Objective: eliminate placeholders downstream

The knowledge base must let `automation-generator` emit runnable Playwright specs with
**no manual placeholders**. Discover concrete, reusable facts — base URL, real routes,
real selectors, the login flow/URL, environments, and business entities — so the only
values injected later at generation time are the test-account passwords (pulled from the
secure store by role). Anything genuinely undiscoverable goes under **Missing Information**.

## Constraints

- **Read-only.** Never modify source code.
- Never generate test cases or automation during indexing.
- Never install dependencies unless explicitly requested.
- Never invent frameworks, APIs, components, or domain concepts that are not evidenced in the code.

## Workflow

1. **Detect the stack** — languages, frameworks, build tools, package managers, from manifests and config.
2. **Map the architecture** — frontend/backend frameworks, folder structure, module boundaries, layers.
3. **Capture the base URL and environments** — the primary application URL (prefer the
   configured one) and any per-environment URLs.
4. **Catalog real routes** — the concrete routes/URL patterns a test would navigate to,
   noting which require authentication.
5. **Inventory automation assets** — existing test framework, config, Page Objects, fixtures, helpers, test data factories (record their **names** so downstream can reuse them).
6. **Determine locator strategy + real selectors** — the project's actual selector priority (data-testid → getByRole → getByLabel → CSS → XPath), with **real selector/testid examples** discovered in the code, mapped to their screen/element. Note: the KB may later gain routes/selectors stamped `verified_at_runtime` (confirmed live by the DOM exploration agent). Those are **preferred** over the source-inferred entries you record here; never overwrite a verified entry with a source-inferred one.
7. **Learn authentication** — login flow, **login URL**, session handling, reusable auth helpers / storage state, and which configured test-account role logs in where.
8. **Extract coding standards** — naming, folder conventions, assertion style, async pattern, error handling, comment density. Record the test-writing half of this in `test_conventions` (`spec_roots`, `spec_naming`, `structure`, `assertion_style`, `data`): downstream generation is asked to write specs that read like the suite they join, and this is the only channel that reaches a run with no local checkout. One short sentence per field, from the team's OWN tests — leave a field empty rather than guessing, and omit the object entirely when the project has no tests yet.
9. **Extract business domain knowledge** — entities, user roles, workflows, business rules, glossary — from docs and code.
10. **Catalog UI components, APIs, environment config, and external integrations** (Azure DevOps, Jira, storage, email, etc.).
11. **Validate completeness** — record what is unknown under "Missing Information" rather than guessing.
12. **Emit both artifacts** — populate `templates/knowledge.md`, then produce a synchronized `knowledge.json` using `templates/knowledge.json`.

## Output

Write both files to the project (default location `docs/qa/knowledge.md` and `docs/qa/knowledge.json`,
or wherever the user specifies). Populate every section of `templates/knowledge.md`. Keep the
`.md` and `.json` **synchronized** — every fact in the JSON must be traceable to the Markdown.

The Markdown must end with a concise **AI Context Summary** (500–1000 words) optimized for
downstream agents.

## Quality Rules

- Prefer existing project patterns over generic solutions.
- Reuse existing helpers and Page Objects — name them so downstream skills can reference them.
- Distinguish **facts** (found in code) from **assumptions** (inferred) explicitly.
- State uncertainty; put gaps under "Missing Information".
- Keep output deterministic and Markdown-structured.

## Handoff / Success Criteria

After this skill runs, `requirement-analyst`, `test-case-generator`, and `automation-generator`
should be able to work **without re-reading the whole codebase** — they consume the knowledge base.
Success means a downstream agent can name the correct framework, locator strategy, reusable
Page Objects, and domain terminology purely from `knowledge.md` / `knowledge.json`.
