// LIVE prod E2E for the P0 UI increments (#62 routing card, #65 usage fallback,
// #66 pricing view) against the deployed CloudFront SPA + real backend.
//
// Unlike the mocked specs, NO page.route stubs: an admin `sk-stratoclave-*` key
// is seeded into sessionStorage the way AuthContext persists a token, and every
// panel is filled by a live /api/mvp/* round trip (CloudFront -> ALB -> ECS ->
// DynamoDB). The SPA does not JWT-decode the token (it stores expires_at and
// sends it as Bearer), and the backend accepts sk-keys as Bearer, sourcing
// roles from the key owner's DynamoDB Users row — so an admin-owned key drives
// the admin surfaces without a Cognito login.
//
// Runs only when PROD_FRONTEND_URL + PROD_ACCESS_TOKEN are set (the harness in
// scripts seeds an admin user + key, sets these, then tears down). Skipped
// otherwise so it never fails on a machine without a deployment.

import { test, expect, type Page } from '@playwright/test'

const FRONTEND = process.env.PROD_FRONTEND_URL ?? ''
const TOKEN = process.env.PROD_ACCESS_TOKEN ?? ''
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

test('#66 pricing page renders the live effective rate table', async ({ page }) => {
  const errors: string[] = []
  page.on('pageerror', (e) => errors.push(String(e)))

  await seedToken(page)
  await page.goto(`${FRONTEND}/admin/pricing`, { waitUntil: 'networkidle' })

  // Assert on locale-agnostic live data (the SPA may render ja or en depending
  // on the user's stored locale, so we match pricing keys / $ rates, not
  // translated labels). The rows are filled by the live
  // GET /api/mvp/admin/pricing-config.
  const opusRow = page.getByRole('row', { name: /opus/ })
  await expect(opusRow).toBeVisible({ timeout: 15_000 })
  await expect(page.getByRole('row', { name: /haiku/ })).toBeVisible()
  const body = await page.locator('body').innerText()
  // A $/MTok rate rendered (dollar sign + digit) — proves the row mapped and
  // the money formatter ran on live data.
  expect(body).toMatch(/\$\d/)
  expect(errors, `runtime errors: ${errors.join(' || ')}`).toEqual([])
})

test('#65 my-usage page renders live usage without runtime errors', async ({
  page,
}) => {
  const errors: string[] = []
  page.on('pageerror', (e) => errors.push(String(e)))

  await seedToken(page)
  await page.goto(`${FRONTEND}/me/usage`, { waitUntil: 'networkidle' })

  // The usage page mounts and fills from the live usage-summary /
  // usage-history calls (empty history renders the empty state, not a table —
  // that's fine; the fallback-badge logic is covered deterministically by the
  // mocked me-usage-fallback.spec.ts). Assert on the live credit balance
  // (grouped number from /api/mvp/me) as a locale-agnostic proof the panels
  // resolved without a runtime error.
  const body = await page.locator('body').innerText()
  expect(body).toMatch(/\d[\d,]{2,}/) // grouped number (credit balance)
  expect(errors, `runtime errors: ${errors.join(' || ')}`).toEqual([])
})

test('#62 tenant detail renders the live routing-config card', async ({ page }) => {
  const errors: string[] = []
  page.on('pageerror', (e) => errors.push(String(e)))

  await seedToken(page)
  // Land on the tenants list first, then open the default org's detail so the
  // tenant_id is one that exists live.
  await page.goto(`${FRONTEND}/admin/tenants`, { waitUntil: 'networkidle' })
  await expect(page.getByText('default-org').first()).toBeVisible({ timeout: 15_000 })
  await page.goto(`${FRONTEND}/admin/tenants/default-org`, {
    waitUntil: 'networkidle',
  })

  // The routing-config card is present (either the configured summary or the
  // empty state) — proving the live GET .../routing-config resolved and the
  // card mounted without error.
  await expect(page.getByTestId('routing-config-card')).toBeVisible({
    timeout: 15_000,
  })
  expect(errors, `runtime errors: ${errors.join(' || ')}`).toEqual([])
})
