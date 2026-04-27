// RBAC helper unit tests.
//
// These pure functions gate UI visibility; buggy behavior here is a
// defense-in-depth regression (the backend still enforces authorization
// via 403/404). The canonical permission table lives in
// `backend/permissions.json`, so these tests also document the mirrored
// expectation on the frontend.

import { describe, expect, it } from 'vitest'

import {
  hasAnyRole,
  hasPermission,
  isAdmin,
  isAdminOrTeamLead,
  isTeamLead,
} from './permissions'

describe('hasPermission', () => {
  it('matches an exact permission string on admin', () => {
    expect(hasPermission(['admin'], 'messages:send')).toBe(true)
  })

  it('expands wildcard resource permissions (users:*) for admin', () => {
    expect(hasPermission(['admin'], 'users:create')).toBe(true)
    expect(hasPermission(['admin'], 'users:delete')).toBe(true)
  })

  it('does not grant admin-only permissions to user', () => {
    expect(hasPermission(['user'], 'users:create')).toBe(false)
    expect(hasPermission(['user'], 'tenants:create')).toBe(false)
  })

  it('grants read-own but not full tenants to team_lead', () => {
    expect(hasPermission(['team_lead'], 'tenants:read-own')).toBe(true)
    expect(hasPermission(['team_lead'], 'tenants:delete')).toBe(false)
  })

  it('grants messages:send to every role (documented contract)', () => {
    expect(hasPermission(['admin'], 'messages:send')).toBe(true)
    expect(hasPermission(['team_lead'], 'messages:send')).toBe(true)
    expect(hasPermission(['user'], 'messages:send')).toBe(true)
  })

  it('returns false for unknown roles or empty role list', () => {
    expect(hasPermission([], 'messages:send')).toBe(false)
    // @ts-expect-error intentional bad input for robustness check
    expect(hasPermission(['nonexistent'], 'messages:send')).toBe(false)
  })
})

describe('role helpers', () => {
  it('hasAnyRole returns true when at least one role matches', () => {
    expect(hasAnyRole(['team_lead', 'user'], ['admin', 'team_lead'])).toBe(true)
    expect(hasAnyRole(['user'], ['admin'])).toBe(false)
    expect(hasAnyRole([], ['admin'])).toBe(false)
  })

  it('isAdmin only returns true for admin-containing role sets', () => {
    expect(isAdmin(['admin'])).toBe(true)
    expect(isAdmin(['admin', 'user'])).toBe(true)
    expect(isAdmin(['team_lead', 'user'])).toBe(false)
  })

  it('isTeamLead detects team_lead regardless of companion roles', () => {
    expect(isTeamLead(['team_lead'])).toBe(true)
    expect(isTeamLead(['team_lead', 'user'])).toBe(true)
    expect(isTeamLead(['admin'])).toBe(false)
  })

  it('isAdminOrTeamLead covers either role', () => {
    expect(isAdminOrTeamLead(['admin'])).toBe(true)
    expect(isAdminOrTeamLead(['team_lead'])).toBe(true)
    expect(isAdminOrTeamLead(['user'])).toBe(false)
  })
})
