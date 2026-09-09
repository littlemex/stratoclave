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
//   - The wire `status` carries the spelling it is STORED in (`"APPROVED"`,
//     `"PENDING"`), so a value read from the API compares against the stored
//     one with no translation step, and
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
    status: 'APPROVED',
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
  // `user_dollar: null` rather than an omitted key: this deployment DOES report the
  // per-user wall and is saying the tenant has not configured one. Omitting it would
  // exercise the older-backend branch in every case in this file.
  await page.route('**/api/mvp/me/limit-raises/wall-status', (route) =>
    route.fulfill({
      json: { tenant_id: 'acme-eng', period: '2026-09', pool: null, user_dollar: null },
    }),
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
            status: 'PENDING',
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
                  status: 'PENDING',
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

// ---------------------------------------------------------------------------
// The engineer who wants to label the work, and to find it again later
// ---------------------------------------------------------------------------
//
// Two steps she walks that no unit test can walk: choosing which of two ceilings
// to file against when the two are different asks with different approvers, and
// coming back at the end of the month to total what a piece of work cost. Both are
// satisfiable by green unit tests on every endpoint involved while she still ends
// up filing against the wrong wall or reading a total as the cost of the work.

test.describe('the engineer labelling the work she is about to pay for', () => {
  test('files against her personal ceiling with a tag, and the tag survives verbatim', async ({
    page,
  }) => {
    // The step: she was refused by her OWN ceiling, not the tenant pool, and she
    // wants this month's migration spend findable later. Two things must be true at
    // the end -- the request went against the wall she picked, and the tag reached
    // the server as she typed it. A page that quietly lowercased it, or that filed
    // against the default wall because the selector was decorative, passes every
    // endpoint's own tests and fails her.
    let captured: Record<string, unknown> | null = null
    let filed = false

    await seedUserSession(page)
    await mockCommonRoutes(page)
    // Her tenant DOES have a per-user ceiling, and she has already spent most of it.
    await page.unroute('**/api/mvp/me/limit-raises/wall-status')
    await page.route('**/api/mvp/me/limit-raises/wall-status', (route) =>
      route.fulfill({
        json: {
          tenant_id: 'acme-eng',
          period: '2026-09',
          pool: {
            status: 'active',
            pool_limit_microusd: 400_000_000,
            remaining_microusd: 380_000_000,
            remaining_grant_cap_microusd: 200_000_000,
          },
          user_dollar: {
            base_microusd: 10_000_000,
            granted_microusd: 0,
            ceiling_microusd: 10_000_000,
            used_microusd: 9_800_000,
            remaining_microusd: 200_000,
            base_is_sealed: true,
          },
        },
      }),
    )

    const filedRow = () => ({
      request_id: 'req-tagged',
      tenant_id: 'acme-eng',
      limit_kind: String(captured?.limit_kind ?? ''),
      status: 'PENDING',
      reason_code: String(captured?.reason_code ?? 'migration'),
      asked_amount_microusd: Number(captured?.asked_amount_microusd ?? 0),
      approved_amount_microusd: null,
      expires_at: null,
      approver_id: null,
      decision_comment: null,
      created_at: '2026-09-09T09:00:00Z',
      observed_limit_microusd: null,
      observed_remaining_microusd: null,
      observed_at: null,
      // The gateway stores the CANONICAL form, which differs in case from what she
      // typed. Her own list therefore shows her `migration-42`, not `Migration-42`,
      // and that is correct rather than a bug to paper over in the UI.
      task_tag: String(captured?.task_tag ?? '').toLowerCase(),
      task_tag_source: 'asserted',
    })

    await page.route('**/api/mvp/me/limit-raises', (route) => {
      if (route.request().method() === 'POST') {
        captured = route.request().postDataJSON()
        filed = true
        return route.fulfill({ status: 201, json: filedRow() })
      }
      return route.fulfill({
        json: {
          tenant_id: 'acme-eng',
          requests: filed ? [filedRow()] : [],
          reason_codes: REASON_CODES,
        },
      })
    })

    await page.goto('/me/limit-raises')

    // Before she chooses: she can see that her own ceiling is nearly gone while the
    // tenant pool is fine. This is what makes the choice informed rather than a
    // guess, and a guess costs her a whole day -- the slot is per wall per person
    // per UTC day.
    await expect(page.getByTestId('wall-user-dollar')).toContainText('$0.20')
    await expect(page.getByTestId('wall-pool')).toContainText('$380.00')
    await expect(page.getByTestId('lr-wall-slot-note')).toBeVisible()

    await page.getByTestId('lr-wall-select').selectOption('user_dollar_quota')
    await page.getByTestId('lr-amount-input').fill('50')
    await page.getByTestId('lr-reason-select').selectOption('migration')
    await page.getByTestId('lr-task-tag-input').fill('Migration-42')
    await page.getByTestId('lr-submit-button').click()

    await expect.poll(() => captured).not.toBeNull()
    // The wall she picked, spelled the way the backend's registry spells it.
    expect(captured?.limit_kind).toBe('user_dollar_quota')
    // Her capitals intact. A client that pre-canonicalised would be a second
    // canonicaliser, and would show her a string it did not send.
    expect(captured?.task_tag).toBe('Migration-42')

    // And afterwards her own list tells her which ceiling and which tag, so two
    // raises against two walls are not one indistinguishable pair of rows.
    await expect(page.getByTestId('lr-row-task-tag')).toContainText('migration-42')
    // Scoped to the CELL: the same words are also the selector's option label, and a
    // bare text match resolves to both.
    await expect(page.getByRole('cell', { name: 'My personal ceiling' })).toBeVisible()
  })

  test('a second filing the same day is answered with the request she already has', async ({
    page,
  }) => {
    // The step nobody designs for: she is not sure her submission landed, so she
    // presses again. The truthful answer is that it went through, and the screen
    // must say that rather than showing her a failure -- otherwise she files a
    // support ticket about a request that exists.
    await seedUserSession(page)
    await mockCommonRoutes(page)
    await page.route('**/api/mvp/me/limit-raises', (route) => {
      if (route.request().method() === 'POST') {
        return route.fulfill({
          status: 409,
          json: {
            detail: {
              type: 'limit_raise_daily_slot_occupied',
              message: 'You have already filed a limit raise today.',
              holder_request_id: 'req-earlier',
              holder_status: 'PENDING',
              reset_at: '2026-09-10T00:00:00+00:00',
            },
          },
        })
      }
      return route.fulfill({
        json: { tenant_id: 'acme-eng', requests: [], reason_codes: REASON_CODES },
      })
    })

    await page.goto('/me/limit-raises')
    await page.getByTestId('lr-amount-input').fill('50')
    await page.getByTestId('lr-reason-select').selectOption('migration')
    await page.getByTestId('lr-submit-button').click()

    const notice = page.getByTestId('lr-slot-occupied')
    await expect(notice).toBeVisible()
    // The id of the request that already exists, so she can go and look at it.
    await expect(notice).toContainText('req-earlier')
    // And when she may try again, with its zone, because a reset time read in her
    // own timezone is wrong for most of the world by up to a day.
    await expect(notice).toContainText('2026-09-10')
  })

  test('reads her own spend by tag, and is told what the totals do not mean', async ({
    page,
  }) => {
    // End of the month. She wants to know what the migration cost. The number alone
    // is the trap: a request for the same work that carried no tag, or whose tag was
    // discarded, is under `unlabelled` with nothing linking it back -- so the row is
    // a floor. If the screen shows her $45.00 and she reports that as the cost of
    // the migration, every endpoint involved was correct and she was still wrong.
    await seedUserSession(page)
    await mockCommonRoutes(page)
    await page.route('**/api/mvp/me/usage/by-tag**', (route) =>
      route.fulfill({
        json: {
          period: '2026-09',
          rows: [
            {
              user_id: 'engineer-1',
              task_tag: 'migration-42',
              requests: 88,
              absent_count: 0,
              dropped_grammar_count: 0,
              cost_microusd: 45_000_000,
              requests_without_cost: 0,
              input_tokens: 900_000,
              output_tokens: 120_000,
            },
            {
              user_id: 'engineer-1',
              task_tag: 'unlabelled',
              requests: 31,
              absent_count: 30,
              dropped_grammar_count: 1,
              cost_microusd: 12_000_000,
              requests_without_cost: 0,
              input_tokens: 200_000,
              output_tokens: 30_000,
            },
          ],
          truncated: false,
          legacy_rows: 0,
          malformed_rows: 0,
          tag_is_caller_asserted: true,
          retention_policy_days: 95,
          retention_deletion_is_asynchronous: true,
          retention_boundary_period_may_fold_incompletely: true,
          tag_total_is_a_lower_bound: true,
        },
      }),
    )

    await page.goto('/me/usage/by-tag')

    await expect(page.getByText('migration-42')).toBeVisible()
    await expect(page.getByText('$45.00')).toBeVisible()

    // The two sentences that decide whether she reports that figure as the cost.
    await expect(page.getByTestId('bt-lower-bound')).toBeVisible()
    await expect(page.getByTestId('bt-caller-asserted')).toBeVisible()

    // And the split inside the unlabelled bucket: 30 requests nobody tagged is a
    // habit, 1 discarded tag is a mistake, and one number cannot say which.
    const split = page.getByTestId('bt-row-unlabelled-split')
    await expect(split).toBeVisible()
    await expect(split).toContainText('30')
    await expect(split).toContainText('1')
  })
})
