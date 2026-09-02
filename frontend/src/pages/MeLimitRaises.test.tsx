// MeLimitRaises — the self-service limit-raise request view (F3 / R12, R24,
// and the interface note on tenant provenance).
//
// Contract: change-pipeline/quota-raise-and-archive/CONTRACT-F3-surfaces.md
//   R12: "Console — the self-service request view (any authenticated user):
//   the walls that apply to the caller and their remaining capacity; a
//   submission carrying the reason enum, a comment and an amount, pre-filled
//   from the raise_hint of the refusal that sent them there — including the
//   tenant, which is carried from the hint and never taken from ambient
//   client context; and the caller's own requests with, for a decided one,
//   the approved amount, the expiry and the approver."
//   R24: "a decided request carries [approved amount, expiry, approver], a
//   pending one carries none."
//
// This component does not exist anywhere in this worktree (F1's raise
// mechanism has not landed here either) — every test below fails at module
// resolution. The test bodies are the executable spec: what a person must
// see, not merely what field the API response carries (per this role's
// brief: "Assert what a person sees, not that a field exists").

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import type { ReactNode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// ---- Module mocks (hoisted) ----
vi.mock('@/lib/api', () => ({
  api: {
    limitRaises: {
      mine: (...args: unknown[]) => (globalThis as any).__lrMine(...args),
      submit: (...args: unknown[]) => (globalThis as any).__lrSubmit(...args),
      reasons: (...args: unknown[]) => (globalThis as any).__lrReasons(...args),
    },
  },
}))

const mockMine = vi.fn()
const mockSubmit = vi.fn()
const mockReasons = vi.fn()
;(globalThis as any).__lrMine = (...a: unknown[]) => mockMine(...a)
;(globalThis as any).__lrSubmit = (...a: unknown[]) => mockSubmit(...a)
;(globalThis as any).__lrReasons = (...a: unknown[]) => mockReasons(...a)

// Imported after the mocks so React sees the stubbed module. This import is
// what fails today: `./MeLimitRaises` does not exist.
import MeLimitRaises from './MeLimitRaises'

function withClient(children: ReactNode, initialPath = '/me/limit-raises') {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0, staleTime: 0 } },
  })
  return (
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[initialPath]}>{children}</MemoryRouter>
    </QueryClientProvider>
  )
}

const DECIDED_ROW = {
  request_id: 'lr_9f2c',
  tenant_id: 'acme-eng',
  reason: 'cascade_shortfall',
  comment: 'need opus for the eval batch',
  requested_amount_microusd: 200_000_000, // she asked for $200
  status: 'approved',
  decided_at: '2026-08-30T09:02:00Z',
  approved_amount_microusd: 50_000_000, // she got $50
  expires_at: '2026-08-31T23:59:59Z',
  // Corrected per contract: a stable id, resolved to a display name by the
  // console — never an address on the wire. (This test used to assert
  // `approver_email`; missed in an earlier reconciliation pass, fixed here.)
  approver_id: 'user-lead-1',
}

const PENDING_ROW = {
  request_id: 'lr_a013',
  tenant_id: 'acme-eng',
  reason: 'cascade_shortfall',
  comment: '',
  requested_amount_microusd: 12_000_000,
  status: 'pending',
  decided_at: null,
  approved_amount_microusd: null,
  expires_at: null,
  approver_id: null,
}

beforeEach(() => {
  mockMine.mockReset()
  mockSubmit.mockReset()
  mockReasons.mockReset()
  mockReasons.mockResolvedValue(['cascade_shortfall', 'seasonal_spike'])
})
afterEach(() => {
  vi.clearAllMocks()
})

describe('MeLimitRaises — R24: decided vs pending join', () => {
  it('renders the approved AMOUNT and expiry, not just the bare status string', async () => {
    mockMine.mockResolvedValue({ requests: [DECIDED_ROW] })
    render(withClient(<MeLimitRaises />))

    // The defect this id exists to prevent: seeing only "APPROVED" lets a
    // requester plan against the $200 she asked for, not the $50 she got.
    await waitFor(() => expect(screen.getByText('$50.00')).toBeInTheDocument())
    // The amount she originally asked for must ALSO still be visible
    // (for contrast), but never presented as what she was granted.
    expect(screen.queryByText('$200.00')).not.toBeNull()
    expect(screen.getByText('$200.00')).not.toBe(screen.getByText('$50.00'))
    // The expiry must be visible, not just the amount.
    expect(screen.getByText(/2026-08-31|Aug 31/)).toBeInTheDocument()
    // The approver must be visibly identified somehow — the wire field is
    // `approver_id` (never an address), resolved to a display name "on
    // demand"; this test does not mock that resolution, so it only asserts
    // an approver identifier renders at all, not a specific display name.
    expect(screen.queryByText('user-lead-1')).not.toBeNull()
  })

  it('renders no decision fields for a pending request, and does not read as queued work', async () => {
    mockMine.mockResolvedValue({ requests: [PENDING_ROW] })
    render(withClient(<MeLimitRaises />))

    await waitFor(() => expect(screen.getByText(/pending/i)).toBeInTheDocument())
    // No approved amount, no expiry, no approver anywhere on the pending row.
    expect(screen.queryByText('$0.00')).toBeNull()
    expect(screen.queryByText(/approved by|approver/i)).toBeNull()
    // "PENDING must not read as 'my work is queued'" — the bare word
    // "Pending" or "Queued" alone fails this; the copy must say the
    // operation was NOT admitted yet.
    expect(
      screen.getByText(/did not (change|queue)|not (been )?queued|waiting for .* approve/i),
    ).toBeInTheDocument()
  })
})

describe('MeLimitRaises — interface note: tenant is carried from the hint, never ambient', () => {
  it('pre-fills the submission tenant from the raise_hint prop, ignoring any ambient tenant context', async () => {
    mockMine.mockResolvedValue({ requests: [] })
    const hint = {
      tenant_id: 'from-the-hint-org',
      requested_model_id: 'claude-opus-4-7',
      target_shortfall_microusd: 11_600_000,
      minimum_raise_microusd: 400_000,
      remaining_cap_microusd: 20_000_000, // B6: comfortably above the minimum — no conflict here
      router_mode: 'cascade',
      pricing_version: '2026-08-rev3',
      priced_at: '2026-09-02T04:11:00Z',
      candidates: [],
      unattempted_model_ids: [],
    }

    render(
      withClient(
        // An "ambient" tenant is simulated via a query param an evil/careless
        // implementation might read instead of the hint — this must be IGNORED.
        <MeLimitRaises raiseHint={hint} />,
        '/me/limit-raises?tenant_id=ambient-context-org',
      ),
    )

    await waitFor(() =>
      expect(screen.getByDisplayValue('from-the-hint-org')).toBeInTheDocument(),
    )
    expect(screen.queryByDisplayValue('ambient-context-org')).toBeNull()
  })

  it('pre-fills the amount from minimum_raise_microusd, not the target shortfall, when a cheaper grantable fallback exists', async () => {
    mockMine.mockResolvedValue({ requests: [] })
    const hint = {
      tenant_id: 'acme-eng',
      requested_model_id: 'claude-opus-4-7',
      target_shortfall_microusd: 11_600_000, // $11.60 — what the TARGET needed
      minimum_raise_microusd: 400_000, // $0.40 — the cheapest grantable fallback
      remaining_cap_microusd: 20_000_000,
      router_mode: 'cascade',
      pricing_version: '2026-08-rev3',
      priced_at: '2026-09-02T04:11:00Z',
      candidates: [],
      unattempted_model_ids: [],
    }
    render(withClient(<MeLimitRaises raiseHint={hint} />))
    await waitFor(() => expect(screen.getByDisplayValue('0.40')).toBeInTheDocument())
  })

  it('names the unattempted candidates plainly when the hint carries any (B5), never silence', async () => {
    mockMine.mockResolvedValue({ requests: [] })
    const hint = {
      tenant_id: 'acme-eng',
      requested_model_id: 'claude-opus-4-7',
      target_shortfall_microusd: 11_600_000,
      minimum_raise_microusd: 11_600_000,
      remaining_cap_microusd: 20_000_000,
      router_mode: 'cascade',
      pricing_version: '2026-08-rev3',
      priced_at: '2026-09-02T04:11:00Z',
      candidates: [
        {
          model_id: 'claude-opus-4-7',
          estimated_cost_microusd: 12_000_000,
          shortfall_microusd: 11_600_000,
          blocker: 'tenant_pool',
          grantable: true,
        },
      ],
      // The pool wall ended the cascade after pricing exactly one candidate
      // (B5) — these two were configured but never priced.
      unattempted_model_ids: ['claude-sonnet-4-6', 'claude-haiku-4-5'],
    }
    render(withClient(<MeLimitRaises raiseHint={hint} />))
    await waitFor(() =>
      expect(screen.getByText(/claude-sonnet-4-6/)).toBeInTheDocument(),
    )
    expect(screen.getByText(/claude-haiku-4-5/)).toBeInTheDocument()
    // Must read as "not attempted", not as "no cheaper option existed" —
    // silence on this field would imply the latter.
    expect(screen.getByText(/not attempted|were not tried|never priced/i)).toBeInTheDocument()
  })
})

describe('MeLimitRaises — B6: the hint must not recommend a raise no approver may grant', () => {
  it('does NOT pre-fill the amount when minimum_raise_microusd exceeds remaining_cap_microusd; renders the conflict instead', async () => {
    mockMine.mockResolvedValue({ requests: [] })
    const hint = {
      tenant_id: 'acme-eng',
      requested_model_id: 'claude-opus-4-7',
      target_shortfall_microusd: 11_600_000,
      minimum_raise_microusd: 11_600_000, // $11.60 needed
      remaining_cap_microusd: 5_000_000, // but the tenant can only be granted $5.00 more — no approver could grant this
      router_mode: 'cascade',
      pricing_version: '2026-08-rev3',
      priced_at: '2026-09-02T04:11:00Z',
      candidates: [
        {
          model_id: 'claude-opus-4-7',
          estimated_cost_microusd: 12_000_000,
          shortfall_microusd: 11_600_000,
          blocker: 'tenant_pool',
          grantable: true,
        },
      ],
      unattempted_model_ids: [],
    }
    render(withClient(<MeLimitRaises raiseHint={hint} />))

    // The conflict must be rendered — a day of latency on a dead end is
    // exactly what this id exists to prevent.
    await waitFor(() =>
      expect(
        screen.getByText(/no approver|could not (be )?grant|exceeds.*cap|cap.*exceed/i),
      ).toBeInTheDocument(),
    )
    // And the amount input must NOT be pre-filled with the impossible
    // figure — pre-filling it invites exactly the round trip B6 exists to
    // save: a request that approval will refuse with 422 grant_cap_exceeded.
    expect(screen.queryByDisplayValue('11.60')).toBeNull()
  })

  it('pre-fills normally when minimum_raise_microusd is within remaining_cap_microusd', async () => {
    mockMine.mockResolvedValue({ requests: [] })
    const hint = {
      tenant_id: 'acme-eng',
      requested_model_id: 'claude-opus-4-7',
      target_shortfall_microusd: 400_000,
      minimum_raise_microusd: 400_000,
      remaining_cap_microusd: 20_000_000, // comfortably above — no conflict
      router_mode: 'cascade',
      pricing_version: '2026-08-rev3',
      priced_at: '2026-09-02T04:11:00Z',
      candidates: [],
      unattempted_model_ids: [],
    }
    render(withClient(<MeLimitRaises raiseHint={hint} />))
    await waitFor(() => expect(screen.getByDisplayValue('0.40')).toBeInTheDocument())
    expect(screen.queryByText(/no approver|could not (be )?grant/i)).toBeNull()
  })
})
