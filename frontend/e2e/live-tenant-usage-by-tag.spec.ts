// A REAL browser against a REAL gateway. No `page.route` mocks at all.
//
// Every other spec in this directory mocks `**/api/mvp/**`, which is the right tool for a
// deterministic UI test and is not evidence that the page can talk to the backend. The unit
// tests assert the URL this page requests; a separate real-machine observation asserts that URL
// is one the caller's role may fetch. **Nothing connected the two** — no browser had ever
// fetched the real route and rendered real rows — so this closes that gap and nothing else.
//
// Opt-in, and skipped otherwise, following the backend's own `SC_GW_LIVE` pattern: it needs a
// gateway, real DynamoDB tables and a seeded fixture, none of which exist in CI.
//
//   1. harness: setup_env.py <prefix> && launch.py <prefix> seed
//   2. CORS_ORIGINS=http://127.0.0.1:3003 launch.py <prefix> serve 8093
//   3. launch.py <prefix> exec seed_live.py     (writes /tmp/live-fixture.json)
//   4. LIVE_BY_TAG=1 VITE_BACKEND_PROXY_TARGET=http://127.0.0.1:8093 \
//        npx playwright test e2e/live-tenant-usage-by-tag.spec.ts
//
// The proxy target is the load-bearing part and the first attempt got it wrong: the app's calls
// are SAME-ORIGIN by design, so pointing `config.json`'s `api.endpoint` at the gateway changed
// nothing -- the browser asked the dev server for `/api/mvp/me` and got its 500, and the page
// showed a sign-in screen. Vite's own `/api` and `/v1` proxy is the seam
// (`VITE_BACKEND_PROXY_TARGET`, default `localhost:8000`). Kill any dev server already on 3003
// first: `reuseExistingServer` would otherwise keep one pointed at the wrong backend, which
// looks exactly like a broken page.
//
// The session is a REAL API key seeded as the bearer. `authFetch` sends
// `Authorization: Bearer <access_token>` and the gateway accepts an API key there, so the
// browser authenticates as a real principal with real scopes rather than a stubbed identity —
// which is the half that matters: the page's request is refused or served by the real
// permission lattice.

import { readFileSync } from 'node:fs'
import { expect, test, type Page } from '@playwright/test'

const LIVE = process.env.LIVE_BY_TAG === '1'
const FIXTURE = '/tmp/live-fixture.json'

type Fixture = {
  tenant: string
  period: string
  admin_key: string
  members: string[]
  gateway: string
}

function fixture(): Fixture {
  return JSON.parse(readFileSync(FIXTURE, 'utf8')) as Fixture
}

async function seedRealSession(page: Page, f: Fixture) {
  await page.addInitScript(
    ({ key }: { key: string }) => {
      window.sessionStorage.setItem(
        'stratoclave_tokens',
        JSON.stringify({
          access_token: key,
          id_token: key,
          refresh_token: null,
          // Far future so AuthContext's refresh margin never trips and no refresh round trip
          // is attempted against a gateway that has no Cognito behind it.
          expires_at: Date.now() + 24 * 60 * 60 * 1000,
        }),
      )
      window.sessionStorage.setItem('stratoclave_locale', 'en')
      // The runtime config the SPA fetches on cold start, pointed at the REAL gateway.
    },
    { key: f.admin_key },
  )
  // The only route interception in this file, and it is not a mock: it serves the runtime config
  // the SPA fetches on cold start, without which it renders a configuration-failure splash and
  // never mounts. `api.endpoint: ''` means "this origin", which is what production serves and
  // what routes these calls through Vite's proxy to the real gateway. Every `/api/mvp/**` request
  // reaches the gateway; none is mocked.
  await page.route('**/config.json', (route) =>
    route.fulfill({
      json: {
        api: { endpoint: '' },
        cognito: {
          client_id: 'live-not-used',
          domain: 'https://live.auth.us-east-1.amazoncognito.com',
          user_pool_id: 'us-east-1_live',
          region: 'us-east-1',
        },
      },
    }),
  )
}

test.describe('the tenant by-tag report, against a live gateway', () => {
  test.skip(!LIVE, 'set LIVE_BY_TAG=1 with a seeded gateway (see this file’s header)')

  test('an administrator sees real rows, real costs and every disclosure', async ({ page }) => {
    const f = fixture()
    await seedRealSession(page, f)

    // Fail loudly on a request the real backend refuses, rather than reading an empty table as
    // "no spend". A 403 here would mean the page is calling a route this key may not use.
    const refused: string[] = []
    page.on('response', (r) => {
      if (r.url().includes('/api/mvp/') && r.status() >= 400) {
        refused.push(`${r.status()} ${r.url()}`)
      }
    })

    await page.goto(`/admin/tenants/${f.tenant}/usage-by-tag`)

    // Real rows. Two members share the tag `deploy`, which is the case the member column exists
    // for: without it these two rows are indistinguishable.
    await expect(page.getByTestId('bt-row-tag').first()).toBeVisible({ timeout: 30_000 })
    const tags = await page.getByTestId('bt-row-tag').allTextContents()
    expect(tags.filter((x) => x === 'deploy')).toHaveLength(2)
    expect(tags).toContain('migration-42')

    const members = await page.getByTestId('bt-row-member').allTextContents()
    expect(new Set(members).size).toBeGreaterThanOrEqual(2)
    for (const m of members) expect(f.members).toContain(m)

    // A real cost, priced by the real gateway from a real Bedrock call — and NOT rendered as
    // "$0.00". This assertion is the reason this spec was worth writing: the first live run
    // showed three requests at $0.00, because a request really can cost less than a cent and the
    // cent formatter truncates it away. `fmtMicroUsdRate`'s own comment in `money.ts` already
    // warned that it "would show a real sub-cent rate as $0.00"; nothing had ever put that
    // warning next to a cost report until a browser did.
    const costs = await page.locator('tbody tr td:nth-last-child(2)').allTextContents()
    expect(costs.length).toBeGreaterThan(0)
    for (const c of costs) {
      expect(c).toMatch(/^\$/)
      expect(c, 'a charged request must not read as free').not.toBe('$0.00')
    }

    // Every disclosure, in a real browser against a real response. This is the assertion the
    // whole shared-component design exists to make possible on more than one surface.
    await expect(page.getByTestId('bt-disclosures')).toBeVisible()
    await expect(page.getByTestId('bt-lower-bound')).toBeVisible()
    await expect(page.getByTestId('bt-caller-asserted')).toBeVisible()
    await expect(page.getByTestId('bt-retention')).toBeVisible()
    await expect(page.getByTestId('bt-coverage')).toBeVisible()

    expect(refused, `the real backend refused a request the page made`).toEqual([])
  })

  test('the member filter narrows the real report', async ({ page }) => {
    const f = fixture()
    await seedRealSession(page, f)
    await page.goto(`/admin/tenants/${f.tenant}/usage-by-tag`)
    await expect(page.getByTestId('bt-row-tag').first()).toBeVisible({ timeout: 30_000 })

    const before = await page.getByTestId('bt-row-member').allTextContents()
    expect(new Set(before).size).toBeGreaterThan(1)

    await page.getByTestId('bt-member-input').fill(f.members[0])
    await page.getByTestId('bt-load-button').click()

    // One member's rows, from the real route with a real `user_id` filter.
    await expect
      .poll(async () => new Set(await page.getByTestId('bt-row-member').allTextContents()).size, {
        timeout: 30_000,
      })
      .toBe(1)
    const after = await page.getByTestId('bt-row-member').allTextContents()
    expect(after.every((m) => m === f.members[0])).toBe(true)
  })

  test('an id matching nobody is an empty result, not an error', async ({ page }) => {
    const f = fixture()
    await seedRealSession(page, f)
    await page.goto(`/admin/tenants/${f.tenant}/usage-by-tag`)
    await expect(page.getByTestId('bt-row-tag').first()).toBeVisible({ timeout: 30_000 })

    await page.getByTestId('bt-member-input').fill('nobody-at-all')
    await page.getByTestId('bt-load-button').click()

    // The real backend returns 200 with no rows. The view must say "nothing for them" and must
    // NOT render the error state, which is what a reader would otherwise report as a bug.
    await expect(page.getByTestId('bt-empty')).toBeVisible({ timeout: 30_000 })
    await expect(page.getByTestId('bt-empty')).toContainText('nobody-at-all')
    await expect(page.getByTestId('bt-error')).toBeHidden()
  })
})
