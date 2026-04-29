// Runtime-config resilience.
//
// `src/main.tsx` awaits `loadRuntimeConfig()` before mounting React.
// The contract is:
//   - On success (HTTP 200 + parseable JSON) → SPA boots normally.
//   - On failure → SPA renders a minimal bilingual splash and does
//     NOT attempt to render the authenticated UI. This is the
//     failure-mode UX we rely on when CloudFront serves a stale
//     config.json during a deploy rollover.
//
// These tests verify the failure branch by intercepting the
// `/config.json` request with `page.route()` so we do not need a
// broken fixture on disk. The success branch is implicitly exercised
// by every other e2e test in this directory.

import { expect, test } from '@playwright/test'

test.describe('runtime config failure', () => {
  test('renders the bilingual splash when /config.json 404s', async ({
    page,
  }) => {
    // Intercept before navigation so the failure is seen on cold start.
    await page.route('**/config.json', (route) =>
      route.fulfill({
        status: 404,
        contentType: 'text/plain',
        body: 'not found',
      }),
    )

    await page.goto('/')

    // Both the English and Japanese headline are intentionally shown
    // side-by-side because at this point i18next has not loaded yet —
    // the user can still recognise the state in either language.
    await expect(
      page.getByRole('heading', { name: /Configuration load failed/i }),
    ).toBeVisible()
    await expect(page.getByText(/設定の読み込みに失敗しました/)).toBeVisible()

    // The sign-in CTA must NOT be rendered; mounting the React tree
    // with a missing config would crash on the first API call.
    await expect(
      page.getByRole('button', { name: /cognito/i }),
    ).toHaveCount(0)
  })

  test('renders the bilingual splash when /config.json returns invalid JSON', async ({
    page,
  }) => {
    await page.route('**/config.json', (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: '{ this is not json',
      }),
    )

    await page.goto('/')

    await expect(
      page.getByRole('heading', { name: /Configuration load failed/i }),
    ).toBeVisible()
    await expect(
      page.getByRole('button', { name: /cognito/i }),
    ).toHaveCount(0)
  })
})
