# Example — automation-generator

## Input (one approved manual test case + Project Knowledge Base)

Knowledge Base (excerpt) says: Playwright / TypeScript; locator priority `data-testid` →
`getByRole`; base URL `https://app.example.com`; route `/invoices/:id` (Invoice detail);
auth handled by the run's saved manual-login session.

**TC-2481-001 — Pay an open invoice (happy path)**
- Preconditions: Logged in as Agent; invoice `INV-1001` is in status *Open*.
- Steps:
  1. Open invoice `INV-1001`.
  2. Click **Pay now**.
  3. Confirm payment.
- Expected Results:
  - Confirmation banner is shown.
  - Invoice status becomes *Paid*.

## Expected Output — `tests/ADO-2481/ADO-2481-TC-2481-001.spec.ts`

No page object exists for the invoice screen yet and no reference spec imports one, so the locators
stay inline **this once** — an invented `../../pages/InvoiceListPage` import would fail collection and
be rejected. A later stage extracts them.

```ts
/**
 * Source ticket : ADO-2481
 * Test Case ID  : TC-2481-001
 * Reused        : @q-agent/playwright-base (test + evidence + saved session)
 */
import { test, expect } from '@q-agent/playwright-base';

test('TC-2481-001 — Pay an open invoice', async ({ page }) => {
  // Arrange — already authenticated by the run's saved session; no login here.
  await page.goto('https://app.example.com/invoices/INV-1001');

  // Act
  await page.getByTestId('pay-now').click();
  await page.getByRole('button', { name: 'Confirm payment' }).click();

  // Assert — one web-first assertion per Expected Result
  await expect(page.getByTestId('confirmation-banner')).toBeVisible();
  await expect(page.getByTestId('invoice-status')).toHaveText('Paid');
});
```

## Expected Output when a page object DOES exist

If a reference spec from this project shows `import { InvoiceListPage } from '../../pages/InvoiceListPage';`,
that file is proven to exist — reuse it, and the spec becomes business steps only:

```ts
import { test, expect } from '@q-agent/playwright-base';
import { InvoiceListPage } from '../../pages/InvoiceListPage';

test('TC-2481-001 — Pay an open invoice', async ({ page }) => {
  const invoices = new InvoiceListPage(page);
  await invoices.open('INV-1001');

  await invoices.pay('INV-1001');

  await expect(invoices.confirmationBanner).toBeVisible();
  await expect(invoices.statusBadge('INV-1001')).toHaveText('Paid');
});
```

## Notes on discipline
- One `test()` per case, plain-string title prefixed with the Test Case ID, no `test.describe`-only file.
- `test`/`expect` come from `@q-agent/playwright-base`, never `@playwright/test`.
- No login is re-scripted per spec — the saved manual-login session authenticates it.
- Imports of shared project files use the real depth (`../../pages/…`) and only when proven to exist.
