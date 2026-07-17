/**
 * Frontend permissions (Phase 2)
 *
 * Must stay in sync with `backend/permissions.json`.
 * The backend is the source of truth; frontend checks are for UI display switching only.
 * Actual authorization is enforced by the backend via 403/404 responses.
 */

import type { UserRole } from '@/types/auth'

// Kept BYTE-FOR-BYTE in sync with backend/permissions.json (the source of
// truth). Drift here only mis-renders UI affordances — the backend still
// enforces via 403/404 — but a stale copy hides/shows the wrong controls. A
// vitest (permissions.test.ts) asserts this map equals permissions.json so the
// two cannot silently diverge.
export const ROLE_PERMISSIONS: Record<UserRole, string[]> = {
  admin: [
    'users:*',
    'tenants:*',
    'usage:*',
    'permissions:*',
    'accounts:*',
    'apikeys:*',
    'billing:*',
    'messages:send',
    'responses:send',
  ],
  team_lead: [
    'tenants:create',
    'tenants:read-own',
    'usage:read-own-tenant',
    'usage:read-self',
    'apikeys:read-self',
    'apikeys:create-self',
    'apikeys:revoke-self',
    'billing:read',
    'billing:write',
    'messages:send',
    'responses:send',
  ],
  user: [
    'messages:send',
    'responses:send',
    'usage:read-self',
    'apikeys:read-self',
    'apikeys:create-self',
    'apikeys:revoke-self',
  ],
}

export function hasPermission(
  roles: UserRole[],
  permission: string,
): boolean {
  const targetResource = permission.split(':', 1)[0]
  for (const role of roles) {
    const perms = ROLE_PERMISSIONS[role] ?? []
    for (const p of perms) {
      if (p === permission) return true
      if (p.endsWith(':*')) {
        const [resource] = p.split(':', 1)
        if (resource === targetResource) return true
      }
    }
  }
  return false
}

export function hasAnyRole(
  roles: UserRole[],
  wanted: UserRole[],
): boolean {
  return roles.some((r) => wanted.includes(r))
}

export function isAdmin(roles: UserRole[]): boolean {
  return roles.includes('admin')
}

export function isTeamLead(roles: UserRole[]): boolean {
  return roles.includes('team_lead')
}

export function isAdminOrTeamLead(roles: UserRole[]): boolean {
  return isAdmin(roles) || isTeamLead(roles)
}
