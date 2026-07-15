// E2E for the tenant routing-config admin card (#62) on AdminTenantDetail.
//
// Mocked dev-server run (no live backend): admin session seeded, all detail-page
// endpoints stubbed. Proves the real browser render of the routing-config card:
// the empty state when unconfigured, and the summary (chain / allowlist /
// fallback) after a config is present.

import { expect, test, type Page } from '@playwright/test'

const TENANT_ID = 'acme-eng'

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

async function mockDetailRoutes(page: Page) {
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
    route.fulfill({
      json: {
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
      },
    }),
  )
  await page.route(`**/api/mvp/admin/tenants/${TENANT_ID}`, (route) =>
    route.fulfill({
      json: {
        tenant_id: TENANT_ID,
        name: 'Acme Eng',
        team_lead_user_id: 'admin-owned',
        default_credit: 100_000,
        status: 'active',
        created_at: '2026-01-01T00:00:00Z',
        updated_at: '2026-01-01T00:00:00Z',
        created_by: 'admin-1',
      },
    }),
  )
  await page.route(`**/api/mvp/admin/tenants/${TENANT_ID}/users`, (route) =>
    route.fulfill({ json: { tenant_id: TENANT_ID, members: [] } }),
  )
  await page.route(`**/api/mvp/admin/tenants/${TENANT_ID}/usage**`, (route) =>
    route.fulfill({
      json: {
        tenant_id: TENANT_ID,
        total_tokens: 0,
        input_tokens: 0,
        output_tokens: 0,
        by_model: {},
        by_user: {},
        sample_size: 0,
      },
    }),
  )
  // No pool budget for these tests (404 => empty state; not under assertion).
  await page.route(`**/api/mvp/admin/tenants/${TENANT_ID}/pool-budget**`, (route) =>
    route.request().method() === 'GET'
      ? route.fulfill({ status: 404, json: { detail: 'none' } })
      : route.continue(),
  )
}

test.describe('tenant routing-config admin card (#62)', () => {
  test('shows the empty state when routing is unconfigured', async ({ page }) => {
    await seedAdminSession(page)
    await mockDetailRoutes(page)
    await page.route(
      `**/api/mvp/admin/tenants/${TENANT_ID}/routing-config`,
      (route) =>
        route.fulfill({
          json: {
            tenant_id: TENANT_ID,
            configured: false,
            allowlist: [],
            chain: [],
            quotas: {},
            fallback_mode: 'loud',
            fallback_default: 'off',
            free_tier_model: null,
          },
        }),
    )

    await page.goto(`/admin/tenants/${TENANT_ID}`)

    await expect(page.getByTestId('routing-config-card')).toBeVisible()
    await expect(page.getByTestId('routing-config-empty')).toBeVisible()
  })

  test('renders the chain / allowlist / fallback summary when configured', async ({
    page,
  }) => {
    await seedAdminSession(page)
    await mockDetailRoutes(page)
    await page.route(
      `**/api/mvp/admin/tenants/${TENANT_ID}/routing-config`,
      (route) =>
        route.fulfill({
          json: {
            tenant_id: TENANT_ID,
            configured: true,
            allowlist: ['claude-opus-4-7', 'claude-haiku-4-5'],
            chain: ['claude-opus-4-7', 'claude-haiku-4-5'],
            quotas: { 'claude-opus-4-7': { unit: 'usd_micro', limit: 500, period: 'monthly' } },
            fallback_mode: 'loud',
            fallback_default: 'on',
            free_tier_model: null,
          },
        }),
    )

    await page.goto(`/admin/tenants/${TENANT_ID}`)

    const summary = page.getByTestId('routing-config-summary')
    await expect(summary).toBeVisible()
    // Chain rendered with the arrow join.
    await expect(summary.getByText('claude-opus-4-7 → claude-haiku-4-5')).toBeVisible()
    // Fallback default surfaced.
    await expect(summary.getByText(/^on$/)).toBeVisible()
  })
})
