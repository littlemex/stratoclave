// Persona journey in the browser: the tenant administrator deciding a raise.
//
// NOT a requirement test. Each case walks a step he walks and asks whether the
// numbers he decides against are true at the moment he reads them. All three
// are satisfiable by passing unit tests on every endpoint involved while he
// still decides against figures that were true hours ago, types a window the
// server will refuse, or grants into a hole no screen shows him.
//
// Runs against the dev server with no live backend, the same shape as
// `tenant-pool-budget.spec.ts`: an admin session seeded into sessionStorage and
// `page.route` mocks that are stateful within a test, so the approval round
// trip is genuinely proven rather than stubbed.
//
//   1. The request's snapshot is hours old. It must be labelled apart from the
//      tenant's position now, or he reads stale numbers as current.
//   2. Every grant dies at the period boundary, so on the 29th his typed
//      `now + 7d` is refused for a window no screen showed him first.
//   3. Headroom is minus $30 and every surface clamps it to zero, so his $10
//      grant visibly does nothing.
//
// **Convergence correction.** This file was originally written blind, with
// no access to the real `LimitRaiseApproval.tsx` / `PoolBudgetCard.tsx`.
// Rewritten against the real components: real route
// (`/admin/tenants/:tenantId/limit-raises`, not `/admin/limit-raises`), real
// `data-testid`s (`pool-budget-summary`, `pool-available`, `pool-granted`,
// `pool-seats`, `lr-comment`, `lr-snapshot-block`,
// `lr-latest-permissible-expiry`, `lr-expiry-input`, ...), real wire shapes
// (lowercase `status`, `expires_at`/`latest_permissible_expiry` as epoch
// ints, `user_id` not `requester_id`, `remaining_microusd` not
// `pool_headroom_microusd`), and the real `latest-permissible-expiry`
// endpoint, which this file never mocked before. Two assertions in case 3
// (`tenant-used-percent`, `limit-raise-covers-deficit-warning`) named UI
// that does not exist anywhere in the real components -- dropped rather
// than invented; the deficit is still verified through the real signed
// `pool-available` figure and `pool-over-ceiling` line.

import { expect, test, type Page } from '@playwright/test'

const TENANT_ID = 'acme-eng'
const PERIOD = '2026-07'
// Computed server-side (F2 owns the predicate) and rendered here: the last
// instant a grant in this period may expire, because a grant outliving its
// period would have its capacity reset out from under it at rollover.
// Noon UTC, deliberately not 23:59:59Z: `LimitRaiseApproval.tsx` renders the
// expiry input's `max` from a LOCAL-time conversion of this epoch
// (`toLocalInputValue`), and a value near the UTC day boundary would land on
// a different calendar date once converted to whatever timezone the test
// runner is in.
const PERIOD_END_EPOCH = Math.floor(Date.parse('2026-07-31T12:00:00Z') / 1000)

function seedAdminSession(page: Page) {
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
    user_id: 'approver-1',
    email: 'approver@example.com',
    org_id: 'default-org',
    roles: ['admin'],
    total_credit: 1_000_000,
    credit_used: 0,
    remaining_credit: 1_000_000,
    currency: 'tokens',
    tenant: { tenant_id: 'default-org', name: 'Default' },
    locale: 'en',
  }
}

// Her pending ask, carrying the numbers as they stood WHEN SHE FILED. The
// `observed_*` pair is a snapshot and is the whole subject of case 1.
function pendingRequest(overrides: Record<string, unknown> = {}) {
  return {
    request_id: 'req-1',
    tenant_id: TENANT_ID,
    user_id: 'engineer-1',
    limit_kind: 'tenant_dollar_pool',
    // Wire status is lowercase (`mvp.grants._request_public` lowercases the
    // stored `STATUS_PENDING`).
    status: 'PENDING',
    reason_code: 'migration',
    comment: 'shipping the migration on Friday',
    asked_amount_microusd: 200_000_000,
    created_at: '2026-07-24T09:00:00Z',
    // Always present per R24 (null until decided).
    approved_amount_microusd: null,
    expires_at: null,
    approver_id: null,
    // Taken at 09:00. He is reading this at 17:00.
    observed_at: '2026-07-24T09:00:00Z',
    observed_limit_microusd: 2_050_000_000,
    observed_remaining_microusd: 400_000_000,
    ...overrides,
  }
}

// F2's tenant read: the ceiling's composition plus the position NOW. F3 renders
// this and does not compute it, so a test that fixtures the composition is
// testing the same numbers the screen is supposed to be showing.
function tenantPoolNow(overrides: Record<string, unknown> = {}) {
  return {
    tenant_id: TENANT_ID,
    period: PERIOD,
    status: 'ACTIVE',
    pool_limit_microusd: 2_050_000_000,
    pool_reserved_microusd: 300_000_000,
    pool_settled_microusd: 1_700_000_000,
    // `PoolBudgetResponse` (`mvp/admin_tenants.py`) names this
    // `remaining_microusd` -- `pool_headroom_microusd` is the
    // repository-level dict's key, never the wire field.
    remaining_microusd: 50_000_000,
    over_ceiling_microusd: 0,
    pool_limit_usd_cents: 205_000,
    remaining_usd_cents: 5_000,
    mode_sentence: 'This budget follows the tenant’s seat count.',
    seat_tracked: true,
    // The composition, so the number displayed as the limit can be decomposed
    // into the number he manages and the number he granted.
    baseline_microusd: 2_000_000_000,
    manual_limit_microusd: null,
    pool_granted_microusd: 50_000_000,
    seat_count: 10,
    seat_rate_microusd: 200_000_000,
    seat_entitlement_microusd: 2_000_000_000,
    entitlement_exceeds_figure: false,
    resume_action: null,
    grant_cap_microusd: 400_000_000,
    effective_grant_cap_microusd: 400_000_000,
    grant_cap_is_derived: false,
    remaining_grant_cap_microusd: 350_000_000,
    ...overrides,
  }
}

async function mockCommonRoutes(page: Page) {
  // The SPA fetches /config.json on cold start; without a valid one it shows
  // the bilingual "Configuration load failed" splash and never mounts React.
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
  // Mocked in every case: the component fetches this unconditionally
  // (`ns.latestPermissibleExpiry()`, no `enabled` gate) and case 1/3 do not
  // exercise it directly but the page would otherwise hang on a real
  // network request under Playwright's route interception.
  await page.route(
    '**/api/mvp/admin/limit-raises/latest-permissible-expiry**',
    (route) =>
      route.fulfill({
        json: { period: PERIOD, latest_permissible_expiry: PERIOD_END_EPOCH },
      }),
  )
}

test.describe('the tenant administrator deciding a raise', () => {
  test('labels the request snapshot apart from the tenant position now', async ({
    page,
  }) => {
    // Persona 2 question 1 — third on his misled-first ranking. The
    // `observed_limit`/`observed_remaining` pair was snapshotted when she filed,
    // possibly hours earlier. What he would be told wrongly: that the remaining
    // capacity on the screen is the remaining capacity now. Here the snapshot
    // says $400 of room and the tenant actually has $50 — so a decision that
    // looks generous against the snapshot is the difference between helping her
    // and not, and nothing on an unlabelled screen distinguishes the two.
    await seedAdminSession(page)
    await mockCommonRoutes(page)
    await page.route('**/api/mvp/admin/limit-raises?**', (route) =>
      route.fulfill({ json: { requests: [pendingRequest()], reason_codes: [] } }),
    )
    await page.route(`**/api/mvp/admin/tenants/${TENANT_ID}/pool-budget**`, (route) =>
      route.fulfill({ json: tenantPoolNow() }),
    )

    await page.goto(`/admin/tenants/${TENANT_ID}/limit-raises`)

    // Her own ask, shown on the queue row (`LimitRaiseApproval.tsx`
    // `DecisionRow` renders `fmtMicroUsd(request.asked_amount_microusd)`
    // next to "Asked by {{user}}" -- no dedicated testid for the amount, so
    // matched by text).
    await expect(page.getByText('Asked by engineer-1')).toBeVisible()
    await expect(page.getByText('$200.00')).toBeVisible()
    // The requester's own comment, distinct from any decision comment.
    await expect(page.getByTestId('lr-comment')).toHaveText(
      'shipping the migration on Friday',
    )

    // Both figures on screen, each in its own labelled place. If only one
    // appears he cannot tell which he is reading; if both appear unlabelled it
    // is worse, because two different numbers for "remaining" read as an error.
    const snapshot = page.getByTestId('lr-snapshot-block')
    await expect(snapshot).toContainText('$400.00')
    await expect(snapshot).toContainText('$2,050.00')
    // And the snapshot must say when it was taken, or "hours ago" is invisible.
    await expect(snapshot).toContainText('2026')

    // The tenant's position NOW, from `PoolBudgetCard` -- a DIFFERENT block
    // from the snapshot above, so the two can disagree without looking like
    // the same number twice.
    await expect(page.getByTestId('pool-available')).toHaveText('$50.00')
    await expect(page.getByTestId('pool-reserved')).toHaveText('$300.00')
    await expect(page.getByTestId('pool-settled')).toHaveText('$1,700.00')

    // The composition, so the $2,050 he sees decomposes into the $2,000 he
    // manages and the $50 already granted.
    await expect(page.getByTestId('pool-granted')).toHaveText('$50.00')
    await expect(page.getByTestId('pool-limit')).toHaveText('$2,050.00')
    // Seat-tracked, stated as a sentence rather than a field.
    await expect(page.getByTestId('pool-mode-sentence')).toContainText(/seat/i)
  })

  test('shows the latest permissible expiry before he types one', async ({
    page,
  }) => {
    // Persona 2 question 7. Every active grant dies at the month boundary,
    // because a grant outliving its period has its capacity reset out from
    // under it. So on the 29th his habitual `now + 7d` is unsatisfiable. What he
    // would be told wrongly: nothing at all — he types a window, the server
    // refuses it, and no screen told him the real one before he typed. The
    // window is computed server-side and rendered here; a client recomputing it
    // would be a second implementation of a lifecycle rule.
    await seedAdminSession(page)
    await mockCommonRoutes(page)
    await page.route('**/api/mvp/admin/limit-raises?**', (route) =>
      route.fulfill({ json: { requests: [pendingRequest()], reason_codes: [] } }),
    )
    await page.route(`**/api/mvp/admin/tenants/${TENANT_ID}/pool-budget**`, (route) =>
      route.fulfill({ json: tenantPoolNow() }),
    )

    await page.goto(`/admin/tenants/${TENANT_ID}/limit-raises`)

    // Visible before the field is touched — the expiry input and its
    // supporting text render inline with every pending request, with no
    // "start approving" step to click through first. Being told after a
    // 422 is being told too late: the round trip already cost her a day.
    const window_ = page.getByTestId('lr-latest-permissible-expiry')
    await expect(window_).toBeVisible()
    // `formatDate` renders via `Date.toLocaleString()` (locale-formatted,
    // e.g. "7/31/2026, 9:00:00 PM"), never an ISO string.
    await expect(window_).toContainText('7/31/2026')

    // And the input itself must not accept beyond it, so the refusal is not the
    // mechanism that teaches him the rule. `lr-expiry-input`'s `max` comes from
    // `toLocalInputValue` (`YYYY-MM-DDTHH:mm`, LOCAL time) -- still July 31st
    // because `PERIOD_END_EPOCH` is noon UTC, far from the day boundary in any
    // realistic timezone.
    await expect(page.getByTestId('lr-expiry-input')).toHaveAttribute(
      'max',
      /2026-07-31/,
    )
  })

  test('shows a negative headroom as a signed deficit, never clamped', async ({
    page,
  }) => {
    // The personas' interaction case 4. A grant revoked with a hold still
    // outstanding leaves headroom legitimately negative: the money was admitted
    // while the grant was live. What he would be told wrongly: that the pool is
    // merely empty. A surface rendering `max(0, headroom)` shows $0, so he
    // grants the $10 she asked for, headroom moves from -$30 to -$20, every
    // request still fails, and the grant visibly did nothing.
    await seedAdminSession(page)
    await mockCommonRoutes(page)
    await page.route('**/api/mvp/admin/limit-raises?**', (route) =>
      route.fulfill({
        json: {
          requests: [
            pendingRequest({
              asked_amount_microusd: 10_000_000,
              observed_remaining_microusd: -30_000_000,
            }),
          ],
          reason_codes: [],
        },
      }),
    )
    await page.route(`**/api/mvp/admin/tenants/${TENANT_ID}/pool-budget**`, (route) =>
      route.fulfill({
        json: tenantPoolNow({
          pool_limit_microusd: 100_000_000,
          pool_reserved_microusd: 40_000_000,
          pool_settled_microusd: 90_000_000,
          remaining_microusd: -30_000_000,
          over_ceiling_microusd: 30_000_000,
          pool_granted_microusd: 0,
          baseline_microusd: 100_000_000,
          seat_tracked: false,
          manual_limit_microusd: 100_000_000,
        }),
      }),
    )

    await page.goto(`/admin/tenants/${TENANT_ID}/limit-raises`)

    // Signed, and negative. `$0.00` here is the defect, not a rounding choice
    // (`PoolStat`'s own `negative` styling is driven by
    // `pool.remaining_microusd < 0`, confirming the component treats this as
    // a real signed value rather than clamping it upstream).
    await expect(page.getByTestId('pool-available')).toHaveText('-$30.00')
    // Said again in words he can act on, because a minus sign is easy to miss
    // on a screen full of money.
    await expect(page.getByTestId('pool-over-ceiling')).toContainText('$30.00')

    // Her ask is $10 against a $30 deficit — visible side by side so the
    // shortfall is a comparison rather than something he discovers by
    // approving it and watching her still get refused.
    await expect(page.getByText('$10.00')).toBeVisible()
  })
})
