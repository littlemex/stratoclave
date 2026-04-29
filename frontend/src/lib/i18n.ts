/**
 * i18n bootstrap.
 *
 * Design contract:
 * - Supported locales live in one place, in one order: `SUPPORTED_LOCALES`.
 *   Backend (`backend/mvp/me.py :: SUPPORTED_LOCALES`) mirrors this tuple.
 * - The server-side default is "ja". A user without a stored locale on
 *   their DynamoDB row renders in Japanese on first login, which matches
 *   the historic UI.
 * - Authoritative source on bootstrap is `GET /api/mvp/me`. Language
 *   detector fallbacks (sessionStorage, `navigator.language`) are only
 *   used for the unauthenticated shell (Login / Callback) where the
 *   user has no server record yet.
 * - When the user toggles the header switcher we call `PATCH /api/mvp/me`
 *   so the choice sticks across devices / sessions. sessionStorage
 *   mirrors it so the next render of the same tab starts in the right
 *   language without a round-trip.
 * - locale is not sensitive: it is safe in sessionStorage/URL/cookie.
 */
import i18n from 'i18next'
import LanguageDetector from 'i18next-browser-languagedetector'
import { initReactI18next } from 'react-i18next'

import en from '@/locales/en.json'
import ja from '@/locales/ja.json'

export const SUPPORTED_LOCALES = ['en', 'ja'] as const
export type Locale = (typeof SUPPORTED_LOCALES)[number]
export const DEFAULT_LOCALE: Locale = 'ja'

export const LOCALE_STORAGE_KEY = 'stratoclave_locale'

export function isSupportedLocale(v: unknown): v is Locale {
  return typeof v === 'string' && (SUPPORTED_LOCALES as readonly string[]).includes(v)
}

void i18n
  .use(LanguageDetector)
  .use(initReactI18next)
  .init({
    resources: {
      en: { translation: en },
      ja: { translation: ja },
    },
    fallbackLng: DEFAULT_LOCALE,
    supportedLngs: [...SUPPORTED_LOCALES],
    nonExplicitSupportedLngs: true, // accept "en-US" → "en"
    interpolation: { escapeValue: false },
    detection: {
      order: ['sessionStorage', 'navigator'],
      lookupSessionStorage: LOCALE_STORAGE_KEY,
      caches: ['sessionStorage'],
    },
    returnNull: false,
  })

export default i18n

/**
 * Change the active UI locale + persist to sessionStorage (no network).
 *
 * Safe to call with unknown input: values that are not in
 * `SUPPORTED_LOCALES` are ignored so a stale / corrupt server row
 * cannot force the UI into a missing language bundle.
 */
export async function changeLocale(locale: Locale): Promise<void> {
  if (!isSupportedLocale(locale)) return
  try {
    window.sessionStorage.setItem(LOCALE_STORAGE_KEY, locale)
  } catch {
    // sessionStorage might be disabled in private-browsing corner cases.
  }
  await i18n.changeLanguage(locale)
}

/**
 * Synchronous variant of `changeLocale` — fire-and-forget locale switch
 * used by callers that can't await (e.g. inside a reducer-adjacent
 * callback chain). The underlying `i18n.changeLanguage` is itself
 * idempotent and safe to invoke from sync contexts.
 */
export function setLocaleLocal(locale: Locale): void {
  void changeLocale(locale)
}
