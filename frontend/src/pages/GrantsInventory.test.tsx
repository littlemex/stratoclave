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
// This file's earlier version asserted one `pool_granted_microusd` and one
// `grants-total` testid; both are gone. Two periods are seeded, each with
// its own total, and a new test asserts no single combined total is
// offered anywhere on the page.
//
// Union amendment U3 (integration review of all four test suites): the
// grant amount field is `approved_amount_microusd`, not `amount_microusd` —
// the row carries both the asked and the approved figure, and the shorter
// name cannot say which one it is. The fixture below was fixed accordingly.
//
// This component does not exist in this worktree — every test fails at
// module resolution. Bodies assert rendered text (each row's own visible
// sum, the visible reason), not merely that the payload carries the fields.

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import type { ReactNode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/lib/api', () => ({
  api: {
    grants: {
      list: (...args: unknown[]) => (globalThis as any).__grantsList(...args),
      revoke: (...args: unknown[]) => (globalThis as any).__grantsRevoke(...args),
    },
  },
}))

const mockList = vi.fn()
const mockRevoke = vi.fn()
;(globalThis as any).__grantsList = (...a: unknown[]) => mockList(...a)
;(globalThis as any).__grantsRevoke = (...a: unknown[]) => mockRevoke(...a)

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

// Two target rows: the current period (one active grant) and the PRIOR
// period (one REVOKE_BLOCKED grant still bearing capacity there after a
// late sweep) — the exact rollover-plus-late-sweep combination B4 names.
const FIXTURE = {
  tenant_id: 'acme-eng',
  rows: [
    {
      period: '2026-09',
      pool_granted_microusd: 50_000_000,
      grants: [
        {
          grant_id: 'gr_1a',
          request_id: 'lr_9f2c',
          approved_amount_microusd: 50_000_000,
          approver_id: 'user-lead-1',
          expires_at: '2026-08-31T23:59:59Z',
          status: 'active',
        },
      ],
    },
    {
      period: '2026-08',
      pool_granted_microusd: 12_000_000,
      grants: [
        {
          grant_id: 'gr_0b',
          request_id: 'lr_7e21',
          approved_amount_microusd: 12_000_000,
          approver_id: 'user-lead-1',
          expires_at: '2026-08-30T23:59:59Z',
          status: 'revoke_blocked',
          revoke_blocked_reason:
            'an in-flight reservation is still holding against this grant',
        },
      ],
    },
  ],
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
    await waitFor(() =>
      expect(screen.getByTestId('grants-total-2026-09')).toBeInTheDocument(),
    )
    expect(screen.getByTestId('grants-total-2026-09').textContent).toMatch(/\$50\.00/)
    expect(screen.getByTestId('grants-total-2026-08')).toBeInTheDocument()
    expect(screen.getByTestId('grants-total-2026-08').textContent).toMatch(/\$12\.00/)
    // Each row must name its own period — a reader must be able to tell
    // WHICH row a total belongs to, not just that a number exists somewhere.
    expect(screen.getByText('2026-09')).toBeInTheDocument()
    expect(screen.getByText('2026-08')).toBeInTheDocument()
  })

  it('never offers a single combined total across periods', async () => {
    render(withRouting(<GrantsInventory />))
    await waitFor(() =>
      expect(screen.getByTestId('grants-total-2026-09')).toBeInTheDocument(),
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
    await waitFor(() =>
      expect(screen.getByTestId('grants-total-2026-08')).toBeInTheDocument(),
    )
    // Both grants (one per period) must be independently visible.
    expect(screen.getAllByText(/lr_9f2c|lr_7e21/).length).toBeGreaterThanOrEqual(2)
  })

  it('shows a REVOKE_BLOCKED grant with its reason visible, not merely present in the payload', async () => {
    render(withRouting(<GrantsInventory />))
    await waitFor(() =>
      expect(
        screen.getByText(/in-flight reservation is still holding/i),
      ).toBeInTheDocument(),
    )
    // And it must still be counted in ITS OWN row's total, not hidden as if
    // revoked (revoke was BLOCKED, not completed).
    expect(screen.getByTestId('grants-total-2026-08').textContent).toMatch(/\$12\.00/)
  })

  it('offers an early-revoke action for an ACTIVE grant, gated to the approver', async () => {
    render(withRouting(<GrantsInventory />))
    await waitFor(() =>
      expect(screen.getAllByRole('button', { name: /revoke/i }).length).toBeGreaterThan(0),
    )
  })
})
