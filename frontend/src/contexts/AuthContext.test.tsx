// AuthContext bootstrap integration tests.
//
// Covers the three entry paths documented in AuthContext.tsx:
//   1. CLI-injected URL token (`?token=eyJ...`)
//   2. Existing, in-date localStorage tokens
//   3. Expired tokens with a refresh token available
//
// We mock `@/lib/api` and `@/lib/cognito` at the module boundary so the
// tests focus on the reducer flow and do not actually call Cognito or
// the backend. Those concerns are already covered by the unit tests in
// `cognito.test.ts` and by backend integration tests.

import { render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { MeResponse } from '@/lib/api'
import type { StoredTokens } from '@/types/auth'

// ---- Mocks -----------------------------------------------------------
// vi.mock is hoisted to the top of the file, so we cannot reference a
// top-level variable from inside the factory. We keep the mock functions
// on `globalThis` instead and address them through a typed helper.
vi.mock('@/lib/api', () => ({
  api: { me: (...args: unknown[]) => (globalThis as any).__mockMe(...args) },
}))

vi.mock('@/lib/cognito', () => ({
  startLogin: vi.fn(),
  handleCallback: vi.fn(),
  refreshTokens: (...args: unknown[]) => (globalThis as any).__mockRefresh(...args),
  logoutRedirect: vi.fn(),
  saveTokens: (t: StoredTokens) =>
    window.sessionStorage.setItem('stratoclave_tokens', JSON.stringify(t)),
  getStoredTokens: () => {
    const raw = window.sessionStorage.getItem('stratoclave_tokens')
    return raw ? (JSON.parse(raw) as StoredTokens) : null
  },
  clearTokens: () => window.sessionStorage.removeItem('stratoclave_tokens'),
  getAccessToken: () => {
    const raw = window.sessionStorage.getItem('stratoclave_tokens')
    return raw ? (JSON.parse(raw) as StoredTokens).access_token : null
  },
}))

// Typed handles to the stubs installed on globalThis above.
const mockMe = vi.fn<[], Promise<MeResponse>>()
const mockRefresh = vi.fn()
;(globalThis as any).__mockMe = (...a: unknown[]) => mockMe(...(a as []))
;(globalThis as any).__mockRefresh = (...a: unknown[]) => mockRefresh(...(a as []))

// Must be imported AFTER vi.mock so React sees the mocked modules.
import { AuthProvider, useAuth } from './AuthContext'

function Probe() {
  const { state } = useAuth()
  return (
    <div>
      <div data-testid="status">{state.status}</div>
      <div data-testid="email">{state.user?.email ?? ''}</div>
      <div data-testid="error">{state.error ?? ''}</div>
    </div>
  )
}

const MOCK_ME: MeResponse = {
  user_id: 'u1',
  email: 'alice@example.com',
  org_id: 'default-org',
  roles: ['user'],
  total_credit: 10000,
  credit_used: 1234,
  remaining_credit: 8766,
  currency: 'tokens',
  tenant: { tenant_id: 'default-org', name: 'Default Org' },
  locale: 'ja',
}

function setUrl(href: string) {
  window.history.replaceState({}, '', href)
}

beforeEach(() => {
  window.sessionStorage.clear()
  mockMe.mockReset()
  mockRefresh.mockReset()
  setUrl('/')
})

afterEach(() => {
  window.sessionStorage.clear()
})

describe('AuthContext bootstrap', () => {
  it('strips ?token= without authenticating (P0-8)', async () => {
    // P0-8 (2026-04 security review): `?token=` is no longer accepted
    // as an authentication channel — it was a session-fixation vector.
    // The bootstrap should silently strip the parameter from the URL
    // and fall through to normal unauthenticated handling.
    setUrl('/?token=eyJcli-token')

    render(
      <AuthProvider>
        <Probe />
      </AuthProvider>,
    )

    await waitFor(() =>
      expect(screen.getByTestId('status').textContent).toBe('unauthenticated'),
    )
    // ?token= must have been scrubbed from the URL on the way in.
    expect(window.location.search).toBe('')
    // No tokens saved — the fixation payload must not persist.
    expect(window.sessionStorage.getItem('stratoclave_tokens')).toBeNull()
    expect(mockMe).not.toHaveBeenCalled()
  })

  it('falls back to localStorage when no URL token is present', async () => {
    const tokens: StoredTokens = {
      access_token: 'eyJaccess',
      id_token: null,
      refresh_token: 'eyJrefresh',
      // 30 min in the future — not near expiry.
      expires_at: Date.now() + 30 * 60 * 1000,
    }
    window.sessionStorage.setItem('stratoclave_tokens', JSON.stringify(tokens))
    mockMe.mockResolvedValueOnce(MOCK_ME)

    render(
      <AuthProvider>
        <Probe />
      </AuthProvider>,
    )

    await waitFor(() =>
      expect(screen.getByTestId('status').textContent).toBe('authenticated'),
    )
    expect(mockMe).toHaveBeenCalledTimes(1)
  })

  it('ends up unauthenticated when localStorage is empty and no URL token is present', async () => {
    render(
      <AuthProvider>
        <Probe />
      </AuthProvider>,
    )
    await waitFor(() =>
      expect(screen.getByTestId('status').textContent).toBe('unauthenticated'),
    )
    expect(mockMe).not.toHaveBeenCalled()
  })

  it('refreshes expired tokens via Cognito when a refresh_token is available', async () => {
    const expired: StoredTokens = {
      access_token: 'old',
      id_token: null,
      refresh_token: 'eyJrefresh',
      // Treat as expired (past) so the refresh branch runs.
      expires_at: Date.now() - 10_000,
    }
    window.sessionStorage.setItem('stratoclave_tokens', JSON.stringify(expired))

    const fresh: StoredTokens = {
      access_token: 'new',
      id_token: null,
      refresh_token: 'eyJrefresh',
      expires_at: Date.now() + 3600_000,
    }
    mockRefresh.mockResolvedValueOnce(fresh)
    mockMe.mockResolvedValueOnce(MOCK_ME)

    render(
      <AuthProvider>
        <Probe />
      </AuthProvider>,
    )

    await waitFor(() =>
      expect(screen.getByTestId('status').textContent).toBe('authenticated'),
    )
    expect(mockRefresh).toHaveBeenCalledWith('eyJrefresh')
  })

  it('clears tokens when /me returns 401 on bootstrap', async () => {
    // No more `?token=` injection, so the 401 path now exercises the
    // existing sessionStorage tokens being invalid (Cognito session
    // revoked, key rotated, tenant archived, etc.).
    const tokens: StoredTokens = {
      access_token: 'eyJstale',
      id_token: null,
      refresh_token: null,
      expires_at: Date.now() + 30 * 60 * 1000,
    }
    window.sessionStorage.setItem('stratoclave_tokens', JSON.stringify(tokens))
    const err = Object.assign(new Error('unauthorized'), { status: 401 })
    mockMe.mockRejectedValueOnce(err)

    render(
      <AuthProvider>
        <Probe />
      </AuthProvider>,
    )

    await waitFor(() =>
      expect(screen.getByTestId('status').textContent).toBe('unauthenticated'),
    )
    expect(window.sessionStorage.getItem('stratoclave_tokens')).toBeNull()
  })
})
