// Amendment A4 to the F1 contract: R21's mode sentence covers the
// team-lead tenant view as well as the admin one.
//
// PR 1 shipped `PUT /team-lead/tenants/{id}/pool-budget` under
// `tenants:update-own`, so a team lead is a WRITER of the ceiling. Under F1
// that write is what latches the row to manual and ends seat tracking --
// so a team lead who sets a figure is the one role that can silently exit
// seat tracking, and (before this test's target lands) also the one role
// with no page anywhere that shows it happened.
//
// **Convergence correction.** This file's own header claimed "zero matches
// for 'pool' across all three files under frontend/src/pages/team-lead/"
// and "`api.teamLead` has no getPoolBudget/setPoolBudget method at all
// today" -- neither survives contact with the real, already-shipped
// `frontend/src/pages/team-lead/TeamLeadTenantDetail.tsx` (which renders
// the shared `PoolBudgetCard` with `poolApi={api.teamLead}`) and
// `frontend/src/lib/api.ts`'s real `api.teamLead.getPoolBudget`/
// `setPoolBudget`. A4 is already satisfied; what was wrong was this file's
// own guess at `PoolBudget`'s shape -- the same `mode`/
// `pool-mode-resume-button` guess `AdminTenantDetail.ceiling.test.tsx` made
// and was corrected for, applied here too (`mode_sentence`/`seat_tracked`,
// `resume_action` rendered as `pool-follow-seats-button`).

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import type { ReactNode } from 'react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { PoolBudget } from '@/lib/api'

vi.mock('@/lib/api', () => ({
  api: {
    teamLead: {
      getTenant: (...args: unknown[]) => (globalThis as any).__tlGetTenant(...args),
      members: (...args: unknown[]) => (globalThis as any).__tlMembers(...args),
      usage: (...args: unknown[]) => (globalThis as any).__tlUsage(...args),
      getPoolBudget: (...args: unknown[]) => (globalThis as any).__tlGetPoolBudget(...args),
      setPoolBudget: (...args: unknown[]) => (globalThis as any).__tlSetPoolBudget(...args),
    },
  },
}))

const mockGetTenant = vi.fn()
const mockMembers = vi.fn()
const mockUsage = vi.fn()
const mockGetPoolBudget = vi.fn()
const mockSetPoolBudget = vi.fn()
;(globalThis as any).__tlGetTenant = () => mockGetTenant()
;(globalThis as any).__tlMembers = () => mockMembers()
;(globalThis as any).__tlUsage = () => mockUsage()
;(globalThis as any).__tlGetPoolBudget = () => mockGetPoolBudget()
;(globalThis as any).__tlSetPoolBudget = () => mockSetPoolBudget()

import TeamLeadTenantDetail from './TeamLeadTenantDetail'

function withProviders(children: ReactNode) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0, staleTime: 0 } },
  })
  return (
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={['/team-lead/tenants/owned-co']}>
        <Routes>
          <Route path="/team-lead/tenants/:tenantId" element={children} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  )
}

const BASE_TENANT = {
  tenant_id: 'owned-co',
  name: 'Owned Co',
  default_credit: 10_000_000,
  status: 'active',
}

// The real shape (`mvp/admin_tenants.py::_pool_response`/`_mode_sentence`),
// mirrored from the same fixture builder as
// `AdminTenantDetail.ceiling.test.tsx` -- one shared surface, one shape.
function poolBudget(overrides: Partial<PoolBudget> = {}): PoolBudget {
  return {
    tenant_id: 'owned-co',
    period: '2026-09',
    status: 'active',
    pool_limit_microusd: 400_000_000,
    pool_reserved_microusd: 0,
    pool_settled_microusd: 0,
    remaining_microusd: 400_000_000,
    over_ceiling_microusd: 0,
    pool_limit_usd_cents: 40_000,
    remaining_usd_cents: 40_000,
    mode_sentence:
      "This pool follows the tenant's seat count: 2 seats entitle it to " +
      "$400.00 a month, and it moves by one seat's worth whenever somebody " +
      'joins or leaves. Setting a figure by hand stops that.',
    seat_tracked: true,
    seat_count: 2,
    seat_rate_microusd: 200_000_000,
    seat_entitlement_microusd: 400_000_000,
    manual_limit_microusd: null,
    pool_granted_microusd: 0,
    baseline_microusd: 400_000_000,
    entitlement_exceeds_figure: false,
    resume_action: null,
    grant_cap_microusd: null,
    effective_grant_cap_microusd: 400_000_000,
    grant_cap_is_derived: true,
    remaining_grant_cap_microusd: 400_000_000,
    ...overrides,
  }
}

beforeEach(() => {
  mockGetTenant.mockReset().mockResolvedValue(BASE_TENANT)
  mockMembers.mockReset().mockResolvedValue({ tenant_id: 'owned-co', members: [] })
  mockUsage.mockReset().mockResolvedValue({
    tenant_id: 'owned-co', total_tokens: 0, input_tokens: 0, output_tokens: 0,
    by_model: {}, sample_size: 0,
  })
  mockSetPoolBudget.mockReset()
})

describe('A4 / R21: the team-lead tenant view also renders the pool mode as a sentence', () => {
  it('renders a seat-tracked sentence naming the seat count', async () => {
    mockGetPoolBudget.mockResolvedValue(poolBudget())

    render(withProviders(<TeamLeadTenantDetail />))

    const sentence = await screen.findByTestId('pool-mode-sentence')
    expect(sentence.textContent).toContain('2')
    expect(screen.queryByTestId('pool-follow-seats-button')).not.toBeInTheDocument()
  })

  it('renders a manual sentence with a resume action after the team lead sets a figure', async () => {
    // This is the hazard A4 names: the team lead's OWN write
    // (PUT .../pool-budget) is what produces this state.
    mockGetPoolBudget.mockResolvedValue(
      poolBudget({
        pool_limit_microusd: 100_000_000,
        remaining_microusd: 100_000_000,
        pool_limit_usd_cents: 10_000,
        remaining_usd_cents: 10_000,
        mode_sentence:
          'This pool is held at $100.00, a figure set by hand, and no longer ' +
          "follows the tenant's seat count. The seats would entitle it to " +
          '$400.00, which is more than the figure, so the figure is now the ' +
          'smaller of the two. Sending {"follow_seats": true} to this endpoint ' +
          'returns it to the seat count.',
        seat_tracked: false,
        manual_limit_microusd: 100_000_000,
        baseline_microusd: 100_000_000,
        entitlement_exceeds_figure: true,
        resume_action: 'follow_seats',
      }),
    )

    render(withProviders(<TeamLeadTenantDetail />))

    await screen.findByTestId('pool-mode-sentence')
    expect(screen.getByTestId('pool-follow-seats-button')).toBeInTheDocument()
  })
})
