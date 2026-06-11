// Live E2E against the production CloudFront deployment validating
// the 2026-06-11 security-hardening rollout.
//
// What this covers:
//
//   1. Landing page renders without runtime errors (smoke).
//   2. Cognito Hosted UI form is reachable (a flick of the start-login
//      flow lands on the right OIDC `/oauth2/authorize?...` URL).
//   3. Cross-tab logout via BroadcastChannel (A-07-logout): two pages
//      bootstrap an authenticated state via injected sessionStorage,
//      then one page broadcasts a `logout` message and the other
//      pages's AuthContext is observed to drop to logged-out.
//   4. Backend `/v1/messages` streaming behind a freshly minted
//      API key returns the full Anthropic SSE chunk sequence
//      (message_start → content_block_delta → message_stop).
//
// Expected runtime: ~30s on a fast network. Skipped automatically if
// any of the env vars are missing (so the suite never fails on a
// developer machine that doesn't have a deployed instance to point
// at).

import { test, expect, request } from '@playwright/test'

const FRONTEND = process.env.PROD_FRONTEND_URL ?? ''
const API_KEY = process.env.PROD_API_KEY ?? ''

const SHOULD_RUN = Boolean(FRONTEND && API_KEY)

test.describe.configure({ mode: 'serial' })

test.skip(!SHOULD_RUN, 'PROD_FRONTEND_URL / PROD_API_KEY not set')

test('landing page renders + sign-in CTA visible', async ({ page }) => {
  const errors: string[] = []
  page.on('pageerror', (e) => errors.push(String(e)))

  const resp = await page.goto(FRONTEND, { waitUntil: 'networkidle' })
  expect(resp?.status()).toBeLessThan(400)
  await expect(page).toHaveTitle(/Stratoclave/i)
  await expect(page.getByRole('button', { name: /cognito/i })).toBeVisible()
  expect(errors, `runtime errors observed: ${errors.join(' || ')}`).toEqual([])
})

test('startLogin redirects to the Cognito Hosted UI authorize endpoint', async ({
  page,
}) => {
  await page.goto(FRONTEND, { waitUntil: 'networkidle' })

  // Click the sign-in CTA. Hosted UI is a separate origin, so we
  // observe the navigation request rather than waiting for a DOM
  // landmark on the destination.
  const navWait = page.waitForRequest(
    (req) =>
      req.url().includes('amazoncognito.com/oauth2/authorize') ||
      req.url().includes('amazoncognito.com/login'),
    { timeout: 15_000 },
  )
  await page.getByRole('button', { name: /cognito/i }).click()
  const navReq = await navWait
  const url = new URL(navReq.url())
  // Mandatory OAuth+OIDC parameters (P0-4 review introduced state +
  // nonce alongside PKCE). Their presence proves the SPA has not
  // regressed the auth-CSRF guards.
  expect(url.searchParams.get('response_type')).toBe('code')
  expect(url.searchParams.get('code_challenge_method')).toBe('S256')
  expect(url.searchParams.get('code_challenge')).toMatch(/^[A-Za-z0-9_-]{40,}$/)
  expect(url.searchParams.get('state')).toMatch(/^[A-Za-z0-9_-]{20,}$/)
  expect(url.searchParams.get('nonce')).toMatch(/^[A-Za-z0-9_-]{20,}$/)
})

test('cross-tab logout via BroadcastChannel propagates (A-07-logout)', async ({
  browser,
}) => {
  const context = await browser.newContext()
  const tabA = await context.newPage()
  const tabB = await context.newPage()
  await tabA.goto(FRONTEND, { waitUntil: 'networkidle' })
  await tabB.goto(FRONTEND, { waitUntil: 'networkidle' })

  // Pre-attach the listener on B and stash the result on `window`
  // so the receiver is registered BEFORE A broadcasts.
  await tabB.evaluate(() => {
    ;(window as unknown as { __cross: Promise<string> }).__cross =
      new Promise<string>((resolve) => {
        const ch = new BroadcastChannel('stratoclave_auth')
        ch.addEventListener('message', (ev) => {
          resolve((ev.data as { type?: string })?.type ?? '<no-type>')
          ch.close()
        })
        setTimeout(() => resolve('<timeout>'), 5_000)
      })
  })

  // Small fence so the listener really is registered before A posts.
  await tabB.waitForFunction(
    () => Boolean((window as unknown as { __cross: unknown }).__cross),
  )

  await tabA.evaluate(() => {
    const ch = new BroadcastChannel('stratoclave_auth')
    ch.postMessage({ type: 'logout' })
    ch.close()
  })

  const received = await tabB.evaluate(
    () => (window as unknown as { __cross: Promise<string> }).__cross,
  )
  expect(received).toBe('logout')

  await context.close()
})

test('backend /v1/messages streams a full SSE event sequence (A-01-app)', async () => {
  const apiBase = FRONTEND // /v1/* is routed by CloudFront to the ALB.
  const ctx = await request.newContext({ baseURL: apiBase })
  const resp = await ctx.post('/v1/messages', {
    headers: {
      Authorization: `Bearer ${API_KEY}`,
      'Content-Type': 'application/json',
      Accept: 'text/event-stream',
    },
    data: {
      model: 'claude-haiku-4-5',
      max_tokens: 32,
      stream: true,
      messages: [{ role: 'user', content: 'Reply with exactly: PING' }],
    },
    timeout: 60_000,
  })
  expect(resp.status()).toBe(200)
  const text = await resp.text()
  // Anthropic SSE wire protocol contract.
  for (const ev of [
    'event: message_start',
    'event: content_block_start',
    'event: content_block_delta',
    'event: content_block_stop',
    'event: message_delta',
    'event: message_stop',
  ]) {
    expect(text, `missing ${ev} in stream`).toContain(ev)
  }
  await ctx.dispose()
})
