// GrantsInventory — the grant inventory console view (F3 / R25).
//
// Contract: change-pipeline/quota-raise-and-archive/CONTRACT-F3-surfaces.md
//   R25: "A grant inventory view: live grants with amount, approver, expiry,
//   status and the request that produced each, and the sum equals
//   `pool_granted`. Unit: the sum reconciles; a `REVOKE_BLOCKED` grant is
//   visible with its reason."
//
// Seam amendment B4 (the integration owner's seam notes, §S6, outside this repository) rewrote what "the sum equals
// pool_granted" means: grants are pinned to a target row (period), and a
// late-swept REVOKE_BLOCKED grant can still bear capacity on the PRIOR
// period's row after rollover. A single combined total across periods is
// exactly the defect this amendment closes — reconciliation is per row.
//
// Union amendment U3 (integration review of all four test suites): the
// grant amount field is `approved_amount_microusd`, not `amount_microusd` —
// the row carries both the asked and the approved figure, and the shorter
// name cannot say which one it is.
//
// Corrections made converging this file against the REAL, already-shipped
// component and backend (this role, working blind, had neither):
//   - The endpoint is `GET /admin/limit-grants` (`mvp/grants.py::admin_list_limit_grants`,
//     F2-owned per S7), whose shape is `{tenant_id, grants: LimitGrant[],
//     reconciliation: GrantReconciliation}` — a FLAT grants list plus a
//     SEPARATE `reconciliation.rows` list, joined client-side by
//     `target_pk`/`target_sk` (`GrantsInventory.tsx`'s own `grantsByRow`) —
//     not the nested `rows[].grants[]` shape this file originally guessed.
//     `reconcile_tenant_grants`'s own shape, per `frontend/src/lib/api.ts`'s
//     comment directly above `GrantReconciliationRow`.
//   - The client calls `api.admin.listLimitGrants`/`api.admin.revokeLimitGrant`
//     (or the `api.teamLead` mirror, chosen by `usePermissions().isAdmin`) —
//     there is no `api.grants` namespace anywhere in `lib/api.ts`.
//   - The component reads `usePermissions()` (`isAdmin`), which requires an
//     `AuthProvider` in the tree; mocked directly rather than standing up the
//     real AuthContext, the same pattern `LimitRaiseApproval.test.tsx` and
//     `src/components/common/ProtectedRoute.test.tsx` already use.
//   - The query is gated behind a tenant-id lookup form (`enabled:
//     submittedTenantId.length > 0`) — a render with no lookup submitted
//     never calls the API at all. Every test below now types a tenant id
//     and clicks "Look up" first.
//   - Rendered testids are `gi-row-total` (one per row, not
//     `grants-total-<period>`) and `gi-revoke-blocked-reason`/`gi-revoke-button`.
//
// Bodies still assert RENDERED TEXT (each row's own visible sum, the visible
// reason), not merely that the payload carries the fields — that intent is
// unchanged by the corrections above.

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import type { ReactNode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

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

vi.mock('@/lib/api', () => ({
  api: {
    admin: {
      listLimitGrants: (...args: unknown[]) => (globalThis as any).__listLimitGrants(...args),
      revokeLimitGrant: (...args: unknown[]) => (globalThis as any).__revokeLimitGrant(...args),
    },
    teamLead: {
      listLimitGrants: (...args: unknown[]) => (globalThis as any).__listLimitGrants(...args),
      revokeLimitGrant: (...args: unknown[]) => (globalThis as any).__revokeLimitGrant(...args),
    },
  },
}))

const mockList = vi.fn()
const mockRevoke = vi.fn()
;(globalThis as any).__listLimitGrants = (...a: unknown[]) => mockList(...a)
;(globalThis as any).__revokeLimitGrant = (...a: unknown[]) => mockRevoke(...a)

import GrantsInventory from './GrantsInventory'

function withRouting(children: ReactNode) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0, staleTime: 0 } },
  })
  return (
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={['/team-lead/tenants/acme-eng/grants']}>
        <Routes>
          <Route path="/team-lead/tenants/:tenantId/grants" element={children} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  )
}

async function lookUpTenant() {
  const user = userEvent.setup()
  await user.type(screen.getByTestId('gi-tenant-id-input'), 'acme-eng')
  await user.click(screen.getByTestId('gi-lookup-button'))
}

// Two target rows: the current period (one active grant) and the PRIOR
// period (one REVOKE_BLOCKED grant still bearing capacity there after a
// late sweep) — the exact rollover-plus-late-sweep combination B4 names.
// Shape: `LimitGrantsResponse` (`frontend/src/lib/api.ts`) — a flat
// `grants` list plus a separate `reconciliation.rows` list, joined by
// `target_pk`/`target_sk`.
const CURRENT_TARGET = { target_pk: 'acme-eng', target_sk: 'BUDGET#2026-09' }
const PRIOR_TARGET = { target_pk: 'acme-eng', target_sk: 'BUDGET#2026-08' }

const FIXTURE = {
  tenant_id: 'acme-eng',
  grants: [
    {
      grant_id: 'gr_1a',
      tenant_id: 'acme-eng',
      request_id: 'lr_9f2c',
      status: 'active',
      approved_amount_microusd: 50_000_000,
      expires_at: 1_788_303_599,
      period: '2026-09',
      ...CURRENT_TARGET,
      approver_user_id: 'user-lead-1',
      created_at: '2026-09-01T00:00:00Z',
      capacity_bearing: true,
      revoke_blocked: false,
      revoke_attempts: 0,
    },
    {
      grant_id: 'gr_0b',
      tenant_id: 'acme-eng',
      request_id: 'lr_7e21',
      status: 'revoke_blocked',
      approved_amount_microusd: 12_000_000,
      expires_at: 1_785_628_799,
      period: '2026-08',
      ...PRIOR_TARGET,
      approver_user_id: 'user-lead-1',
      created_at: '2026-08-01T00:00:00Z',
      capacity_bearing: true,
      revoke_blocked: true,
      revoke_attempts: 1,
      revoke_blocked_reason:
        'an in-flight reservation is still holding against this grant',
      blocked_at: '2026-08-30T00:00:00Z',
    },
  ],
  reconciliation: {
    reconciler: 'tenant_grants',
    tenant_id: 'acme-eng',
    period: '2026-09',
    rows: [
      {
        ...CURRENT_TARGET,
        period: '2026-09',
        active_only_sum_microusd: 50_000_000,
        capacity_bearing_sum_microusd: 50_000_000,
        blocked_grant_ids: [],
        pool_granted_microusd: 50_000_000,
        drift_microusd: 0,
        grant_cap_microusd: null,
        effective_grant_cap_microusd: 100_000_000,
        cap_is_derived: true,
        remaining_cap_microusd: 50_000_000,
        cap_exceeded: false,
      },
      {
        ...PRIOR_TARGET,
        period: '2026-08',
        active_only_sum_microusd: 0,
        capacity_bearing_sum_microusd: 12_000_000,
        blocked_grant_ids: ['gr_0b'],
        pool_granted_microusd: 12_000_000,
        drift_microusd: 0,
        grant_cap_microusd: null,
        effective_grant_cap_microusd: 100_000_000,
        cap_is_derived: true,
        remaining_cap_microusd: 88_000_000,
        cap_exceeded: false,
      },
    ],
    orphans: [],
    clean: true,
  },
}

beforeEach(() => {
  mockList.mockReset()
  mockRevoke.mockReset()
  mockList.mockResolvedValue(FIXTURE)
})
afterEach(() => {
  vi.clearAllMocks()
})

describe('GrantsInventory — R25, reconciling per target row (B4)', () => {
  it('shows each row its OWN reconciling total, labelled by period', async () => {
    render(withRouting(<GrantsInventory />))
    await lookUpTenant()
    await waitFor(() =>
      expect(screen.getAllByTestId('gi-row-total').length).toBe(2),
    )
    const totals = screen.getAllByTestId('gi-row-total').map((el) => el.textContent)
    expect(totals.some((t) => t?.includes('$50.00'))).toBe(true)
    expect(totals.some((t) => t?.includes('$12.00'))).toBe(true)
    // Each row must name its own period — a reader must be able to tell
    // WHICH row a total belongs to, not just that a number exists somewhere.
    expect(screen.getByText(/Period 2026-09/)).toBeInTheDocument()
    expect(screen.getByText(/Period 2026-08/)).toBeInTheDocument()
  })

  it('never offers a single combined total across periods', async () => {
    render(withRouting(<GrantsInventory />))
    await lookUpTenant()
    await waitFor(() =>
      expect(screen.getAllByTestId('gi-row-total').length).toBe(2),
    )
    // $50 + $12 = $62 — a combined figure is exactly the defect B4 closes
    // (it would silently misrepresent the prior period's still-bearing
    // grant as part of the current period's capacity). It must not appear
    // anywhere on the page.
    expect(screen.queryByText(/\$62\.00/)).toBeNull()
    expect(screen.queryByTestId('grants-total')).toBeNull()
  })

  it('shows the PRIOR period as its own row, not merged into or dropped from the current one', async () => {
    render(withRouting(<GrantsInventory />))
    await lookUpTenant()
    await waitFor(() =>
      expect(screen.getAllByTestId('gi-row-total').length).toBe(2),
    )
    // Both grants (one per period) must be independently visible.
    expect(screen.getAllByText(/lr_9f2c|lr_7e21/).length).toBeGreaterThanOrEqual(2)
  })

  it('shows a REVOKE_BLOCKED grant with its reason visible, not merely present in the payload', async () => {
    render(withRouting(<GrantsInventory />))
    await lookUpTenant()
    await waitFor(() =>
      expect(
        screen.getByText(/in-flight reservation is still holding/i),
      ).toBeInTheDocument(),
    )
    // And it must still be counted in ITS OWN row's total, not hidden as if
    // revoked (revoke was BLOCKED, not completed).
    const totals = screen.getAllByTestId('gi-row-total').map((el) => el.textContent)
    expect(totals.some((t) => t?.includes('$12.00'))).toBe(true)
  })

  it('offers an early-revoke action for an ACTIVE grant, gated to the approver', async () => {
    render(withRouting(<GrantsInventory />))
    await lookUpTenant()
    await waitFor(() =>
      expect(screen.getAllByRole('button', { name: /revoke/i }).length).toBeGreaterThan(0),
    )
  })
})
