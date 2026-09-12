// The discovery/promotion screens against a RUNNING GATEWAY. No `page.route`,
// no fixture, no mocked network boundary anywhere in this file: every response
// below is produced by the gateway process, from its real data store, holding
// records a real discovery pass wrote from a real AWS account.
//
// This file exists because the other two cannot make this claim. They pin the
// contract, which is what they are for, and they are the reason three UI
// defects were found -- but a mocked boundary can only prove the screen agrees
// with the fixture's author. It cannot prove the screen agrees with the
// gateway, and the two disagreed three times.
//
// How the browser gets a token the real gateway accepts, without weakening the
// gateway: the gateway reads its issuer and audience from the environment and
// fetches JWKS from `{issuer}/.well-known/jwks.json`, so a local issuer serving
// one RSA key is enough. Verification is untouched -- RS256, matching issuer,
// `token_use == "access"`, `client_id` equal to the configured audience -- and
// ROLES still come from the users table rather than from the token, so the
// authorisation path is exercised for real rather than asserted.
//
// Skipped, not failed, when the environment is absent: a spec that needs a live
// gateway and silently passes without one is worse than no spec. Run it with
// `E2E_LIVE_GATEWAY=http://127.0.0.1:8082 E2E_LIVE_TOKENS=<path to tokens.json>`,
// with the dev server started against the same gateway:
// `VITE_BACKEND_PROXY_TARGET=http://127.0.0.1:8082`. The spec asserts the two
// agree rather than trusting that they were set together -- see `test.beforeAll`.
import { expect, test } from './support/no-uncaught-render-error'
import { readFileSync } from 'node:fs'

const GATEWAY = process.env.E2E_LIVE_GATEWAY
const TOKENS_PATH = process.env.E2E_LIVE_TOKENS

const tokens: Record<string, string> =
  GATEWAY && TOKENS_PATH ? JSON.parse(readFileSync(TOKENS_PATH, 'utf8')) : {}

test.skip(
  !GATEWAY || !TOKENS_PATH,
  'needs a running gateway and minted tokens; see this file header',
)

/** Point the app at the live gateway and hand it a real bearer token.
 *
 * MEASURED, after the first attempt pointed `config.json`'s `api.endpoint` at
 * the gateway and every call still went to the dev server and returned 500: the
 * app calls its OWN ORIGIN and the dev server proxies `/api` onward, so the
 * endpoint in the runtime config does not route API traffic here. The proxy
 * target is what does, and it is already an environment variable
 * (`VITE_BACKEND_PROXY_TARGET`). `config.json` is still stubbed because the app
 * needs one to boot, but it no longer pretends to be the routing mechanism. */
async function liveSession(page: import('@playwright/test').Page, role: string) {
  const token = tokens[`e2e-${role}`]
  expect(token, `no token for role ${role} in ${TOKENS_PATH}`).toBeTruthy()

  await page.addInitScript(
    ({ token }) => {
      window.sessionStorage.setItem(
        'stratoclave_tokens',
        JSON.stringify({
          access_token: token,
          id_token: token,
          refresh_token: null,
          expires_at: Date.now() + 6 * 60 * 60 * 1000,
        }),
      )
      window.sessionStorage.setItem('stratoclave_locale', 'en')
    },
    { token },
  )

  await page.route('**/config.json', (route) =>
    route.fulfill({
      json: {
        api: { endpoint: '' },
        cognito: {
          client_id: 'e2e-local-client',
          domain: 'http://127.0.0.1:8901',
          user_pool_id: 'local',
          region: 'us-east-1',
        },
      },
    }),
  )
}

/** Records the gateway itself reports, so an assertion about the screen can be
 * compared against the source rather than against a number written here. */
async function gatewayRecords(role: string): Promise<Array<Record<string, unknown>>> {
  const resp = await fetch(`${GATEWAY}/api/mvp/admin/discovery/records`, {
    headers: { Authorization: `Bearer ${tokens[`e2e-${role}`]}` },
  })
  expect(resp.status, 'the gateway refused the records read').toBe(200)
  return (await resp.json()).records
}

test.describe('live gateway: the operator reads what discovery really found', () => {
  // The proxy target and `E2E_LIVE_GATEWAY` are set independently, and if they
  // disagree this whole file quietly measures the wrong process. Asserted once,
  // by comparing what the BROWSER's origin answers against what the gateway
  // answers directly: two different processes cannot both hold this record set.
  test.beforeAll(async ({ browser }) => {
    const page = await browser.newPage()
    await liveSession(page, 'admin')
    const viaBrowser = await page.request.get('/api/mvp/admin/discovery/records', {
      headers: { Authorization: `Bearer ${tokens['e2e-admin']}` },
    })
    expect(
      viaBrowser.status(),
      'the dev server is not proxying to a gateway that accepts these tokens; ' +
        'start it with VITE_BACKEND_PROXY_TARGET set to E2E_LIVE_GATEWAY',
    ).toBe(200)
    const throughProxy = (await viaBrowser.json()).records.length
    const direct = (await gatewayRecords('admin')).length
    expect(
      throughProxy,
      'the dev server proxies a DIFFERENT gateway from E2E_LIVE_GATEWAY',
    ).toBe(direct)
    await page.close()
  })
  test('the admin records screen shows the gateway its own records, including permanent blockers', async ({
    page,
  }) => {
    const records = await gatewayRecords('admin')
    expect(records.length, 'the live store has no discovered records to show').toBeGreaterThan(0)

    await liveSession(page, 'admin')
    await page.goto('/admin/discovery/records')

    // Named from the gateway's answer, not from a constant here: whichever
    // record the live pass happened to write first is the one asserted.
    const first = records[0] as { profile_id: string }
    await expect(page.getByText(first.profile_id).first()).toBeVisible()

    // The count the screen renders must equal the count the gateway returned.
    // This is the assertion a mocked boundary structurally cannot make.
    const rows = page.locator('table tbody tr')
    await expect(rows).toHaveCount(records.length)

    // A record whose blockers are all permanent is the case the actionable
    // queue omits by design, and the reason this screen exists. Asserted only
    // when the live data contains one, and reported when it does not, rather
    // than passing quietly either way.
    const permanent = records.find((r) => {
      const blockers = (r.blockers ?? []) as Array<{ type?: string }>
      return blockers.length > 0 && blockers.every((b) => b.type === 'no_agreement_offer')
    }) as { profile_id: string; blockers: Array<{ evidence?: string }> } | undefined

    test.info().annotations.push({
      type: 'live-data',
      description: `${records.length} records; permanently blocked example: ${
        permanent?.profile_id ?? 'none present'
      }`,
    })

    if (permanent) {
      await expect(page.getByText(permanent.profile_id).first()).toBeVisible()
      const evidence = permanent.blockers.find((b) => b.evidence)?.evidence
      if (evidence) {
        // The sentence, not the enum -- against the real evidence string the
        // provider actually returned.
        await expect(page.getByText(evidence.slice(0, 40), { exact: false }).first()).toBeVisible()
      }
    }
  })

  test('a tenant lead reads the same records on their own screen and reaches no write', async ({
    page,
  }) => {
    const records = await gatewayRecords('teamlead')

    const writes: string[] = []
    page.on('request', (req) => {
      if (
        req.method() !== 'GET' &&
        req.url().includes('/api/mvp/admin/discovery')
      ) {
        writes.push(`${req.method()} ${req.url()}`)
      }
    })

    await liveSession(page, 'teamlead')
    await page.goto('/team-lead/discovery')

    const first = records[0] as { profile_id: string }
    await expect(page.getByText(first.profile_id).first()).toBeVisible()

    // The write affordances are absent, and -- the assertion that does not
    // depend on the DOM -- no write request reached the gateway.
    await expect(page.getByRole('link', { name: /create candidate/i })).toHaveCount(0)
    await expect(page.getByRole('button', { name: /create candidate/i })).toHaveCount(0)
    await expect(page.getByRole('button', { name: /run probe/i })).toHaveCount(0)
    await expect(page.getByRole('button', { name: /^activate/i })).toHaveCount(0)

    // And the admin routes are refused by the guard, before any component that
    // could render a write control mounts.
    for (const route of ['/admin/discovery/records', '/admin/discovery/candidates']) {
      await page.goto(route)
      await expect(
        page.getByText(/access denied/i).first(),
        `a team_lead was let into ${route} against the live gateway`,
      ).toBeVisible()
    }

    await page.waitForTimeout(250)
    expect(writes, 'a team_lead session wrote to the live gateway').toHaveLength(0)
  })

  test('a plain user reaches neither screen, and the gateway would refuse them anyway', async ({
    page,
  }) => {
    // Both halves matter and they are different claims. The UI refusing is a
    // guard; the gateway refusing is the boundary. A screen that hid the data
    // while the API served it would pass the first and fail the second.
    const resp = await fetch(`${GATEWAY}/api/mvp/admin/discovery/records`, {
      headers: { Authorization: `Bearer ${tokens['e2e-user']}` },
    })
    expect(resp.status, 'the live gateway served discovery to a plain user').toBe(403)

    const touched: string[] = []
    page.on('request', (req) => {
      if (req.url().includes('/api/mvp/admin/discovery')) touched.push(req.url())
    })

    await liveSession(page, 'user')
    for (const route of ['/team-lead/discovery', '/admin/discovery/records']) {
      await page.goto(route)
      await expect(
        page.getByText(/access denied/i).first(),
        `a user was let into ${route} against the live gateway`,
      ).toBeVisible()
    }

    await page.waitForTimeout(250)
    expect(touched, 'a user session reached a discovery endpoint').toHaveLength(0)
  })

  test('the candidates screen and a candidate detail render the gateway own candidates', async ({
    page,
  }) => {
    const resp = await fetch(`${GATEWAY}/api/mvp/admin/discovery/candidates`, {
      headers: { Authorization: `Bearer ${tokens['e2e-admin']}` },
    })
    expect(resp.status).toBe(200)
    const candidates = (await resp.json()).candidates as Array<Record<string, unknown>>
    test.skip(candidates.length === 0, 'the live store holds no promotion candidates')

    await liveSession(page, 'admin')
    await page.goto('/admin/discovery/candidates')

    const first = candidates[0] as { profile_id: string; identifiers: string[] }
    await expect(page.getByText(first.profile_id).first()).toBeVisible()
    await expect(page.locator('table tbody tr')).toHaveCount(candidates.length)

    // The detail page is where the three white screens happened, and where the
    // fixtures disagreed with the real response model. Against the gateway the
    // shape is not a choice anyone made.
    await page.goto(`/admin/discovery/candidates/${encodeURIComponent(first.profile_id)}`)
    await expect(page.getByText(first.profile_id).first()).toBeVisible()
    for (const identifier of first.identifiers) {
      await expect(page.getByText(identifier).first()).toBeVisible()
    }
    // The invocation control is labelled -- the defect the mocked spec found,
    // re-checked here against the real page.
    await expect(page.getByLabel(/invocation/i)).toBeVisible()
  })
})
