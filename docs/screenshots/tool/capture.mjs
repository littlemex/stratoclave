/**
 * Stratoclave documentation screenshot capture tool.
 *
 * Captures all 13 console screens for one locale (en or ja).
 *
 * Usage:
 *   TOKEN=<access_token> \
 *   BASE_URL=https://<your-cloudfront>.cloudfront.net \
 *   ADMIN_USER_ID=<cognito-sub> \
 *   TRUSTED_ACCOUNT_ID=<aws-account-id-or-skip> \
 *   LOCALE=en \
 *   node capture.mjs
 *
 * File naming:
 *   LOCALE=en  → <base>.png        (canonical English version)
 *   LOCALE=ja  → <base>.ja.png     (Japanese sibling)
 *
 * Locale: the console takes it from the signed-in user's record on every
 * `/me` fetch, so a client-side override alone is replaced as soon as the app
 * boots. The tool therefore sets the user's locale with `PATCH /api/mvp/me`
 * before capturing (the same call the console's language switcher makes), and
 * also seeds the session key the app reads before `/me` answers. The user's
 * previous locale is restored when the run ends. Locale is per-user server
 * state, so run the en and ja captures one after the other, not in parallel;
 * the run checks the locale it captured is still the one it set.
 *
 * API_BASE_URL defaults to BASE_URL (CloudFront serves both); set it when the
 * console and the API are on different origins, e.g. a local Vite dev server.
 *
 * The access token goes in sessionStorage under the key `frontend/src/lib/
 * cognito.ts` uses; the console moved tokens out of localStorage, and a token
 * left there is ignored, which made every authenticated shot a sign-in page.
 * `frontend/src/lib/captureTool.test.ts` keeps both keys and the storage in
 * step with the app.
 */

import { chromium } from 'playwright'
import { resolve, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const __dirname = dirname(fileURLToPath(import.meta.url))
const IMG_DIR = resolve(__dirname, '..', 'images')

const BASE_URL = process.env.BASE_URL ?? 'https://d111111abcdef8.cloudfront.net'
const API_BASE_URL = process.env.API_BASE_URL ?? BASE_URL
const TOKEN = process.env.TOKEN
const ADMIN_USER_ID = process.env.ADMIN_USER_ID
const TRUSTED_ACCOUNT_ID = process.env.TRUSTED_ACCOUNT_ID  // optional
const LOCALE = (process.env.LOCALE ?? 'en').toLowerCase()
const TENANT_ID = process.env.TENANT_ID ?? 'default-org'

if (!TOKEN) throw new Error('TOKEN env required')
if (!ADMIN_USER_ID) throw new Error('ADMIN_USER_ID env required')
if (LOCALE !== 'en' && LOCALE !== 'ja') {
  throw new Error(`LOCALE must be "en" or "ja", got "${LOCALE}"`)
}

const VIEWPORT = { width: 1440, height: 900 }
const LOCALE_STORAGE_KEY = 'stratoclave_locale'  // matches frontend/src/lib/i18n.ts
const TOKEN_STORAGE_KEY = 'stratoclave_tokens'

// File suffix: en is the canonical "no suffix" version, ja is the sibling.
const SUFFIX = LOCALE === 'en' ? '' : '.ja'

// Per-page anchor headings, by locale. Each entry is the h1 text that
// must appear before we screenshot — guards against navigating before
// the React tree has rendered the localized text.
const ANCHORS = {
  en: {
    login: 'Stratoclave',
    dashboard: 'Welcome',
    me_usage: 'My usage history',
    me_api_keys: 'Long-lived API keys',
    admin_users: 'User administration',
    admin_user_new: 'New user',
    admin_user_detail: '@',           // user email contains @
    admin_tenants: 'Tenant administration',
    admin_tenant_detail: 'Members',   // matches admin_tenant_detail.members_title
    admin_usage_logs: 'All usage',
    admin_trusted_accounts: 'Trusted AWS accounts',
    admin_trusted_account_detail: 'Allowed role patterns',
  },
  ja: {
    login: 'Stratoclave',
    dashboard: 'ようこそ',
    me_usage: '自分の使用履歴',
    me_api_keys: 'API',
    admin_users: 'ユーザー管理',
    admin_user_new: '新規ユーザー作成',
    admin_user_detail: '@',
    admin_tenants: 'テナント管理',
    admin_tenant_detail: 'メンバー',
    admin_usage_logs: 'すべての使用履歴',
    admin_trusted_accounts: '信頼された AWS アカウント',
    admin_trusted_account_detail: '許可されたロールパターン',
  },
}

const A = ANCHORS[LOCALE]

/**
 * Run `fn(page)` against a brand-new browser context.
 * Each shot starts fresh — no cookie / storage residue from the prior shot.
 */
async function freshBrowser(fn) {
  const browser = await chromium.launch()
  try {
    const ctx = await browser.newContext({
      viewport: VIEWPORT,
      deviceScaleFactor: 1,
      storageState: undefined,
    })
    const page = await ctx.newPage()
    await fn(page, ctx)
  } finally {
    await browser.close()
  }
}

/**
 * Seed the auth token and the locale into sessionStorage, where the console
 * reads them, before the navigation that is captured. Called after the first navigation lands
 * us on the correct origin so the storage write goes to the right
 * domain bucket.
 */
async function seedStorage(page, { token } = { token: TOKEN }) {
  await page.evaluate(
    ([tokenKey, tokenVal, localeKey, localeVal]) => {
      if (tokenVal) {
        sessionStorage.setItem(
          tokenKey,
          JSON.stringify({
            access_token: tokenVal,
            id_token: null,
            refresh_token: null,
            expires_at: Date.now() + 55 * 60 * 1000,
          }),
        )
      }
      sessionStorage.setItem(localeKey, localeVal)
    },
    [TOKEN_STORAGE_KEY, token, LOCALE_STORAGE_KEY, LOCALE],
  )
}

async function captureUnauth(name, anchor, waitMs = 3000) {
  await freshBrowser(async (page) => {
    await page.goto(`${BASE_URL}/`, { waitUntil: 'networkidle' })
    // Triple-sure: clear auth, but seed locale.
    await page.evaluate(
      ([tokenKey, localeKey, localeVal]) => {
        sessionStorage.removeItem(tokenKey)
        sessionStorage.setItem(localeKey, localeVal)
      },
      [TOKEN_STORAGE_KEY, LOCALE_STORAGE_KEY, LOCALE],
    )
    await page.reload({ waitUntil: 'networkidle' })
    if (anchor) {
      await page.locator('h1', { hasText: anchor }).first().waitFor({ timeout: 15_000 }).catch(() => {})
    }
    await page.waitForTimeout(waitMs)
    const out = resolve(IMG_DIR, `${name}${SUFFIX}.png`)
    await page.screenshot({ path: out })
    const h1 = await page.locator('h1').first().textContent().catch(() => '(no h1)')
    console.log(`[cap:${LOCALE}] ${name}${SUFFIX}.png  h1=${JSON.stringify(h1)}`)
  })
}

async function captureUnauthHover(name, anchor, hoverSelector, waitMs = 800) {
  await freshBrowser(async (page) => {
    await page.goto(`${BASE_URL}/`, { waitUntil: 'networkidle' })
    await page.evaluate(
      ([tokenKey, localeKey, localeVal]) => {
        sessionStorage.removeItem(tokenKey)
        sessionStorage.setItem(localeKey, localeVal)
      },
      [TOKEN_STORAGE_KEY, LOCALE_STORAGE_KEY, LOCALE],
    )
    await page.reload({ waitUntil: 'networkidle' })
    if (anchor) {
      await page.locator('h1', { hasText: anchor }).first().waitFor({ timeout: 15_000 }).catch(() => {})
    }
    if (hoverSelector) {
      try {
        await page.locator(hoverSelector).first().hover({ timeout: 5_000 })
      } catch (err) {
        console.warn(`  [warn] hover target "${hoverSelector}" not found, falling back to plain shot`)
      }
    }
    await page.waitForTimeout(waitMs)
    const out = resolve(IMG_DIR, `${name}${SUFFIX}.png`)
    await page.screenshot({ path: out })
    console.log(`[cap:${LOCALE}] ${name}${SUFFIX}.png  hover=${hoverSelector}`)
  })
}

async function captureAuth(name, path, { anchor, fullPage = false, waitMs = 2500 } = {}) {
  await freshBrowser(async (page) => {
    // 1. Land on origin so storage writes hit the right domain.
    await page.goto(`${BASE_URL}/`, { waitUntil: 'networkidle' })
    // 2. Seed token + locale.
    await seedStorage(page)
    // 3. Navigate to the target page; React boots with both.
    await page.goto(`${BASE_URL}${path}`, { waitUntil: 'networkidle' })
    if (anchor) {
      try {
        await page.locator('h1', { hasText: anchor }).first().waitFor({ timeout: 20_000 })
      } catch {
        const got = await page.locator('h1').first().textContent().catch(() => '?')
        console.warn(`  [warn] anchor "${anchor}" not found; h1="${got}"`)
      }
    }
    await page.waitForTimeout(waitMs)
    const out = resolve(IMG_DIR, `${name}${SUFFIX}.png`)
    await page.screenshot({ path: out, fullPage })
    const finalUrl = page.url()
    const finalH1 = await page.locator('h1').first().textContent().catch(() => '(none)')
    // A token the console does not accept lands on the sign-in page, which is a
    // screenshot that looks fine and is wrong. Refuse it rather than saving it.
    if (finalH1?.trim() === A.login || /\/login\b/.test(finalUrl)) {
      throw new Error(`${name}: landed on the sign-in page; the token was not accepted`)
    }
    console.log(`[cap:${LOCALE}] ${name}${SUFFIX}.png  ${finalUrl}  h1=${JSON.stringify(finalH1)}`)
  })
}

/** The signed-in user's server-side locale, read and written through /me. */
async function me(method, body) {
  const res = await fetch(`${API_BASE_URL}/api/mvp/me`, {
    method,
    headers: { Authorization: `Bearer ${TOKEN}`, 'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined,
  })
  if (!res.ok) throw new Error(`${method} /api/mvp/me failed: ${res.status} ${await res.text()}`)
  return res.json()
}

async function main() {
  const previous = (await me('GET')).locale
  await me('PATCH', { locale: LOCALE })
  try {
    await captureAll()
    const now = (await me('GET')).locale
    if (now !== LOCALE) {
      throw new Error(`the user's locale changed to ${now} during the run; run locales one at a time`)
    }
  } finally {
    if (previous && previous !== LOCALE) await me('PATCH', { locale: previous })
  }
}

async function captureAll() {
  // 01. Login (unauth)
  await captureUnauth('01_login', A.login)

  // 01b. Login with language switcher hovered — header LanguageSwitcher button.
  // The switcher renders the OTHER locale's endonym, so on en it shows
  // "日本語" and on ja it shows "English". Match by class hook.
  await captureUnauthHover('01b_login_hover', A.login, 'button[aria-pressed]')

  // 02. Dashboard (auth, root path)
  await captureAuth('02_dashboard', '/', { anchor: A.dashboard })

  // 03. /me/usage
  await captureAuth('03_me_usage', '/me/usage', { anchor: A.me_usage, fullPage: true })

  // 04. /admin/users
  await captureAuth('04_admin_users', '/admin/users', { anchor: A.admin_users, fullPage: true })

  // 05. /admin/users/new
  await captureAuth('05_admin_user_new', '/admin/users/new', { anchor: A.admin_user_new, fullPage: true })

  // 06. /admin/users/{id}
  await captureAuth(
    '06_admin_user_detail',
    `/admin/users/${encodeURIComponent(ADMIN_USER_ID)}`,
    { anchor: A.admin_user_detail, fullPage: true },
  )

  // 07. /admin/tenants
  await captureAuth('07_admin_tenants', '/admin/tenants', { anchor: A.admin_tenants, fullPage: true })

  // 08. /admin/tenants/{id}
  await captureAuth(
    '08_admin_tenant_detail',
    `/admin/tenants/${encodeURIComponent(TENANT_ID)}`,
    { anchor: A.admin_tenant_detail },
  )

  // 09. /admin/usage
  await captureAuth('09_admin_usage_logs', '/admin/usage', { anchor: A.admin_usage_logs, fullPage: true })

  // 10. /admin/trusted-accounts
  await captureAuth('10_admin_trusted_accounts', '/admin/trusted-accounts', {
    anchor: A.admin_trusted_accounts,
    fullPage: true,
  })

  // 11. /admin/trusted-accounts/{id}
  if (TRUSTED_ACCOUNT_ID) {
    await captureAuth(
      '11_admin_trusted_account_detail',
      `/admin/trusted-accounts/${encodeURIComponent(TRUSTED_ACCOUNT_ID)}`,
      { anchor: A.admin_trusted_account_detail, fullPage: true },
    )
  } else {
    console.warn('[skip] 11_admin_trusted_account_detail (set TRUSTED_ACCOUNT_ID to enable)')
  }

  // 12. /me/api-keys
  await captureAuth('12_me_api_keys', '/me/api-keys', { anchor: A.me_api_keys, fullPage: true })

  console.log(`[DONE] locale=${LOCALE} → suffix="${SUFFIX || '(none)'}"`)
}

main().catch((err) => {
  console.error(err)
  process.exit(1)
})
