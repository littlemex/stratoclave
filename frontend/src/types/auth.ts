/**
 * Phase 2 auth model
 *
 * - Source of truth for `roles` is the Backend DynamoDB Users table (`cognito:groups` is ignored)
 * - The backend only accepts access_token (id_token returns 401)
 * - The frontend attaches access_token in the Authorization header of fetch requests
 * - email / roles are fetched via `GET /api/mvp/me` immediately after login (access_token carries no email claim)
 */

export type UserRole = 'admin' | 'team_lead' | 'user'

// i18n: supported UI locales, server-clamped. Keep in sync with
// backend/mvp/me.py::SUPPORTED_LOCALES.
export type UserLocale = 'en' | 'ja'

export interface StoredTokens {
  access_token: string
  id_token: string | null
  refresh_token: string | null
  expires_at: number // epoch ms
}

export interface AuthUser {
  user_id: string
  email: string
  org_id: string
  roles: UserRole[]
  locale: UserLocale
}

export interface AuthState {
  status: 'loading' | 'authenticated' | 'unauthenticated'
  user: AuthUser | null
  tokens: StoredTokens | null
  error: string | null
}

export type AuthAction =
  | { type: 'AUTH_LOADING' }
  | { type: 'AUTH_SUCCESS'; user: AuthUser; tokens: StoredTokens }
  | { type: 'AUTH_FAILURE'; error: string }
  | { type: 'AUTH_LOGOUT' }
  | { type: 'TOKENS_UPDATED'; tokens: StoredTokens }
  | { type: 'LOCALE_UPDATED'; locale: UserLocale }
