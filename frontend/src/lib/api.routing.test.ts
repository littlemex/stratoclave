// P0-11 routing-config api.ts wiring tests: assert the client methods hit the
// correct URL + method + body. This is the UI-side contract with the backend
// admin API; the component (AdminTenantDetail RoutingConfigCard) is exercised
// live via CloudFront E2E.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { api } from './api'

const okJson = (body: unknown) =>
  Promise.resolve({
    ok: true,
    status: 200,
    headers: new Headers({ 'content-type': 'application/json' }),
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(JSON.stringify(body)),
  } as Response)

let fetchMock: ReturnType<typeof vi.fn>

beforeEach(() => {
  fetchMock = vi.fn(() => okJson({ ok: true }))
  vi.stubGlobal('fetch', fetchMock)
  // authFetch reads a token from storage; stub a bearer so requests build.
  vi.stubGlobal('localStorage', {
    getItem: () => 'test-token',
    setItem: () => {},
    removeItem: () => {},
  } as unknown as Storage)
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

function calledUrl(): string {
  const arg = fetchMock.mock.calls[0][0]
  return typeof arg === 'string' ? arg : (arg as Request).url
}
function calledInit(): RequestInit {
  return (fetchMock.mock.calls[0][1] ?? {}) as RequestInit
}

describe('routing-config api wiring', () => {
  it('getRoutingConfig GETs the tenant routing-config path', async () => {
    await api.admin.getRoutingConfig('acme eng')
    expect(calledUrl()).toContain('/api/mvp/admin/tenants/acme%20eng/routing-config')
    // GET (no method or method GET)
    const m = calledInit().method
    expect(m === undefined || m === 'GET').toBe(true)
  })

  it('setRoutingConfig PUTs the JSON body', async () => {
    const body = { chain: ['claude-sonnet-4-6'], fallback_default: 'on' as const }
    await api.admin.setRoutingConfig('t1', body)
    expect(calledUrl()).toContain('/api/mvp/admin/tenants/t1/routing-config')
    expect(calledInit().method).toBe('PUT')
    expect(JSON.parse(calledInit().body as string)).toEqual(body)
  })

  it('getUserRoutingConfig encodes tenant + user in the path', async () => {
    await api.admin.getUserRoutingConfig('t1', 'u/2')
    expect(calledUrl()).toContain('/api/mvp/admin/tenants/t1/users/u%2F2/routing-config')
  })

  it('setUserRoutingConfig PUTs the user override body', async () => {
    await api.admin.setUserRoutingConfig('t1', 'u1', { fallback: 'off' })
    expect(calledUrl()).toContain('/api/mvp/admin/tenants/t1/users/u1/routing-config')
    expect(calledInit().method).toBe('PUT')
    expect(JSON.parse(calledInit().body as string)).toEqual({ fallback: 'off' })
  })
})
