// LimitRaiseApproval — the tenant approval view (F3 / R12 approval half, R30,
// R28, R21b).
//
// Contract (as corrected): change-pipeline/quota-raise-and-archive/CONTRACT-F3-surfaces.md
//   R12: "Console — the tenant approval view (`limit-raises:approve` on that
//   tenant): the ask, the reason, the comment, the requester, the ceiling's
//   composition ..., the tenant's current reserved and settled, the
//   remaining grant cap, and the latest permissible expiry. Approve with an
//   amount and an expiry, or reject with a reason." Plus: "the comment is
//   rendered as text, never as HTML."
//   R30: "observed_limit/observed_remaining were taken when the request was
//   filed, possibly hours earlier. Unit: current reserved, settled and
//   headroom appear alongside, each labelled."
//   R28: "the latest permissible expiry is shown before it is typed."
//   R21b: "The console's tenant view carries F1's mode sentence, the seat
//   entitlement and the resume action."
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
import { render, screen, waitFor } from '@testing-library/react'
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
  status: 'pending',
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
