# QAgent Playwright Automation Generation Architecture Update

## Status

**Proposed architecture update**

## Purpose

Refactor QAgent's current Playwright automation generation flow from a **single large generated spec file** into a reusable, layered automation architecture.

The current flow is:

```text
Requirement / Feature
        |
        v
Browser Harness
        |
        |-- Travel / explore the application
        |-- Discover pages, elements, flows, and behavior
        |
        v
AI Test Generation
        |
        v
One large *.spec.ts
```

The generated spec currently contains most or all of:

- Test cases
- Locators
- Page navigation
- UI interactions
- Authentication/setup
- Helper functions
- Test data
- Assertions
- Repeated implementation logic

Each new Feature is generated independently. Existing automation code is not reliably reused across generation runs.

This must change.

The new architecture should make QAgent behave more like a **senior automation engineer**:

> Explore first, understand the existing automation assets, reuse what already exists, generate only what is missing, and keep test specifications focused on business scenarios.

---

# 1. Goals

The refactor must achieve the following:

1. Keep **Browser Harness** as the mechanism for discovering and understanding the application.
2. Separate application exploration from code generation.
3. Generate maintainable Playwright projects instead of monolithic specs.
4. Reuse existing Page Objects, fixtures, utilities, and test data across Features.
5. Introduce a shared QAgent Playwright Base Framework for generic automation infrastructure.
6. Make generated test specs represent business scenarios rather than low-level browser actions.
7. Prevent duplicate Page Objects and duplicate utility implementations.
8. Allow QAgent to incrementally extend an existing automation project.
9. Make regeneration safe and behavior-preserving.
10. Minimize generated code while maximizing reuse.

The primary optimization target is:

```text
REUSE > EXTEND > CREATE
```

and:

```text
MAINTAINABILITY > MINIMUM NUMBER OF FILES
```

---

# 2. Non-Goals

This update does NOT mean:

- Every Test Case gets its own spec file.
- Every UI interaction gets its own class.
- Every project must use the same Page Objects.
- All application-specific code should move into the Base Framework.
- Browser Harness should be replaced.
- QAgent should generate a generic abstraction for every action.
- Existing automation should be deleted and regenerated unnecessarily.

Avoid over-engineering.

---

# 3. Target Architecture

The target architecture has three logical layers.

```text
+------------------------------------------------------+
|                      QAgent                          |
|                                                      |
| Requirement / Feature / Test Cases                   |
| Browser Harness Exploration                          |
| Automation Planning                                  |
| Code Generation                                      |
+---------------------------+--------------------------+
                            |
                            v
+------------------------------------------------------+
|             @qagent/playwright-base                  |
|                                                      |
| Generic automation infrastructure                    |
|                                                      |
| Auth / Fixtures / API / Evidence / Utils / Logging  |
| Retry / Config / Common helpers                     |
+---------------------------+--------------------------+
                            |
                            v
+------------------------------------------------------+
|             Application Automation Project           |
|                                                      |
| Pages / Components / Tests / Test Data / App Setup  |
+------------------------------------------------------+
```

## Layer 1 — QAgent

Responsible for:

- Understanding requirements
- Understanding Test Cases
- Exploring the application through Browser Harness
- Building an automation plan
- Identifying reusable assets
- Generating or modifying project-specific automation

QAgent should answer:

> What needs to be tested and what application behavior is required?

## Layer 2 — QAgent Playwright Base Framework

Responsible for generic automation infrastructure.

Examples:

- Authentication infrastructure
- Common fixtures
- API client helpers
- Screenshot/evidence helpers
- Logging
- Retry utilities
- File upload/download helpers
- Random test data utilities
- Environment configuration
- Common Playwright helpers
- Reporting/evidence integration

QAgent should NOT regenerate these for every Feature.

## Layer 3 — Application Automation Project

Responsible for application-specific behavior.

Examples:

- LoginPage
- UserListPage
- UserFormPage
- OrderPage
- ProductPage
- Application-specific fixtures
- Application-specific test data
- Feature-specific test specs

---

# 4. New End-to-End Generation Flow

The current flow should evolve from:

```text
Browser Harness
      |
      v
Generate Full Spec
```

to:

```text
Feature / Test Cases
        |
        v
+-----------------------+
| Browser Harness       |
| Exploration           |
+-----------+-----------+
            |
            v
+-----------------------+
| Exploration Model     |
|                       |
| Pages                 |
| Components            |
| Locators              |
| Actions               |
| Navigation            |
| Authentication        |
| Business flows        |
| Observed behavior     |
+-----------+-----------+
            |
            v
+-----------------------+
| Existing Project      |
| Analysis              |
+-----------+-----------+
            |
            v
+-----------------------+
| Reuse / Extend /      |
| Create Decision       |
+-----------+-----------+
            |
            v
+-----------------------+
| Automation Plan       |
+-----------+-----------+
            |
            v
+-----------------------+
| Generate / Modify     |
| Project Assets        |
+-----------+-----------+
            |
            v
+-----------------------+
| Generate Test Specs   |
+-----------+-----------+
            |
            v
+-----------------------+
| Validate / Execute    |
+-----------------------+
```

The critical change is:

> **Browser Harness explores the application, but it does not directly dictate the final code structure.**

The exploration output becomes structured knowledge used by the planning/generation phase.

---

# 5. Browser Harness Responsibility

Browser Harness remains the source of truth for application discovery.

During exploration, QAgent should collect information such as:

```text
Page
- URL
- Page title
- Navigation paths

Elements
- Role
- Label
- Text
- Test ID
- CSS/XPath when unavoidable
- Visibility
- Enabled state

Interactions
- Click
- Fill
- Select
- Upload
- Download
- Submit

Business behavior
- Form validation
- Success messages
- Error messages
- Navigation after action
- Table/list changes
- Modal behavior

Authentication
- Login flow
- Authenticated state
- Required account
```

However, Browser Harness output should NOT directly become:

```text
recorded-actions.spec.ts
```

Instead, it should become an intermediate exploration model.

---

# 6. Exploration Model

Introduce a normalized internal representation between Browser Harness and code generation.

Conceptually:

```ts
type ExplorationModel = {
    pages: PageModel[];
    components: ComponentModel[];
    flows: UserFlowModel[];
    authentication?: AuthenticationModel;
};
```

Example:

```json
{
  "page": "User Management",
  "url": "/users",
  "elements": [
    {
      "name": "Create User",
      "role": "button",
      "locatorStrategy": "role"
    }
  ],
  "flows": [
    {
      "name": "Create User",
      "actions": [
        "click Create User",
        "fill Name",
        "fill Email",
        "click Save",
        "verify success"
      ]
    }
  ]
}
```

The exact schema can evolve.

The important architectural principle is:

```text
Browser Harness observations
            !=
Generated Playwright source code
```

The exploration model is the bridge.

---

# 7. Existing Project Analysis

Before generating code, QAgent MUST inspect the existing automation project.

At minimum inspect:

```text
tests/
pages/
components/
fixtures/
data/
utils/
config/
playwright.config.ts
package.json
```

The generator should identify:

- Existing Page Objects
- Existing component objects
- Existing fixtures
- Existing utilities
- Existing authentication
- Existing test data
- Existing selectors
- Existing naming conventions
- Existing project architecture
- Existing package dependencies

The generator must treat the existing project as an asset library.

---

# 8. Reuse Decision Hierarchy

For every required capability, QAgent must use this decision order:

```text
1. REUSE
2. EXTEND
3. CREATE
```

## 8.1 REUSE

If an existing implementation can satisfy the requirement:

```text
UserFormPage.ts
```

reuse it.

Do not create:

```text
CreateUserPage.ts
UserCreationPage.ts
UserFormV2.ts
```

just because the current Feature has a new Test Case.

## 8.2 EXTEND

If the existing object is relevant but missing a capability:

```ts
UserFormPage.fillUser()
```

can be extended with:

```ts
UserFormPage.expectDuplicateEmailError()
```

rather than creating a second page object.

## 8.3 CREATE

Only create a new asset if no suitable existing asset exists.

---

# 9. Base Framework

Create a shared package:

```text
@qagent/playwright-base
```

The exact package name may be adjusted based on repository conventions.

Example structure:

```text
qagent-playwright-base/
|
+-- src/
|   +-- auth/
|   +-- fixtures/
|   +-- api/
|   +-- evidence/
|   +-- logging/
|   +-- utils/
|   +-- assertions/
|   +-- config/
|   +-- index.ts
|
+-- package.json
+-- tsconfig.json
+-- README.md
```

The Base Framework should contain generic functionality that is expected to be useful across multiple application automation projects.

Examples:

```text
auth/
    authentication state management

fixtures/
    base fixtures
    authenticated fixtures

api/
    API helpers

evidence/
    screenshot
    trace
    video
    attachment helpers

utils/
    wait helpers
    file helpers
    date helpers
    random data helpers

logging/
    structured automation logging

config/
    environment helpers
```

---

# 10. What MUST NOT Go Into Base Framework

Application-specific Page Objects must remain in the application project.

Do NOT put:

```text
@qagent/playwright-base
|
+-- LoginPage.ts
+-- UserPage.ts
+-- OrderPage.ts
+-- ProductPage.ts
```

These belong to the application.

The Base Framework should not know how a specific application's UI works.

---

# 11. Application Project Structure

A generated project should look approximately like:

```text
automation-project/
|
+-- tests/
|   +-- user-management/
|   |   +-- user-creation.spec.ts
|   |   +-- user-deletion.spec.ts
|   |
|   +-- orders/
|
+-- pages/
|   +-- LoginPage.ts
|   +-- UserListPage.ts
|   +-- UserFormPage.ts
|   +-- OrderPage.ts
|
+-- components/
|   +-- DataTable.ts
|   +-- ConfirmationModal.ts
|
+-- fixtures/
|   +-- app.fixture.ts
|
+-- data/
|   +-- users.ts
|   +-- orders.ts
|
+-- utils/
|   +-- app-specific-utils.ts
|
+-- config/
|   +-- environments.ts
|
+-- playwright.config.ts
+-- package.json
```

The project should depend on:

```json
{
  "dependencies": {
    "@qagent/playwright-base": "^1.x"
  }
}
```

---

# 12. Test Spec Responsibility

Test specs represent business scenarios.

A spec should read like:

```ts
test('TC01 - Create user successfully', async ({
    userListPage,
    userFormPage
}) => {
    await userListPage.openCreateUser();

    await userFormPage.fillUser(validUser);

    await userFormPage.submit();

    await userListPage.expectUserExists(validUser.email);
});
```

The spec should NOT be a recording of browser actions.

Avoid:

```ts
test('TC01', async ({ page }) => {
    await page.goto('/users');
    await page.getByRole('button', { name: 'Create User' }).click();
    await page.locator('#name').fill('John');
    await page.locator('#email').fill('john@example.com');
    await page.locator('button[type=submit]').click();
    await page.waitForTimeout(2000);
    // ...
});
```

Low-level implementation belongs in Page Objects or supporting infrastructure.

---

# 13. Feature-to-Spec Mapping

Do not force:

```text
1 Feature = 1 spec
```

and do not force:

```text
1 Test Case = 1 spec
```

Instead:

```text
Feature
|
+-- Business flow A
|   +-- TC01
|   +-- TC02
|   +-- TC03
|
+-- Business flow B
    +-- TC04
    +-- TC05
```

may become:

```text
tests/
+-- feature/
    +-- creation.spec.ts
    +-- deletion.spec.ts
```

The grouping should be based on business flow and maintainability.

---

# 14. Page Object Design Rules

Page Objects should contain:

- Locators
- UI interactions
- Navigation
- Component-level behavior
- UI-specific assertions where appropriate

Page Objects should not contain:

- Entire business test cases
- Environment configuration
- Generic logging infrastructure
- Generic screenshot infrastructure
- Large test datasets
- Unrelated helper functions

Use meaningful domain methods:

```ts
await userFormPage.fillUser(user);
await userFormPage.submit();
await userFormPage.expectDuplicateEmailError();
```

instead of exposing every browser action to the spec.

---

# 15. Component Objects

Not everything needs to be a Page Object.

If a UI element is reused across multiple pages, use a Component Object.

Examples:

```text
components/
+-- DataTable.ts
+-- DatePicker.ts
+-- ConfirmationModal.ts
+-- NavigationMenu.ts
```

This prevents Page Objects from becoming unnecessarily large.

---

# 16. Fixtures

Fixtures should provide reusable dependencies.

Example:

```ts
type AppFixtures = {
    userListPage: UserListPage;
    userFormPage: UserFormPage;
};

export const test = base.extend<AppFixtures>({
    userListPage: async ({ page }, use) => {
        await use(new UserListPage(page));
    },

    userFormPage: async ({ page }, use) => {
        await use(new UserFormPage(page));
    }
});
```

Generic fixtures should come from the Base Framework where possible.

Application-specific fixtures should remain in the project.

---

# 17. Authentication

Authentication should not be repeated in every generated Test Case.

Prefer:

```text
Base Framework
    |
    +-- auth state
    +-- authenticated context
```

and:

```text
Application Project
    |
    +-- application-specific login flow if required
```

Generated tests should consume the authenticated context.

Avoid repeatedly generating:

```ts
await page.goto('/login');
await page.fill(...);
await page.fill(...);
await page.click(...);
```

unless the Test Case specifically tests authentication.

---

# 18. Test Data

Move reusable test data out of spec files.

Example:

```text
data/
+-- users.ts
```

```ts
export const validUser = {
    name: 'John Doe',
    email: 'john@example.com'
};
```

Use data factories where dynamic values are required:

```ts
const user = createUserData();
```

Generic factories can belong in the Base Framework.

Application-specific data models belong in the application project.

---

# 19. Incremental Generation

This is one of the most important requirements.

QAgent should support:

```text
Feature A generated
       |
       v
Feature B arrives
       |
       v
Analyze existing project
       |
       +-- Reuse Feature A assets
       |
       +-- Extend existing assets
       |
       +-- Create only missing assets
```

The generator must NOT assume that every Feature starts from an empty project.

The automation repository is a continuously evolving codebase.

---

# 20. Code Ownership

Use the following mental model:

```text
Base Framework
    = generic automation capability

Application Pages
    = application UI knowledge

Fixtures
    = test dependency/setup

Data
    = scenario input

Specs
    = business intent
```

This separation should be reflected in generated code.

---

# 21. Duplicate Detection

Before creating a new file/class/function, QAgent should search for semantically equivalent existing implementations.

Examples:

Existing:

```text
pages/UserPage.ts
```

New requirement asks for:

```text
Create User
```

Do not automatically create:

```text
pages/CreateUserPage.ts
```

First inspect whether `UserPage.ts` already owns user-management interactions.

Likewise:

Existing:

```text
utils/waitForDownload.ts
```

Do not generate another:

```text
helpers/download.ts
```

---

# 22. Locator Reuse

Prefer existing stable locators.

When creating new locators, prefer:

```text
1. data-testid
2. role + accessible name
3. label
4. placeholder
5. stable semantic locator
6. CSS
7. XPath
```

Avoid brittle selectors where possible.

If Browser Harness discovers a better selector than an existing selector, QAgent should consider updating the existing Page Object rather than creating a duplicate locator elsewhere.

---

# 23. Browser Harness and Page Object Relationship

Browser Harness is responsible for discovering:

```text
"How does the application behave?"
```

Page Objects are responsible for encoding:

```text
"How do we reliably automate that behavior?"
```

These are different responsibilities.

Therefore:

```text
Browser Harness
    |
    v
Observed UI behavior
    |
    v
Automation Planner
    |
    v
Existing Page Object?
    |
    +-- Yes --> reuse/extend
    |
    +-- No --> generate Page Object
```

Do not blindly convert every Browser Harness observation into a locator in a spec file.

---

# 24. Automation Planning Intermediate Artifact

Before code generation, QAgent should conceptually create an Automation Plan.

Example:

```json
{
  "feature": "User Management",
  "specGroups": [
    {
      "name": "user-creation",
      "testCases": ["TC01", "TC02", "TC03"]
    }
  ],
  "pages": [
    {
      "name": "UserListPage",
      "action": "reuse"
    },
    {
      "name": "UserFormPage",
      "action": "extend"
    }
  ],
  "fixtures": [
    {
      "name": "authenticatedUser",
      "action": "reuse-base"
    }
  ],
  "data": [
    {
      "name": "userData",
      "action": "create"
    }
  ]
}
```

This plan does not have to be persisted as a public file initially, but the generator architecture should have an equivalent internal representation.

This separation makes generation more deterministic and easier to validate.

---

# 25. Generation Rules

The generator must follow these rules:

### Rule 1

Never generate a large monolithic spec when the Feature contains reusable flows.

### Rule 2

Never duplicate an existing Page Object without first analyzing it.

### Rule 3

Never regenerate generic infrastructure that belongs to `@qagent/playwright-base`.

### Rule 4

Never place reusable UI interactions directly inside multiple specs.

### Rule 5

Never place large reusable test data directly inside multiple specs.

### Rule 6

Prefer extending an existing abstraction over creating a parallel abstraction.

### Rule 7

Keep business intent visible in the Test Spec.

### Rule 8

Use Browser Harness observations as input, not as the final source-code structure.

### Rule 9

Generation must be incremental and aware of the existing repository.

### Rule 10

Generated code must compile and be executable.

---

# 26. Validation

After generation, QAgent should validate:

```text
[ ] All Test Cases are represented
[ ] No Test Case was accidentally dropped
[ ] Existing Page Objects were considered
[ ] Existing fixtures were considered
[ ] Existing utilities were considered
[ ] No obvious duplicate Page Objects exist
[ ] No obvious duplicate utilities exist
[ ] Imports are valid
[ ] TypeScript compiles
[ ] Playwright tests can execute
[ ] Authentication works
[ ] Evidence collection works
[ ] Assertions preserve original intent
```

Where possible, execute generated tests through the existing Browser Harness / Playwright execution pipeline.

---

# 27. Migration Strategy

Do not rewrite the entire QAgent architecture in one step.

Recommended migration:

## Phase 1 — Define Base Framework

Extract generic code from the current generated specs:

```text
auth
fixtures
utils
evidence
logging
config
```

Create:

```text
@qagent/playwright-base
```

## Phase 2 — Add Existing Project Analysis

Before generation, inspect:

```text
pages/
fixtures/
data/
utils/
```

## Phase 3 — Introduce Reuse/Extend/Create Planning

Add a planning step before code generation.

## Phase 4 — Generate Page Objects

Move reusable UI interaction out of specs.

## Phase 5 — Generate Feature Specs

Generate business-focused specs.

## Phase 6 — Add Incremental Generation

Ensure a second Feature can reuse the first Feature's automation assets.

## Phase 7 — Validation

Add compilation, structural, duplication, and execution validation.

---

# 28. Example: Before vs After

## Before

```text
Feature: User Management

user-management.spec.ts
|
+-- login()
+-- createUser()
+-- deleteUser()
+-- locators
+-- helper functions
+-- test data
+-- TC01
+-- TC02
+-- TC03
+-- TC04
```

When Feature 2 arrives, QAgent generates another large file and duplicates:

```text
login()
waitForSomething()
generateRandomEmail()
takeEvidence()
```

## After

```text
@qagent/playwright-base
|
+-- auth
+-- evidence
+-- utils
+-- fixtures
|
+-- Application Project
    |
    +-- pages/
    |   +-- LoginPage.ts
    |   +-- UserListPage.ts
    |   +-- UserFormPage.ts
    |
    +-- components/
    |
    +-- fixtures/
    |
    +-- data/
    |
    +-- tests/
        +-- user-creation.spec.ts
        +-- user-deletion.spec.ts
```

When Feature 2 arrives:

```text
QAgent
  |
  +-- reuse LoginPage
  +-- reuse auth fixture
  +-- reuse evidence utility
  +-- reuse UserListPage
  +-- extend UserPage if necessary
  +-- create OrderPage
  +-- create order specs
```

Only genuinely new application behavior is generated.

---

# 29. Senior Automation QA Principles

The generator should follow these principles:

### Single Responsibility

Each layer has one responsibility.

### DRY

Do not duplicate stable automation behavior.

### Explicit Business Intent

A reviewer should understand what a test verifies without reading locator implementation.

### Stable Locators

Prefer resilient selectors.

### Incremental Architecture

The project should become more reusable as more Features are automated.

### Controlled Abstraction

Abstract meaningful application concepts, not individual browser actions.

### Deterministic Generation

Planning should happen before code generation.

### Existing Code Is a First-Class Input

The repository is part of the context, not merely the output destination.

---

# 30. Final Architecture Principle

The most important change is conceptual.

QAgent should move from:

```text
"Explore the browser and write a Playwright script."
```

to:

```text
"Explore the application, understand the existing automation
architecture, plan the smallest set of changes required, reuse
existing assets, and generate maintainable automation code."
```

The desired behavior is:

```text
                    NEW FEATURE
                        |
                        v
                Browser Harness
                        |
                        v
               Exploration Model
                        |
                        v
              Existing Project Scan
                        |
                        v
             +----------------------+
             | Automation Planning  |
             +----------+-----------+
                        |
              +---------+---------+
              |         |         |
            REUSE     EXTEND    CREATE
              |         |         |
              +---------+---------+
                        |
                        v
              Generate / Modify
                        |
                        v
                 Test Specs
                        |
                        v
              Compile + Execute
                        |
                        v
                    Evidence
```

The success metric is not:

> "How quickly can QAgent generate a Playwright file?"

The success metric is:

> **"How little new code does QAgent need to generate while still fully covering the new Test Cases?"**

A mature QAgent automation project should become **more reusable over time**, not larger through repeated duplication.
