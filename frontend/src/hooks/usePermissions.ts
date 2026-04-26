import { useAuth } from '@/contexts/AuthContext'
import {
  hasPermission,
  isAdmin,
  isAdminOrTeamLead,
  isTeamLead,
} from '@/lib/permissions'
import type { UserRole } from '@/types/auth'

export function usePermissions() {
  const { state } = useAuth()
  const roles: UserRole[] = state.user?.roles ?? []
  const orgId = state.user?.org_id ?? null

  return {
    roles,
    orgId,
    isAdmin: isAdmin(roles),
    isTeamLead: isTeamLead(roles),
    isAdminOrTeamLead: isAdminOrTeamLead(roles),
    can: (permission: string) => hasPermission(roles, permission),
  }
}
