import { type ReactNode } from 'react'
import { Navigate, Outlet } from 'react-router-dom'

import { useAuth } from '@/contexts/AuthContext'
import { usePermissions } from '@/hooks/usePermissions'
import { LoadingScreen } from '@/components/common/LoadingScreen'
import { AccessDenied } from '@/components/common/AccessDenied'
import type { UserRole } from '@/types/auth'

interface ProtectedRouteProps {
  children?: ReactNode
  requiredRoles?: UserRole[]
  requireAll?: boolean
  fallback?: ReactNode
}

export default function ProtectedRoute({
  children,
  requiredRoles,
  requireAll = false,
  fallback,
}: ProtectedRouteProps) {
  const { state } = useAuth()
  const { roles } = usePermissions()

  if (state.status === 'loading') {
    return <LoadingScreen message="認証情報を確認しています" />
  }

  if (state.status === 'unauthenticated') {
    return <Navigate to="/" replace />
  }

  if (requiredRoles && requiredRoles.length > 0) {
    const ok = requireAll
      ? requiredRoles.every((r) => roles.includes(r))
      : requiredRoles.some((r) => roles.includes(r))
    if (!ok) return fallback ? <>{fallback}</> : <AccessDenied />
  }

  return children ? <>{children}</> : <Outlet />
}
