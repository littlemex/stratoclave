/**
 * Regression guard for sweep-4: handleCallback must verify the OIDC
 * `nonce` claim inside id_token equals the value stored in sessionStorage
 * before startLogin kicked off the flow.
 *
 * Without this check, an attacker who can steer a victim browser to
 * ``/callback?code=<attacker-code>&state=<leaked-state>`` replays a
 * previously-issued id_token. State alone only prevents login CSRF;
 * nonce specifically binds the id_token to THIS authorization request.
 *
 * Sweep-1 shipped this check; sweep-3 squash lost it. This test locks
 * the behaviour back in.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// Stub the runtime-config accessors BEFORE importing cognito.ts. vi.mock
// is hoisted so this takes effect at module resolution time, which
// vi.doMock inside it() would miss.
vi.mock('./config', () => ({
  getClientId: () => 'client-id',
  getCognitoDomain: () => 'https://example.auth.us-east-1.amazoncognito.com',
  getRedirectUri: () => 'http://127.0.0.1:3003/callback',
}))

import { clearTokens, handleCallback } from './cognito'

function b64url(s: string) {
  return btoa(s).replace(/\+/g, '-').replace(/\//g, '_').replace(/=/g, '')
}
function makeJwt(payload: Record<string, unknown>) {
  return `${b64url(JSON.stringify({ alg: 'RS256' }))}.${b64url(JSON.stringify(payload))}.sig`
}

describe('handleCallback OIDC nonce verification', () => {
  const origLocation = window.location
  beforeEach(() => {
    sessionStorage.clear()
    // @ts-expect-error override for test
    delete window.location
    // @ts-expect-error override for test
    window.location = new URL(
      'http://127.0.0.1:3003/callback?code=dummy-auth-code&state=expected-state',
    )
    // Provide PKCE verifier and state/nonce that startLogin would have
    // written.
    sessionStorage.setItem('stratoclave_pkce_verifier', 'verifier-xyz')
    sessionStorage.setItem('stratoclave_oauth_state', 'expected-state')
    sessionStorage.setItem('stratoclave_oauth_nonce', 'nonce-HONEST-42')
  })
  afterEach(() => {
    // @ts-expect-error restore
    window.location = origLocation
    sessionStorage.clear()
    clearTokens()
    vi.restoreAllMocks()
  })

  function mockFetch(body: Record<string, unknown>) {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => body,
      }),
    )
  }

  it('rejects a callback whose id_token carries a mismatched nonce', async () => {
    const tamperedIdToken = makeJwt({
      sub: 'user-x',
      nonce: 'nonce-EVIL-1337',
      iss: 'https://cognito-idp.example.com/pool',
    })
    mockFetch({
      access_token: 'at',
      id_token: tamperedIdToken,
      refresh_token: 'rt',
      expires_in: 3600,
    })
    await expect(handleCallback()).rejects.toThrow(/nonce/i)
  })

  it('accepts a callback whose id_token nonce matches what startLogin stored', async () => {
    const goodIdToken = makeJwt({
      sub: 'user-x',
      nonce: 'nonce-HONEST-42',
      iss: 'https://cognito-idp.example.com/pool',
    })
    mockFetch({
      access_token: 'at',
      id_token: goodIdToken,
      refresh_token: 'rt',
      expires_in: 3600,
    })
    const tokens = await handleCallback()
    expect(tokens.access_token).toBe('at')
  })

  // Sweep-4 round-2 blind review fail-closed guards.
  it('rejects a response that omits id_token entirely', async () => {
    mockFetch({
      access_token: 'at',
      refresh_token: 'rt',
      expires_in: 3600,
      // id_token deliberately missing: simulates MITM stripping the field
      // or an App Client misconfig that dropped the openid scope.
    })
    await expect(handleCallback()).rejects.toThrow(/id_token/i)
  })

  it('rejects a response whose id_token is an empty string', async () => {
    mockFetch({
      access_token: 'at',
      id_token: '',
      refresh_token: 'rt',
      expires_in: 3600,
    })
    await expect(handleCallback()).rejects.toThrow(/id_token/i)
  })

  it('rejects an id_token that has no nonce claim', async () => {
    const noNonce = makeJwt({
      sub: 'user-x',
      iss: 'https://cognito-idp.example.com/pool',
    })
    mockFetch({
      access_token: 'at',
      id_token: noNonce,
      refresh_token: 'rt',
      expires_in: 3600,
    })
    await expect(handleCallback()).rejects.toThrow(/nonce/i)
  })

  it('rejects an id_token whose nonce is not a string', async () => {
    const weirdNonce = makeJwt({
      sub: 'user-x',
      // number instead of string — fail-closed guard must reject this.
      nonce: 12345,
      iss: 'https://cognito-idp.example.com/pool',
    })
    mockFetch({
      access_token: 'at',
      id_token: weirdNonce,
      refresh_token: 'rt',
      expires_in: 3600,
    })
    await expect(handleCallback()).rejects.toThrow(/nonce/i)
  })

  it('rejects when sessionStorage has no stored nonce (replay into a fresh tab)', async () => {
    sessionStorage.removeItem('stratoclave_oauth_nonce')
    const goodNonce = makeJwt({
      sub: 'user-x',
      nonce: 'nonce-HONEST-42',
      iss: 'https://cognito-idp.example.com/pool',
    })
    mockFetch({
      access_token: 'at',
      id_token: goodNonce,
      refresh_token: 'rt',
      expires_in: 3600,
    })
    await expect(handleCallback()).rejects.toThrow(/nonce/i)
  })
})
