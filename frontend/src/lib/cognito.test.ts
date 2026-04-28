// sessionStorage helpers in cognito.ts (P0-7: moved off localStorage).
//
// The network-touching functions (startLogin, handleCallback,
// refreshTokens, logoutRedirect) are not exercised here; they are
// better covered by the AuthContext integration suite with MSW (follow-
// up PR). This file locks in the save/get/clear contract used by every
// CLI-injected and browser-stored session.

import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { clearTokens, getAccessToken, getStoredTokens, saveTokens } from './cognito'
import type { StoredTokens } from '@/types/auth'

const FIXTURE: StoredTokens = {
  access_token: 'eyJaccess',
  id_token: 'eyJid',
  refresh_token: 'eyJrefresh',
  expires_at: Date.now() + 60 * 60 * 1000,
}

describe('cognito session-storage helpers', () => {
  beforeEach(() => {
    window.sessionStorage.clear()
  })
  afterEach(() => {
    window.sessionStorage.clear()
  })

  it('round-trips a StoredTokens shape', () => {
    saveTokens(FIXTURE)
    const got = getStoredTokens()
    expect(got).toEqual(FIXTURE)
  })

  it('returns null when nothing is stored', () => {
    expect(getStoredTokens()).toBeNull()
    expect(getAccessToken()).toBeNull()
  })

  it('returns null on malformed JSON', () => {
    window.sessionStorage.setItem('stratoclave_tokens', 'not-json')
    expect(getStoredTokens()).toBeNull()
  })

  it('clearTokens removes the key', () => {
    saveTokens(FIXTURE)
    clearTokens()
    expect(getStoredTokens()).toBeNull()
    expect(window.sessionStorage.getItem('stratoclave_tokens')).toBeNull()
  })

  it('getAccessToken returns only the access_token field', () => {
    saveTokens(FIXTURE)
    expect(getAccessToken()).toBe('eyJaccess')
  })

  it('saveTokens overwrites the previous value', () => {
    saveTokens(FIXTURE)
    const updated: StoredTokens = { ...FIXTURE, access_token: 'eyJnew' }
    saveTokens(updated)
    expect(getAccessToken()).toBe('eyJnew')
  })
})
