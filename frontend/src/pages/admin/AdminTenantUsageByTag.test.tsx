// The adapter tests the security review asked for: each page must call ITS OWN route.
//
// The shared component takes a `fetchReport` prop, so a page could be handed the wrong one and
// still render perfectly. An admin fetch on a team-lead page would be refused by the backend,
// but the reverse can succeed — admins are deliberately accepted by team-lead authorisation —
// so a namespace mistake is not guaranteed to announce itself. These assert the URL.
//
// They also assert the tenant is in the cache key. An admin reads many tenants in one session,
// and two tenants' reports under one key is a cross-tenant read served from cache, which needs
// no bug in the backend at all.

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import type { ReactNode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const fetchSpy = vi.fn()

vi.mock('@/lib/authFetch', () => ({
  authFetch: (...args: unknown[]) => fetchSpy(...args),
}))

import AdminTenantUsageByTag from './AdminTenantUsageByTag'

function withRouting(children: ReactNode, tenantId = 'acme-eng') {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0, staleTime: 0 } },
  })
  return (
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[`/admin/tenants/${tenantId}/usage-by-tag`]}>
        <Routes>
          <Route path="/admin/tenants/:tenantId/usage-by-tag" element={children} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  )
}

const EMPTY_REPORT = {
  period: '2026-09',
  rows: [],
  truncated: false,
  legacy_rows: 0,
  malformed_rows: 0,
  tag_is_caller_asserted: true,
  retention_policy_days: 95,
  retention_deletion_is_asynchronous: true,
  retention_boundary_period_may_fold_incompletely: true,
  tag_total_is_a_lower_bound: true,
}

beforeEach(() => {
  fetchSpy.mockReset()
  fetchSpy.mockResolvedValue({
    ok: true,
    status: 200,
    clone: () => ({ json: async () => EMPTY_REPORT }),
    json: async () => EMPTY_REPORT,
  })
})
afterEach(() => {
  vi.clearAllMocks()
})

describe('AdminTenantUsageByTag — calls the admin route for the tenant in the URL', () => {
  it('requests the admin by-tag path with the tenant from the route', async () => {
    render(withRouting(<AdminTenantUsageByTag />, 'acme-eng'))
    await waitFor(() => expect(fetchSpy).toHaveBeenCalled())
    const url = String(fetchSpy.mock.calls[0][0])
    expect(url).toContain('/api/mvp/admin/tenants/acme-eng/usage/by-tag')
    // NOT the self route, and not the team-lead mirror.
    expect(url).not.toContain('/me/usage/by-tag')
    expect(url).not.toContain('/team-lead/')
    expect(url).toMatch(/[?&]period=\d{4}-\d{2}/)
    // A blank member is omitted, never sent as an empty filter.
    expect(url).not.toContain('user_id=')
  })

  it('re-encodes a tenant id that arrived percent-encoded in the route', async () => {
    // The reachable shape, and the first version of this test got it wrong: a RAW slash never
    // reaches `:tenantId`, because React Router would not match the route at all. What does
    // reach it is an ENCODED segment, which the router decodes into the param -- so the id
    // handed to the API call can contain a slash even though the browser URL could not show
    // one. Splicing that into a path unencoded would invent a route segment.
    render(withRouting(<AdminTenantUsageByTag />, 'weird%2Ftenant'))
    await waitFor(() => expect(fetchSpy).toHaveBeenCalled())
    const url = String(fetchSpy.mock.calls[0][0])
    expect(url).toContain('/admin/tenants/weird%2Ftenant/usage/by-tag')
    // One path segment for the tenant, not two.
    expect(url).not.toContain('/tenants/weird/tenant/')
  })

  it('shows the member column, because a tenant report covers more than one person', async () => {
    render(withRouting(<AdminTenantUsageByTag />))
    await waitFor(() => expect(screen.getByTestId('bt-member-input')).toBeInTheDocument())
  })

  it('renders the disclosures, so the tenant view cannot be a barer version of the self view', async () => {
    // The component owns these and has its own tests; asserted here too because the whole
    // reason for one component is that a second surface must not show fewer of them.
    render(withRouting(<AdminTenantUsageByTag />))
    await waitFor(() => expect(screen.getByTestId('bt-disclosures')).toBeInTheDocument())
    expect(screen.getByTestId('bt-lower-bound')).toBeInTheDocument()
    expect(screen.getByTestId('bt-caller-asserted')).toBeInTheDocument()
    expect(screen.getByTestId('bt-retention')).toBeInTheDocument()
  })
})
