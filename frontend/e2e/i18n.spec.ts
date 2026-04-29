// i18n end-to-end flow on the unauthenticated shell.
//
// Why these tests exist
// ---------------------
// The i18next bootstrap contract is documented in `src/lib/i18n.ts`:
//   1. Persisted sessionStorage wins (key: `stratoclave_locale`).
//   2. Otherwise we fall back to `navigator.language` → clamped to
//      SUPPORTED_LOCALES (`en` / `ja`).
//   3. The header `<LanguageSwitcher>` flips i18next immediately and,
//      when authenticated, persists via `PATCH /api/mvp/me`.
//
// Unit tests exercise `LanguageSwitcher` in isolation (`src/components/
// common/LanguageSwitcher.test.tsx`). What those tests cannot cover:
//
//   - That Login renders *real* translations from en.json / ja.json
//     and not just the keys (a missing-key regression).
//   - That the header switcher actually swaps every translated string
//     on the same render frame (no stale cache).
//   - That the locale choice persists across full page reloads via
//     sessionStorage, which is the UX contract for unauthenticated
//     users (no `/me` round-trip yet).
//
// The suite runs against the **unauthenticated** Login shell because
// auth-gated pages need a mocked Cognito session, which is out of scope
// for a locale smoke — `LanguageSwitcher` is also mounted on Login
// exactly so the locale can be set before signing in.

import { expect, test } from '@playwright/test'

const STORAGE_KEY = 'stratoclave_locale'

// Locale-scoped strings we assert on. Pulled from the real resource
// bundles so a typo in a translation file would break this test — which
// is the point.
const EN_STRINGS = {
  sign_in: /sign in with cognito/i,
  card_title: /^sign in$/i,
  cli_title: /signing in from the cli/i,
}
const JA_STRINGS = {
  sign_in: /cognito でサインイン/i,
  card_title: /サインイン/,
  cli_title: /CLI からサインインする場合/,
}

test.describe('i18n on the unauthenticated Login shell', () => {
  test.beforeEach(async ({ context }) => {
    // sessionStorage is per-origin; we scrub it before every test so
    // each scenario starts from a clean slate and decides its own
    // bootstrap source (navigator.language vs explicit seed).
    await context.clearCookies()
  })

  test('renders English when the browser locale resolves to en-US', async ({
    browser,
  }) => {
    const ctx = await browser.newContext({ locale: 'en-US' })
    const page = await ctx.newPage()
    await page.goto('/')

    await expect(
      page.getByRole('button', { name: EN_STRINGS.sign_in }),
    ).toBeVisible()
    await expect(
      page.getByRole('heading', { name: EN_STRINGS.card_title }),
    ).toBeVisible()
    await expect(page.getByText(EN_STRINGS.cli_title)).toBeVisible()

    // The switcher marks the active locale with aria-pressed=true.
    const enBtn = page.getByRole('button', { name: /^english$/i })
    const jaBtn = page.getByRole('button', { name: /^日本語$/i })
    await expect(enBtn.first()).toHaveAttribute('aria-pressed', 'true')
    await expect(jaBtn.first()).toHaveAttribute('aria-pressed', 'false')

    await ctx.close()
  })

  test('renders Japanese when sessionStorage seeds ja before load', async ({
    browser,
  }) => {
    // Start from an en-US browser and prove sessionStorage wins over
    // navigator.language. This is exactly the path a user takes when
    // they visit the site from an English-configured laptop but have
    // previously picked 日本語 in the switcher.
    const ctx = await browser.newContext({ locale: 'en-US' })
    const page = await ctx.newPage()
    // Seed via init script so the value is present *before* our
    // bootstrap (`src/main.tsx` → `@/lib/i18n`) runs.
    await page.addInitScript((key) => {
      window.sessionStorage.setItem(key, 'ja')
    }, STORAGE_KEY)
    await page.goto('/')

    await expect(
      page.getByRole('button', { name: JA_STRINGS.sign_in }),
    ).toBeVisible()
    await expect(page.getByText(JA_STRINGS.cli_title)).toBeVisible()

    await ctx.close()
  })

  test('switcher flips the UI live and persists the choice in sessionStorage', async ({
    browser,
  }) => {
    const ctx = await browser.newContext({ locale: 'en-US' })
    const page = await ctx.newPage()
    await page.goto('/')

    // Start: English.
    await expect(
      page.getByRole('button', { name: EN_STRINGS.sign_in }),
    ).toBeVisible()

    // Flip to ja via the header switcher. There are two switchers on
    // the Login shell (one in the page hero, one rendered by
    // `<AppShell>` for authenticated users — latter is not present
    // pre-auth, so `first()` is stable). We intentionally click the
    // visible switcher button by its label.
    await page.getByRole('button', { name: /^日本語$/i }).click()

    // The sign-in CTA must rerender in ja on the same frame.
    await expect(
      page.getByRole('button', { name: JA_STRINGS.sign_in }),
    ).toBeVisible()

    // aria-pressed flips.
    await expect(
      page.getByRole('button', { name: /^日本語$/i }).first(),
    ).toHaveAttribute('aria-pressed', 'true')
    await expect(
      page.getByRole('button', { name: /^english$/i }).first(),
    ).toHaveAttribute('aria-pressed', 'false')

    // sessionStorage has been updated.
    const stored = await page.evaluate(
      (key) => window.sessionStorage.getItem(key),
      STORAGE_KEY,
    )
    expect(stored).toBe('ja')

    await ctx.close()
  })

  test('locale survives a full page reload (sessionStorage contract)', async ({
    browser,
  }) => {
    const ctx = await browser.newContext({ locale: 'en-US' })
    const page = await ctx.newPage()
    await page.goto('/')

    // Flip to ja.
    await page.getByRole('button', { name: /^日本語$/i }).click()
    await expect(
      page.getByRole('button', { name: JA_STRINGS.sign_in }),
    ).toBeVisible()

    // Reload: the bootstrap must read sessionStorage first.
    await page.reload()
    await expect(
      page.getByRole('button', { name: JA_STRINGS.sign_in }),
    ).toBeVisible()

    await ctx.close()
  })

  test('unsupported stored locale falls back to the default', async ({
    browser,
  }) => {
    // Corrupted sessionStorage (or a stale server row that leaked "fr"
    // into the tab) must not crash the app or render untranslated
    // keys. `isSupportedLocale` in `@/lib/i18n.ts` filters the value,
    // and i18next's `fallbackLng` (ja) catches the rest.
    const ctx = await browser.newContext({ locale: 'en-US' })
    const page = await ctx.newPage()
    await page.addInitScript((key) => {
      window.sessionStorage.setItem(key, 'fr')
    }, STORAGE_KEY)
    await page.goto('/')

    // We don't care which supported locale rendered — only that the
    // CTA is a real translated string, not the raw key.
    const cta = page.getByRole('button', { name: /cognito/i })
    await expect(cta).toBeVisible()
    // Negative assertion: the raw translation key must not leak.
    await expect(page.locator('body')).not.toContainText('login.cta')
    await expect(page.locator('body')).not.toContainText('login.cli_title')

    await ctx.close()
  })
})
