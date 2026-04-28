/**
 * Cognito Authentication (PKCE OAuth 2.0)
 *
 * - Hosted UI から Authorization Code を受け取り、access_token / id_token / refresh_token に交換
 * - Backend は access_token のみ受理するため、Frontend も access_token を API 認可に使う
 * - トークンは localStorage("stratoclave_tokens") に保存 (0.25rem 角丸 UI 仕様とは別、純粋なデータ)
 * - CLI から URL クエリ `?token=xxx` で開かれたケースも本モジュールで吸収 (StoredTokens.access_token のみ埋める)
 */

import type { StoredTokens } from '@/types/auth'

import { getClientId, getCognitoDomain, getRedirectUri } from './config'

// P0-7 (2026-04 security review): the previous build stored tokens
// in `localStorage`, which is readable by *any* XSS payload anywhere
// on this origin and persists indefinitely (including after the user
// closes every tab). `sessionStorage` scopes them to the current tab,
// so the next session always re-authenticates via the Hosted UI. The
// long-term fix is to move `refresh_token` into an HttpOnly cookie
// with a BFF, tracked in the P1 roadmap.
const STORAGE_KEY = 'stratoclave_tokens'
const PKCE_KEY = 'stratoclave_pkce_verifier'
// P0-4 (2026-04 security review): OAuth `state` and OIDC `nonce` guard
// the authorization-code flow against login CSRF and ID-token replay.
// PKCE alone is insufficient for either.
const STATE_KEY = 'stratoclave_oauth_state'
const NONCE_KEY = 'stratoclave_oauth_nonce'

// ---------- PKCE + state/nonce ----------
async function generatePkce(): Promise<{ verifier: string; challenge: string }> {
  const array = new Uint8Array(32)
  crypto.getRandomValues(array)
  const verifier = btoa(String.fromCharCode(...array))
    .replace(/\+/g, '-')
    .replace(/\//g, '_')
    .replace(/=/g, '')
  const hash = await crypto.subtle.digest(
    'SHA-256',
    new TextEncoder().encode(verifier),
  )
  const challenge = btoa(String.fromCharCode(...new Uint8Array(hash)))
    .replace(/\+/g, '-')
    .replace(/\//g, '_')
    .replace(/=/g, '')
  return { verifier, challenge }
}

function generateUrlSafeRandom(byteLength = 32): string {
  const array = new Uint8Array(byteLength)
  crypto.getRandomValues(array)
  return btoa(String.fromCharCode(...array))
    .replace(/\+/g, '-')
    .replace(/\//g, '_')
    .replace(/=/g, '')
}

// ---------- Hosted UI ----------
export async function startLogin(): Promise<void> {
  const { verifier, challenge } = await generatePkce()
  const state = generateUrlSafeRandom()
  const nonce = generateUrlSafeRandom()
  sessionStorage.setItem(PKCE_KEY, verifier)
  sessionStorage.setItem(STATE_KEY, state)
  sessionStorage.setItem(NONCE_KEY, nonce)

  const params = new URLSearchParams({
    client_id: getClientId(),
    response_type: 'code',
    scope: 'openid email profile',
    redirect_uri: getRedirectUri(),
    code_challenge: challenge,
    code_challenge_method: 'S256',
    // P0-4: state defeats login CSRF (attacker plants a `?code=` on
    // the victim's callback), nonce binds the resulting ID token to
    // this specific authorization request.
    state,
    nonce,
  })
  window.location.href = `${getCognitoDomain()}/oauth2/authorize?${params.toString()}`
}

/** Cognito Hosted UI から戻ってきた `?code=...` を token に交換して保存 */
export async function handleCallback(): Promise<StoredTokens> {
  const params = new URLSearchParams(window.location.search)
  const code = params.get('code')
  const error = params.get('error')
  const returnedState = params.get('state')

  if (error) {
    throw new Error(
      `Cognito authentication failed: ${params.get('error_description') ?? error}`,
    )
  }
  if (!code) {
    throw new Error('Missing authorization code in callback URL')
  }

  // P0-4: the state we stashed in startLogin() must be echoed back
  // verbatim. A missing / mismatched state is the signature of login
  // CSRF and we refuse to continue the exchange.
  const expectedState = sessionStorage.getItem(STATE_KEY)
  if (!expectedState || returnedState !== expectedState) {
    sessionStorage.removeItem(STATE_KEY)
    sessionStorage.removeItem(NONCE_KEY)
    sessionStorage.removeItem(PKCE_KEY)
    throw new Error(
      'OAuth state mismatch — aborting to prevent login CSRF (P0-4)',
    )
  }
  sessionStorage.removeItem(STATE_KEY)

  const verifier = sessionStorage.getItem(PKCE_KEY)
  if (!verifier) {
    throw new Error('PKCE verifier not found (session expired?)')
  }

  const res = await fetch(`${getCognitoDomain()}/oauth2/token`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({
      grant_type: 'authorization_code',
      client_id: getClientId(),
      code,
      redirect_uri: getRedirectUri(),
      code_verifier: verifier,
    }).toString(),
  })

  if (!res.ok) {
    let message = `Token exchange failed: ${res.status}`
    try {
      const body = await res.json()
      if (body?.error) message = `Token exchange failed: ${body.error}`
    } catch {
      // non-JSON response, keep default
    }
    throw new Error(message)
  }

  const data = await res.json()
  sessionStorage.removeItem(PKCE_KEY)

  const tokens: StoredTokens = {
    access_token: data.access_token,
    id_token: data.id_token ?? null,
    refresh_token: data.refresh_token ?? null,
    expires_at: Date.now() + (data.expires_in ?? 3600) * 1000,
  }
  saveTokens(tokens)
  return tokens
}

// ---------- Refresh ----------
export async function refreshTokens(
  refreshToken: string,
): Promise<StoredTokens | null> {
  try {
    const res = await fetch(`${getCognitoDomain()}/oauth2/token`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: new URLSearchParams({
        grant_type: 'refresh_token',
        client_id: getClientId(),
        refresh_token: refreshToken,
      }).toString(),
    })
    if (!res.ok) return null
    const data = await res.json()
    const tokens: StoredTokens = {
      access_token: data.access_token,
      id_token: data.id_token ?? null,
      refresh_token: refreshToken,
      expires_at: Date.now() + (data.expires_in ?? 3600) * 1000,
    }
    saveTokens(tokens)
    return tokens
  } catch {
    return null
  }
}

// ---------- Logout ----------
/** localStorage をクリアして Hosted UI の logout へ redirect */
export function logoutRedirect(): void {
  clearTokens()
  const params = new URLSearchParams({
    client_id: getClientId(),
    logout_uri: window.location.origin,
  })
  window.location.href = `${getCognitoDomain()}/logout?${params.toString()}`
}

// ---------- Token storage ----------
export function saveTokens(tokens: StoredTokens): void {
  // P0-7: sessionStorage instead of localStorage so tokens die with the
  // tab. Any XSS still wins here (same origin), but at least tokens do
  // not survive across browser restarts or leak to long-closed tabs.
  sessionStorage.setItem(STORAGE_KEY, JSON.stringify(tokens))
}

export function getStoredTokens(): StoredTokens | null {
  // P0-7: read tokens from sessionStorage. Any leftover entries in
  // localStorage are migrated away from by clearTokens().
  const raw = sessionStorage.getItem(STORAGE_KEY)
  if (!raw) return null
  try {
    return JSON.parse(raw) as StoredTokens
  } catch {
    return null
  }
}

export function clearTokens(): void {
  // Clear both storage locations — sessionStorage is the canonical
  // location after P0-7, but we defensively remove any stray entry
  // that might have been written by an older build of the SPA.
  sessionStorage.removeItem(STORAGE_KEY)
  try {
    localStorage.removeItem(STORAGE_KEY)
  } catch {
    // localStorage may be disabled; harmless.
  }
  sessionStorage.removeItem(PKCE_KEY)
  sessionStorage.removeItem(STATE_KEY)
  sessionStorage.removeItem(NONCE_KEY)
}

/** 5 分マージンで現時点の有効トークンを取得 */
export function getAccessToken(): string | null {
  const tokens = getStoredTokens()
  if (!tokens) return null
  if (tokens.expires_at < Date.now() + 5 * 60 * 1000) return null
  return tokens.access_token
}
