// LimitRaiseApproval — the tenant approval view.
//
// It must show, for an approver holding `limit-raises:approve` on a tenant:
// the ask, the reason, the comment, the requester, the ceiling's
// composition, the tenant's current reserved and settled, the remaining
// grant cap, and the latest permissible expiry -- shown before it is typed.
// It must let the approver approve with an amount and an expiry, or reject
// with a reason. The comment renders as text, never as HTML.
//
// The observed values on a request (`observed_limit`/`observed_remaining`)
// were taken when the request was filed, possibly hours earlier; this view
// shows the tenant's CURRENT reserved, settled and headroom alongside them,
// each labelled, so a reader cannot mistake the stale snapshot for the live
// figure. The tenant view also carries F1's mode sentence, the seat
// entitlement and the resume action.
//
// CONVERGENCE NOTE (F3 test/impl triage). This file originally assumed a
// single, invented `api.limitRaises.approvalDetail(requestId)` endpoint
// returning `{ request, current, ceiling, remaining_grant_cap_microusd,
// latest_permissible_expiry }` in one call, and a request-scoped route. The
// REAL, already-shipped component (`./LimitRaiseApproval.tsx`) is a
// TENANT-scoped queue: it composes THREE real endpoints
// (`ns.getPoolBudget`, `ns.latestPermissibleExpiry`, `ns.listLimitRaises`),
// delegates R21b's ceiling composition and R30's LIVE "current" position to
// `PoolBudgetCard` (a shared component F1/F2 already ship and test on the
// admin/team-lead tenant-detail pages), and renders each pending request as
// a `DecisionRow`. This file is rewritten against that real shape rather
// than the invented one -- every contract assertion below is preserved or
// strengthened, none weakened.
//
// Two real, verified backend gaps surfaced while rewriting this file
// (reported upstream, not fixed here -- out of this file's scope):
//   1. `admin_list_limit_raises`/`_request_public()` (`backend/mvp/grants.py`)
//      never returns the requester's OWN `comment` to the approver -- only
//      `decision_comment` (the approver's reply) is projected, and R12
//      explicitly requires the approver to see the requester's comment.
//   2. `submit_limit_raise` never persists `observed_limit_microusd` /
//      `observed_remaining_microusd` (the exact gap the assignment's own
//      "R30 snapshot" task names), and `TenantBudgetsRepository.pool_summary()`
//      carries no `as_of` timestamp for the LIVE read either -- so today
//      neither half of R30's "two distinct timestamps" can be verified
//      end-to-end. `LimitRaiseApproval.tsx` is written to render both
//      correctly the day the backend supplies them (verified below via
//      injected fixture data, since the API layer is mocked), and degrades
//      honestly ("not recorded") in their absence today.

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import type { ReactNode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// This component reads `usePermissions()` (for `isAdmin`, which picks the
// `api.admin` vs `api.teamLead` route namespace), which in turn requires an
// `AuthProvider` in the tree. Every fixture below is authored as an
// approving admin, so the hook is mocked directly rather than standing up
// the real AuthContext/AuthProvider -- the same pattern this repo already
// uses in `src/components/common/ProtectedRoute.test.tsx`.
vi.mock('@/hooks/usePermissions', () => ({
  usePermissions: () => ({
    roles: ['admin'],
    orgId: 'acme-eng',
    isAdmin: true,
    isTeamLead: false,
    isAdminOrTeamLead: true,
    can: () => true,
  }),
}))

const mockGetPoolBudget = vi.fn()
const mockSetPoolBudget = vi.fn()
const mockLatestPermissibleExpiry = vi.fn()
const mockListLimitRaises = vi.fn()
const mockApproveLimitRaise = vi.fn()
const mockRejectLimitRaise = vi.fn()

vi.mock('@/lib/api', async () => {
  const actual = await vi.importActual<typeof import('@/lib/api')>('@/lib/api')
  const ns = {
    getPoolBudget: (...a: unknown[]) => mockGetPoolBudget(...a),
    setPoolBudget: (...a: unknown[]) => mockSetPoolBudget(...a),
    latestPermissibleExpiry: (...a: unknown[]) => mockLatestPermissibleExpiry(...a),
    listLimitRaises: (...a: unknown[]) => mockListLimitRaises(...a),
    approveLimitRaise: (...a: unknown[]) => mockApproveLimitRaise(...a),
    rejectLimitRaise: (...a: unknown[]) => mockRejectLimitRaise(...a),
  }
  return {
    ...actual,
    api: { ...actual.api, admin: { ...actual.api.admin, ...ns }, teamLead: { ...actual.api.teamLead, ...ns } },
  }
})

import LimitRaiseApproval from './LimitRaiseApproval'

function withRouting(children: ReactNode) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0, staleTime: 0 } },
  })
  return (
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={['/team-lead/tenants/acme-eng/limit-raises']}>
        <Routes>
          <Route path="/team-lead/tenants/:tenantId/limit-raises" element={children} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  )
}

// A real `PoolBudget` (backend/mvp/admin_tenants.py's `PoolBudgetResponse`,
// mirrored by `frontend/src/lib/api.ts`'s `PoolBudget`), not the invented
// merged shape. `remaining_microusd` is NEGATIVE -- a real deficit -- and
// `resume_action` is the REAL sentinel F1 already ships (`"follow_seats"` or
// `null`), never a `sizing`/`resumable` field that does not exist on the
// wire.
const POOL_FIXTURE = {
  tenant_id: 'acme-eng',
  period: '2026-08',
  status: 'active',
  pool_limit_microusd: 40_000_000,
  pool_reserved_microusd: 500_000,
  pool_settled_microusd: 39_800_000,
  remaining_microusd: -300_000, // NEGATIVE — a real deficit
  over_ceiling_microusd: 300_000,
  pool_limit_usd_cents: 4_000_00,
  remaining_usd_cents: -30,
  mode_sentence:
    'This budget was set manually; membership changes do not change this budget.',
  seat_tracked: false,
  seat_count: 12,
  seat_rate_microusd: 50_000_000,
  seat_entitlement_microusd: 600_000_000,
  manual_limit_microusd: 40_000_000,
  pool_granted_microusd: 62_000_000,
  baseline_microusd: 600_000_000,
  entitlement_exceeds_figure: false,
  resume_action: 'follow_seats',
  grant_cap_microusd: null,
  effective_grant_cap_microusd: 600_000_000,
  grant_cap_is_derived: true,
  remaining_grant_cap_microusd: 8_000_000,
}

// `latestPermissibleExpiry()` (`backend/mvp/grants.py`'s
// `latest_permissible_expiry_for_period`) returns an EPOCH INT, matching
// every other `expires_at` in this codebase.
const LATEST_EXPIRY_EPOCH = Math.floor(
  new Date('2026-08-31T23:59:59Z').getTime() / 1000,
)
const EXPIRY_FIXTURE = { period: '2026-08', latest_permissible_expiry: LATEST_EXPIRY_EPOCH }

// One pending request. `comment` and `observed_*` are the two backend gaps
// named in this file's header comment -- injected here so the RENDERING
// side can be verified today, ahead of the backend fix that will start
// supplying them for real.
const PENDING_REQUEST = {
  request_id: 'lr_9f2c',
  tenant_id: 'acme-eng',
  user_id: 'requester-1',
  status: 'PENDING',
  limit_kind: 'pool',
  reason_code: 'cascade_shortfall',
  asked_amount_microusd: 200_000_000,
  created_at: '2026-08-28T14:00:05Z',
  comment: '<b>please</b> approve & hurry',
  observed_limit_microusd: 40_000_000,
  observed_remaining_microusd: 2_000_000,
  observed_at: '2026-08-28T14:00:00Z',
  approved_amount_microusd: null,
  expires_at: null,
  approver_id: null,
}

const QUEUE_FIXTURE = {
  tenant_id: 'acme-eng',
  requests: [PENDING_REQUEST],
  reason_codes: ['cascade_shortfall', 'seasonal_spike'],
}

beforeEach(() => {
  mockGetPoolBudget.mockReset()
  mockSetPoolBudget.mockReset()
  mockLatestPermissibleExpiry.mockReset()
  mockListLimitRaises.mockReset()
  mockApproveLimitRaise.mockReset()
  mockRejectLimitRaise.mockReset()
  mockGetPoolBudget.mockResolvedValue(POOL_FIXTURE)
  mockLatestPermissibleExpiry.mockResolvedValue(EXPIRY_FIXTURE)
  mockListLimitRaises.mockResolvedValue(QUEUE_FIXTURE)
  mockSetPoolBudget.mockResolvedValue({ ...POOL_FIXTURE, manual_limit_microusd: null })
})
afterEach(() => {
  vi.clearAllMocks()
})

describe('LimitRaiseApproval — R12: comment renders as text, never HTML', () => {
  it('does not interpret the comment as markup (no <b> tag rendered, literal text visible)', async () => {
    render(withRouting(<LimitRaiseApproval />))
    await waitFor(() => expect(screen.getByTestId('lr-comment')).toBeInTheDocument())
    const node = screen.getByTestId('lr-comment')
    // The literal source text must be present verbatim...
    expect(node.textContent).toContain('<b>please</b> approve & hurry')
    // ...and must NOT have been parsed into a <b> element or lost the '&'
    // to double-escaping (the sharpest check per this role's brief).
    expect(node.querySelector('b')).toBeNull()
    expect(node.innerHTML).not.toContain('<b>')
  })
})

describe('LimitRaiseApproval — R30: current position is labelled apart from the snapshot', () => {
  it('shows both the AT-REQUEST snapshot and the CURRENT reserved/settled, each labelled, in distinct DOM nodes', async () => {
    render(withRouting(<LimitRaiseApproval />))
    await waitFor(() => expect(screen.getByTestId('lr-snapshot-block')).toBeInTheDocument())

    // "Current" is PoolBudgetCard's live read (R21b/R30 are one F2 call,
    // rendered by the shared component this page already composes with —
    // per the component's own comment, this view adds nothing on top of
    // it). "Snapshot" is the per-request block this file adds.
    const currentBlock = screen.getByTestId('pool-budget-summary')
    const snapshotBlock = screen.getByTestId('lr-snapshot-block')
    expect(snapshotBlock).not.toBe(currentBlock)
    // `formatDate` renders via `toLocaleString()` (deliberately local-time,
    // not UTC), so the expected string is computed the same way rather than
    // hardcoded in UTC -- this must hold in any timezone the suite runs in.
    expect(snapshotBlock.textContent).toContain(
      new Date(PENDING_REQUEST.observed_at).toLocaleString(),
    )
    expect(currentBlock.textContent).toMatch(/\$39\.80/)

    // NOT independently verifiable today: `pool_summary()` carries no
    // `as_of` for the live read (backend gap #2 above), so "current" has no
    // timestamp of its own to assert against yet. The snapshot's own
    // timestamp (asserted above) is the half this file can actually check.
  })

  it('renders the deficit SIGNED, never clamped to $0.00 (remaining_microusd = -300_000)', async () => {
    render(withRouting(<LimitRaiseApproval />))
    await waitFor(() => expect(screen.getByTestId('pool-available')).toBeInTheDocument())
    const available = screen.getByTestId('pool-available')
    expect(available.textContent).toMatch(/-\$0\.30/)
    expect(available.textContent).not.toMatch(/\$0\.00/)
  })
})

// Seam amendment B1 (the integration owner's seam notes, §S10, outside this repository): the suspended-pool refusal is F2's
// server-side lifecycle rule now, not F3's to test. The test that used to
// live here ("surfaces a suspended-pool refusal ... as a legible banner")
// asserted the specific wording of a refusal F2 now owns end to end; it is
// DELETED, not retargeted, because pinning that wording here would be
// exactly the "two independent statements of one shape drift" the amendment
// exists to stop. What remains is R28's one F3-owned fact: display of the
// latest permissible expiry, before it is typed.
describe('LimitRaiseApproval — R28: latest permissible expiry shown before typing', () => {
  it('shows the latest permissible expiry BEFORE any value is typed into the expiry field', async () => {
    render(withRouting(<LimitRaiseApproval />))
    await waitFor(() =>
      expect(screen.getByTestId('lr-latest-permissible-expiry')).toBeInTheDocument(),
    )
    expect(screen.getByTestId('lr-latest-permissible-expiry').textContent).toContain(
      new Date(LATEST_EXPIRY_EPOCH * 1000).toLocaleString(),
    )
    // It must be visible text, not merely the <input max="..."> attribute —
    // "shown before it is typed" (R28's own phrasing) requires prose, per
    // this role's brief ("assert what a person sees").
    const expiryInput = screen.getByLabelText(/expir/i)
    expect(expiryInput).toHaveAttribute('max')
  })
})

// R36/B6 — the amount-side twin of R28 above. The real defect this closes:
// an approver used to learn the tenant's remaining grant cap only from a 422
// `grant_cap_exceeded` AFTER typing an amount and clicking approve, while the
// requester's own 402 `raise_hint` already carried `remaining_cap_microusd`
// (C14.22) — the one reader who actually sets the figure was the one reader
// who could not see it first. `remaining_grant_cap_microusd` comes from the
// SAME `getPoolBudget` call this page already makes for `PoolBudgetCard`
// (POOL_FIXTURE's own field, $8.00 here), never a second endpoint and never
// a client-side reimplementation of `effective_grant_cap_for_row`.
describe('LimitRaiseApproval — R36/B6: remaining grant cap shown before typing an amount', () => {
  it('shows the remaining grant cap BEFORE any value is typed, and disables approval when the pre-filled ask exceeds it', async () => {
    render(withRouting(<LimitRaiseApproval />))
    await waitFor(() =>
      expect(screen.getByTestId('lr-remaining-grant-cap')).toBeInTheDocument(),
    )
    // Visible prose, same standard R28 holds the expiry bound to — not just
    // an attribute nobody reads.
    const capHint = screen.getByTestId('lr-remaining-grant-cap')
    expect(capHint.textContent).toMatch(/\$8\.00/)
    expect(capHint.textContent).toMatch(/derived from baseline/i)

    // The request's own ask is $200, ten times POOL_FIXTURE's $8 cap — the
    // exact shape of the real defect (a small baseline-derived cap on a
    // tenant asking for far more than it). Blocked BEFORE the round trip,
    // not discovered from its refusal.
    await waitFor(() =>
      expect(screen.getByTestId('lr-amount-over-cap')).toBeInTheDocument(),
    )
    expect(screen.getByTestId('lr-approve-button')).toBeDisabled()
    expect(mockApproveLimitRaise).not.toHaveBeenCalled()
  })

  it('clears the over-cap warning and re-enables approval once the typed amount is brought within the cap', async () => {
    const user = userEvent.setup()
    render(withRouting(<LimitRaiseApproval />))
    await waitFor(() =>
      expect(screen.getByTestId('lr-amount-over-cap')).toBeInTheDocument(),
    )

    const amountInput = screen.getByTestId('lr-approve-amount')
    await user.clear(amountInput)
    await user.type(amountInput, '5')
    // A comment is required whenever the approved figure is LESS than the
    // ask (the pre-existing `givingLess` rule) — $5 against a $200 ask
    // trips it, same as it would with no cap involved.
    await user.type(screen.getByTestId('lr-decision-comment'), 'capped by tenant grant cap')

    await waitFor(() =>
      expect(screen.queryByTestId('lr-amount-over-cap')).not.toBeInTheDocument(),
    )
    expect(screen.getByTestId('lr-approve-button')).not.toBeDisabled()
  })

  it('renders a zero remaining cap plainly (never hidden, never a silently larger figure)', async () => {
    mockGetPoolBudget.mockReset()
    mockGetPoolBudget.mockResolvedValue({
      ...POOL_FIXTURE,
      baseline_microusd: 0,
      effective_grant_cap_microusd: 0,
      remaining_grant_cap_microusd: 0,
    })
    render(withRouting(<LimitRaiseApproval />))
    await waitFor(() =>
      expect(screen.getByTestId('lr-remaining-grant-cap')).toBeInTheDocument(),
    )
    expect(screen.getByTestId('lr-remaining-grant-cap').textContent).toMatch(/\$0\.00/)
    await waitFor(() =>
      expect(screen.getByTestId('lr-amount-over-cap')).toBeInTheDocument(),
    )
    expect(screen.getByTestId('lr-approve-button')).toBeDisabled()
  })
})

describe('LimitRaiseApproval — R21b: mode sentence, seat entitlement, resume action', () => {
  it('renders the mode sentence verbatim, not a paraphrase of `mode`', async () => {
    render(withRouting(<LimitRaiseApproval />))
    await waitFor(() =>
      expect(
        screen.getByText(
          'This budget was set manually; membership changes do not change this budget.',
        ),
      ).toBeInTheDocument(),
    )
  })

  it('renders the seat entitlement (seat count), not just the derived microusd figure', async () => {
    render(withRouting(<LimitRaiseApproval />))
    await waitFor(() => expect(screen.getByTestId('pool-seats')).toBeInTheDocument())
    const seats = screen.getByTestId('pool-seats')
    // The raw count (12) AND the derived entitlement ($600.00) must both be
    // present — the count alone is the fact R21b names ("not just the
    // derived microusd figure"); this asserts it is not the ONLY thing
    // rendered by requiring both.
    expect(seats.textContent).toMatch(/12/)
    expect(seats.textContent).toMatch(/\$600\.00|600/)
  })

  it('shows a resume action when the ceiling is resumable', async () => {
    render(withRouting(<LimitRaiseApproval />))
    await waitFor(() =>
      expect(screen.getByTestId('pool-follow-seats-button')).toBeInTheDocument(),
    )
  })

  // Contract correction: the resume action is NOT a `sizing` toggle (F1
  // deletes that attribute entirely) — it is `PUT .../pool-budget` with
  // `{"follow_seats": true}`, clearing `manual_limit`. Redirected from the
  // earlier (reasonable, but now-wrong) `sizing`-based reading.
  it('clicking resume calls the REAL pool-budget PUT with follow_seats: true, not a sizing toggle', async () => {
    const user = userEvent.setup()
    render(withRouting(<LimitRaiseApproval />))
    await waitFor(() =>
      expect(screen.getByTestId('pool-follow-seats-button')).toBeInTheDocument(),
    )
    await user.click(screen.getByTestId('pool-follow-seats-button'))

    await waitFor(() => expect(mockSetPoolBudget).toHaveBeenCalled())
    const [tenantIdArg, bodyArg] = mockSetPoolBudget.mock.calls[0]
    expect(tenantIdArg).toBe('acme-eng')
    expect((bodyArg as { follow_seats: boolean }).follow_seats).toBe(true)
    // No surface may still reference `sizing` — F1 deletes the attribute,
    // so a component built on it would ship against a mechanism that no
    // longer exists.
    expect(bodyArg).not.toHaveProperty('sizing')
  })
})

// ---------------------------------------------------------------------------
// The two refusals the per-user wall introduced
// ---------------------------------------------------------------------------

/**
 * Get the approve button into a pressable state and press it.
 *
 * The decision comment is required here, not incidental: approving LESS than was
 * asked (this fixture asks $200) is gated on the approver explaining why, so
 * without it the button stays disabled and every assertion below fails as "the
 * refusal did not render" rather than "the form was never submitted".
 */
async function attemptApproval() {
  await waitFor(() =>
    expect(screen.getByTestId('lr-approve-button')).toBeInTheDocument(),
  )
  fireEvent.change(screen.getByTestId('lr-approve-amount'), { target: { value: '5' } })
  fireEvent.change(screen.getByTestId('lr-decision-comment'), {
    target: { value: 'partial for now' },
  })
  await waitFor(() =>
    expect(screen.getByTestId('lr-approve-button')).not.toBeDisabled(),
  )
  fireEvent.click(screen.getByTestId('lr-approve-button'))
}

function refusal(detailBody: Record<string, unknown>) {
  return Object.assign(new Error(String(detailBody.message ?? 'refused')), {
    status: 409,
    detailBody,
  })
}

describe('LimitRaiseApproval — a short pool and an elapsed period are not the same event', () => {
  it('a short pool says the request SURVIVES and names who has to act', async () => {
    // The mistake this prevents: reading it as a generic failure and telling the
    // requester to refile. That burns her once-a-day slot and produces a second
    // request that will be refused identically.
    mockApproveLimitRaise.mockRejectedValue(
      refusal({
        type: 'pool_headroom_short',
        message: "Tenant acme-eng's pool has 500000 micro-USD of headroom...",
        wall: 'tenant_dollar_pool',
        tenant_id: 'acme-eng',
        observed_headroom_microusd: 500_000,
        approved_amount_microusd: 5_000_000,
      }),
    )
    render(withRouting(<LimitRaiseApproval />))
    await attemptApproval()

    await waitFor(() =>
      expect(screen.getByTestId('refusal-pool-headroom-short')).toBeInTheDocument(),
    )
    // Both figures, as money: an approver who cannot see the gap cannot tell how
    // much to ask the pool for.
    expect(screen.getByTestId('refusal-pool-headroom-figures')).toHaveTextContent(/\$0\.50/)
    expect(screen.getByTestId('refusal-pool-headroom-figures')).toHaveTextContent(/\$5\.00/)
    // And a route to the prerequisite, which files NOTHING on the approver's behalf.
    expect(screen.getByTestId('refusal-pool-raise-link')).toBeInTheDocument()
    expect(mockSetPoolBudget).not.toHaveBeenCalled()
  })

  it('an elapsed period says the request is OVER', async () => {
    // The opposite mistake: waiting for a state that cannot arrive. Nothing can make
    // a pinned period current again.
    mockApproveLimitRaise.mockRejectedValue(
      refusal({
        type: 'limit_raise_period_elapsed',
        message: 'This request was filed in 2026-07, and that period is no longer current',
        tenant_id: 'acme-eng',
        filed_period: '2026-07',
        current_period: '2026-08',
      }),
    )
    render(withRouting(<LimitRaiseApproval />))
    await attemptApproval()

    await waitFor(() =>
      expect(screen.getByTestId('refusal-period-elapsed')).toBeInTheDocument(),
    )
    expect(screen.getByTestId('refusal-period-elapsed')).toHaveTextContent(/2026-07/)
    expect(screen.getByTestId('refusal-period-elapsed')).toHaveTextContent(/2026-08/)
    // The two must not be interchangeable on screen. This is the whole point of
    // splitting them, so it is asserted rather than left to the eye.
    expect(screen.queryByTestId('refusal-pool-headroom-short')).toBeNull()
    expect(screen.queryByTestId('refusal-pool-raise-link')).toBeNull()
  })

  it('an unknown code renders, and renders its CODE rather than its prose', async () => {
    // `api.ts` requires an unknown code to render rather than fail closed. But a
    // future refusal's `message` may be written for an operator, not for an
    // approver, so the sentence is not passed through -- the machine token is,
    // because that is what makes the refusal reportable.
    mockApproveLimitRaise.mockRejectedValue(
      refusal({
        type: 'some_future_refusal',
        message: 'internal: shard 7 quarantined pending fraud review of tenant acme-eng',
      }),
    )
    render(withRouting(<LimitRaiseApproval />))
    await attemptApproval()

    await waitFor(() => expect(screen.getByTestId('refusal-unknown')).toBeInTheDocument())
    expect(screen.getByTestId('refusal-unknown-code')).toHaveTextContent('some_future_refusal')
    expect(screen.queryByText(/shard 7 quarantined/)).toBeNull()
    expect(screen.queryByText(/fraud review/)).toBeNull()
  })

  it('a refusal with no structured body still tells the approver something', async () => {
    // Degrading to the old flat-string path rather than rendering nothing.
    mockApproveLimitRaise.mockRejectedValue(
      Object.assign(new Error('Gateway timeout'), { status: 504 }),
    )
    render(withRouting(<LimitRaiseApproval />))
    await attemptApproval()
    await waitFor(() => expect(screen.getByText(/Gateway timeout/)).toBeInTheDocument())
    expect(screen.queryByTestId('refusal-unknown')).toBeNull()
  })
})
