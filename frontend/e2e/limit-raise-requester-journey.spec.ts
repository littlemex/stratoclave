// Persona journey in the browser: the engineer who got refused, reading her
// own request.
//
// NOT a requirement test. Each case walks a step she walks and asks whether
// what the screen tells her is true. Both steps below are satisfiable by
// passing unit tests on every endpoint involved while she still plans against
// the wrong number.
//
// Runs against the dev server with no live backend, the same shape as
// `tenant-pool-budget.spec.ts`: a session seeded into sessionStorage (the P0-7
// token model) and `page.route` mocks that are stateful within a test, so a
// round trip is genuinely proven rather than stubbed.
//
//   1. She asked $200 and was granted $50. Her own request view must show the
//      $50 and the expiry, or `APPROVED` beside her $200 ask reads as $200.
//   2. Her ask exceeds what any approver is allowed to grant. The refusal must
//      hand her the maximum, or she re-files the same impossible figure
//      tomorrow.

import { expect, test, type Page } from '@playwright/test'

const TENANT_ID = 'acme-eng'
// A cap deliberately below her ask: the hint's minimum and the approver's
// ceiling are computed from different sources, so they can and do conflict.
const REMAINING_CAP_MICROUSD = 75_000_000

// A far-future expiry so AuthContext's 5-minute refresh margin never trips and
// no refresh_token round trip is attempted.
function seedUserSession(page: Page) {
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
    user_id: 'engineer-1',
    email: 'engineer@example.com',
    // Her ambient tenant. Deliberately NOT the tenant she was refused on:
    // the submission must carry the tenant from the refusal's hint, never from
    // ambient client context, or a profile defaulting elsewhere files a
    // perfectly valid request against the wrong tenant.
    org_id: 'default-org',
    roles: ['user'],
    total_credit: 1_000_000,
    credit_used: 0,
    remaining_credit: 1_000_000,
    currency: 'tokens',
    tenant: { tenant_id: 'default-org', name: 'Default' },
    locale: 'en',
  }
}

// The row her own request list returns once he has decided it. `asked` and
// `approved` sit side by side because "you got less" is a comparison and she
// cannot make it from one number.
function decidedRequest() {
  return {
    request_id: 'req-1',
    tenant_id: TENANT_ID,
    limit_kind: 'tenant_pool',
    status: 'APPROVED',
    reason_code: 'deadline',
    comment: 'shipping the migration on Friday',
    asked_amount_microusd: 200_000_000,
    // The three facts that live on the grant row and reach her nowhere else.
    approved_amount_microusd: 50_000_000,
    expires_at: '2026-07-31T23:59:59Z',
    approver_id: 'approver-1',
    decision_comment: 'half of the ask, one week',
    created_at: '2026-07-24T09:00:00Z',
  }
}

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
  await page.route('**/api/mvp/me', (route) => route.fulfill({ json: meResponse() }))
}

test.describe('the refused engineer reading her own request', () => {
  test('shows the amount she was granted, not the amount she asked for', async ({
    page,
  }) => {
    // Persona 1 step 4 and persona 2 question 3 — the top of both misled-first
    // rankings, from her seat. What she would be told wrongly: that `APPROVED`
    // means she got her $200. She then plans a $200 job against a ceiling that
    // rose by $50 and hits his figure mid-task, and she cannot plan around a
    // deadline she was never shown either. Both facts live on the grant row,
    // and a view that renders only the request row shows her neither.
    await seedUserSession(page)
    await mockCommonRoutes(page)
    await page.route('**/api/mvp/me/limit-raises**', (route) =>
      route.fulfill({ json: { requests: [decidedRequest()] } }),
    )

    await page.goto('/me/limit-raises')

    const row = page.getByTestId('limit-raise-req-1')
    await expect(row).toBeVisible()
    await expect(row.getByTestId('limit-raise-status')).toHaveText(/approved/i)

    // The figure she must plan against, shown as money and not as a raw
    // micro-USD integer she has to divide in her head.
    await expect(row.getByTestId('limit-raise-approved-amount')).toHaveText('$50.00')
    // And her own ask, so the shortfall is visible as a comparison rather than
    // something she discovers by hitting it.
    await expect(row.getByTestId('limit-raise-asked-amount')).toHaveText('$200.00')

    // The deadline, before it arrives rather than by dying at it.
    await expect(row.getByTestId('limit-raise-expires-at')).toContainText('2026-07-31')
    // Who decided, as an id the console resolves — never an address.
    await expect(row.getByTestId('limit-raise-approver')).toBeVisible()
    await expect(row.getByTestId('limit-raise-approver')).not.toContainText('@')
    // And why she got half, so tomorrow's ask is a better ask instead of an
    // identical re-file.
    await expect(row.getByTestId('limit-raise-decision-comment')).toContainText(
      'half of the ask',
    )
  })

  test('hands her the approvable maximum when her ask exceeds the cap', async ({
    page,
  }) => {
    // Seam S12, from her seat. The minimum raise that would unblock her is
    // derived from her shortfall; the remaining grant cap is a hard ceiling on
    // what any approver may grant. When the first exceeds the second, the
    // figure the console leads her to is one no approver can grant, and she
    // spends a day of latency discovering it. What she would be told wrongly:
    // that her request is in the queue and someone will decide it. She must
    // learn the maximum here, in the refusal, or tomorrow's re-file is the
    // identical dead end.
    let capturedPostBody: Record<string, unknown> | null = null

    await seedUserSession(page)
    await mockCommonRoutes(page)

    // Stateful within the test: an empty list until a POST lands, then the
    // refusal is the only thing that happened — so the assertion below proves a
    // round trip rather than a rendered fixture.
    await page.route('**/api/mvp/me/limit-raises**', (route) => {
      if (route.request().method() === 'POST') {
        capturedPostBody = route.request().postDataJSON()
        return route.fulfill({
          status: 422,
          json: {
            detail: {
              reason: 'grant_cap_exceeded',
              // The envelope carries the cap precisely so no client has to
              // pre-validate against a figure that can drift.
              remaining_cap_microusd: REMAINING_CAP_MICROUSD,
              asked_amount_microusd: 200_000_000,
            },
          },
        })
      }
      return route.fulfill({ json: { requests: [] } })
    })

    await page.goto('/me/limit-raises')

    await page.getByTestId('limit-raise-new-button').click()
    await page.getByTestId('limit-raise-amount-input').fill('$200')
    await page.getByTestId('limit-raise-reason-select').selectOption('deadline')
    await page.getByTestId('limit-raise-comment-input').fill('shipping Friday')
    await page.getByTestId('limit-raise-submit').click()

    // The refusal has to be actionable. A bare code, or a generic "request
    // failed", leaves her with no figure to type and the same ask tomorrow.
    const error = page.getByTestId('limit-raise-error')
    await expect(error).toBeVisible()
    await expect(error).toContainText('$75.00')

    // And the request that was refused must not be shown as filed — a view that
    // optimistically lists it makes her wait for a decision on a request that
    // does not exist.
    await expect(page.getByTestId('limit-raise-req-1')).toHaveCount(0)

    // The tenant travelled with the refusal that sent her here and was not
    // taken from her ambient session, whose tenant is `default-org`.
    expect(capturedPostBody).not.toBeNull()
    expect(capturedPostBody?.tenant_id).toBe(TENANT_ID)
    // Integer micro-USD, not a float: $200 is 200000000, never 2.0e8 rounded.
    expect(capturedPostBody?.asked_amount_microusd).toBe(200_000_000)
  })
})
