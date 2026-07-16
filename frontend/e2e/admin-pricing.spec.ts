// E2E for the read-only pricing-config admin page (#66).
//
// Runs against the dev server with no live backend: an admin session is seeded
// into sessionStorage (P0-7 token model) and every API call the page makes is
// fulfilled by page.route mocks. Proves the real browser render of the
// /admin/pricing page: the effective rate table, full-precision $/MTok display,
// the models column, and the default vs override source label.

import { expect, test, type Page } from '@playwright/test'

function seedAdminSession(page: Page) {
  return page.addInitScript(() => {
    const tokens = {
      access_token: 'e2e-fake-access-token',
      id_token: 'e2e-fake-id-token',
      refresh_token: null,
      expires_at: Date.now() + 24 * 60 * 60 * 1000,
    }
    window.sessionStorage.setItem('stratoclave_tokens', JSON.stringify(tokens))
    window.sessionStorage.setItem('stratoclave_locale', 'en')
  })
}

function meResponse() {
  return {
    user_id: 'admin-1',
    email: 'admin@example.com',
    org_id: 'default-org',
    roles: ['admin'],
    total_credit: 1_000_000,
    credit_used: 0,
    remaining_credit: 1_000_000,
    currency: 'tokens',
    tenant: { tenant_id: 'default-org', name: 'Default' },
    locale: 'en',
  }
}

async function mockConfigAndMe(page: Page) {
  await page.route('**/config.json', (route) =>
    route.fulfill({
      json: {
        api: { endpoint: '' },
        cognito: {
          client_id: 'e2e-client-id',
          domain: 'https://e2e.auth.us-east-1.amazoncognito.com',
          user_pool_id: 'us-east-1_e2epool',
          region: 'us-east-1',
        },
      },
    }),
  )
  await page.route('**/api/mvp/me', (route) =>
    route.fulfill({ json: meResponse() }),
  )
}

test.describe('admin pricing view', () => {
  test('renders the effective rate table with $/MTok, models, and source', async ({
    page,
  }) => {
    await seedAdminSession(page)
    await mockConfigAndMe(page)
    await page.route('**/api/mvp/admin/pricing-config', (route) =>
      route.fulfill({
        json: {
          version: null,
          rates: [
            {
              pricing_key: 'opus',
              input_per_mtok_microusd: 5_000_000,
              output_per_mtok_microusd: 25_000_000,
              cache_read_per_mtok_microusd: 500_000,
              cache_write_per_mtok_microusd: 6_250_000,
              source: 'default',
              models: ['claude-opus-4-7', 'claude-opus-4-6'],
            },
          ],
        },
      }),
    )

    await page.goto('/admin/pricing')

    // Page heading + built-in-defaults version line (the phrase also appears
    // in the intro copy, so assert at least one match rather than a unique one).
    await expect(page.getByRole('heading', { name: /pricing/i })).toBeVisible()
    await expect(page.getByText(/Built-in defaults/i).first()).toBeVisible()

    // The row: pricing key, a mapped model, and the $5 input rate (full
    // precision, trailing zeros trimmed -> "$5", not "$5.00").
    const row = page.getByRole('row', { name: /opus/ })
    await expect(row).toBeVisible()
    await expect(row.getByText('$5', { exact: true })).toBeVisible()
    await expect(row.getByText('$25', { exact: true })).toBeVisible()
    await expect(page.getByText('claude-opus-4-7, claude-opus-4-6')).toBeVisible()
    // Default source label (not the override badge).
    await expect(row.getByText(/^default$/i)).toBeVisible()
  })

  test('shows the override badge + version when a rate is customized', async ({
    page,
  }) => {
    await seedAdminSession(page)
    await mockConfigAndMe(page)
    await page.route('**/api/mvp/admin/pricing-config', (route) =>
      route.fulfill({
        json: {
          version: 'v2026-07',
          rates: [
            {
              pricing_key: 'haiku',
              // A deliberately sub-cent-precise override to prove the
              // full-precision formatter (would be $0.08 under the cents
              // rounder): 75_000 micro-USD/MTok -> "$0.075".
              input_per_mtok_microusd: 75_000,
              output_per_mtok_microusd: 6_000_000,
              cache_read_per_mtok_microusd: 200_000,
              cache_write_per_mtok_microusd: 2_500_000,
              source: 'override',
              models: ['claude-haiku-4-5'],
            },
          ],
        },
      }),
    )

    await page.goto('/admin/pricing')

    const row = page.getByRole('row', { name: /haiku/ })
    await expect(row).toBeVisible()
    // Override badge present.
    await expect(row.getByText(/^override$/i)).toBeVisible()
    // Sub-cent rate is shown exactly, NOT rounded to $0.00 or $0.08.
    await expect(row.getByText('$0.075', { exact: true })).toBeVisible()
    // The override version appears in the card header.
    await expect(page.getByText(/v2026-07/)).toBeVisible()
  })

  test('shows a load error when the endpoint fails', async ({ page }) => {
    await seedAdminSession(page)
    await mockConfigAndMe(page)
    await page.route('**/api/mvp/admin/pricing-config', (route) =>
      route.fulfill({ status: 500, json: { detail: 'boom' } }),
    )

    await page.goto('/admin/pricing')
    await expect(page.getByText(/Failed to load pricing config/i)).toBeVisible()
  })
})
