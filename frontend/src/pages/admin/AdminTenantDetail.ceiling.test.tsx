// R21 + R29 (the F1 contract) -- the console half of two clauses:
//
//   R21: "The mode is a sentence, not a field, in the console tenant view
//   ... with the seat entitlement and the resume action." Verified by:
//   "the surfaces render it."
//
//   R29: "Negative headroom is rendered as a signed deficit, never clamped
//   ... the surfaces show ceiling, settled, reserved, signed available and
//   an 'over ceiling by' line."
//
// **Convergence correction.** This file's own design note (`design-F1.md`)
// is an explicitly non-authoritative "test/design author" hypothesis
// ("this note is the interface every failing test in this drop targets; it
// is not itself an implementation"), and this file's original header claimed
// "today PoolBudgetCard renders... no mode sentence, no resume action, no
// 'over ceiling by' line" and that `PoolBudget` "carries no `mode`,
// `seat_count`, or `manual_limit_microusd` field at all". Neither claim
// survives contact with the real, already-shipped
// `frontend/src/components/common/PoolBudgetCard.tsx` and
// `frontend/src/lib/api.ts`'s real `PoolBudget` interface, which F3's own
// later work (R21b/B6, the remaining-grant-cap line) was built directly on
// top of: `mode_sentence: string` (R21's own text -- "a sentence, not a
// field" -- is what the real field name honours; this file's mock supplied
// a `mode: 'seat_tracked' | 'manual'` FIELD, exactly the shape the contract
// warns against), `seat_tracked: boolean`, `resume_action: string | null`
// (truthy exactly when there is something to resume to, rendered as
// `data-testid="pool-follow-seats-button"`, never `pool-mode-resume-button`),
// and `entitlement_exceeds_figure: boolean` (rendered as its own element,
// `data-testid="pool-entitlement-outgrew"`, never a `data-outgrown`
// attribute on the sentence itself). `over_ceiling_microusd` is also real
// and required -- the component renders the "over ceiling by" line only
// when it is present and positive, which this file's own R29 fixture never
// supplied.

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { PoolBudget } from '@/lib/api'

vi.mock('@/lib/api', () => ({
  api: {
    admin: {
      getTenant: (...args: unknown[]) => (globalThis as any).__getTenant(...args),
      tenantUsers: (...args: unknown[]) => (globalThis as any).__tenantUsers(...args),
      tenantUsage: (...args: unknown[]) => (globalThis as any).__tenantUsage(...args),
      getPoolBudget: (...args: unknown[]) => (globalThis as any).__getPoolBudget(...args),
      setPoolBudget: (...args: unknown[]) => (globalThis as any).__setPoolBudget(...args),
      getRoutingConfig: (...args: unknown[]) => (globalThis as any).__getRoutingConfig(...args),
    },
  },
}))

const mockGetTenant = vi.fn()
const mockTenantUsers = vi.fn()
const mockTenantUsage = vi.fn()
const mockGetPoolBudget = vi.fn()
const mockSetPoolBudget = vi.fn()
const mockGetRoutingConfig = vi.fn()
;(globalThis as any).__getTenant = () => mockGetTenant()
;(globalThis as any).__tenantUsers = () => mockTenantUsers()
;(globalThis as any).__tenantUsage = () => mockTenantUsage()
;(globalThis as any).__getPoolBudget = () => mockGetPoolBudget()
;(globalThis as any).__setPoolBudget = () => mockSetPoolBudget()
;(globalThis as any).__getRoutingConfig = () => mockGetRoutingConfig()

import AdminTenantDetail from './AdminTenantDetail'

function withProviders(children: ReactNode) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0, staleTime: 0 } },
  })
  return (
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={['/admin/tenants/acme-eng']}>
        <Routes>
          <Route path="/admin/tenants/:tenantId" element={children} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  )
}

const BASE_TENANT = {
  tenant_id: 'acme-eng',
  name: 'Acme Eng',
  default_credit: 10_000_000,
  status: 'active',
}

// The real shape (`mvp/admin_tenants.py::_pool_response`/`_mode_sentence`),
// not this file's earlier guess. Every field `PoolBudgetCard.tsx` reads is
// present so a missing one cannot silently render "undefined" and pass.
function poolBudget(overrides: Partial<PoolBudget> = {}): PoolBudget {
  return {
    tenant_id: 'acme-eng',
    period: '2026-09',
    status: 'active',
    pool_limit_microusd: 600_000_000,
    pool_reserved_microusd: 0,
    pool_settled_microusd: 0,
    remaining_microusd: 600_000_000,
    over_ceiling_microusd: 0,
    pool_limit_usd_cents: 60_000,
    remaining_usd_cents: 60_000,
    mode_sentence:
      "This pool follows the tenant's seat count: 3 seats entitle it to " +
      "$600.00 a month, and it moves by one seat's worth whenever somebody " +
      'joins or leaves. Setting a figure by hand stops that.',
    seat_tracked: true,
    seat_count: 3,
    seat_rate_microusd: 200_000_000,
    seat_entitlement_microusd: 600_000_000,
    manual_limit_microusd: null,
    pool_granted_microusd: 0,
    baseline_microusd: 600_000_000,
    entitlement_exceeds_figure: false,
    resume_action: null,
    grant_cap_microusd: null,
    effective_grant_cap_microusd: 600_000_000,
    grant_cap_is_derived: true,
    remaining_grant_cap_microusd: 600_000_000,
    ...overrides,
  }
}

beforeEach(() => {
  mockGetTenant.mockReset().mockResolvedValue(BASE_TENANT)
  mockTenantUsers.mockReset().mockResolvedValue({ tenant_id: 'acme-eng', members: [] })
  mockTenantUsage.mockReset().mockResolvedValue({
    tenant_id: 'acme-eng', total_tokens: 0, input_tokens: 0, output_tokens: 0,
    by_model: {}, sample_size: 0,
  })
  mockGetRoutingConfig.mockReset().mockResolvedValue({
    tenant_id: 'acme-eng', configured: false, allowlist: [], chain: [],
    quotas: {}, fallback_mode: 'off', fallback_default: '',
  })
  mockSetPoolBudget.mockReset()
})

describe('R21: the pool mode renders as a sentence, not a field', () => {
  it('renders a seat-tracked sentence naming the seat count and rate', async () => {
    mockGetPoolBudget.mockResolvedValue(poolBudget())

    render(withProviders(<AdminTenantDetail />))

    const sentence = await screen.findByTestId('pool-mode-sentence')
    expect(sentence.textContent).toContain('3')
    // A seat-tracked row has no resume action -- there is nothing to resume.
    expect(screen.queryByTestId('pool-follow-seats-button')).not.toBeInTheDocument()
  })

  it('renders a manual sentence with a resume action', async () => {
    mockGetPoolBudget.mockResolvedValue(
      poolBudget({
        pool_limit_microusd: 100_000_000,
        remaining_microusd: 100_000_000,
        pool_limit_usd_cents: 10_000,
        remaining_usd_cents: 10_000,
        mode_sentence:
          'This pool is held at $100.00, a figure set by hand, and no longer ' +
          "follows the tenant's seat count. The seats would entitle it to " +
          '$600.00, which is more than the figure, so the figure is now the ' +
          'smaller of the two. Sending {"follow_seats": true} to this endpoint ' +
          'returns it to the seat count.',
        seat_tracked: false,
        manual_limit_microusd: 100_000_000,
        baseline_microusd: 100_000_000,
        entitlement_exceeds_figure: true,
        resume_action: 'follow_seats',
      }),
    )

    render(withProviders(<AdminTenantDetail />))

    await screen.findByTestId('pool-mode-sentence')
    expect(screen.getByTestId('pool-follow-seats-button')).toBeInTheDocument()
  })

  it('flags a manual row whose live entitlement has outgrown its figure', async () => {
    // 3 seats * $200 = $600 entitlement, but the manual figure is only $100.
    mockGetPoolBudget.mockResolvedValue(
      poolBudget({
        pool_limit_microusd: 100_000_000,
        remaining_microusd: 100_000_000,
        pool_limit_usd_cents: 10_000,
        remaining_usd_cents: 10_000,
        mode_sentence:
          'This pool is held at $100.00, a figure set by hand, and no longer ' +
          "follows the tenant's seat count. The seats would entitle it to " +
          '$600.00, which is more than the figure, so the figure is now the ' +
          'smaller of the two. Sending {"follow_seats": true} to this endpoint ' +
          'returns it to the seat count.',
        seat_tracked: false,
        manual_limit_microusd: 100_000_000,
        baseline_microusd: 100_000_000,
        seat_entitlement_microusd: 600_000_000,
        entitlement_exceeds_figure: true,
        resume_action: 'follow_seats',
      }),
    )

    render(withProviders(<AdminTenantDetail />))

    await screen.findByTestId('pool-mode-sentence')
    // Rendered as its own element (`entitlement_exceeds_figure`), never a
    // `data-outgrown` attribute on the sentence itself.
    expect(screen.getByTestId('pool-entitlement-outgrew')).toBeInTheDocument()
  })
})

describe('R29: negative headroom is a signed deficit, never clamped', () => {
  it('shows an explicit "over ceiling by" line when remaining is negative', async () => {
    mockGetPoolBudget.mockResolvedValue(
      poolBudget({
        pool_limit_microusd: 100_000_000,
        pool_settled_microusd: 150_000_000,
        remaining_microusd: -50_000_000, // signed, uncapped
        // The magnitude of the overshoot -- a DIFFERENT, required field, not
        // derived by the component from the signed remaining figure.
        over_ceiling_microusd: 50_000_000,
        pool_limit_usd_cents: 10_000,
        remaining_usd_cents: -5_000,
        seat_tracked: false,
        seat_count: 1,
        manual_limit_microusd: 100_000_000,
        baseline_microusd: 100_000_000,
        resume_action: 'follow_seats',
      }),
    )

    render(withProviders(<AdminTenantDetail />))

    const overCeiling = await screen.findByTestId('pool-over-ceiling')
    // $50.00 over -- rendered as a positive deficit, not a bare negative sign
    // buried in the "remaining" slot.
    expect(overCeiling.textContent).toMatch(/\$50\.00/)
  })

  it('does not show the "over ceiling by" line when remaining is non-negative', async () => {
    mockGetPoolBudget.mockResolvedValue(
      poolBudget({
        pool_limit_microusd: 100_000_000,
        remaining_microusd: 100_000_000,
        over_ceiling_microusd: 0,
        pool_limit_usd_cents: 10_000,
        remaining_usd_cents: 10_000,
        seat_tracked: true,
        seat_count: 1,
        manual_limit_microusd: null,
        baseline_microusd: 100_000_000,
        seat_entitlement_microusd: 100_000_000,
      }),
    )

    render(withProviders(<AdminTenantDetail />))

    await screen.findByTestId('pool-budget-summary')
    await waitFor(() => {
      expect(screen.queryByTestId('pool-over-ceiling')).not.toBeInTheDocument()
    })
  })
})
