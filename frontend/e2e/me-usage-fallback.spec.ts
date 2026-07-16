// E2E for the P0-11 fallback visibility on the My-Usage page (#65).
//
// Mocked dev-server run (no live backend): a user session is seeded and the
// usage-summary / usage-history endpoints are stubbed. Proves the real browser
// render of the fallback badge on a cascaded row, the summary fallback count,
// and — critically — that a legacy row (fallback_occurred null) shows NO badge.

import { expect, test, type Page } from '@playwright/test'

function seedSession(page: Page) {
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
    route.fulfill({
      json: {
        user_id: 'u1',
        email: 'u1@example.com',
        org_id: 'default-org',
        roles: ['user'],
        total_credit: 1_000_000,
        credit_used: 100,
        remaining_credit: 999_900,
        currency: 'tokens',
        tenant: { tenant_id: 'default-org', name: 'Default' },
        locale: 'en',
      },
    }),
  )
}

test.describe('my-usage P0-11 fallback visibility', () => {
  test('shows the fallback badge + summary count on a cascaded row', async ({
    page,
  }) => {
    await seedSession(page)
    await mockConfigAndMe(page)
    await page.route('**/api/mvp/me/usage-summary**', (route) =>
      route.fulfill({
        json: {
          tenant_id: 'default-org',
          total_credit: 1_000_000,
          credit_used: 100,
          remaining_credit: 999_900,
          by_model: { 'us.anthropic.claude-haiku-4-5': 100 },
          by_tenant: { 'default-org': 100 },
          sample_size: 1,
          since_days: 30,
          fallback_count: 1,
        },
      }),
    )
    await page.route('**/api/mvp/me/usage-history**', (route) =>
      route.fulfill({
        json: {
          history: [
            {
              tenant_id: 'default-org',
              tenant_name: 'Default',
              model_id: 'us.anthropic.claude-haiku-4-5',
              input_tokens: 60,
              output_tokens: 40,
              total_tokens: 100,
              recorded_at: '2026-04-20T12:00:00Z',
              requested_model_id: 'us.anthropic.claude-opus-4-7',
              fallback_occurred: true,
            },
          ],
          next_cursor: null,
        },
      }),
    )

    await page.goto('/me/usage')

    // The effective model row renders...
    await expect(page.getByText(/claude-haiku-4-5/).first()).toBeVisible()
    // ...with a fallback badge (en copy: "fallback").
    await expect(page.getByText(/^fallback$/i)).toBeVisible()
    // ...and the summary reports the count.
    await expect(page.getByText(/served by a fallback model/i)).toBeVisible()
  })

  test('legacy row (fallback_occurred null) shows NO badge', async ({ page }) => {
    await seedSession(page)
    await mockConfigAndMe(page)
    await page.route('**/api/mvp/me/usage-summary**', (route) =>
      route.fulfill({
        json: {
          tenant_id: 'default-org',
          total_credit: 1_000_000,
          credit_used: 100,
          remaining_credit: 999_900,
          by_model: { 'us.anthropic.claude-opus-4-7': 100 },
          by_tenant: { 'default-org': 100 },
          sample_size: 1,
          since_days: 30,
          // no fallback_count -> no summary line
        },
      }),
    )
    await page.route('**/api/mvp/me/usage-history**', (route) =>
      route.fulfill({
        json: {
          history: [
            {
              tenant_id: 'default-org',
              tenant_name: 'Default',
              model_id: 'us.anthropic.claude-opus-4-7',
              input_tokens: 60,
              output_tokens: 40,
              total_tokens: 100,
              recorded_at: '2026-04-20T12:00:00Z',
              // legacy shape: no requested_model_id, no fallback_occurred
            },
          ],
          next_cursor: null,
        },
      }),
    )

    await page.goto('/me/usage')

    await expect(page.getByText(/claude-opus-4-7/).first()).toBeVisible()
    // No fallback badge and no summary line for a legacy/unknown row.
    await expect(page.getByText(/^fallback$/i)).toHaveCount(0)
    await expect(page.getByText(/served by a fallback model/i)).toHaveCount(0)
  })
})
