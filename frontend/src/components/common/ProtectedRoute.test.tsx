// Integration tests for the RBAC routing guard.
//
// The component mixes three concerns:
//   1. Redirect unauthenticated visitors to "/" (LoginPage).
//   2. Render AccessDenied when an authenticated user lacks the
//      required roles.
//   3. Render child routes / <Outlet /> when the user is allowed.
//
// We fake the auth and permissions hooks so the tests stay focused on
// ProtectedRoute's own logic; the hooks themselves are exercised by
// their own unit tests (see permissions.test.ts).

import { render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'

import type { AuthState } from '@/types/auth'

// Mock factories so we can override per-test.
const mockAuthState = vi.fn()
const mockPermissions = vi.fn()

vi.mock('@/contexts/AuthContext', () => ({
  useAuth: () => ({
    state: mockAuthState(),
    dispatch: vi.fn(),
    login: vi.fn(),
    logout: vi.fn(),
    softLogout: vi.fn(),
    reloadUser: vi.fn(),
  }),
  AuthProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}))

vi.mock('@/hooks/usePermissions', () => ({
  usePermissions: () => mockPermissions(),
}))

// Must be imported AFTER the vi.mock blocks above so the mocks take effect.
import ProtectedRoute from './ProtectedRoute'

function renderAt(path: string, ui: React.ReactNode) {
  return render(<MemoryRouter initialEntries={[path]}>{ui}</MemoryRouter>)
}

const LOADING: AuthState = {
  status: 'loading',
  user: null,
  tokens: null,
  error: null,
}
const UNAUTH: AuthState = {
  status: 'unauthenticated',
  user: null,
  tokens: null,
  error: null,
}
const AUTH_ADMIN: AuthState = {
  status: 'authenticated',
  user: {
    user_id: 'u1',
    email: 'admin@example.com',
    org_id: 'default-org',
    roles: ['admin', 'user'],
    locale: 'ja',
  },
  tokens: {
    access_token: 'x',
    id_token: null,
    refresh_token: null,
    expires_at: Date.now() + 3600_000,
  },
  error: null,
}
const AUTH_USER: AuthState = {
  ...AUTH_ADMIN,
  user: { ...AUTH_ADMIN.user!, roles: ['user'] },
}

describe('ProtectedRoute', () => {
  it('renders a loading screen while auth is resolving', () => {
    mockAuthState.mockReturnValue(LOADING)
    mockPermissions.mockReturnValue({ roles: [] })
    renderAt(
      '/',
      <Routes>
        <Route
          path="/"
          element={
            <ProtectedRoute>
              <div>secret</div>
            </ProtectedRoute>
          }
        />
      </Routes>,
    )
    // The loading screen label is i18n-driven; match either the en or
    // ja translation of `callback.processing` so this test is not
    // brittle to whichever locale leaks in from an earlier test that
    // flipped i18next globally.
    expect(
      screen.getByText(/(サインイン処理中|Completing sign-in)/),
    ).toBeInTheDocument()
  })

  it('redirects unauthenticated users back to /', () => {
    mockAuthState.mockReturnValue(UNAUTH)
    mockPermissions.mockReturnValue({ roles: [] })
    renderAt(
      '/admin',
      <Routes>
        <Route path="/" element={<div>login-page</div>} />
        <Route
          path="/admin"
          element={
            <ProtectedRoute>
              <div>admin-only</div>
            </ProtectedRoute>
          }
        />
      </Routes>,
    )
    expect(screen.getByText('login-page')).toBeInTheDocument()
    expect(screen.queryByText('admin-only')).not.toBeInTheDocument()
  })

  it('renders children for authenticated users without role restriction', () => {
    mockAuthState.mockReturnValue(AUTH_USER)
    mockPermissions.mockReturnValue({ roles: ['user'] })
    renderAt(
      '/me',
      <Routes>
        <Route
          path="/me"
          element={
            <ProtectedRoute>
              <div>me-page</div>
            </ProtectedRoute>
          }
        />
      </Routes>,
    )
    expect(screen.getByText('me-page')).toBeInTheDocument()
  })

  it('blocks user from admin-only routes with AccessDenied', () => {
    mockAuthState.mockReturnValue(AUTH_USER)
    mockPermissions.mockReturnValue({ roles: ['user'] })
    renderAt(
      '/admin',
      <Routes>
        <Route
          path="/admin"
          element={
            <ProtectedRoute requiredRoles={['admin']}>
              <div>admin-only</div>
            </ProtectedRoute>
          }
        />
      </Routes>,
    )
    expect(screen.queryByText('admin-only')).not.toBeInTheDocument()
    // AccessDenied renders its own marker; assert we did not render content.
    expect(screen.queryByText('me-page')).not.toBeInTheDocument()
  })

  it('lets admins through admin-only routes', () => {
    mockAuthState.mockReturnValue(AUTH_ADMIN)
    mockPermissions.mockReturnValue({ roles: ['admin', 'user'] })
    renderAt(
      '/admin',
      <Routes>
        <Route
          path="/admin"
          element={
            <ProtectedRoute requiredRoles={['admin']}>
              <div>admin-only</div>
            </ProtectedRoute>
          }
        />
      </Routes>,
    )
    expect(screen.getByText('admin-only')).toBeInTheDocument()
  })

  it('requireAll=false permits users with any listed role', () => {
    mockAuthState.mockReturnValue({
      ...AUTH_ADMIN,
      user: { ...AUTH_ADMIN.user!, roles: ['team_lead'] },
    })
    mockPermissions.mockReturnValue({ roles: ['team_lead'] })
    renderAt(
      '/admin-or-lead',
      <Routes>
        <Route
          path="/admin-or-lead"
          element={
            <ProtectedRoute requiredRoles={['admin', 'team_lead']}>
              <div>admin-or-lead</div>
            </ProtectedRoute>
          }
        />
      </Routes>,
    )
    expect(screen.getByText('admin-or-lead')).toBeInTheDocument()
  })

  it('requireAll=true demands every listed role', () => {
    mockAuthState.mockReturnValue({
      ...AUTH_ADMIN,
      user: { ...AUTH_ADMIN.user!, roles: ['team_lead'] },
    })
    mockPermissions.mockReturnValue({ roles: ['team_lead'] })
    renderAt(
      '/both',
      <Routes>
        <Route
          path="/both"
          element={
            <ProtectedRoute requiredRoles={['admin', 'team_lead']} requireAll>
              <div>both</div>
            </ProtectedRoute>
          }
        />
      </Routes>,
    )
    expect(screen.queryByText('both')).not.toBeInTheDocument()
  })
})
