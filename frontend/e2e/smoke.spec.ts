// Minimum-viable smoke test: the unauthenticated landing page renders
// and shows the Cognito sign-in entry point.
//
// This suite deliberately does not talk to a real backend. A later
// iteration will stub /api/mvp/* via MSW or a dev proxy and exercise
// admin / user flows end-to-end.

import { expect, test } from '@playwright/test'

test.describe('unauthenticated landing', () => {
  test('shows the sign-in hero and the Cognito sign-in affordance', async ({
    page,
  }) => {
    await page.goto('/')
    // The app must render without throwing runtime errors.
    await expect(page).toHaveTitle(/Stratoclave/i)
    // The primary call-to-action is the Cognito sign-in button.
    await expect(
      page.getByRole('button', { name: /cognito でサインイン/i }),
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
