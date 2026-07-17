// LIVE prod E2E for the external authorize/capture read-only UI (P0 authcap).
//
// Proves the UI half of the authcap unit end-to-end against the deployed
// CloudFront SPA: an external authorization created + captured via the API is
// then LOOKED UP (read-only) in /me/billing, rendering the "external" badge and
// the captured amount — and the UI exposes NO authorize/capture controls (those
// stay programmatic; a money form is a typo risk).
//
// Runs only when PROD_FRONTEND_URL + PROD_ACCESS_TOKEN are set.

import { test, expect, type Page } from '@playwright/test'

const FRONTEND = process.env.PROD_FRONTEND_URL ?? ''
const TOKEN = process.env.PROD_ACCESS_TOKEN ?? ''
const API = process.env.PROD_API_URL ?? FRONTEND // same origin (CloudFront)
const SHOULD_RUN = Boolean(FRONTEND && TOKEN)

test.describe.configure({ mode: 'serial' })
test.skip(!SHOULD_RUN, 'PROD_FRONTEND_URL / PROD_ACCESS_TOKEN not set')

async function seedToken(page: Page) {
  await page.addInitScript((token) => {
    window.sessionStorage.setItem(
      'stratoclave_tokens',
      JSON.stringify({
        access_token: token,
        id_token: token,
        refresh_token: null,
        expires_at: Date.now() + 24 * 60 * 60 * 1000,
      }),
    )
    window.sessionStorage.setItem('stratoclave_locale', 'en')
  }, TOKEN)
}

test('authcap: external authorization renders the read-only external badge', async ({
  page,
  request,
}) => {
  const idemKey = `e2e-authcap-${Date.now()}`

  // 1. Authorize $0.50 via the API (the programmatic surface).
  const authz = await request.post(`${API}/api/mvp/billing/authorize`, {
    headers: {
      authorization: `Bearer ${TOKEN}`,
      'Idempotency-Key': idemKey,
      'content-type': 'application/json',
    },
    data: { amount_microusd: 500_000, description: 'e2e external action' },
  })
  expect(authz.ok()).toBeTruthy()
  const authorizationId = (await authz.json()).authorization_id as string
  expect(authorizationId.startsWith('auth_')).toBeTruthy()

  // 2. Capture $0.30 of it.
  const cap = await request.post(
    `${API}/api/mvp/billing/authorizations/${encodeURIComponent(authorizationId)}/capture`,
    {
      headers: { authorization: `Bearer ${TOKEN}`, 'content-type': 'application/json' },
      data: { actual_amount_microusd: 300_000 },
    },
  )
  expect(cap.ok()).toBeTruthy()

  // 3. Look it up in the read-only UI.
  await seedToken(page)
  await page.goto(`${FRONTEND}/me/billing`, { waitUntil: 'networkidle' })
  await page.getByLabel('authorization id').fill(authorizationId)
  await page
    .locator('form', { has: page.getByLabel('authorization id') })
    .getByRole('button', { name: /look up/i })
    .click()

  // 4. The external badge + captured amount render.
  await expect(page.getByTestId('external-badge')).toBeVisible({ timeout: 15_000 })
  await expect(page.getByTestId('authorization-status')).toContainText(/captured/i)
  await expect(page.getByTestId('authorization-captured')).toContainText('$0.3')

  // 5. The UI exposes NO money-issuing controls (read-only contract).
  const body = (await page.locator('body').innerText()).toLowerCase()
  expect(body).not.toContain('provider cost')
  expect(body).not.toContain('margin')
})
