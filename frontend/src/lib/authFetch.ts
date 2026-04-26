/**
 * Authenticated fetch wrapper
 *
 * Backend (`backend/mvp/deps.py`) は `Authorization: Bearer <access_token>` を要求する。
 * - 401: tokens を消してログインへ
 * - 403: permission denied
 * - 429 / 5xx: エラーメッセージ表示 (グローバルハンドラ登録があれば)
 */

import type { ErrorType } from '@/contexts/ErrorContext'

import { getAccessToken } from './cognito'

let errorSink: ((message: string, kind: ErrorType) => void) | null = null
let logoutSink: (() => void) | null = null

export function registerErrorHandler(h: typeof errorSink): void { errorSink = h }
export function unregisterErrorHandler(): void { errorSink = null }
export function registerLogoutHandler(h: typeof logoutSink): void { logoutSink = h }
export function unregisterLogoutHandler(): void { logoutSink = null }

function report(message: string, kind: ErrorType): void {
  if (errorSink) errorSink(message, kind)
  else console.error(`[authFetch] ${kind}: ${message}`)
}

const DEFAULT_TIMEOUT_MS = 15_000

export async function authFetch(
  input: RequestInfo | URL,
  init?: RequestInit,
): Promise<Response> {
  const headers = new Headers(init?.headers)
  const token = getAccessToken()
  if (token) headers.set('Authorization', `Bearer ${token}`)

  const controller = new AbortController()
  if (init?.signal) {
    init.signal.addEventListener('abort', () => controller.abort())
  }
  const timeoutId = setTimeout(() => controller.abort(), DEFAULT_TIMEOUT_MS)

  try {
    const res = await fetch(input, {
      ...init,
      headers,
      signal: controller.signal,
    })
    clearTimeout(timeoutId)

    if (res.status === 401) {
      report('認証が切れました。再度ログインしてください。', 'unauthorized')
      if (logoutSink) logoutSink()
      return res
    }
    if (res.status === 403) {
      let detail = 'この操作を実行する権限がありません。'
      try {
        const body = await res.clone().json()
        if (body?.detail) detail = body.detail
      } catch {
        // ignore
      }
      report(detail, 'forbidden')
      return res
    }
    if (res.status === 429) {
      report('リクエストが多すぎます。しばらく待ってから再試行してください。', 'rate_limit')
      return res
    }
    if (res.status >= 500) {
      let detail = 'サーバーエラーが発生しました。少し待ってから再度お試しください。'
      try {
        const body = await res.clone().json()
        if (body?.detail) detail = body.detail
      } catch {
        // ignore
      }
      report(detail, 'server')
      return res
    }
    return res
  } catch (err) {
    clearTimeout(timeoutId)
    if (err instanceof DOMException && err.name === 'AbortError') {
      report('通信がタイムアウトしました。', 'timeout')
      throw err
    }
    if (err instanceof TypeError) {
      report('ネットワークエラーが発生しました。', 'network')
      throw err
    }
    throw err
  }
}
