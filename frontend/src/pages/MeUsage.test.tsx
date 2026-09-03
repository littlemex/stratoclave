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

// vitest >=3 collapsed the legacy `vi.fn<TArgs, TReturn>()` form into
// a single function-type generic.
const mockUsageSummary = vi.fn<(sinceDays?: number) => Promise<UsageSummary>>()
const mockUsageHistory = vi.fn<(opts: unknown) => Promise<UsageHistoryResponse>>()
;(globalThis as any).__usageSummary = (...a: unknown[]) =>
  mockUsageSummary(a[0] as number | undefined)
;(globalThis as any).__usageHistory = (...a: unknown[]) =>
  mockUsageHistory(a[0])

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

    // "Period total consumption" = sum of by_model values → 1,800 + 700 = 2,500 (may
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

  it('shows a fallback badge + summary count when a request cascaded (P0-11)', async () => {
    mockUsageSummary.mockResolvedValue({ ...FIXTURE_SUMMARY, fallback_count: 1 })
    mockUsageHistory.mockResolvedValue({
      history: [
        {
          tenant_id: 'default-org',
          tenant_name: 'Default Organization',
          model_id: 'us.anthropic.claude-haiku-4-5',
          input_tokens: 10,
          output_tokens: 5,
          total_tokens: 15,
          recorded_at: '2026-04-20T12:00:00Z',
          requested_model_id: 'claude-opus-4-7',
          fallback_occurred: true,
        },
      ],
      next_cursor: null,
    })

    render(withClient(<MeUsage />))

    // The badge renders (default en copy: "fallback").
    await waitFor(() =>
      expect(screen.getAllByText(/fallback/i).length).toBeGreaterThan(0),
    )
    // Effective model shown; requested surfaced via the badge title.
    expect(screen.getAllByText(/claude-haiku-4-5/).length).toBeGreaterThan(0)
  })

  it('renders no fallback badge for a legacy row (fallback_occurred null)', async () => {
    mockUsageSummary.mockResolvedValue(FIXTURE_SUMMARY) // no fallback_count
    mockUsageHistory.mockResolvedValue(FIXTURE_HISTORY) // no fallback_occurred

    render(withClient(<MeUsage />))
    await waitFor(() =>
      expect(screen.getAllByText(/claude-opus-4-7/).length).toBeGreaterThan(0),
    )
    // Legacy row: badge text must NOT appear.
    expect(screen.queryByText(/^fallback$/i)).toBeNull()
  })

  // F3 / R38 — "A user cannot distinguish 'my grant expired' from 'the
  // router changed its mind', and a usage view spanning an expiry shows a
  // model change with no cause."
  //
  // `fallback_reason` is not yet a field on `UsageHistoryEntry` (see this
  // role's design note, section R38 — it requires a new backend field
  // threaded through `usage_logs.py::record()`, `me.py`'s response model,
  // and this type). The cast below simulates the API already carrying it so this test
  // exercises ONLY the rendering gap: today `MeUsage.tsx`'s fallback badge
  // title is the fixed string `me_usage.fallback_from` ("Fell back from
  // requested {{requested}}") regardless of cause — there is no branch for
  // "this fallback happened because a grant expired" at all.
  it('names the grant expiry as the fallback cause, not the fixed fallback copy (R38)', async () => {
    mockUsageSummary.mockResolvedValue({ ...FIXTURE_SUMMARY, fallback_count: 1 })
    mockUsageHistory.mockResolvedValue({
      history: [
        {
          tenant_id: 'default-org',
          tenant_name: 'Default Organization',
          model_id: 'us.anthropic.claude-haiku-4-5',
          input_tokens: 10,
          output_tokens: 5,
          total_tokens: 15,
          recorded_at: '2026-04-20T12:00:00Z',
          requested_model_id: 'claude-opus-4-7',
          fallback_occurred: true,
          // Not yet a real field on UsageHistoryEntry — see comment above.
          fallback_reason: 'grant_expired',
        } as UsageHistoryResponse['history'][number] & { fallback_reason: string },
      ],
      next_cursor: null,
    })

    render(withClient(<MeUsage />))

    await waitFor(() =>
      expect(screen.getAllByText(/fallback/i).length).toBeGreaterThan(0),
    )
    // The current fixed copy ("Fell back from requested claude-opus-4-7")
    // never mentions expiry for ANY cause — this must fail today, and must
    // start passing once the badge's title branches on fallback_reason.
    //
    // `/fallback/i` also matches the KPI summary line above the table
    // ("1 request(s) served by a fallback model", `me_usage.fallback_count`)
    // which renders EARLIER in the DOM and carries no `title` — a bare
    // `getAllByText(...)[0]` grabs that one, not the badge, and asserts
    // against its (always-empty) `closest('[title]')`. The badge's own text
    // is the exact string "fallback" (`me_usage.fallback_badge`, no other
    // words in that span), so an exact match isolates it from the KPI line.
    const badge = screen.getAllByText('fallback', { exact: true })[0]
    const title = badge.closest('[title]')?.getAttribute('title') ?? ''
    expect(title).toMatch(/expir/i)
  })
})
