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
// Checked before writing this file: zero matches for "pool" across all three
// files under frontend/src/pages/team-lead/ (contract's own verification).
// `frontend/src/lib/api.ts`'s `api.teamLead` object has no
// getPoolBudget/setPoolBudget method at all today either -- the gap is both
// "no query" and "no render". This file's mock supplies the shape those
// methods are expected to have (mirroring `api.admin.getPoolBudget`'s
// existing shape plus F1's new seat_count/manual_limit_microusd/mode
// fields), so the test is evidence about the RENDER, independent of exactly
// how the implementer names the client method.
//
// Every test below fails today because `TeamLeadTenantDetail` renders no
// pool information of any kind -- there is no element to find.

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import type { ReactNode } from 'react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

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
    mockGetPoolBudget.mockResolvedValue({
      tenant_id: 'owned-co', period: '2026-09', status: 'active',
      pool_limit_microusd: 400_000_000, pool_reserved_microusd: 0,
      pool_settled_microusd: 0, remaining_microusd: 400_000_000,
      pool_limit_usd_cents: 40_000, remaining_usd_cents: 40_000,
      mode: 'seat_tracked', seat_count: 2, manual_limit_microusd: null,
    })

    render(withProviders(<TeamLeadTenantDetail />))

    const sentence = await screen.findByTestId('pool-mode-sentence')
    expect(sentence.textContent).toContain('2')
    expect(screen.queryByTestId('pool-mode-resume-button')).not.toBeInTheDocument()
  })

  it('renders a manual sentence with a resume action after the team lead sets a figure', async () => {
    // This is the hazard A4 names: the team lead's OWN write
    // (PUT .../pool-budget) is what produces this state.
    mockGetPoolBudget.mockResolvedValue({
      tenant_id: 'owned-co', period: '2026-09', status: 'active',
      pool_limit_microusd: 100_000_000, pool_reserved_microusd: 0,
      pool_settled_microusd: 0, remaining_microusd: 100_000_000,
      pool_limit_usd_cents: 10_000, remaining_usd_cents: 10_000,
      mode: 'manual', seat_count: 2, manual_limit_microusd: 100_000_000,
    })

    render(withProviders(<TeamLeadTenantDetail />))

    await screen.findByTestId('pool-mode-sentence')
    expect(screen.getByTestId('pool-mode-resume-button')).toBeInTheDocument()
  })
})
