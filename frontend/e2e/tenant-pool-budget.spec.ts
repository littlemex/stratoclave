// E2E for the tenant dollar pool-budget admin UI (A-1).
//
// Runs against the dev server with no live backend: an admin session is
// seeded into sessionStorage (the P0-7 token model) and every API call the
// AdminTenantDetail page makes is fulfilled by `page.route` mocks. The pool
// endpoints are stateful within a test so we can prove the round trip:
//
//   1. A tenant with no pool renders the "no pool budget" empty state.
//   2. Opening the dialog, typing "$500", and saving PUTs limit_usd_cents
//      = 50000 (dollar string parsed to integer cents, no float) and the
//      card then shows the $500.00 ceiling returned by the mock.
//
// This is the request-time control a credential broker cannot offer, so it
// is worth guarding end to end.

import { expect, test, type Page } from '@playwright/test'

const TENANT_ID = 'acme-eng'

// A far-future expiry so AuthContext's 5-minute refresh margin never trips
// and no refresh_token round trip is attempted.
function seedAdminSession(page: Page) {
  return page.addInitScript(() => {
    const tokens = {
      access_token: 'e2e-fake-access-token',
      id_token: 'e2e-fake-id-token',
      refresh_token: null,
      expires_at: Date.now() + 24 * 60 * 60 * 1000,
    }
    window.sessionStorage.setItem('stratoclave_tokens', JSON.stringify(tokens))
    // Pin the locale so assertions can match English copy deterministically.
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

function tenantResponse() {
  return {
    tenant_id: TENANT_ID,
    name: 'Acme Eng',
    team_lead_user_id: 'admin-owned',
    default_credit: 100_000,
    status: 'active',
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    created_by: 'admin-1',
  }
}

function poolResponse(limitMicro: number, settledMicro = 0) {
  const remaining = limitMicro - settledMicro
  return {
    tenant_id: TENANT_ID,
    period: '2026-07',
    status: 'active',
    pool_limit_microusd: limitMicro,
    pool_reserved_microusd: 0,
    pool_settled_microusd: settledMicro,
    remaining_microusd: remaining,
    pool_limit_usd_cents: Math.round(limitMicro / 10_000),
    remaining_usd_cents: Math.round(remaining / 10_000),
  }
}

// Wire the common read endpoints the detail page always calls. Pool routing
// is registered per-test because its behaviour differs by scenario.
async function mockCommonRoutes(page: Page) {
  // The SPA fetches /config.json on cold start; without a valid one it shows
  // the bilingual "Configuration load failed" splash and never mounts React.
  // api.endpoint = '' makes the app use window.location.origin, so the
  // **/api/mvp/** routes below match.
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
  await page.route(
    `**/api/mvp/admin/tenants/${TENANT_ID}`,
    (route) => route.fulfill({ json: tenantResponse() }),
  )
  await page.route(
    `**/api/mvp/admin/tenants/${TENANT_ID}/users`,
    (route) => route.fulfill({ json: { tenant_id: TENANT_ID, members: [] } }),
  )
  await page.route(
    `**/api/mvp/admin/tenants/${TENANT_ID}/usage**`,
    (route) =>
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
}

test.describe('tenant pool budget admin UI', () => {
  test('shows the empty state when the tenant has no pool', async ({ page }) => {
    await seedAdminSession(page)
    await mockCommonRoutes(page)
    await page.route(
      `**/api/mvp/admin/tenants/${TENANT_ID}/pool-budget**`,
      (route) => {
        // GET with no pool set -> 404 (the UI treats this as "no pool").
        if (route.request().method() === 'GET') {
          return route.fulfill({
            status: 404,
            json: { detail: 'No pool budget set' },
          })
        }
        return route.continue()
      },
    )

    await page.goto(`/admin/tenants/${TENANT_ID}`)

    const card = page.getByTestId('pool-budget-card')
    await expect(card).toBeVisible()
    await expect(page.getByTestId('pool-budget-empty')).toBeVisible()
    await expect(
      page.getByRole('button', { name: /set pool budget/i }),
    ).toBeVisible()
  })

  test('sets a $500 ceiling and reflects it on the card', async ({ page }) => {
    await seedAdminSession(page)
    await mockCommonRoutes(page)

    // Stateful pool mock: 404 until a PUT lands, then the stored pool.
    let stored: ReturnType<typeof poolResponse> | null = null
    let capturedPutBody: Record<string, unknown> | null = null

    await page.route(
      `**/api/mvp/admin/tenants/${TENANT_ID}/pool-budget**`,
      (route) => {
        const method = route.request().method()
        if (method === 'PUT') {
          capturedPutBody = route.request().postDataJSON()
          const cents = Number(capturedPutBody?.limit_usd_cents ?? 0)
          stored = poolResponse(cents * 10_000)
          return route.fulfill({ json: stored })
        }
        // GET
        if (stored) return route.fulfill({ json: stored })
        return route.fulfill({ status: 404, json: { detail: 'none' } })
      },
    )

    await page.goto(`/admin/tenants/${TENANT_ID}`)

    // Empty state first.
    await expect(page.getByTestId('pool-budget-empty')).toBeVisible()

    // Open the dialog and enter "$500".
    await page.getByTestId('pool-budget-set-button').click()
    const amount = page.getByTestId('pool-limit-usd-input')
    await expect(amount).toBeVisible()
    await amount.fill('$500')
    await page.getByTestId('pool-budget-submit').click()

    // The card now shows the ceiling; the summary replaces the empty state.
    await expect(page.getByTestId('pool-budget-summary')).toBeVisible()
    await expect(page.getByTestId('pool-limit')).toHaveText('$500.00')
    await expect(page.getByTestId('pool-remaining')).toHaveText('$500.00')

    // The PUT sent integer cents, not a float — 500 USD == 50000 cents.
    expect(capturedPutBody).not.toBeNull()
    expect(capturedPutBody?.limit_usd_cents).toBe(50000)
  })

  test('blocks submission on a sub-cent amount', async ({ page }) => {
    await seedAdminSession(page)
    await mockCommonRoutes(page)
    await page.route(
      `**/api/mvp/admin/tenants/${TENANT_ID}/pool-budget**`,
      (route) =>
        route.request().method() === 'GET'
          ? route.fulfill({ status: 404, json: { detail: 'none' } })
          : route.continue(),
    )

    await page.goto(`/admin/tenants/${TENANT_ID}`)
    await page.getByTestId('pool-budget-set-button').click()
    await page.getByTestId('pool-limit-usd-input').fill('1.234')

    // Validation message shows and the submit button is disabled.
    await expect(page.getByText(/valid dollar amount/i)).toBeVisible()
    await expect(page.getByTestId('pool-budget-submit')).toBeDisabled()
  })
})
