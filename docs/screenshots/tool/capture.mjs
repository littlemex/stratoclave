/**
 * Stratoclave documentation screenshot capture tool.
 *
 * Usage:
 *   TOKEN=<access_token> \
 *   BASE_URL=https://<your-cloudfront>.cloudfront.net \
 *   ADMIN_USER_ID=<cognito-sub> \
 *   node capture.mjs
 */

import { chromium } from 'playwright'
import { resolve, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const __dirname = dirname(fileURLToPath(import.meta.url))
const IMG_DIR = resolve(__dirname, '..', 'images')

const BASE_URL = process.env.BASE_URL ?? 'https://d111111abcdef8.cloudfront.net'
const TOKEN = process.env.TOKEN
const ADMIN_USER_ID = process.env.ADMIN_USER_ID
if (!TOKEN) throw new Error('TOKEN env required')
if (!ADMIN_USER_ID) throw new Error('ADMIN_USER_ID env required')

const VIEWPORT = { width: 1440, height: 900 }

async function freshBrowser(fn) {
  const browser = await chromium.launch()
  try {
    // Explicitly fresh context — no persistent storage.
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

async function captureUnauth(name, waitMs = 3000) {
  await freshBrowser(async (page) => {
    await page.goto(`${BASE_URL}/`, { waitUntil: 'networkidle' })
    // Make triple-sure storage is empty for the unauth shot.
    await page.evaluate(() => {
      localStorage.clear()
      sessionStorage.clear()
    })
    await page.reload({ waitUntil: 'networkidle' })
    await page.waitForTimeout(waitMs)
    const out = resolve(IMG_DIR, `${name}.png`)
    await page.screenshot({ path: out })
    const h1 = await page.locator('h1').first().textContent().catch(() => '(no h1)')
    const url = page.url()
    console.log(`[cap] ${name} -> ${url}  h1=${JSON.stringify(h1)}`)
  })
}

async function captureAuth(name, path, { anchor, fullPage = false, waitMs = 2500 } = {}) {
  await freshBrowser(async (page) => {
    // 1. Visit root so localStorage is same-origin.
    await page.goto(`${BASE_URL}/`, { waitUntil: 'networkidle' })
    // 2. Inject token.
    await page.evaluate((token) => {
      localStorage.setItem(
        'stratoclave_tokens',
        JSON.stringify({
          access_token: token,
          id_token: null,
          refresh_token: null,
          expires_at: Date.now() + 55 * 60 * 1000,
        }),
      )
    }, TOKEN)
    // 3. Navigate to the target page.
    await page.goto(`${BASE_URL}${path}`, { waitUntil: 'networkidle' })
    if (anchor) {
      try {
        await page
          .locator('h1', { hasText: anchor })
          .first()
          .waitFor({ timeout: 20_000 })
      } catch {
        const got = await page.locator('h1').first().textContent().catch(() => '?')
        console.warn(`  [warn] anchor "${anchor}" not found; h1="${got}"`)
      }
    }
    await page.waitForTimeout(waitMs)
    const out = resolve(IMG_DIR, `${name}.png`)
    await page.screenshot({ path: out, fullPage })
    const finalUrl = page.url()
    const finalH1 = await page.locator('h1').first().textContent().catch(() => '(none)')
    console.log(`[cap] ${name} -> ${finalUrl}  h1=${JSON.stringify(finalH1)}`)
  })
}

async function main() {
  await captureUnauth('01_login')

  await captureAuth('02_dashboard', '/', { anchor: 'ようこそ' })

  await captureAuth('03_me_usage', '/me/usage', {
    anchor: '自分の使用履歴',
    fullPage: true,
  })

  await captureAuth('05_admin_user_new', '/admin/users/new', {
    anchor: '新規ユーザー作成',
    fullPage: true,
  })

  await captureAuth('06_admin_user_detail', `/admin/users/${encodeURIComponent(ADMIN_USER_ID)}`, {
    anchor: '@',
    fullPage: true,
  })

  await captureAuth('08_admin_tenant_detail', '/admin/tenants/default-org', {
    anchor: 'Default',
    fullPage: false,
  })

  await captureAuth('12_me_api_keys', '/me/api-keys', {
    anchor: 'API',
    fullPage: true,
  })

  console.log('[DONE]')
}

main().catch((err) => {
  console.error(err)
  process.exit(1)
})
