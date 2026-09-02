// LimitRaiseApproval — the tenant approval view (F3 / R12 approval half, R30,
// R28, R21b).
//
// Contract (as corrected): change-pipeline/quota-raise-and-archive/CONTRACT-F3-surfaces.md
//   R12: "Console — the tenant approval view (`limit-raises:approve` on that
//   tenant): the ask, the reason, the comment, the requester, the ceiling's
//   composition ..., the tenant's current reserved and settled, the
//   remaining grant cap, and the latest permissible expiry. Approve with an
//   amount and an expiry, or reject with a reason." Plus: "the comment is
//   rendered as text, never as HTML."
//   R30: "observed_limit/observed_remaining were taken when the request was
//   filed, possibly hours earlier. Unit: current reserved, settled and
//   headroom appear alongside, each labelled."
//   R28: "the latest permissible expiry is shown before it is typed."
//   R21b: "The console's tenant view carries F1's mode sentence, the seat
//   entitlement and the resume action."
//
// This component does not exist in this worktree — every test fails at
// module resolution. Test bodies assert RENDERED TEXT, per this role's
// brief, not merely that the API payload carries a field.

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import type { ReactNode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/lib/api', () => ({
  api: {
    limitRaises: {
      approvalDetail: (...args: unknown[]) => (globalThis as any).__lrDetail(...args),
      approve: (...args: unknown[]) => (globalThis as any).__lrApprove(...args),
      reject: (...args: unknown[]) => (globalThis as any).__lrReject(...args),
    },
    // The R21b "resume action" is NOT a limit-raises endpoint at all — per
    // contract correction it is the REAL, already-existing pool-budget PUT
    // (`api.adminTenants.setPoolBudget`, backend/mvp/admin_tenants.py's PUT
    // .../pool-budget) called with `{ follow_seats: true }`, which clears
    // `manual_limit`. F1 deletes the `sizing` attribute entirely, so a
    // resume built on `sizing` would ship against a mechanism that no
    // longer exists.
    adminTenants: {
      setPoolBudget: (...args: unknown[]) => (globalThis as any).__setPoolBudget(...args),
    },
  },
}))

const mockDetail = vi.fn()
const mockApprove = vi.fn()
const mockReject = vi.fn()
const mockSetPoolBudget = vi.fn()
;(globalThis as any).__lrDetail = (...a: unknown[]) => mockDetail(...a)
;(globalThis as any).__lrApprove = (...a: unknown[]) => mockApprove(...a)
;(globalThis as any).__lrReject = (...a: unknown[]) => mockReject(...a)
;(globalThis as any).__setPoolBudget = (...a: unknown[]) => mockSetPoolBudget(...a)

import LimitRaiseApproval from './LimitRaiseApproval'

function withRouting(children: ReactNode) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0, staleTime: 0 } },
  })
  return (
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={['/team-lead/tenants/acme-eng/limit-raises/lr_9f2c']}>
        <Routes>
          <Route
            path="/team-lead/tenants/:tenantId/limit-raises/:requestId"
            element={children}
          />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  )
}

// A minimal but complete detail payload, per this role's design note's
// R12/R30/R28/R21b response shapes.
const DETAIL_FIXTURE = {
  request: {
    request_id: 'lr_9f2c',
    tenant_id: 'acme-eng',
    reason: 'cascade_shortfall',
    comment: '<b>please</b> approve & hurry',
    requester_email: 'requester@acme.example',
    requested_amount_microusd: 200_000_000,
    observed_limit_microusd: 40_000_000,
    observed_remaining_microusd: 2_000_000,
    observed_at: '2026-08-28T14:00:00Z',
  },
  current: {
    pool_reserved_microusd: 500_000,
    pool_settled_microusd: 39_800_000,
    remaining_microusd: -300_000, // NEGATIVE — a real deficit
    as_of: '2026-08-30T09:00:00Z',
  },
  ceiling: {
    seat_entitlement: { seats: 12, seat_monthly_usd: 50, contributes_microusd: 600_000_000 },
    manual_limit_microusd: 40_000_000,
    pool_granted_microusd: 62_000_000,
    baseline_microusd: 600_000_000,
    mode: 'fixed',
    mode_sentence:
      'This budget was set manually; membership changes do not change this budget.',
    // Contract correction: no `sizing` attribute (F1 deletes it). Resumable
    // means "manual_limit is set"; there is no separate resume_action
    // object — the button's target is the real, already-existing
    // PUT .../pool-budget endpoint, called with `{ follow_seats: true }`.
    resumable: true,
  },
  remaining_grant_cap_microusd: 8_000_000,
  latest_permissible_expiry: '2026-08-31T23:59:59Z',
}

beforeEach(() => {
  mockDetail.mockReset()
  mockApprove.mockReset()
  mockReject.mockReset()
  mockSetPoolBudget.mockReset()
  mockDetail.mockResolvedValue(DETAIL_FIXTURE)
  mockSetPoolBudget.mockResolvedValue({
    ...DETAIL_FIXTURE.ceiling,
    manual_limit_microusd: null,
  })
})
afterEach(() => {
  vi.clearAllMocks()
})

describe('LimitRaiseApproval — R12: comment renders as text, never HTML', () => {
  it('does not interpret the comment as markup (no <b> tag rendered, literal text visible)', async () => {
    render(withRouting(<LimitRaiseApproval />))
    await waitFor(() => expect(screen.getByTestId('lr-comment')).toBeInTheDocument())
    const node = screen.getByTestId('lr-comment')
    // The literal source text must be present verbatim...
    expect(node.textContent).toContain('<b>please</b> approve & hurry')
    // ...and must NOT have been parsed into a <b> element or lost the '&'
    // to double-escaping (the sharpest check per this role's brief).
    expect(node.querySelector('b')).toBeNull()
    expect(node.innerHTML).not.toContain('<b>')
  })
})

describe('LimitRaiseApproval — R30: current position is labelled apart from the snapshot', () => {
  it('shows both the AT-REQUEST snapshot and the CURRENT reserved/settled, each labelled, with distinct timestamps', async () => {
    render(withRouting(<LimitRaiseApproval />))
    await waitFor(() => expect(screen.getByTestId('lr-current-block')).toBeInTheDocument())

    const snapshotBlock = screen.getByTestId('lr-snapshot-block')
    const currentBlock = screen.getByTestId('lr-current-block')
    expect(snapshotBlock.textContent).toMatch(/2026-08-28/)
    expect(currentBlock.textContent).toMatch(/2026-08-30/)
    // The two blocks must be visually/structurally distinct, not one merged
    // number — this asserts they are different DOM nodes with different text.
    expect(snapshotBlock).not.toBe(currentBlock)
  })

  it('renders the deficit SIGNED, never clamped to $0.00 (remaining_microusd = -300_000)', async () => {
    render(withRouting(<LimitRaiseApproval />))
    await waitFor(() => expect(screen.getByTestId('lr-current-block')).toBeInTheDocument())
    const currentBlock = screen.getByTestId('lr-current-block')
    expect(currentBlock.textContent).toMatch(/-\$0\.30|-0\.30|\(0\.30\)/)
    expect(currentBlock.textContent).not.toMatch(/\$0\.00 remaining/i)
  })
})

// Seam amendment B1 (the integration owner's seam notes, §S10, outside this repository): the suspended-pool refusal is F2's
// server-side lifecycle rule now, not F3's to test. The test that used to
// live here ("surfaces a suspended-pool refusal ... as a legible banner")
// asserted the specific wording of a refusal F2 now owns end to end; it is
// DELETED, not retargeted, because pinning that wording here would be
// exactly the "two independent statements of one shape drift" the amendment
// exists to stop. What remains is R28's one F3-owned fact: display of the
// latest permissible expiry, before it is typed.
describe('LimitRaiseApproval — R28: latest permissible expiry shown before typing', () => {
  it('shows the latest permissible expiry BEFORE any value is typed into the expiry field', async () => {
    render(withRouting(<LimitRaiseApproval />))
    await waitFor(() =>
      expect(screen.getByTestId('lr-latest-permissible-expiry')).toBeInTheDocument(),
    )
    expect(screen.getByTestId('lr-latest-permissible-expiry').textContent).toMatch(
      /2026-08-31|Aug 31/,
    )
    // It must be visible text, not merely the <input max="..."> attribute —
    // "shown before it is typed" (R28's own phrasing) requires prose, per
    // this role's brief ("assert what a person sees").
    const expiryInput = screen.getByLabelText(/expiry/i)
    expect(expiryInput).toHaveAttribute('max')
  })
})

describe('LimitRaiseApproval — R21b: mode sentence, seat entitlement, resume action', () => {
  it('renders the mode sentence verbatim, not a paraphrase of `mode`', async () => {
    render(withRouting(<LimitRaiseApproval />))
    await waitFor(() =>
      expect(
        screen.getByText(
          'This budget was set manually; membership changes do not change this budget.',
        ),
      ).toBeInTheDocument(),
    )
  })

  it('renders the seat entitlement (seat count), not just the derived microusd figure', async () => {
    render(withRouting(<LimitRaiseApproval />))
    await waitFor(() => expect(screen.getByText(/12 seats?/i)).toBeInTheDocument())
  })

  it('shows a resume action when the ceiling is resumable', async () => {
    render(withRouting(<LimitRaiseApproval />))
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /resume/i })).toBeInTheDocument(),
    )
  })

  // Contract correction: the resume action is NOT a `sizing` toggle (F1
  // deletes that attribute entirely) — it is `PUT .../pool-budget` with
  // `{"follow_seats": true}`, clearing `manual_limit`. Redirected from the
  // earlier (reasonable, but now-wrong) `sizing`-based reading.
  it('clicking resume calls the REAL pool-budget PUT with follow_seats: true, not a sizing toggle', async () => {
    const user = userEvent.setup()
    render(withRouting(<LimitRaiseApproval />))
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /resume/i })).toBeInTheDocument(),
    )
    await user.click(screen.getByRole('button', { name: /resume/i }))

    await waitFor(() => expect(mockSetPoolBudget).toHaveBeenCalled())
    const [tenantIdArg, bodyArg] = mockSetPoolBudget.mock.calls[0]
    expect(tenantIdArg).toBe('acme-eng')
    expect(bodyArg).toEqual({ follow_seats: true })
    // No surface may still reference `sizing` — F1 deletes the attribute,
    // so a component built on it would ship against a mechanism that no
    // longer exists.
    expect(bodyArg).not.toHaveProperty('sizing')
  })
})
