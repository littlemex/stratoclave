// MeBilling page rendering tests — PENDING protocol response coverage.
//
// The PENDING protocol (docs/design/pending-protocol.md) lets the backend's
// authorize/capture/void/get endpoints answer with new HTTP outcomes: 402
// credit_exhausted, 410 (expired hold), 503 budget_unavailable, 200
// replayed=true, and 409 (capture-vs-void race). This page only ever calls the
// READ-ONLY `api.runBilling` / `api.getAuthorization` GETs (authorize/capture/
// void are deliberately NOT exposed in the UI — see the component's own
// comment), so the only new-response surface it can hit is `GET
// /authorizations/{id}` returning 404/410/503 (or any other non-2xx), plus a
// success payload whose `status` is one of the four values the PENDING
// protocol's `get_authorization` can now report (authorized/captured/voided/
// expired — see backend/mvp/billing_authorize.py::get_authorization).
//
// Today `MeBilling.tsx` does not branch on the HTTP status at all: `q.isError`
// / `authQ.isError` render the SAME fixed copy regardless of whether the
// failure was a 404, 410, or 503. These tests pin that behavior down (no
// crash, one consistent message per section) and separately verify the
// success path renders every valid `status` string without throwing.

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { ReactNode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { AuthorizationStatus, RunBreakdownTenant } from '@/lib/api'

// ---- Module mocks (hoisted) ----
vi.mock('@/lib/api', () => ({
  api: {
    runBilling: (...args: unknown[]) => (globalThis as any).__runBilling(...args),
    getAuthorization: (...args: unknown[]) =>
      (globalThis as any).__getAuthorization(...args),
  },
}))

const mockRunBilling = vi.fn<(runId: string) => Promise<RunBreakdownTenant>>()
const mockGetAuthorization =
  vi.fn<(authorizationId: string) => Promise<AuthorizationStatus>>()
;(globalThis as any).__runBilling = (...a: unknown[]) =>
  mockRunBilling(a[0] as string)
;(globalThis as any).__getAuthorization = (...a: unknown[]) =>
  mockGetAuthorization(a[0] as string)

// Imported after the mocks so React sees the stubbed module.
import MeBilling from './MeBilling'

function withClient(children: ReactNode) {
  const client = new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: 0, staleTime: 0 },
    },
  })
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>
}

// Mirrors the shape `jsonRequest` (src/lib/api.ts) throws on a non-2xx
// response: an Error carrying `.status` (+ optional `.detail`).
function httpError(status: number, detail?: string): Error & { status: number; detail?: string } {
  const err = new Error(detail ?? `${status} error`) as Error & {
    status: number
    detail?: string
  }
  err.status = status
  err.detail = detail
  return err
}

const RUN_FIXTURE: RunBreakdownTenant = {
  tenant_id: 'acme-billing',
  run_id: 'run-1',
  total_settled_microusd: 1_500_000,
  events: [
    {
      event_type: 'SETTLE',
      settled_microusd: 1_500_000,
      components: {},
      ts_ms: 1_700_000_000_000,
    },
  ],
}

beforeEach(() => {
  mockRunBilling.mockReset()
  mockGetAuthorization.mockReset()
})
afterEach(() => {
  vi.clearAllMocks()
})

describe('MeBilling — run lookup error handling', () => {
  it('renders the billing-error message on a 404 (run not found)', async () => {
    mockRunBilling.mockRejectedValue(httpError(404, 'run not found'))
    const user = userEvent.setup()
    render(withClient(<MeBilling />))

    await user.type(screen.getByLabelText('workflow run id'), 'run-x')
    await user.click(screen.getAllByRole('button', { name: 'Look up' })[0])

    await waitFor(() =>
      expect(screen.getByTestId('billing-error')).toBeInTheDocument(),
    )
    expect(screen.queryByTestId('total-charged')).toBeNull()
  })

  it('renders the SAME billing-error message on a 503 (budget_unavailable-adjacent failure)', async () => {
    // The run-billing read path itself has no PENDING-protocol branch, but a
    // transient 503 from the shared ledger infra must still degrade to the
    // same non-crashing error state as a 404 — the UI has no status-specific
    // copy today, so this pins that lack of differentiation rather than
    // assuming a message the component does not produce.
    mockRunBilling.mockRejectedValue(
      httpError(503, 'Budget reservation is temporarily unavailable. Retry shortly.'),
    )
    const user = userEvent.setup()
    render(withClient(<MeBilling />))

    await user.type(screen.getByLabelText('workflow run id'), 'run-y')
    await user.click(screen.getAllByRole('button', { name: 'Look up' })[0])

    await waitFor(() =>
      expect(screen.getByTestId('billing-error')).toBeInTheDocument(),
    )
    expect(screen.getByTestId('billing-error').textContent).toBe('Run not found.')
  })

  it('renders the total once the run resolves successfully', async () => {
    mockRunBilling.mockResolvedValue(RUN_FIXTURE)
    const user = userEvent.setup()
    render(withClient(<MeBilling />))

    await user.type(screen.getByLabelText('workflow run id'), 'run-1')
    await user.click(screen.getAllByRole('button', { name: 'Look up' })[0])

    await waitFor(() =>
      expect(screen.getByTestId('total-charged')).toHaveTextContent('$1.50'),
    )
  })
})

describe('MeBilling — authorization lookup error handling (authcap / PENDING protocol)', () => {
  it.each([
    [404, 'authorization not found'],
    [410, 'authorization expired'],
    [503, 'Temporarily unavailable; retry shortly.'],
  ])(
    'renders the authorization-error message without crashing on a %i response',
    async (status, detail) => {
      mockGetAuthorization.mockRejectedValue(httpError(status, detail))
      const user = userEvent.setup()
      render(withClient(<MeBilling />))

      await user.type(screen.getByLabelText('authorization id'), 'auth_x')
      await user.click(screen.getAllByRole('button', { name: 'Look up' })[1])

      await waitFor(() =>
        expect(screen.getByTestId('authorization-error')).toBeInTheDocument(),
      )
      // No status-specific branch exists in MeBilling.tsx today: every error
      // (404/410/503/anything else) renders this exact fixed copy. Pin that,
      // rather than assert a per-status message the component does not emit.
      expect(screen.getByTestId('authorization-error').textContent).toBe(
        'Authorization not found.',
      )
    },
  )

  // `get_authorization` (backend/mvp/billing_authorize.py) can report any of
  // these four `status` strings on a 200. None is special-cased by MeBilling
  // (it renders whatever string comes back), so each must render without
  // throwing and show the literal status text.
  it.each<[AuthorizationStatus['status'], Partial<AuthorizationStatus>]>([
    ['authorized', {}],
    ['captured', { terminal: 'SETTLE', captured_microusd: 700_000 }],
    ['voided', { terminal: 'RELEASE' }],
    ['expired', { terminal: 'RECLAIM' }],
  ])('renders status=%s from a successful lookup', async (status, extra) => {
    mockGetAuthorization.mockResolvedValue({
      authorization_id: 'auth_abc',
      tenant_id: 'acme-billing',
      amount_microusd: 1_000_000,
      status,
      ...extra,
    })
    const user = userEvent.setup()
    render(withClient(<MeBilling />))

    await user.type(screen.getByLabelText('authorization id'), 'auth_abc')
    await user.click(screen.getAllByRole('button', { name: 'Look up' })[1])

    await waitFor(() =>
      expect(screen.getByTestId('authorization-status')).toHaveTextContent(status),
    )
    if (extra.terminal) {
      expect(screen.getByTestId('authorization-status')).toHaveTextContent(
        extra.terminal,
      )
    }
    if (extra.captured_microusd != null) {
      await waitFor(() =>
        expect(screen.getByTestId('authorization-captured')).toBeInTheDocument(),
      )
    }
  })

  it('renders replayed authorization status (idempotent authorize replay) without a captured row', async () => {
    // A replayed authorize (authorize_response.replayed=true) never reaches
    // this GET path directly, but its resulting hold is looked up the same
    // way as a fresh one — status stays "authorized" and there is no
    // captured_microusd yet. This documents that a replay is indistinguishable
    // from a fresh authorize once read back through GET /authorizations/{id}
    // (the backend's AuthorizationStatus shape carries no `replayed` field).
    mockGetAuthorization.mockResolvedValue({
      authorization_id: 'auth_replay',
      tenant_id: 'acme-billing',
      amount_microusd: 500_000,
      status: 'authorized',
    })
    const user = userEvent.setup()
    render(withClient(<MeBilling />))

    await user.type(screen.getByLabelText('authorization id'), 'auth_replay')
    await user.click(screen.getAllByRole('button', { name: 'Look up' })[1])

    await waitFor(() =>
      expect(screen.getByTestId('authorization-status')).toHaveTextContent(
        'authorized',
      ),
    )
    expect(screen.queryByTestId('authorization-captured')).toBeNull()
  })
})
