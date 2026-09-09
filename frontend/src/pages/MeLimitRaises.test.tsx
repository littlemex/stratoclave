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
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
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

// ---------------------------------------------------------------------------
// The task tag, the wall choice, and the idempotency token
// ---------------------------------------------------------------------------

/** Fill the two fields the submit button requires, so a test can press it. */
async function fillMinimumViableRequest() {
  // Waiting for the OPTION, not just the select: the reason codes arrive with the
  // `listMyLimitRaises` response, and a `<select>` silently ignores a value that has
  // no matching option -- which leaves the submit button disabled and the failure
  // reads as "the page never submitted" rather than "the test raced the fetch".
  await waitFor(() =>
    expect(screen.getByRole('option', { name: 'usage_spike' })).toBeInTheDocument(),
  )
  fireEvent.change(screen.getByTestId('lr-reason-select'), {
    target: { value: 'usage_spike' },
  })
  fireEvent.change(screen.getByTestId('lr-amount-input'), { target: { value: '50' } })
}

const WITH_REASONS = { requests: [], reason_codes: ['usage_spike'] }

describe('MeLimitRaises — the idempotency token is per press, not per mount', () => {
  it('two submissions from ONE mount carry DIFFERENT client tokens', async () => {
    // The defect: `submit_limit_raise` treats a repeated `client_token` as a replay
    // and returns the request the FIRST call produced. With one token held for the
    // component's lifetime, changing the amount and pressing again returned the
    // earlier request while this page cleared the form and reported success -- the
    // requester saw a submission of an amount she had not asked for, silently.
    //
    // Asserted on the TOKENS rather than on the rendered outcome on purpose: the
    // rendered outcome of the bug is indistinguishable from success, which is
    // exactly why nothing caught it.
    mockMine.mockResolvedValue(WITH_REASONS)
    mockSubmit.mockResolvedValue({ ...PENDING_ROW })
    render(withClient(<MeLimitRaises />))

    await fillMinimumViableRequest()
    fireEvent.click(screen.getByTestId('lr-submit-button'))
    await waitFor(() => expect(mockSubmit).toHaveBeenCalledTimes(1))

    fireEvent.change(screen.getByTestId('lr-amount-input'), { target: { value: '500' } })
    fireEvent.click(screen.getByTestId('lr-submit-button'))
    await waitFor(() => expect(mockSubmit).toHaveBeenCalledTimes(2))

    const first = mockSubmit.mock.calls[0][0].client_token
    const second = mockSubmit.mock.calls[1][0].client_token
    expect(typeof first).toBe('string')
    expect(first).not.toBe(second)
    // And the second call really did carry the new amount, so the two presses are
    // two distinct asks rather than one repeated.
    expect(mockSubmit.mock.calls[1][0].asked_amount_microusd).toBe(500_000_000)
  })
})

describe('MeLimitRaises — the wall the requester is filing against', () => {
  it('sends the selected wall as limit_kind, using the registry key verbatim', async () => {
    mockMine.mockResolvedValue(WITH_REASONS)
    mockSubmit.mockResolvedValue({ ...PENDING_ROW })
    render(withClient(<MeLimitRaises />))

    await fillMinimumViableRequest()
    fireEvent.change(screen.getByTestId('lr-wall-select'), {
      target: { value: 'user_dollar_quota' },
    })
    fireEvent.click(screen.getByTestId('lr-submit-button'))

    await waitFor(() => expect(mockSubmit).toHaveBeenCalledTimes(1))
    // The wire value must be the backend's `RESERVE_LIMITS` key: `submit_limit_raise`
    // validates against that registry and refuses anything else, so a
    // display-friendly spelling would be rejected server-side.
    expect(mockSubmit.mock.calls[0][0].limit_kind).toBe('user_dollar_quota')
  })

  it('says that a filing consumes that wall\'s allowance for the day', async () => {
    // The cost of choosing the wrong wall is a whole day, and the backend only says
    // so after the slot is gone.
    mockMine.mockResolvedValue(WITH_REASONS)
    render(withClient(<MeLimitRaises />))
    await waitFor(() =>
      expect(screen.getByTestId('lr-wall-slot-note')).toBeInTheDocument(),
    )
  })

  it('distinguishes a tenant with no personal ceiling from a backend that cannot say', async () => {
    mockMine.mockResolvedValue(WITH_REASONS)
    // `null` -- the wall does not apply to this tenant.
    mockWallStatus.mockResolvedValue({
      tenant_id: 'acme-eng', period: '2026-09', pool: null, user_dollar: null,
    })
    const { unmount } = render(withClient(<MeLimitRaises />))
    await waitFor(() =>
      expect(screen.getByTestId('user-dollar-absent')).toBeInTheDocument(),
    )
    expect(screen.queryByTestId('user-dollar-unknown')).toBeNull()
    unmount()

    // Field absent entirely -- an older backend. Reporting "not configured" here
    // would invent a fact from a missing key, and the requester would conclude the
    // wall is off when nobody said so.
    mockWallStatus.mockResolvedValue({
      tenant_id: 'acme-eng', period: '2026-09', pool: null,
    })
    render(withClient(<MeLimitRaises />))
    await waitFor(() =>
      expect(screen.getByTestId('user-dollar-unknown')).toBeInTheDocument(),
    )
    expect(screen.queryByTestId('user-dollar-absent')).toBeNull()
  })

  it('shows the personal ceiling and what is left of it', async () => {
    mockMine.mockResolvedValue(WITH_REASONS)
    mockWallStatus.mockResolvedValue({
      tenant_id: 'acme-eng',
      period: '2026-09',
      pool: null,
      user_dollar: {
        base_microusd: 10_000_000,
        granted_microusd: 40_000_000,
        ceiling_microusd: 50_000_000,
        used_microusd: 12_000_000,
        remaining_microusd: 38_000_000,
        base_is_sealed: true,
      },
    })
    render(withClient(<MeLimitRaises />))
    // What is left to HER, which is the figure that decides whether asking makes
    // sense -- and the ceiling it is left out of.
    await waitFor(() => expect(screen.getByText(/\$38\.00/)).toBeInTheDocument())
    expect(screen.getByText(/\$50\.00/)).toBeInTheDocument()
    // A ceiling that is mostly granted room reads very differently from one that is
    // mostly base, so the split is stated rather than folded into one number.
    expect(screen.getByTestId('user-dollar-granted')).toBeInTheDocument()
  })
})

describe('MeLimitRaises — the task tag', () => {
  it('sends the tag VERBATIM, with its case intact', async () => {
    mockMine.mockResolvedValue(WITH_REASONS)
    mockSubmit.mockResolvedValue({ ...PENDING_ROW })
    render(withClient(<MeLimitRaises />))

    await fillMinimumViableRequest()
    fireEvent.change(screen.getByTestId('lr-task-tag-input'), {
      target: { value: 'Migration-42' },
    })
    fireEvent.click(screen.getByTestId('lr-submit-button'))

    await waitFor(() => expect(mockSubmit).toHaveBeenCalledTimes(1))
    // NOT `migration-42`. The gateway canonicalises; a client that pre-lowercased
    // would be a second canonicaliser, and would show the requester a different
    // string from the one it filed.
    expect(mockSubmit.mock.calls[0][0].task_tag).toBe('Migration-42')
  })

  it('omits the tag entirely when it is blank, rather than sending an empty string', async () => {
    mockMine.mockResolvedValue(WITH_REASONS)
    mockSubmit.mockResolvedValue({ ...PENDING_ROW })
    render(withClient(<MeLimitRaises />))
    await fillMinimumViableRequest()
    fireEvent.click(screen.getByTestId('lr-submit-button'))
    await waitFor(() => expect(mockSubmit).toHaveBeenCalledTimes(1))
    // A raise with no tag is accepted: the tag is attribution, not authorisation.
    expect(mockSubmit.mock.calls[0][0].task_tag).toBeUndefined()
  })

  it('refuses to submit a tag outside the grammar, and says which characters are allowed', async () => {
    mockMine.mockResolvedValue(WITH_REASONS)
    mockSubmit.mockResolvedValue({ ...PENDING_ROW })
    render(withClient(<MeLimitRaises />))
    await fillMinimumViableRequest()
    fireEvent.change(screen.getByTestId('lr-task-tag-input'), {
      target: { value: 'has space' },
    })
    await waitFor(() =>
      expect(screen.getByTestId('lr-task-tag-invalid')).toBeInTheDocument(),
    )
    expect(screen.getByTestId('lr-submit-button')).toBeDisabled()
    fireEvent.click(screen.getByTestId('lr-submit-button'))
    expect(mockSubmit).not.toHaveBeenCalled()
  })

  it('accepts the reserved word, because that meaning belongs to the gateway', async () => {
    // A UI that rejected UNLABELLED would hold a second copy of the reserved list,
    // and would refuse a tag the gateway accepts-and-reports-on.
    mockMine.mockResolvedValue(WITH_REASONS)
    mockSubmit.mockResolvedValue({ ...PENDING_ROW })
    render(withClient(<MeLimitRaises />))
    await fillMinimumViableRequest()
    fireEvent.change(screen.getByTestId('lr-task-tag-input'), {
      target: { value: 'UNLABELLED' },
    })
    fireEvent.click(screen.getByTestId('lr-submit-button'))
    await waitFor(() => expect(mockSubmit).toHaveBeenCalledTimes(1))
    expect(mockSubmit.mock.calls[0][0].task_tag).toBe('UNLABELLED')
  })

  it('shows the stored tag on her own request, and says so when there is none', async () => {
    mockMine.mockResolvedValue({
      requests: [
        { ...PENDING_ROW, request_id: 'lr_tagged', task_tag: 'migration-42' },
        { ...PENDING_ROW, request_id: 'lr_bare' },
      ],
      reason_codes: [],
    })
    render(withClient(<MeLimitRaises />))
    await waitFor(() => expect(screen.getByText('migration-42')).toBeInTheDocument())
    // Not blank: a requester holding both needs to see which is which.
    expect(screen.getAllByRole('row').length).toBeGreaterThan(2)
  })

  it('names the wall on each of her requests', async () => {
    mockMine.mockResolvedValue({
      requests: [
        { ...PENDING_ROW, request_id: 'lr_pool', limit_kind: 'tenant_dollar_pool' },
        { ...PENDING_ROW, request_id: 'lr_user', limit_kind: 'user_dollar_quota' },
        // A wall this build has never heard of renders its key rather than nothing:
        // erasing the one field that tells two rows apart is worse than showing a
        // machine name.
        { ...PENDING_ROW, request_id: 'lr_future', limit_kind: 'some_future_wall' },
      ],
      reason_codes: [],
    })
    render(withClient(<MeLimitRaises />))
    await waitFor(() => expect(screen.getByText('some_future_wall')).toBeInTheDocument())
  })
})

describe('MeLimitRaises — the day is already spent', () => {
  it('names the request already on file instead of showing an error', async () => {
    // Somebody unsure whether their submission landed needs to be told that it did.
    // A red line saying the submission failed is the opposite of the truth.
    mockMine.mockResolvedValue(WITH_REASONS)
    mockSubmit.mockRejectedValue(
      Object.assign(new Error('You have already filed a limit raise today'), {
        status: 409,
        detailBody: {
          type: 'limit_raise_daily_slot_occupied',
          message: 'You have already filed a limit raise today',
          holder_request_id: 'lr_earlier',
          holder_status: 'PENDING',
          reset_at: '2026-09-10T00:00:00+00:00',
        },
      }),
    )
    render(withClient(<MeLimitRaises />))
    await fillMinimumViableRequest()
    fireEvent.click(screen.getByTestId('lr-submit-button'))

    await waitFor(() =>
      expect(screen.getByTestId('lr-slot-occupied')).toBeInTheDocument(),
    )
    // The id is what lets her go and look at it.
    expect(screen.getByText(/lr_earlier/)).toBeInTheDocument()
    expect(screen.getByText(/2026-09-10/)).toBeInTheDocument()
  })
})
