import { useEffect } from 'react'
import { Navigate, Route, Routes } from 'react-router-dom'
import { useTranslation } from 'react-i18next'

import { ErrorToast } from '@/components/ErrorToast'
import { LoadingScreen } from '@/components/common/LoadingScreen'
import ProtectedRoute from '@/components/common/ProtectedRoute'
import { AppShell } from '@/components/layout/AppShell'
import { useAuth, useSoftLogout } from '@/contexts/AuthContext'
import { useError } from '@/contexts/ErrorContext'
import {
  registerErrorHandler,
  registerLogoutHandler,
  unregisterErrorHandler,
  unregisterLogoutHandler,
} from '@/lib/authFetch'

import Callback from '@/pages/Callback'
import Login from '@/pages/Login'
import Dashboard from '@/pages/Dashboard'
import MeUsage from '@/pages/MeUsage'
import MeApiKeys from '@/pages/MeApiKeys'
import AdminUsers from '@/pages/admin/AdminUsers'
import AdminUserNew from '@/pages/admin/AdminUserNew'
import AdminUserDetail from '@/pages/admin/AdminUserDetail'
import AdminTenants from '@/pages/admin/AdminTenants'
import AdminTenantDetail from '@/pages/admin/AdminTenantDetail'
import AdminUsageLogs from '@/pages/admin/AdminUsageLogs'
import AdminTrustedAccounts from '@/pages/admin/AdminTrustedAccounts'
import AdminTrustedAccountDetail from '@/pages/admin/AdminTrustedAccountDetail'
import TeamLeadTenants from '@/pages/team-lead/TeamLeadTenants'
import TeamLeadTenantNew from '@/pages/team-lead/TeamLeadTenantNew'
import TeamLeadTenantDetail from '@/pages/team-lead/TeamLeadTenantDetail'

export default function App() {
  const { state } = useAuth()
  const softLogout = useSoftLogout()
  const { showError } = useError()
  const { t } = useTranslation()

  useEffect(() => {
    registerErrorHandler(showError)
    registerLogoutHandler(() => softLogout())
    return () => {
      unregisterErrorHandler()
      unregisterLogoutHandler()
    }
  }, [showError, softLogout])

  // Bootstrapping the AuthContext (network I/O + token check)
  if (state.status === 'loading') {
    return <LoadingScreen message={t('callback.processing')} />
  }

  return (
    <>
      <ErrorToast />
      <Routes>
        {/* Cognito callback は認証状態に関わらず到達可能にする */}
        <Route path="/callback" element={<Callback />} />

        {state.status === 'unauthenticated' ? (
          <>
            <Route path="/" element={<Login />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </>
        ) : (
          <Route element={<ProtectedRoute />}>
            <Route element={<AppShell />}>
              <Route path="/" element={<Dashboard />} />
              <Route path="/me/usage" element={<MeUsage />} />
              <Route path="/me/api-keys" element={<MeApiKeys />} />
              <Route element={<ProtectedRoute requiredRoles={['admin']} />}>
                <Route path="/admin/users" element={<AdminUsers />} />
                <Route path="/admin/users/new" element={<AdminUserNew />} />
                <Route path="/admin/users/:userId" element={<AdminUserDetail />} />
                <Route path="/admin/tenants" element={<AdminTenants />} />
                <Route path="/admin/tenants/:tenantId" element={<AdminTenantDetail />} />
                <Route path="/admin/usage" element={<AdminUsageLogs />} />
                <Route path="/admin/trusted-accounts" element={<AdminTrustedAccounts />} />
                <Route
                  path="/admin/trusted-accounts/:accountId"
                  element={<AdminTrustedAccountDetail />}
                />
              </Route>
              <Route element={<ProtectedRoute requiredRoles={['team_lead', 'admin']} />}>
                <Route path="/team-lead/tenants" element={<TeamLeadTenants />} />
                <Route path="/team-lead/tenants/new" element={<TeamLeadTenantNew />} />
                <Route
                  path="/team-lead/tenants/:tenantId"
                  element={<TeamLeadTenantDetail />}
                />
              </Route>
              <Route path="*" element={<Navigate to="/" replace />} />
            </Route>
          </Route>
        )}
      </Routes>
    </>
  )
}
