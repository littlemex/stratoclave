import { type ReactNode } from 'react'

import { usePermissions } from '@/hooks/usePermissions'
import type { UserRole } from '@/types/auth'

interface RoleGateProps {
  children: ReactNode
  requiredRoles?: UserRole[]
  requiredPermission?: string
  fallback?: ReactNode
}

export default function RoleGate({
  children,
  requiredRoles,
  requiredPermission,
  fallback = null,
}: RoleGateProps) {
  const { roles, can } = usePermissions()

  if (requiredRoles && !requiredRoles.some((r) => roles.includes(r))) {
    return <>{fallback}</>
  }
  if (requiredPermission && !can(requiredPermission)) {
    return <>{fallback}</>
  }
  return <>{children}</>
}
