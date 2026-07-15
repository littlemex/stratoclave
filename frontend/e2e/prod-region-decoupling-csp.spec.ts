// LIVE prod E2E for the v2.2 region-decoupling CSP change.
//
// The CSP form-action was changed from a hardcoded us-east-1/us-west-2 pair to
// a single, deploy-region-derived Cognito domain. A wrong region there would
// break the Hosted UI login POST in the BROWSER only (a CSP violation invisible
// to backend tests) — Fable's exact concern (L-2 / #6). This spec proves, in a
// real Chromium, that:
//   1. the SPA renders with no runtime/CSP errors,
//   2. the served CSP form-action is the single deploy-region Cognito domain
//      (us-east-1) and NOT the retired us-west-2 entry,
//   3. clicking sign-in actually reaches the Cognito Hosted UI authorize
//      endpoint (i.e. form-action does not block the navigation).
//
// Runs only when PROD_FRONTEND_URL is set.

import { test, expect } from '@playwright/test'

const FRONTEND = process.env.PROD_FRONTEND_URL ?? ''
// The deploy region the CSP form-action should name (defaults to us-east-1).
const EXPECT_REGION = process.env.PROD_DEPLOY_REGION ?? 'us-east-1'
const SHOULD_RUN = Boolean(FRONTEND)

test.skip(!SHOULD_RUN, 'PROD_FRONTEND_URL not set')

test('served CSP form-action is the single deploy-region Cognito domain', async ({
  request,
}) => {
  const resp = await request.get(FRONTEND)
  expect(resp.status()).toBeLessThan(400)
  const csp = resp.headers()['content-security-policy'] ?? ''
  expect(csp).toContain(
    `form-action 'self' https://*.auth.${EXPECT_REGION}.amazoncognito.com`,
  )
  // Exactly one amazoncognito form-action target (no stray extra region).
  const targets = (csp.match(/\*\.auth\.[a-z0-9-]+\.amazoncognito\.com/g) ?? [])
  expect(targets).toEqual([`*.auth.${EXPECT_REGION}.amazoncognito.com`])
  // The retired hardcoded us-west-2 entry must be gone.
  expect(csp).not.toContain('auth.us-west-2.amazoncognito.com')
})

test('SPA renders with no CSP violation or runtime error', async ({ page }) => {
  const errors: string[] = []
  const cspViolations: string[] = []
  page.on('pageerror', (e) => errors.push(String(e)))
  // Capture CSP violation reports surfaced to the console.
  page.on('console', (msg) => {
    const t = msg.text()
    if (/content security policy|refused to|form-action/i.test(t)) {
      cspViolations.push(t)
    }
  })

  const resp = await page.goto(FRONTEND, { waitUntil: 'networkidle' })
  expect(resp?.status()).toBeLessThan(400)
  await expect(page).toHaveTitle(/Stratoclave/i)
  expect(errors, `runtime errors: ${errors.join(' || ')}`).toEqual([])
  expect(cspViolations, `CSP violations: ${cspViolations.join(' || ')}`).toEqual([])
})

test('sign-in reaches the Cognito Hosted UI authorize endpoint (form-action not blocked)', async ({
  page,
}) => {
  await page.goto(FRONTEND, { waitUntil: 'networkidle' })
  const cta = page.getByRole('button', { name: /cognito|sign in|log in/i }).first()
  await expect(cta).toBeVisible()

  // The Hosted UI is a separate origin; observe the navigation toward the
  // authorize endpoint rather than waiting for its DOM (which may 302 further).
  const [nav] = await Promise.all([
    page
      .waitForRequest(
        (req) => /\/oauth2\/authorize/.test(req.url()),
        { timeout: 15_000 },
      )
      .catch(() => null),
    cta.click().catch(() => undefined),
  ])
  expect(
    nav,
    'sign-in did not reach /oauth2/authorize — a bad CSP form-action would block this',
  ).not.toBeNull()
  expect(nav!.url()).toMatch(/amazoncognito\.com|\/oauth2\/authorize/)
})
