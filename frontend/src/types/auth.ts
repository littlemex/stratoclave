/**
 * Phase 2 認証モデル
 *
 * - `roles` の真実源は Backend DynamoDB Users テーブル (`cognito:groups` は無視)
 * - Backend は access_token のみ受理 (id_token は 401)
 * - Frontend は access_token を fetch の Authorization ヘッダに乗せる
 * - email / roles は login 直後に `GET /api/mvp/me` から取得 (access_token には email claim 非存在)
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
