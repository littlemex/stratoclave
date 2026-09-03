// MeLimitRaises — the self-service limit-raise request view.
//
// It must show: the walls that apply to the caller and their remaining
// capacity; a submission carrying the reason enum, a comment and an amount,
// pre-filled from the raise_hint of the refusal that sent them there --
// including the tenant, which is carried from the hint and never taken from
// ambient client context; and the caller's own requests with, for a decided
// one, the approved amount, the expiry and the approver. A decided request
// carries all three of those; a pending one carries none.
//
// The test bodies are the executable spec: what a person must see, not
// merely what field the API response carries -- assert what a person sees,
// not that a field exists.

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import type { ReactNode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// ---- Module mocks (hoisted) ----
// The real `api` object (`frontend/src/lib/api.ts`) is FLAT --
// `api.listMyLimitRaises` / `api.submitLimitRaise` / `api.myWallStatus` --
// not a nested `api.limitRaises.{mine,submit,reasons}` namespace this test
// used to guess. There is also no separate "reasons" endpoint: the real
// component reads `reason_codes` off the `listMyLimitRaises` response
// itself (or the hint), never a dedicated fetch. `myWallStatus` is added
// because the component calls it unconditionally on mount (R12: "the walls
// that apply to the caller").
vi.mock('@/lib/api', () => ({
  api: {
    listMyLimitRaises: (...args: unknown[]) => (globalThis as any).__lrMine(...args),
    submitLimitRaise: (...args: unknown[]) => (globalThis as any).__lrSubmit(...args),
    myWallStatus: (...args: unknown[]) => (globalThis as any).__lrWallStatus(...args),
  },
}))

const mockMine = vi.fn()
const mockSubmit = vi.fn()
const mockWallStatus = vi.fn()
;(globalThis as any).__lrMine = (...a: unknown[]) => mockMine(...a)
;(globalThis as any).__lrSubmit = (...a: unknown[]) => mockSubmit(...a)
;(globalThis as any).__lrWallStatus = (...a: unknown[]) => mockWallStatus(...a)

// Imported after the mocks so React sees the stubbed module. This import is
// what fails today: `./MeLimitRaises` does not exist.
import MeLimitRaises from './MeLimitRaises'

// R24 join fields, plus the hint prop, travel through a `MemoryRouter`
// rather than through component props: `MeLimitRaises` reads the hint from
// `useLocation().state.raiseHint` (contract journey amendment U4 --
// "the hint reaches the request screen through navigation state, and a
// deep link pre-fills nothing"), never from a prop. A component-level prop
// would be a second way to supply the same fact, which this epic's own
// pattern (one source per fact, one name) rules out everywhere else.
function withClient(
  children: ReactNode,
  opts: { pathname?: string; raiseHint?: unknown } = {},
) {
  const { pathname = '/me/limit-raises', raiseHint } = opts
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0, staleTime: 0 } },
  })
  const entry = raiseHint !== undefined ? { pathname, state: { raiseHint } } : pathname
  return (
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[entry]}>{children}</MemoryRouter>
    </QueryClientProvider>
  )
}

// `expires_at` is the wire's epoch-SECONDS int (`backend/mvp/grants.py`'s
// `_request_public`: `int(item["expires_at"])`, the same convention every
// other `expires_at` in this codebase uses) -- not an ISO string. Computed
// rather than hand-typed so the literal date stays legible.
const AUG_31_2026_EOD_EPOCH = Math.floor(Date.UTC(2026, 7, 31, 23, 59, 59) / 1000)

const DECIDED_ROW = {
  request_id: 'lr_9f2c',
  tenant_id: 'acme-eng',
  reason_code: 'cascade_shortfall',
  decision_comment: 'need opus for the eval batch',
  // The pinned wire name is `asked_amount_microusd`, not
  // `requested_amount_microusd` -- this test used to guess the latter.
  asked_amount_microusd: 200_000_000, // she asked for $200
  status: 'APPROVED',
  decided_at: '2026-08-30T09:02:00Z',
  approved_amount_microusd: 50_000_000, // she got $50
  expires_at: AUG_31_2026_EOD_EPOCH,
  created_at: '2026-08-29T00:00:00Z',
  limit_kind: 'tenant_pool',
  // Corrected per contract: a stable id, resolved to a display name by the
  // console — never an address on the wire. (This test used to assert
  // `approver_email`; missed in an earlier reconciliation pass, fixed here.)
  approver_id: 'user-lead-1',
}

const PENDING_ROW = {
  request_id: 'lr_a013',
  tenant_id: 'acme-eng',
  reason_code: 'cascade_shortfall',
  asked_amount_microusd: 12_000_000,
  status: 'PENDING',
  decided_at: null,
  approved_amount_microusd: null,
  expires_at: null,
  created_at: '2026-08-29T00:00:00Z',
  limit_kind: 'tenant_pool',
  approver_id: null,
}

beforeEach(() => {
  mockMine.mockReset()
  mockSubmit.mockReset()
  mockWallStatus.mockReset()
  mockWallStatus.mockResolvedValue({
    tenant_id: 'acme-eng',
    period: '2026-09',
    pool: null,
  })
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
    // Both figures render inside one prose sentence per the contract's own
    // quoted wording ("Approved $50.00, expires ...") rather than as bare,
    // isolated text nodes, so the figures are matched by regex (a partial
    // match against each element's full text) rather than exact string
    // equality -- the same pattern this test already uses for the expiry
    // one line below.
    await waitFor(() => expect(screen.getByText(/\$50\.00/)).toBeInTheDocument())
    // The amount she originally asked for must ALSO still be visible
    // (for contrast), but never presented as what she was granted.
    expect(screen.queryByText(/\$200\.00/)).not.toBeNull()
    expect(screen.getByText(/\$200\.00/)).not.toBe(screen.getByText(/\$50\.00/))
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

    // Gate on the row itself loading -- not on the literal word "pending",
    // which the row's own copy deliberately never uses (see the assertion
    // below): the row's `data-testid` is the loading signal instead.
    await waitFor(() =>
      expect(screen.getByTestId('lr-status-pending')).toBeInTheDocument(),
    )
    // No approved amount, no expiry, no approver IDENTIFIER on the pending
    // row. Not a bare "no text containing /approver/i anywhere": the
    // mandated pending copy itself says "your tenant's approver to
    // review" (change-pipeline/quota-raise-and-archive/design-F3.md's own
    // quoted wording, bullet 3) -- so the
    // check is for the dedicated approver-name block this component
    // renders only on a decided row, not for the word's mere presence.
    expect(screen.queryByText('$0.00')).toBeNull()
    expect(screen.queryByTestId('lr-status-approver')).toBeNull()
    expect(screen.queryByText('user-lead-1')).toBeNull()
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
        <MeLimitRaises />,
        { pathname: '/me/limit-raises?tenant_id=ambient-context-org', raiseHint: hint },
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
    render(withClient(<MeLimitRaises />, { raiseHint: hint }))
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
    render(withClient(<MeLimitRaises />, { raiseHint: hint }))
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
    render(withClient(<MeLimitRaises />, { raiseHint: hint }))

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
    render(withClient(<MeLimitRaises />, { raiseHint: hint }))
    await waitFor(() => expect(screen.getByDisplayValue('0.40')).toBeInTheDocument())
    expect(screen.queryByText(/no approver|could not (be )?grant/i)).toBeNull()
  })
})
