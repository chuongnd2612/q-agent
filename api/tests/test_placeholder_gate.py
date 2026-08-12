"""Tests for the placeholder / flaky-pattern static spec gate (#181)."""

from __future__ import annotations

from app.services import placeholder_gate


def _clean_spec(body: str = "") -> str:
    return (
        "import { test, expect } from '@playwright/test';\n\n"
        "test('TC-01 — Login works', async ({ page }) => {\n"
        f"{body}"
        "  await page.goto('/login');\n"
        "  await expect(page.getByTestId('login-btn')).toBeVisible();\n"
        "});\n"
    )


def test_find_flaky_patterns_clean_spec_is_empty():
    assert placeholder_gate.find_flaky_patterns(_clean_spec()) == []


def test_find_flaky_patterns_flags_hard_wait():
    code = _clean_spec("  await page.waitForTimeout(2000);\n")
    findings = placeholder_gate.find_flaky_patterns(code)
    assert any("waitForTimeout" in f for f in findings)


def test_find_flaky_patterns_flags_zero_assertions():
    code = (
        "import { test } from '@playwright/test';\n\n"
        "test('TC-01', async ({ page }) => {\n"
        "  await page.goto('/login');\n"
        "});\n"
    )
    findings = placeholder_gate.find_flaky_patterns(code)
    assert any("zero assertions" in f for f in findings)


def test_find_flaky_patterns_flags_brittle_class_selector():
    code = _clean_spec("  await page.locator('.btn-primary').click();\n")
    findings = placeholder_gate.find_flaky_patterns(code)
    assert any("brittle CSS locator" in f and ".btn-primary" in f for f in findings)


def test_find_flaky_patterns_flags_nth_child_selector():
    code = _clean_spec("  await page.locator('table tr:nth-child(2) td').click();\n")
    findings = placeholder_gate.find_flaky_patterns(code)
    assert any("brittle CSS locator" in f for f in findings)


def test_find_flaky_patterns_does_not_flag_data_testid_or_id():
    code = _clean_spec(
        "  await page.locator('#user-menu').click();\n"
        "  await page.locator('[data-testid=\"submit\"]').click();\n"
    )
    assert placeholder_gate.find_flaky_patterns(code) == []


def test_gate_spec_rejects_hard_wait_regardless_of_grounding():
    """Flaky-pattern findings are always a rejection — a genuine code defect the
    model should fix, not a missing-KB-input situation — even with no KB at all."""
    code = _clean_spec("  await page.waitForTimeout(500);\n")
    result = placeholder_gate.gate_spec(code, {})
    assert result["outcome"] == "rejected"
    assert any("waitForTimeout" in f for f in result["findings"])


def test_gate_spec_rejects_zero_assertions_even_with_full_grounding():
    code = (
        "import { test } from '@playwright/test';\n\n"
        "test('TC-01', async ({ page }) => {\n"
        "  await page.goto('/login');\n"
        "});\n"
    )
    grounded = {"routes": [{"path": "/login"}], "selectors": ["#user"], "base_url": "https://x"}
    result = placeholder_gate.gate_spec(code, grounded)
    assert result["outcome"] == "rejected"
    assert any("zero assertions" in f for f in result["findings"])


def test_gate_spec_passes_clean_grounded_spec():
    grounded = {
        "routes": [{"path": "/login"}],
        "selectors": [{"selector": "login-btn"}],
        "base_url": "https://x",
    }
    result = placeholder_gate.gate_spec(_clean_spec(), grounded)
    assert result["outcome"] == "passed"


def test_gate_spec_verified_entries_count_as_grounding():
    """A spec grounded ONLY on ``verified_at_runtime`` entries passes — the extra
    stamp keys must not stop routes/selectors from being recognized as known (#329)."""
    grounded = {
        "routes": [
            {"path": "/login", "verified_at_runtime": "2026-07-15T00:00:00Z", "source": "exploration"}
        ],
        "selectors": [
            {
                "selector": "login-btn",
                "strategy": "data-testid",
                "verified_at_runtime": "2026-07-15T00:00:00Z",
                "source": "exploration",
            }
        ],
        "base_url": "https://x",
    }
    result = placeholder_gate.gate_spec(_clean_spec(), grounded)
    assert result["outcome"] == "passed"


# --- template-literal goto() targets (parameterized routes) --------------------
# A goto() built from grounded constants — `${BASE_URL}/employers/${ID}/...` — is
# the idiomatic way to parameterize a route. It must not be mistaken for an
# invented reference just because the raw string can't string-equal a concrete KB
# route (the false positive that stuck specs in a regenerate loop).

_PARAM_KNOWN = {
    "routes": [{"path": "/employers/86923fff/groups/e1b71606"}],
    "selectors": ["brokers-tab"],
    "base_url": "https://portal.example.net",
}


def _param_spec(goto: str) -> str:
    return (
        "import { test, expect } from '@playwright/test';\n"
        "const BASE_URL = 'https://portal.example.net';\n"
        "const EMPLOYER_ID = '86923fff';\nconst GROUP_ID = 'e1b71606';\n"
        "test('TC-01', async ({ page }) => {\n"
        f"  await page.goto({goto});\n"
        "  await expect(page.getByTestId('brokers-tab')).toBeVisible();\n"
        "});\n"
    )


def test_gate_spec_passes_parameterized_template_route():
    code = _param_spec("`${BASE_URL}/employers/${EMPLOYER_ID}/groups/${GROUP_ID}`")
    assert placeholder_gate.gate_spec(code, _PARAM_KNOWN)["outcome"] == "passed"


def test_gate_spec_passes_plain_absolute_url_with_base():
    code = _param_spec("'https://portal.example.net/employers/86923fff/groups/e1b71606'")
    assert placeholder_gate.gate_spec(code, _PARAM_KNOWN)["outcome"] == "passed"


def test_gate_spec_rejects_invented_static_segment_in_template():
    code = _param_spec("`${BASE_URL}/totally-made-up-screen`")
    result = placeholder_gate.gate_spec(code, _PARAM_KNOWN)
    assert result["outcome"] == "rejected"
    assert any("made-up-screen" in f for f in result["findings"])


def test_gate_spec_rejects_wrong_shape_parameterized_route():
    code = _param_spec("`${BASE_URL}/vendors/${EMPLOYER_ID}`")
    assert placeholder_gate.gate_spec(code, _PARAM_KNOWN)["outcome"] == "rejected"


# --------------------------------------------------------------- layered specs (#542)

_LAYERED_KNOWN = {
    "routes": [{"path": "/invoices/INV-1001"}],
    "selectors": [{"selector": "pay-now"}, {"selector": "confirmation-banner"}],
    "base_url": "https://app.example.com",
}


def _layered_spec(extra_imports: str = "") -> str:
    """A spec in the shape #542 asks for: base-package import, no inline login."""
    return (
        "import { test, expect } from '@q-agent/playwright-base';\n"
        f"{extra_imports}"
        "\ntest('TC-01 — Pay an open invoice', async ({ page }) => {\n"
        "  await page.goto('/invoices/INV-1001');\n"
        "  await page.getByTestId('pay-now').click();\n"
        "  await expect(page.getByTestId('confirmation-banner')).toBeVisible();\n"
        "});\n"
    )


def test_gate_spec_passes_base_package_import():
    """The now-mandatory import must not read as an invented reference."""
    assert placeholder_gate.gate_spec(_layered_spec(), _LAYERED_KNOWN)["outcome"] == "passed"


def test_gate_spec_passes_project_asset_imports():
    """`../../pages` / `../../fixtures` / `../../data` are legitimate in a layered spec."""
    extra = (
        "import { InvoiceListPage } from '../../pages/InvoiceListPage';\n"
        "import { appTest } from '../../fixtures/app.fixture';\n"
        "import { validUser } from '../../data/users';\n"
    )
    assert placeholder_gate.gate_spec(_layered_spec(extra), _LAYERED_KNOWN)["outcome"] == "passed"


def test_allowed_imports_cover_the_layered_shape():
    assert placeholder_gate.is_allowed_import("@q-agent/playwright-base")
    assert placeholder_gate.is_allowed_import("@playwright/test")
    assert placeholder_gate.is_allowed_import("../../pages/LoginPage")
    assert placeholder_gate.is_allowed_import("../../fixtures/app.fixture")
    assert not placeholder_gate.is_allowed_import("@faker-js/faker")


def test_import_specifiers_extracts_every_form():
    code = (
        "import { test } from '@q-agent/playwright-base';\n"
        "import '../../config/setup';\n"
        "const p = require('../../utils/paths');\n"
    )
    assert placeholder_gate.import_specifiers(code) == [
        "@q-agent/playwright-base",
        "../../config/setup",
        "../../utils/paths",
    ]


def test_base_assertion_helpers_count_as_assertions():
    """A spec asserting only via the base package's helpers is not "zero assertions"."""
    code = (
        "import { test, expectVisible, expectUrl } from '@q-agent/playwright-base';\n\n"
        "test('TC-01 — Pay an open invoice', async ({ page }) => {\n"
        "  await page.goto('/invoices/INV-1001');\n"
        "  await expectVisible(page.getByTestId('confirmation-banner'));\n"
        "  await expectUrl(page, '/invoices/INV-1001');\n"
        "});\n"
    )
    assert placeholder_gate.count_assertions(code) >= 2
    assert placeholder_gate.find_flaky_patterns(code) == []
    assert placeholder_gate.gate_spec(code, _LAYERED_KNOWN)["outcome"] == "passed"
