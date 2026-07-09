/**
 * Authenticated fetch wrapper
 *
 * The backend (`backend/mvp/deps.py`) requires `Authorization: Bearer <access_token>`.
 * - 401: clear tokens and redirect to login
 * - 403: permission denied
 * - 429 / 5xx: display error message (if a global handler is registered)
 */

import type { ErrorType } from '@/contexts/ErrorContext'

import { getAccessToken } from './cognito'
import i18n from './i18n'

let errorSink: ((message: string, kind: ErrorType) => void) | null = null
let logoutSink: (() => void) | null = null

export function registerErrorHandler(h: typeof errorSink): void { errorSink = h }
export function unregisterErrorHandler(): void { errorSink = null }
export function registerLogoutHandler(h: typeof logoutSink): void { logoutSink = h }
export function unregisterLogoutHandler(): void { logoutSink = null }

// Resolve the localized toast message for an error kind. Falls back to the
// generic message when a specific key is missing. A server-provided `detail`
// (403/5xx) takes precedence over the localized default when supplied.
function report(kind: ErrorType, detail?: string): void {
  const message = detail ?? i18n.t(`error_toast.${kind}`)
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
      report('unauthorized')
      if (logoutSink) logoutSink()
      return res
    }
    if (res.status === 403) {
      let detail: string | undefined
      try {
        const body = await res.clone().json()
        if (body?.detail) detail = body.detail
      } catch {
        // ignore
      }
      report('forbidden', detail)
      return res
    }
    if (res.status === 429) {
      report('rate_limit')
      return res
    }
    if (res.status >= 500) {
      let detail: string | undefined
      try {
        const body = await res.clone().json()
        if (body?.detail) detail = body.detail
      } catch {
        // ignore
      }
      report('server', detail)
      return res
    }
    return res
  } catch (err) {
    clearTimeout(timeoutId)
    if (err instanceof DOMException && err.name === 'AbortError') {
      report('timeout')
      throw err
    }
    if (err instanceof TypeError) {
      report('network')
      throw err
    }
    throw err
  }
}
