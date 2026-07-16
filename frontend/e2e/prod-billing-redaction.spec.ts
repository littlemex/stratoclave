// LIVE prod E2E for L5-d billing redaction against the deployed CloudFront SPA.
//
// Proves the FINAL defence line Fable named: the tenant billing detail DOM must
// never contain provider cost / margin. Drives a real inference with a known
// x-sc-workflow-run-id (so a per-run breakdown exists), opens /me/billing, looks
// the run up, and asserts the rendered page shows the charge but NOT the words
// "provider cost" / "margin" nor the admin-only fields.
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

test('L5-d: tenant billing detail renders the charge and NEVER cost/margin', async ({
  page,
  request,
}) => {
  const runId = `e2e-billing-${Date.now()}`

  // 1. Drive a real inference carrying the run id so a per-run breakdown exists.
  const inf = await request.post(`${API}/v1/messages`, {
    headers: {
      authorization: `Bearer ${TOKEN}`,
      'x-sc-workflow-run-id': runId,
      'content-type': 'application/json',
    },
    data: {
      model: 'claude-haiku-4-5',
      max_tokens: 16,
      messages: [{ role: 'user', content: 'hi' }],
    },
  })
  expect(inf.ok()).toBeTruthy()

  // 2. Open the tenant billing page, look the run up.
  await seedToken(page)
  await page.goto(`${FRONTEND}/me/billing`, { waitUntil: 'networkidle' })
  await page.getByLabel('workflow run id').fill(runId)
  await page.getByRole('button', { name: /look up/i }).click()

  // 3. The charge total renders (proves the live round trip worked).
  await expect(page.getByTestId('total-charged')).toBeVisible({ timeout: 15_000 })

  // 4. THE redaction assertion: nowhere in the DOM do cost/margin appear.
  const body = (await page.locator('body').innerText()).toLowerCase()
  expect(body).not.toContain('provider cost')
  expect(body).not.toContain('margin')
  expect(body).not.toContain('provider_cost')
  // A $ charge amount IS shown (the tenant sees what they were charged).
  expect(body).toMatch(/\$\d/)
})
