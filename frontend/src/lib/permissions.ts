/**
 * Frontend permissions (Phase 2)
 *
 * Backend `backend/permissions.json` と一致させる。
 * Backend が真実源なので、Frontend の判定はあくまで UI 表示切替用。
 * 実際の認可は Backend が 403/404 で拒否する。
 */

import type { UserRole } from '@/types/auth'

export const ROLE_PERMISSIONS: Record<UserRole, string[]> = {
  admin: ['users:*', 'tenants:*', 'usage:*', 'permissions:*', 'messages:send'],
  team_lead: [
    'tenants:create',
    'tenants:read-own',
    'usage:read-own-tenant',
    'messages:send',
  ],
  user: ['messages:send', 'usage:read-self'],
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
