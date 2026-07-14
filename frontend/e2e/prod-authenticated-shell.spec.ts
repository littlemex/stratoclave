// Live E2E against the production CloudFront deployment, exercising the
// AUTHENTICATED shell — the part prod-deploy-2026-06-11.spec.ts stops short
// of (it only proves the unauthenticated landing + the OIDC redirect).
//
// It injects a REAL Cognito access token (minted out of band and passed via
// PROD_ACCESS_TOKEN) into sessionStorage the same way AuthContext persists it,
// then loads the SPA against the real backend. So unlike the mocked
// tenant-pool-budget.spec.ts, every panel here is filled by a live
// /api/mvp/* round trip through CloudFront -> ALB -> ECS -> DynamoDB.
//
// What this covers:
//   1. The dashboard renders the signed-in shell (greeting, credit balance,
//      role badges) with no runtime errors — proving /api/mvp/me resolves and
//      the token model works end to end on the deployed build.
//   2. Client-side routing to /admin/tenants renders the live tenant list
//      (admin-only surface) — proving RBAC + a second live API call.
//
// Skipped automatically unless PROD_FRONTEND_URL and PROD_ACCESS_TOKEN are set,
// so it never fails on a machine without a deployed instance + fresh token.

import { test, expect } from '@playwright/test'

const FRONTEND = process.env.PROD_FRONTEND_URL ?? ''
const ACCESS_TOKEN = process.env.PROD_ACCESS_TOKEN ?? ''

const SHOULD_RUN = Boolean(FRONTEND && ACCESS_TOKEN)

test.describe.configure({ mode: 'serial' })
test.skip(!SHOULD_RUN, 'PROD_FRONTEND_URL / PROD_ACCESS_TOKEN not set')

// Persist the token the way AuthContext does, before any app code runs.
async function seedRealToken(page: import('@playwright/test').Page) {
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
  }, ACCESS_TOKEN)
}

test('authenticated dashboard renders live account state', async ({ page }) => {
  const errors: string[] = []
  page.on('pageerror', (e) => errors.push(String(e)))

  await seedRealToken(page)
  await page.goto(FRONTEND, { waitUntil: 'networkidle' })

  // The signed-in shell replaces the "Sign in" hero once /api/mvp/me resolves.
  // Match the sign-out control (locale-agnostic) as the authenticated landmark.
  const signOut = page.getByRole('button', { name: /sign out|サインアウト/i })
  await expect(signOut).toBeVisible({ timeout: 15_000 })

  // The credit balance panel is fed by the live /api/mvp/me response; assert a
  // numeric balance rendered (grouped digits) rather than a spinner or error.
  const body = await page.locator('body').innerText()
  expect(body).toMatch(/\d[\d,]{2,}/) // a grouped number (credit balance)
  // Role badges come straight from the DynamoDB Users row.
  expect(body).toMatch(/admin/i)

  expect(errors, `runtime errors: ${errors.join(' || ')}`).toEqual([])
})

test('admin can route to the live tenant list', async ({ page }) => {
  const errors: string[] = []
  page.on('pageerror', (e) => errors.push(String(e)))

  await seedRealToken(page)
  await page.goto(FRONTEND, { waitUntil: 'networkidle' })
  await page.getByRole('button', { name: /sign out|サインアウト/i }).waitFor({
    timeout: 15_000,
  })

  // Client-side navigation to the admin tenants surface.
  await page.getByRole('link', { name: /tenant|テナント/i }).first().click()
  await page.waitForURL(/\/admin\/tenants/, { timeout: 15_000 })
  await page.waitForLoadState('networkidle')

  // The list is populated by a live admin API call; the default org row must
  // be present (its tenant_id is stable across deployments).
  await expect(page.getByText('default-org')).toBeVisible({ timeout: 15_000 })

  expect(errors, `runtime errors: ${errors.join(' || ')}`).toEqual([])
})
