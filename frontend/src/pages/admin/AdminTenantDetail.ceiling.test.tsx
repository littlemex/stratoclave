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
// Design note's reading (section 6/7): the admin console (AdminTenantDetail,
// the one page today that renders PoolBudget at all) is the surface this
// file targets; the team-lead console renders no pool information whatsoever
// today, a separate pre-existing gap this file does not attempt to close.
//
// Today `PoolBudgetCard` renders period/status/limit/remaining/reserved/
// settled only -- no mode sentence, no resume action, no "over ceiling by"
// line -- and `PoolBudget` (frontend/src/lib/api.ts) carries no `mode`,
// `seat_count`, or `manual_limit_microusd` field at all. Every test below
// fails today because the element it looks for does not exist, NOT because
// of a money-formatting bug (`fmtMicroUsd` itself already renders negative
// numbers correctly -- see src/lib/money.test.ts).

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

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
    mockGetPoolBudget.mockResolvedValue({
      tenant_id: 'acme-eng', period: '2026-09', status: 'active',
      pool_limit_microusd: 600_000_000, pool_reserved_microusd: 0,
      pool_settled_microusd: 0, remaining_microusd: 600_000_000,
      pool_limit_usd_cents: 60_000, remaining_usd_cents: 60_000,
      mode: 'seat_tracked', seat_count: 3, manual_limit_microusd: null,
    })

    render(withProviders(<AdminTenantDetail />))

    const sentence = await screen.findByTestId('pool-mode-sentence')
    expect(sentence.textContent).toContain('3')
    // A seat-tracked row has no resume action -- there is nothing to resume.
    expect(screen.queryByTestId('pool-mode-resume-button')).not.toBeInTheDocument()
  })

  it('renders a manual sentence with a resume action', async () => {
    mockGetPoolBudget.mockResolvedValue({
      tenant_id: 'acme-eng', period: '2026-09', status: 'active',
      pool_limit_microusd: 100_000_000, pool_reserved_microusd: 0,
      pool_settled_microusd: 0, remaining_microusd: 100_000_000,
      pool_limit_usd_cents: 10_000, remaining_usd_cents: 10_000,
      mode: 'manual', seat_count: 3, manual_limit_microusd: 100_000_000,
    })

    render(withProviders(<AdminTenantDetail />))

    await screen.findByTestId('pool-mode-sentence')
    expect(screen.getByTestId('pool-mode-resume-button')).toBeInTheDocument()
  })

  it('flags a manual row whose live entitlement has outgrown its figure', async () => {
    // 3 seats * $200 = $600 entitlement, but the manual figure is only $100.
    mockGetPoolBudget.mockResolvedValue({
      tenant_id: 'acme-eng', period: '2026-09', status: 'active',
      pool_limit_microusd: 100_000_000, pool_reserved_microusd: 0,
      pool_settled_microusd: 0, remaining_microusd: 100_000_000,
      pool_limit_usd_cents: 10_000, remaining_usd_cents: 10_000,
      mode: 'manual', seat_count: 3, manual_limit_microusd: 100_000_000,
      seat_entitlement_microusd: 600_000_000,
    })

    render(withProviders(<AdminTenantDetail />))

    const sentence = await screen.findByTestId('pool-mode-sentence')
    expect(sentence.getAttribute('data-outgrown')).toBe('true')
  })
})

describe('R29: negative headroom is a signed deficit, never clamped', () => {
  it('shows an explicit "over ceiling by" line when remaining is negative', async () => {
    mockGetPoolBudget.mockResolvedValue({
      tenant_id: 'acme-eng', period: '2026-09', status: 'active',
      pool_limit_microusd: 100_000_000, pool_reserved_microusd: 0,
      pool_settled_microusd: 150_000_000,
      remaining_microusd: -50_000_000,   // signed, uncapped
      pool_limit_usd_cents: 10_000, remaining_usd_cents: -5_000,
      mode: 'manual', seat_count: 1, manual_limit_microusd: 100_000_000,
    })

    render(withProviders(<AdminTenantDetail />))

    const overCeiling = await screen.findByTestId('pool-over-ceiling')
    // $50.00 over -- rendered as a positive deficit, not a bare negative sign
    // buried in the "remaining" slot.
    expect(overCeiling.textContent).toMatch(/\$50\.00/)
  })

  it('does not show the "over ceiling by" line when remaining is non-negative', async () => {
    mockGetPoolBudget.mockResolvedValue({
      tenant_id: 'acme-eng', period: '2026-09', status: 'active',
      pool_limit_microusd: 100_000_000, pool_reserved_microusd: 0,
      pool_settled_microusd: 0, remaining_microusd: 100_000_000,
      pool_limit_usd_cents: 10_000, remaining_usd_cents: 10_000,
      mode: 'seat_tracked', seat_count: 1, manual_limit_microusd: null,
    })

    render(withProviders(<AdminTenantDetail />))

    await screen.findByTestId('pool-budget-summary')
    await waitFor(() => {
      expect(screen.queryByTestId('pool-over-ceiling')).not.toBeInTheDocument()
    })
  })
})
