// MeUsage page rendering tests.
//
// Focus is the display of `/api/mvp/me/usage-summary` and `/usage-history`
// responses — the single place where the backend P0-4 `by_model` fix is
// observable from the UI. We mock `api.usageSummary` and
// `api.usageHistory` and render the page inside a QueryClientProvider so
// React Query works as in production.

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { UsageHistoryResponse, UsageSummary } from '@/lib/api'

// ---- Module mocks (hoisted) ----
vi.mock('@/lib/api', () => ({
  api: {
    usageSummary: (...args: unknown[]) =>
      (globalThis as any).__usageSummary(...args),
    usageHistory: (...args: unknown[]) =>
      (globalThis as any).__usageHistory(...args),
  },
}))

const mockUsageSummary = vi.fn<[number?], Promise<UsageSummary>>()
const mockUsageHistory = vi.fn<[unknown], Promise<UsageHistoryResponse>>()
;(globalThis as any).__usageSummary = (...a: unknown[]) =>
  mockUsageSummary(...(a as []))
;(globalThis as any).__usageHistory = (...a: unknown[]) =>
  mockUsageHistory(...(a as []))

// Imported after the mocks so React sees stubbed modules.
import MeUsage from './MeUsage'

function withClient(children: ReactNode) {
  const client = new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: 0, staleTime: 0 },
    },
  })
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>
}

const FIXTURE_SUMMARY: UsageSummary = {
  tenant_id: 'default-org',
  total_credit: 10_000,
  credit_used: 2_500,
  remaining_credit: 7_500,
  by_model: {
    'us.anthropic.claude-opus-4-7': 1_800,
    'us.anthropic.claude-sonnet-4-6': 700,
  },
  by_tenant: { 'default-org': 2_500 },
  sample_size: 4,
  since_days: 30,
}

const FIXTURE_HISTORY: UsageHistoryResponse = {
  history: [
    {
      tenant_id: 'default-org',
      tenant_name: 'Default Organization',
      model_id: 'us.anthropic.claude-opus-4-7',
      input_tokens: 1000,
      output_tokens: 200,
      total_tokens: 1200,
      recorded_at: '2026-04-20T12:00:00Z',
    },
  ],
  next_cursor: null,
}

beforeEach(() => {
  mockUsageSummary.mockReset()
  mockUsageHistory.mockReset()
})
afterEach(() => {
  vi.clearAllMocks()
})

describe('MeUsage', () => {
  it('renders KPI totals from the summary payload', async () => {
    mockUsageSummary.mockResolvedValue(FIXTURE_SUMMARY)
    mockUsageHistory.mockResolvedValue(FIXTURE_HISTORY)

    render(withClient(<MeUsage />))

    // "期間の総消費" = sum of by_model values → 1,800 + 700 = 2,500 (may
    // also appear in the credit_used slot). Using getAllByText confirms
    // at least one occurrence and avoids brittle ordering.
    await waitFor(() =>
      expect(screen.getAllByText(/2,500/).length).toBeGreaterThan(0),
    )
    expect(screen.getAllByText(/7,500/).length).toBeGreaterThan(0)
    expect(screen.getAllByText(/10,000/).length).toBeGreaterThan(0)
    // by_model entries rendered as rows of the breakdown panel (and
    // again in the history table for opus).
    expect(screen.getAllByText(/claude-opus-4-7/).length).toBeGreaterThan(0)
    expect(screen.getAllByText(/claude-sonnet-4-6/).length).toBeGreaterThan(0)
  })

  it('recovers gracefully when usage-summary returns zero models', async () => {
    mockUsageSummary.mockResolvedValue({
      ...FIXTURE_SUMMARY,
      by_model: {},
      by_tenant: {},
      credit_used: 0,
      remaining_credit: FIXTURE_SUMMARY.total_credit,
      sample_size: 0,
    })
    mockUsageHistory.mockResolvedValue({ history: [], next_cursor: null })

    render(withClient(<MeUsage />))

    // Empty state: remaining credit = full budget.
    await waitFor(() =>
      expect(screen.getAllByText(/10,000/).length).toBeGreaterThan(0),
    )
  })

  it('requests the summary with the default 30-day window', async () => {
    mockUsageSummary.mockResolvedValue(FIXTURE_SUMMARY)
    mockUsageHistory.mockResolvedValue(FIXTURE_HISTORY)

    render(withClient(<MeUsage />))
    await waitFor(() => expect(mockUsageSummary).toHaveBeenCalled())
    expect(mockUsageSummary).toHaveBeenCalledWith(30)
    expect(mockUsageHistory).toHaveBeenCalled()
  })
})
