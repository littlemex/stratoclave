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
// **Convergence correction.** This file was rewritten against the REAL,
// already-shipped `frontend/src/pages/MeLimitRaises.tsx` (verified against
// its own already-green unit suite, `MeLimitRaises.test.tsx`). Several of
// this file's original guesses did not survive contact with the real
// component or the real backend (`backend/mvp/grants.py`):
//
//   - There is no `data-testid="limit-raise-req-<n>"` per row, no
//     `limit-raise-status`/`limit-raise-approved-amount`/
//     `limit-raise-asked-amount`/`limit-raise-expires-at`/
//     `limit-raise-approver`/`limit-raise-decision-comment`, no
//     `limit-raise-new-button` (there is no separate "new request" dialog --
//     the form is always on the page), no `limit-raise-amount-input`/
//     `limit-raise-reason-select`/`limit-raise-comment-input`/
//     `limit-raise-submit`/`limit-raise-error`. The real ids are
//     `lr-amount-input`, `lr-reason-select`, `lr-comment-input`,
//     `lr-submit-button`, `lr-status-approved`, `lr-status-pending`.
//   - `POST /api/mvp/me/limit-raises` (`SubmitLimitRaiseRequest`) has no
//     `tenant_id` field at all (`extra="forbid"`) -- the tenant is always
//     the caller's own session. The real field is `asked_amount_microusd`
//     (never `requested_amount_microusd`), and `reason_code` must be one of
//     the four real `RAISE_REASON_CODES` (`onboarding`, `usage_spike`,
//     `migration`, `incident_response`, `other`) -- `deadline` is not one.
//   - The wire `status` is lowercase (`"approved"`/`"pending"`), and
//     `expires_at` is an epoch-SECONDS integer, never an ISO string
//     (`mvp/grants.py::_request_public`).
//   - The approver identity field is `approver_id`.
//   - **The second test's whole premise does not hold.** `GrantCapExceeded`
//     (the `422 grant_cap_exceeded` this test built its second case around)
//     is raised only inside `approve_limit_raise` -- the APPROVER's decision
//     path -- never at submission time (verified against
//     `backend/mvp/grants.py`: `submit_limit_raise` does not read the grant
//     cap at all). The B6 "do not recommend a raise no approver may grant"
//     behaviour this test wanted is a CLIENT-side rule driven by the
//     `raise_hint` a 402 carries, rendered from React Router navigation
//     state (`useLocation().state.raiseHint`, contract amendment U4) -- and
//     by the component's own comment, "nothing in this console yet sends a
//     chat/completions request that could 402", so there is today no real,
//     URL-navigable path that lands a browser on this page with a hint
//     attached. That behaviour is already fully covered at the unit level
//     (`MeLimitRaises.test.tsx`'s "B6" describe block, green) by injecting
//     router state directly, which Playwright's URL-only navigation cannot
//     reproduce against the real `BrowserRouter`. The second case below is
//     replaced with a real, URL-navigable round trip this page DOES support
//     end to end: filing a plain request from the form and seeing it land
//     in her own list as pending, worded so it does not read as queued or
//     already decided.

import { expect, test, type Page } from '@playwright/test'

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
    org_id: 'acme-eng',
    roles: ['user'],
    total_credit: 1_000_000,
    credit_used: 0,
    remaining_credit: 1_000_000,
    currency: 'tokens',
    tenant: { tenant_id: 'acme-eng', name: 'Acme Eng' },
    locale: 'en',
  }
}

const REASON_CODES = ['onboarding', 'usage_spike', 'migration', 'incident_response', 'other']

// `expires_at` is the wire's epoch-SECONDS int (`backend/mvp/grants.py`'s
// `_request_public`), not an ISO string.
const AUG_31_2026_EOD_EPOCH = Math.floor(Date.UTC(2026, 7, 31, 23, 59, 59) / 1000)

// The row her own request list returns once he has decided it. `asked` and
// `approved` sit side by side because "you got less" is a comparison and she
// cannot make it from one number.
function decidedRequest() {
  return {
    request_id: 'req-1',
    tenant_id: 'acme-eng',
    limit_kind: 'tenant_dollar_pool',
    status: 'approved',
    reason_code: 'migration',
    asked_amount_microusd: 200_000_000,
    // The facts that live on the grant row and reach her nowhere else.
    approved_amount_microusd: 50_000_000,
    expires_at: AUG_31_2026_EOD_EPOCH,
    approver_id: 'approver-1',
    decision_comment: 'half of the ask, one week',
    created_at: '2026-07-24T09:00:00Z',
    observed_limit_microusd: null,
    observed_remaining_microusd: null,
    observed_at: null,
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
  // R12: "the walls that apply to the caller" -- called unconditionally on
  // mount by the real component. No pool row keeps this test's assertions
  // focused on the request list.
  await page.route('**/api/mvp/me/limit-raises/wall-status', (route) =>
    route.fulfill({ json: { tenant_id: 'acme-eng', period: '2026-09', pool: null } }),
  )
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
    await page.route('**/api/mvp/me/limit-raises', (route) => {
      if (route.request().method() !== 'GET') return route.continue()
      return route.fulfill({
        json: { tenant_id: 'acme-eng', requests: [decidedRequest()], reason_codes: REASON_CODES },
      })
    })

    await page.goto('/me/limit-raises')

    const approved = page.getByTestId('lr-status-approved')
    await expect(approved).toBeVisible()
    // Both figures in one sentence (the contract's own wording: "Approved
    // $50.00, expires ..."), so a plain substring match against the whole
    // sentence is exactly the shape a person reads.
    await expect(approved).toContainText('$50.00')
    // The deadline, before it arrives rather than by dying at it.
    await expect(approved).toContainText('Aug 31, 2026')

    // Her own ask, so the shortfall is visible as a comparison rather than
    // something she discovers by hitting it — a different cell from the
    // approved-amount sentence above.
    await expect(page.getByText('$200.00')).toBeVisible()

    // Who decided, as an id the console resolves — never an address.
    const approver = page.getByTestId('lr-status-approver')
    await expect(approver).toBeVisible()
    await expect(approver).toContainText('approver-1')
    await expect(approver).not.toContainText('@')

    // And why she got half, so tomorrow's ask is a better ask instead of an
    // identical re-file.
    await expect(page.getByText(/half of the ask/)).toBeVisible()
  })

  test('a plain filing lands as pending, never reading as decided or queued', async ({
    page,
  }) => {
    // Replaces this file's original second case (see the file-level comment
    // for why the over-cap/B6 scenario it built is not reachable through a
    // real browser navigation today, and is already covered at the unit
    // level). What remains real and E2E-checkable: the round trip from an
    // empty list, through the plain form, to her own request appearing --
    // and reading honestly as NOT YET DECIDED rather than as queued work or
    // (worse) as already granted.
    let capturedPostBody: Record<string, unknown> | null = null
    let filed = false

    await seedUserSession(page)
    await mockCommonRoutes(page)

    await page.route('**/api/mvp/me/limit-raises', (route) => {
      const method = route.request().method()
      if (method === 'POST') {
        capturedPostBody = route.request().postDataJSON()
        filed = true
        return route.fulfill({
          status: 201,
          json: {
            request_id: 'req-2',
            tenant_id: 'acme-eng',
            limit_kind: 'tenant_dollar_pool',
            status: 'pending',
            reason_code: capturedPostBody?.reason_code ?? 'migration',
            asked_amount_microusd: capturedPostBody?.asked_amount_microusd ?? 0,
            approved_amount_microusd: null,
            expires_at: null,
            approver_id: null,
            decision_comment: null,
            created_at: '2026-07-24T09:00:00Z',
            observed_limit_microusd: null,
            observed_remaining_microusd: null,
            observed_at: null,
          },
        })
      }
      // GET: empty until the POST above lands, then her one pending request
      // — proving the round trip rather than rendering a static fixture.
      return route.fulfill({
        json: {
          tenant_id: 'acme-eng',
          requests: filed
            ? [
                {
                  request_id: 'req-2',
                  tenant_id: 'acme-eng',
                  limit_kind: 'tenant_dollar_pool',
                  status: 'pending',
                  reason_code: capturedPostBody?.reason_code ?? 'migration',
                  asked_amount_microusd: capturedPostBody?.asked_amount_microusd ?? 0,
                  approved_amount_microusd: null,
                  expires_at: null,
                  approver_id: null,
                  decision_comment: null,
                  created_at: '2026-07-24T09:00:00Z',
                  observed_limit_microusd: null,
                  observed_remaining_microusd: null,
                  observed_at: null,
                },
              ]
            : [],
          reason_codes: REASON_CODES,
        },
      })
    })

    await page.goto('/me/limit-raises')
    await expect(page.getByText(/have not filed/i)).toBeVisible()

    await page.getByTestId('lr-amount-input').fill('200')
    await page.getByTestId('lr-reason-select').selectOption('migration')
    await page.getByTestId('lr-comment-input').fill('shipping the migration on Friday')
    await page.getByTestId('lr-submit-button').click()

    // The round trip actually happened, with the real field names.
    await expect.poll(() => capturedPostBody).not.toBeNull()
    expect(capturedPostBody?.asked_amount_microusd).toBe(200_000_000)
    expect(capturedPostBody?.reason_code).toBe('migration')
    expect(capturedPostBody).not.toHaveProperty('tenant_id')

    // Her new request appears, and reads as genuinely undecided — "PENDING"
    // alone, or any wording implying it is queued/guaranteed work, is the
    // defect this assertion exists to catch.
    const pending = page.getByTestId('lr-status-pending')
    await expect(pending).toBeVisible()
    await expect(pending).toContainText(/did not (change|queue)|not (been )?queued|waiting for .* approve/i)
  })
})
