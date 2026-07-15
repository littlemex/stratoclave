// E2E for the admin API-key revoke UI (#1) on AdminUserDetail.
//
// Mocked dev-server run: admin session seeded, all detail-page endpoints
// stubbed. Proves the real browser flow — list a user's keys, click Revoke ->
// confirm dialog -> DELETE .../by-key-id/{key_id} -> the row flips to REVOKED
// (via query invalidation refetch). A revoked key shows a badge, no button.

import { expect, test, type Page } from '@playwright/test'

const USER_ID = 'u-1'
const ACTIVE_KEY = 'sk-stratoclave-AbCdXyz9'
const REVOKED_KEY = 'sk-stratoclave-DeAdbEEf'

function seedAdminSession(page: Page) {
  return page.addInitScript(() => {
    window.sessionStorage.setItem(
      'stratoclave_tokens',
      JSON.stringify({
        access_token: 'e2e-fake',
        id_token: 'e2e-fake',
        refresh_token: null,
        expires_at: Date.now() + 24 * 60 * 60 * 1000,
      }),
    )
    window.sessionStorage.setItem('stratoclave_locale', 'en')
  })
}

async function mockDetailRoutes(page: Page) {
  await page.route('**/config.json', (route) =>
    route.fulfill({
      json: {
        api: { endpoint: '' },
        cognito: {
          client_id: 'e2e',
          domain: 'https://e2e.auth.us-east-1.amazoncognito.com',
          user_pool_id: 'us-east-1_e2e',
          region: 'us-east-1',
        },
      },
    }),
  )
  await page.route('**/api/mvp/me', (route) =>
    route.fulfill({
      json: {
        user_id: 'admin-1', email: 'admin@example.com', org_id: 'default-org',
        roles: ['admin'], total_credit: 1_000_000, credit_used: 0,
        remaining_credit: 1_000_000, currency: 'tokens',
        tenant: { tenant_id: 'default-org', name: 'Default' }, locale: 'en',
      },
    }),
  )
  await page.route(`**/api/mvp/admin/users/${USER_ID}`, (route) =>
    route.fulfill({
      json: {
        user_id: USER_ID, email: 'target@example.com', org_id: 'default-org',
        roles: ['user'], auth_provider: 'cognito', created_at: '2026-01-01T00:00:00Z',
        total_credit: 0, credit_used: 0, remaining_credit: 0,
        tenants: [],
      },
    }),
  )
  await page.route('**/api/mvp/admin/tenants**', (route) =>
    route.fulfill({ json: { tenants: [] } }),
  )
}

test.describe('admin user API-key revoke UI (#1)', () => {
  test('lists keys and revokes an active one via confirm dialog', async ({ page }) => {
    await seedAdminSession(page)
    await mockDetailRoutes(page)

    let revoked = false
    await page.route(`**/api/mvp/admin/users/${USER_ID}/api-keys**`, (route) =>
      route.fulfill({
        json: [
          {
            key_id: ACTIVE_KEY, name: 'ci-key', user_id: USER_ID, scopes: ['messages:send'],
            created_at: '2026-01-01T00:00:00Z', expires_at: null,
            revoked_at: revoked ? '2026-05-01T00:00:00Z' : null,
          },
          {
            key_id: REVOKED_KEY, name: 'old-key', user_id: USER_ID, scopes: [],
            created_at: '2025-01-01T00:00:00Z', expires_at: null,
            revoked_at: '2025-06-01T00:00:00Z',
          },
        ],
      }),
    )
    await page.route('**/api/mvp/admin/api-keys/by-key-id/**', (route) => {
      expect(route.request().method()).toBe('DELETE')
      const tail = route.request().url().split('/by-key-id/')[1]
      expect(decodeURIComponent(tail)).toBe(ACTIVE_KEY)
      revoked = true
      return route.fulfill({ status: 200, json: {} })
    })

    await page.goto(`/admin/users/${USER_ID}`)

    // Card + both rows render; the already-revoked key shows a badge, no button.
    await expect(page.getByTestId('admin-user-api-keys-card')).toBeVisible()
    await expect(page.getByText('ci-key')).toBeVisible()
    await expect(page.getByTestId(`api-key-revoked-badge-${REVOKED_KEY}`)).toBeVisible()
    await expect(page.getByTestId(`api-key-revoke-${REVOKED_KEY}`)).toHaveCount(0)

    // Revoke the active key: button -> confirm dialog -> confirm.
    await page.getByTestId(`api-key-revoke-${ACTIVE_KEY}`).click()
    await expect(page.getByTestId('api-key-revoke-dialog')).toBeVisible()
    await page.getByTestId('api-key-revoke-confirm').click()

    // Invalidation refetches; the row now shows the revoked badge, no button.
    await expect(page.getByTestId(`api-key-revoked-badge-${ACTIVE_KEY}`)).toBeVisible()
    await expect(page.getByTestId(`api-key-revoke-${ACTIVE_KEY}`)).toHaveCount(0)
  })

  test('shows the empty state when the user has no keys', async ({ page }) => {
    await seedAdminSession(page)
    await mockDetailRoutes(page)
    await page.route(`**/api/mvp/admin/users/${USER_ID}/api-keys**`, (route) =>
      route.fulfill({ json: [] }),
    )

    await page.goto(`/admin/users/${USER_ID}`)
    await expect(page.getByTestId('admin-user-api-keys-card')).toBeVisible()
    await expect(page.getByTestId('api-keys-empty')).toBeVisible()
  })
})
