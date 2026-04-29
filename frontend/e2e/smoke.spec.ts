// Minimum-viable smoke: the unauthenticated landing page renders and
// shows the Cognito sign-in affordance.
//
// We match the sign-in CTA by locale-agnostic regex so this test is
// stable whether the browser's `navigator.language` detector falls on
// en or ja (Playwright defaults to en-US on macOS runners, en-US on
// CI). Dedicated locale-specific assertions live in `i18n.spec.ts`.

import { expect, test } from '@playwright/test'

test.describe('unauthenticated landing', () => {
  test('shows the sign-in hero and the Cognito sign-in affordance', async ({
    page,
  }) => {
    await page.goto('/')
    // The app must render without throwing runtime errors.
    await expect(page).toHaveTitle(/Stratoclave/i)
    // The primary CTA button, in whichever language i18next resolved:
    //   en: "Sign in with Cognito"
    //   ja: "Cognito でサインイン"
    // Both strings contain the literal token "Cognito", so a single
    // case-insensitive regex covers both locales.
    await expect(
      page.getByRole('button', { name: /cognito/i }),
    ).toBeVisible()
  })

  test('redirects /admin to the landing page when unauthenticated', async ({
    page,
  }) => {
    await page.goto('/admin/users')
    // ProtectedRoute pushes the visitor back to `/`.
    await expect(page).toHaveURL(/\/$/)
  })
})
