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

import { expect, test, type Page } from '@playwright/test'

const TENANT_ID = 'acme-eng'
const PERIOD = '2026-07'
// Computed server-side (F2 owns the predicate) and rendered here: the last
// instant a grant in this period may expire, because a grant outliving its
// period would have its capacity reset out from under it at rollover.
const PERIOD_END = '2026-07-31T23:59:59Z'

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
    tenant_name: 'Acme Eng',
    requester_id: 'engineer-1',
    limit_kind: 'tenant_pool',
    status: 'PENDING',
    reason_code: 'deadline',
    comment: 'shipping the migration on Friday',
    asked_amount_microusd: 200_000_000,
    created_at: '2026-07-24T09:00:00Z',
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
    status: 'active',
    pool_limit_microusd: 2_050_000_000,
    pool_reserved_microusd: 300_000_000,
    pool_settled_microusd: 1_700_000_000,
    pool_headroom_microusd: 50_000_000,
    remaining_microusd: 50_000_000,
    pool_limit_usd_cents: 205_000,
    remaining_usd_cents: 5_000,
    // The composition, so the number displayed as the limit can be decomposed
    // into the number he manages and the number he granted.
    baseline_microusd: 2_000_000_000,
    manual_limit_microusd: null,
    pool_granted_microusd: 50_000_000,
    seat_count: 10,
    seat_entitlement_microusd: 2_000_000_000,
    grant_cap_microusd: 400_000_000,
    remaining_grant_cap_microusd: 350_000_000,
    latest_permissible_expiry: PERIOD_END,
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
    await page.route('**/api/mvp/admin/limit-raises**', (route) =>
      route.fulfill({ json: { requests: [pendingRequest()] } }),
    )
    await page.route(`**/api/mvp/admin/tenants/${TENANT_ID}/pool-budget**`, (route) =>
      route.fulfill({ json: tenantPoolNow() }),
    )

    await page.goto('/admin/limit-raises')

    const row = page.getByTestId('limit-raise-req-1')
    await expect(row).toBeVisible()
    await expect(row.getByTestId('limit-raise-asked-amount')).toHaveText('$200.00')

    // Both figures on screen, each in its own labelled place. If only one
    // appears he cannot tell which he is reading; if both appear unlabelled it
    // is worse, because two different numbers for "remaining" read as an error.
    const snapshot = page.getByTestId('limit-raise-observed-remaining')
    await expect(snapshot).toHaveText('$400.00')
    // And the snapshot must say when it was taken, or "hours ago" is invisible.
    await expect(page.getByTestId('limit-raise-observed-at')).toContainText(
      '2026-07-24',
    )

    const now = page.getByTestId('tenant-current-remaining')
    await expect(now).toHaveText('$50.00')
    await expect(page.getByTestId('tenant-current-reserved')).toHaveText('$300.00')
    await expect(page.getByTestId('tenant-current-settled')).toHaveText('$1,700.00')

    // The composition, so the $2,050 he sees decomposes into the $2,000 he
    // manages and the $50 already granted. Without it he reads one number and
    // R17's mode sentence is the only thing standing between him and retyping
    // a figure that double-counts the grant.
    await expect(page.getByTestId('tenant-pool-granted')).toHaveText('$50.00')
    await expect(page.getByTestId('tenant-baseline')).toHaveText('$2,000.00')
    // Seat-tracked, stated as a sentence rather than a field, because the one
    // thing his next PUT does silently is end it forever.
    await expect(page.getByTestId('tenant-sizing-mode')).toContainText(/seat/i)
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
    await page.route('**/api/mvp/admin/limit-raises**', (route) =>
      route.fulfill({ json: { requests: [pendingRequest()] } }),
    )
    await page.route(`**/api/mvp/admin/tenants/${TENANT_ID}/pool-budget**`, (route) =>
      route.fulfill({ json: tenantPoolNow() }),
    )

    await page.goto('/admin/limit-raises')
    await page.getByTestId('limit-raise-approve-button').click()

    // Visible before the field is touched. Being told after a 422 is being told
    // too late: the round trip already cost her a day.
    const window_ = page.getByTestId('limit-raise-latest-expiry')
    await expect(window_).toBeVisible()
    await expect(window_).toContainText('2026-07-31')

    // And the input itself must not accept beyond it, so the refusal is not the
    // mechanism that teaches him the rule.
    await expect(page.getByTestId('limit-raise-expires-at-input')).toHaveAttribute(
      'max',
      new RegExp('2026-07-31'),
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
    // request still fails, and the grant visibly did nothing. Any percentage
    // has the same problem in the other direction: used is 130% of the ceiling
    // and a bar capped at 100 hides exactly the $30 that matters.
    await seedAdminSession(page)
    await mockCommonRoutes(page)
    await page.route('**/api/mvp/admin/limit-raises**', (route) =>
      route.fulfill({
        json: {
          requests: [
            pendingRequest({
              asked_amount_microusd: 10_000_000,
              observed_remaining_microusd: -30_000_000,
            }),
          ],
        },
      }),
    )
    await page.route(`**/api/mvp/admin/tenants/${TENANT_ID}/pool-budget**`, (route) =>
      route.fulfill({
        json: tenantPoolNow({
          pool_limit_microusd: 100_000_000,
          pool_reserved_microusd: 40_000_000,
          pool_settled_microusd: 90_000_000,
          pool_headroom_microusd: -30_000_000,
          remaining_microusd: -30_000_000,
          pool_granted_microusd: 0,
          baseline_microusd: 100_000_000,
        }),
      }),
    )

    await page.goto('/admin/limit-raises')

    // Signed, and negative. `$0.00` here is the defect, not a rounding choice.
    await expect(page.getByTestId('tenant-current-remaining')).toHaveText('-$30.00')
    // Said again in words he can act on, because a minus sign is easy to miss
    // on a screen full of money.
    await expect(page.getByTestId('tenant-over-ceiling-by')).toContainText('$30.00')

    // Her ask is $10 against a $30 deficit. The screen must let him see that
    // the first $30 of anything he grants buys nothing — otherwise he approves,
    // she is still refused, and neither of them can explain it.
    await expect(page.getByTestId('limit-raise-asked-amount')).toHaveText('$10.00')
    await expect(page.getByTestId('limit-raise-covers-deficit-warning')).toBeVisible()

    // And no percentage capped at 100: used is 130% here and a full bar is a
    // lie about $30.
    const used = page.getByTestId('tenant-used-percent')
    await expect(used).toBeVisible()
    await expect(used).not.toHaveText('100%')
    await expect(used).toContainText('130')
  })
})
