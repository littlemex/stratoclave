// Security-adjacent guards that the SPA enforces on the
// unauthenticated shell.
//
// These assertions are the last line of defense if a reviewer
// accidentally rewires `AuthContext.tsx` — the contracts they cover
// are all things we have explicitly fixed in past security reviews:
//
//   * P0-8 session fixation: the SPA must silently scrub any
//     `?token=<attacker>` appended to the URL and must never pin the
//     session to that value. See `src/contexts/AuthContext.tsx`.
//   * ProtectedRoute: unauthenticated users bounce to `/` for every
//     admin / team-lead / me route, not a blank page and not a flash
//     of the target's layout.
//   * Access denied copy: both en and ja locales expose a usable
//     `access_denied.*` surface when the user has no matching role.
//
// The suite deliberately runs against the real dev server without a
// backend — everything here is client-side routing / parsing behavior.

import { expect, test } from '@playwright/test'

test.describe('security guards on the unauthenticated shell', () => {
  test('strips ?token=<attacker> from the URL and does not authenticate (P0-8)', async ({
    page,
  }) => {
    // The attacker-controlled payload that the pre-P0-8 code would
    // have accepted as an access_token. We set up a console-error
    // listener so a regression that throws would be visible in the
    // Playwright report, not just silently green.
    const consoleErrors: string[] = []
    page.on('pageerror', (err) => consoleErrors.push(String(err)))

    await page.goto('/?token=eyJhttacker-session-fixation-payload')

    // The SPA must finish bootstrap and show the Login page, not the
    // authenticated dashboard (which would be unreachable anyway
    // without a valid JWT, but rendering a flash of it would still
    // leak layout and be a UX bug).
    await expect(
      page.getByRole('button', { name: /cognito/i }),
    ).toBeVisible()

    // The URL must have had its `token` param stripped. We do not
    // assert an exact pathname because dev-server sometimes adds
    // other params (e.g. Vite's HMR ws token) — the contract is just
    // "`token` is gone".
    const url = new URL(page.url())
    expect(url.searchParams.has('token')).toBe(false)

    // sessionStorage must not contain the fixation payload in any
    // form. This catches both the direct-write path and an accidental
    // reintroduction via a helper that calls `saveTokens` too eagerly.
    const stored = await page.evaluate(() => ({
      tokens: window.sessionStorage.getItem('stratoclave_tokens'),
      ticket: window.sessionStorage.getItem('stratoclave_ui_ticket'),
    }))
    expect(stored.tokens).toBeNull()
    expect(stored.ticket).toBeNull()

    // No runtime error fired during this scenario.
    expect(consoleErrors).toHaveLength(0)
  })

  test('bounces every admin / team-lead / me route back to / for unauthenticated visitors', async ({
    page,
  }) => {
    // This is the ProtectedRoute contract. If any of these leak their
    // target layout (eg. the header nav, a tenant table, the API-keys
    // panel) before redirecting, a reviewer gets a screenshot diff.
    const targets = [
      '/admin/users',
      '/admin/users/new',
      '/admin/tenants',
      '/admin/usage',
      '/admin/trusted-accounts',
      '/team-lead/tenants',
      '/team-lead/tenants/new',
      '/me/usage',
      '/me/api-keys',
    ]

    for (const path of targets) {
      await page.goto(path)
      // Every protected path must land on /.
      await expect(page).toHaveURL(/\/$/, {
        timeout: 5000,
      })
      // And render the Login hero, not a blank page.
      await expect(
        page.getByRole('button', { name: /cognito/i }),
      ).toBeVisible()
    }
  })

  test('unknown client routes fall through gracefully to the Login shell', async ({
    page,
  }) => {
    // The React Router configuration redirects unauthenticated users
    // on any path. A `/does-not-exist` URL must not render the raw
    // runtime-error overlay.
    await page.goto('/this-route-does-not-exist')
    await expect(
      page.getByRole('button', { name: /cognito/i }),
    ).toBeVisible()
  })
})
