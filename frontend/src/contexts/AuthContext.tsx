/**
 * AuthContext (Phase 2)
 *
 * - access_token を localStorage に保存
 * - email / roles / org_id は login 直後に `GET /api/mvp/me` から取得
 * - 入口は 3 経路:
 *   1. CLI から開かれた `?token=<access_token>` (stratoclave ui open 経由)
 *   2. Hosted UI からの `/callback?code=...` (Callback.tsx)
 *   3. 既存 localStorage のトークン
 * - refresh_token があれば 5 分マージンで自動更新
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useReducer,
  type Dispatch,
  type ReactNode,
} from 'react'

import { api } from '@/lib/api'
import {
  clearTokens,
  getStoredTokens,
  logoutRedirect,
  refreshTokens as refreshTokensFromCognito,
  saveTokens,
  startLogin,
} from '@/lib/cognito'
import type {
  AuthAction,
  AuthState,
  AuthUser,
  StoredTokens,
  UserRole,
} from '@/types/auth'

const TOKEN_REFRESH_MARGIN = 5 * 60 * 1000
const TOKEN_CHECK_INTERVAL = 60 * 1000
const VALID_ROLES: UserRole[] = ['admin', 'team_lead', 'user']

interface AuthContextValue {
  state: AuthState
  dispatch: Dispatch<AuthAction>
  login: () => Promise<void>
  logout: () => void
  softLogout: () => void
  reloadUser: () => Promise<void>
}

const AuthContext = createContext<AuthContextValue | null>(null)

const initialState: AuthState = {
  status: 'loading',
  user: null,
  tokens: null,
  error: null,
}

function reducer(state: AuthState, action: AuthAction): AuthState {
  switch (action.type) {
    case 'AUTH_LOADING':
      return { ...state, status: 'loading', error: null }
    case 'AUTH_SUCCESS':
      return {
        status: 'authenticated',
        user: action.user,
        tokens: action.tokens,
        error: null,
      }
    case 'AUTH_FAILURE':
      return {
        status: 'unauthenticated',
        user: null,
        tokens: null,
        error: action.error,
      }
    case 'AUTH_LOGOUT':
      return { status: 'unauthenticated', user: null, tokens: null, error: null }
    case 'TOKENS_UPDATED':
      return { ...state, tokens: action.tokens }
    default:
      return state
  }
}

function sanitizeRoles(raw: unknown): UserRole[] {
  if (!Array.isArray(raw)) return []
  return raw.filter((r): r is UserRole =>
    typeof r === 'string' && (VALID_ROLES as string[]).includes(r),
  )
}

async function fetchMe(): Promise<AuthUser> {
  const me = await api.me()
  return {
    user_id: me.user_id,
    email: me.email ?? '',
    org_id: me.org_id ?? 'default-org',
    roles: sanitizeRoles(me.roles),
  }
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [state, dispatch] = useReducer(reducer, initialState)

  const login = useCallback(async () => {
    await startLogin()
  }, [])

  const logout = useCallback(() => {
    dispatch({ type: 'AUTH_LOGOUT' })
    logoutRedirect()
  }, [])

  const softLogout = useCallback(() => {
    clearTokens()
    dispatch({ type: 'AUTH_LOGOUT' })
  }, [])

  const reloadUser = useCallback(async () => {
    const tokens = getStoredTokens()
    if (!tokens) {
      dispatch({ type: 'AUTH_FAILURE', error: 'No tokens' })
      return
    }
    try {
      const user = await fetchMe()
      dispatch({ type: 'AUTH_SUCCESS', user, tokens })
    } catch (err) {
      const status = (err as { status?: number } | null)?.status
      if (status === 401) {
        softLogout()
      } else {
        dispatch({
          type: 'AUTH_FAILURE',
          error: err instanceof Error ? err.message : String(err),
        })
      }
    }
  }, [softLogout])

  /* ------------------------------------------------------------------ */
  /* Bootstrap                                                          */
  /* ------------------------------------------------------------------ */
  useEffect(() => {
    const url = new URL(window.location.href)

    // P0-8 (2026-04 security review): accepting `?token=<access_token>`
    // straight off the URL let anyone who could lure a victim to
    // `https://app.example/?token=<attacker>` pin the victim's SPA to
    // an attacker-controlled Cognito identity (session fixation). We
    // now strip any such parameter from the URL and ignore it.
    const strippedToken = url.searchParams.get('token')
    if (strippedToken !== null) {
      url.searchParams.delete('token')
      window.history.replaceState({}, document.title, url.pathname + url.search)
    }

    // P0-8 follow-up: the sanctioned CLI → SPA handoff channel. The
    // CLI mints a single-use, 30 s-TTL nonce on the backend and opens
    // the SPA with `?ui_ticket=<nonce>`. We (a) strip the query
    // parameter before any third-party script has a chance to see it,
    // (b) POST the nonce to `/api/mvp/auth/ui-ticket/consume` which
    // atomically deletes the backend record and hands back the real
    // tokens, and (c) drop the tokens into sessionStorage via the
    // normal `saveTokens` path. The nonce alone carries no API
    // authority, so leaking it in the URL bar during that brief
    // window is acceptable.
    const uiTicket = url.searchParams.get('ui_ticket')
    if (uiTicket !== null) {
      url.searchParams.delete('ui_ticket')
      window.history.replaceState({}, document.title, url.pathname + url.search)
    }

    const bootstrap = async () => {
      // 1. CLI → SPA handoff via single-use ticket (P0-8 follow-up).
      if (uiTicket) {
        try {
          const resp = await fetch('/api/mvp/auth/ui-ticket/consume', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ ticket: uiTicket }),
          })
          if (!resp.ok) {
            throw new Error(
              `ui-ticket consume failed: HTTP ${resp.status}`,
            )
          }
          const data = (await resp.json()) as {
            access_token: string
            id_token: string | null
            refresh_token: string | null
            expires_in: number | null
          }
          const tokens: StoredTokens = {
            access_token: data.access_token,
            id_token: data.id_token ?? null,
            refresh_token: data.refresh_token ?? null,
            expires_at:
              Date.now() + ((data.expires_in ?? 3600) * 1000),
          }
          saveTokens(tokens)
          try {
            const user = await fetchMe()
            dispatch({ type: 'AUTH_SUCCESS', user, tokens })
          } catch (err) {
            clearTokens()
            dispatch({
              type: 'AUTH_FAILURE',
              error:
                err instanceof Error
                  ? `CLI handoff 済だが /me 呼び出しに失敗: ${err.message}`
                  : 'CLI handoff 済だが /me 呼び出しに失敗',
            })
          }
          return
        } catch (err) {
          // Fall through to normal bootstrap on ticket failure; a
          // stale/expired ticket is a common UX case after a retry.
          console.warn(
            'ui_ticket exchange failed, falling back to stored session',
            err,
          )
        }
      }

      // 2. 既存 sessionStorage のトークン (P0-7)
      const stored = getStoredTokens()
      if (!stored) {
        dispatch({ type: 'AUTH_FAILURE', error: 'No tokens' })
        return
      }

      // 期限切れに近ければ refresh
      if (stored.expires_at < Date.now() + TOKEN_REFRESH_MARGIN) {
        if (stored.refresh_token) {
          const fresh = await refreshTokensFromCognito(stored.refresh_token)
          if (fresh) {
            try {
              const user = await fetchMe()
              dispatch({ type: 'AUTH_SUCCESS', user, tokens: fresh })
            } catch (err) {
              clearTokens()
              dispatch({
                type: 'AUTH_FAILURE',
                error: err instanceof Error ? err.message : String(err),
              })
            }
            return
          }
        }
        clearTokens()
        dispatch({ type: 'AUTH_FAILURE', error: 'Session expired' })
        return
      }

      // 有効トークンがあるので me を叩いて確定
      try {
        const user = await fetchMe()
        dispatch({ type: 'AUTH_SUCCESS', user, tokens: stored })
      } catch (err) {
        const status = (err as { status?: number } | null)?.status
        if (status === 401) {
          clearTokens()
          dispatch({ type: 'AUTH_FAILURE', error: 'Session invalid' })
        } else {
          dispatch({
            type: 'AUTH_FAILURE',
            error: err instanceof Error ? err.message : String(err),
          })
        }
      }
    }

    void bootstrap()
  }, [])

  /* ------------------------------------------------------------------ */
  /* Refresh timer                                                      */
  /* ------------------------------------------------------------------ */
  useEffect(() => {
    const handle = setInterval(() => {
      const tokens = getStoredTokens()
      if (!tokens || !tokens.refresh_token) return
      if (tokens.expires_at < Date.now() + TOKEN_REFRESH_MARGIN) {
        void refreshTokensFromCognito(tokens.refresh_token).then((fresh) => {
          if (fresh) dispatch({ type: 'TOKENS_UPDATED', tokens: fresh })
        })
      }
    }, TOKEN_CHECK_INTERVAL)
    return () => clearInterval(handle)
  }, [])

  /* ------------------------------------------------------------------ */
  /* Cross-tab sync                                                     */
  /* ------------------------------------------------------------------ */
  useEffect(() => {
    const handler = (e: StorageEvent) => {
      if (e.key !== 'stratoclave_tokens') return
      if (!e.newValue) {
        dispatch({ type: 'AUTH_LOGOUT' })
      } else {
        void reloadUser()
      }
    }
    window.addEventListener('storage', handler)
    return () => window.removeEventListener('storage', handler)
  }, [reloadUser])

  return (
    <AuthContext.Provider
      value={{ state, dispatch, login, logout, softLogout, reloadUser }}
    >
      {children}
    </AuthContext.Provider>
  )
}

export function useAuth() {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth must be used within AuthProvider')
  return ctx
}

export function useSoftLogout() {
  return useAuth().softLogout
}
