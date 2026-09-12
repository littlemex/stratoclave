// E2E for the boundary between the three discovery/promotion operator
// screens described in the model-onboarding brief: `admin` may read AND
// write, `team_lead` may read but not write (they hold `models:discover`,
// never `models:promote`), `user` may reach neither.
//
// These are the negative specs the brief calls the ones that matter: the
// positive "admin sees the queue" cases in `admin-discovery.spec.ts` would
// pass against a screen that gates nothing. The risk this file guards
// against is specifically the "single combined screen" one the brief names:
// a screen built to satisfy `admin` naturally shows the write controls right
// next to the reads it also has to show `team_lead`, because the SAME
// backend read route (`GET /api/mvp/admin/discovery/records`, gated on
// `models:discover`) already answers both roles --
// `test_a_permanently_blocked_record_appears_in_the_listing_the_queue_omits_it_from`
// `backend/tests/test_discovery_operator_surfaces.py` runs that exact GET as
// `team_lead` and expects 200. So a `team_lead` reaching this screen with a
// legitimate, working read is the realistic case, not a hardened one, and
// "the write button happens to be hidden" is not enough on its own -- this
// file also asserts no write request ever leaves the browser for that role.
//
// CONVERGENCE PASS. The premise above -- one screen serving both roles --
// is not what was built, and the difference makes this file STRONGER rather
// than weaker. The real arrangement is four admin-only routes under
// `/admin/discovery/*` and a separate `/team-lead/discovery` page that
// contains no create, probe or activate code path at all.
//
// So the "single combined screen" risk this file was written to guard has
// been designed out rather than defended against, and the assertion that
// replaces it is a harder one: a `team_lead` is turned away from the ADMIN
// routes by the route guard itself, before any component mounts, while still
// reaching every read they are entitled to on their own page. Both halves are
// asserted below, and both keep the original bar -- absent affordances AND
// zero write requests on the wire, never merely a hidden button.

import { type Page } from '@playwright/test'
// A crash renders nothing, so every assertion after it fails for the wrong
// reason. See the module's header for the three times that happened here.
import { expect, test } from './support/no-uncaught-render-error'

const PERMANENT_BLOCKER_PROFILE_ID = 'us.acme.forever-blocked-v1'
const CANDIDATE_PROFILE_ID = 'us.acme.readable-candidate-v1'
const EVIDENCE = 'ValidationException: Agreement not supported for this model.'

function seedSession(page: Page) {
  return page.addInitScript(() => {
    window.sessionStorage.setItem(
      'stratoclave_tokens',
      JSON.stringify({
        access_token: 'e2e-fake-access-token',
        id_token: 'e2e-fake-id-token',
        refresh_token: null,
        expires_at: Date.now() + 24 * 60 * 60 * 1000,
      }),
    )
    window.sessionStorage.setItem('stratoclave_locale', 'en')
  })
}

async function mockShell(page: Page, roles: string[]) {
  await page.route('**/config.json', (route) =>
    route.fulfill({
      json: {
        api: { endpoint: '' },
        cognito: {
          client_id: 'e2e-client-id',
          domain: 'https://e2e.auth.us-east-1.amazoncognito.com',
          user_pool_id: 'us-east-1_e2epool',
          region: 'us-east-1',
        },
      },
    }),
  )
  await page.route('**/api/mvp/me', (route) =>
    route.fulfill({
      json: {
        user_id: 'operator-1',
        email: 'operator-1@example.com',
        org_id: 'default-org',
        roles,
        total_credit: 1_000_000,
        credit_used: 0,
        remaining_credit: 1_000_000,
        currency: 'tokens',
        tenant: { tenant_id: 'default-org', name: 'Default' },
        locale: 'en',
      },
    }),
  )
}

/** A candidate in the shape `CandidateResponse` actually returns. Every field
 * here is required on that model; a fixture missing one white-screens the
 * screen that reads it, which is how three of these were found. */
function candidate(profileId: string) {
  return {
    profile_id: profileId,
    aliases: [`${profileId}-alias`],
    pricing_key: 'opus',
    jurisdiction: 'us',
    provider: 'anthropic',
    bedrock_model_id: profileId,
    bedrock_region: 'us-east-1',
    wire_protocol: 'messages',
    state: 'candidate',
    identifiers: [`${profileId}-alias`, profileId],
    model_family: profileId,
    profile_scope: 'us',
    created_at: '2026-09-01T00:00:00+00:00',
    created_by: 'operator-1',
    verdicts: {
      sync: { invocation: 'sync', state: 'unverified' },
      stream: { invocation: 'stream', state: 'unverified' },
    },
  }
}

function record(profileId: string, blockers: unknown[] = []) {
  return {
    profile_id: profileId,
    provider: 'anthropic',
    profile_scope: 'us',
    model_family: profileId,
    jurisdiction_bounded: true,
    destination_regions: ['us-east-1'],
    invocation_region: 'us-east-1',
    blockers,
    revision: 'rev-1',
  }
}

/** Every write path this domain has, by method + path suffix -- used to
 * assert none of them is ever hit by a role that must not reach them. */
const WRITE_PATH_SUFFIXES = ['/candidates', '/probe', '/activate']

function isDiscoveryWriteRequest(url: string, method: string): boolean {
  if (!url.includes('/api/mvp/admin/discovery')) return false
  if (method === 'GET') return false
  return WRITE_PATH_SUFFIXES.some((suffix) => url.includes(suffix))
}

test.describe('discovery/promotion role boundary', () => {
  test('team_lead reaches the read data (including permanent-blocker evidence) but cannot reach promote, probe or activate', async ({
    page,
  }) => {
    await seedSession(page)
    await mockShell(page, ['team_lead'])

    await page.route('**/api/mvp/admin/discovery/records', (route) =>
      route.fulfill({
        json: {
          records: [
            record(PERMANENT_BLOCKER_PROFILE_ID, [
              {
                type: 'no_agreement_offer',
                subtype: 'not_marketplace_metered',
                evidence: EVIDENCE,
                first_seen: '2026-09-01T00:00:00+00:00',
                last_seen: '2026-09-01T00:00:00+00:00',
              },
            ]),
            record(CANDIDATE_PROFILE_ID),
          ],
        },
      }),
    )
    await page.route('**/api/mvp/admin/discovery/queue', (route) =>
      route.fulfill({ json: { entries: [] } }),
    )
    await page.route('**/api/mvp/admin/discovery/candidates', (route) => {
      if (route.request().method() === 'GET') {
        return route.fulfill({ json: { candidates: [candidate(CANDIDATE_PROFILE_ID)] } })
      }
      // A team_lead's browser must never reach this branch (POST); if it
      // does, the mock still answers with the real backend's own contract
      // (`test_candidate_creation_is_gated_on_the_promote_scope`) so a bug
      // that DOES fire the request is caught by the request-log assertion
      // below rather than by a network error obscuring it.
      return route.fulfill({
        status: 403,
        json: { detail: { type: 'forbidden', message: 'models:promote required' } },
      })
    })
    await page.route(
      `**/api/mvp/admin/discovery/candidates/${encodeURIComponent(CANDIDATE_PROFILE_ID)}/probe`,
      (route) =>
        route.fulfill({
          status: 403,
          json: { detail: { type: 'forbidden', message: 'models:promote required' } },
        }),
    )
    await page.route(
      `**/api/mvp/admin/discovery/candidates/${encodeURIComponent(CANDIDATE_PROFILE_ID)}/activate`,
      (route) =>
        route.fulfill({
          status: 403,
          json: { detail: { type: 'forbidden', message: 'models:promote required' } },
        }),
    )

    const requests: string[] = []
    page.on('request', (req) => {
      if (isDiscoveryWriteRequest(req.url(), req.method())) {
        requests.push(`${req.method()} ${req.url()}`)
      }
    })

    await page.goto('/team-lead/discovery')

    // The reads reach: the permanently blocked record's own evidence text is
    // visible, not merely its type code, exactly as it would be for admin
    // (`admin-discovery.spec.ts`'s equivalent read case) -- this role holds
    // `models:discover`, so a screen that hid this from them would be hiding
    // data they are entitled to, not enforcing a boundary.
    await expect(page.getByText(PERMANENT_BLOCKER_PROFILE_ID).first()).toBeVisible()
    await expect(page.getByText(EVIDENCE, { exact: false }).first()).toBeVisible()
    await expect(page.getByText(CANDIDATE_PROFILE_ID).first()).toBeVisible()

    // Not merely a hidden button: the three write affordances must be
    // ABSENT (count 0), not merely invisible via CSS, for a discover-only
    // caller.
    await expect(page.getByRole('button', { name: /create candidate/i })).toHaveCount(0)
    await expect(page.getByRole('button', { name: /^probe$/i })).toHaveCount(0)
    await expect(page.getByRole('button', { name: /^activate$/i })).toHaveCount(0)

    // Give any async render one more tick, then assert the network itself
    // never carried a write for this session -- the deeper guarantee the
    // brief asks for, independent of whatever the DOM happens to render.
    await page.waitForTimeout(250)
    expect(
      requests,
      'a team_lead session sent a discovery write request',
    ).toHaveLength(0)

    // The half this file gained from the separate-route design: the admin
    // routes are not merely free of affordances for this role, they are
    // unreachable. `ProtectedRoute` refuses before the component mounts, so
    // there is no render in which a write control could appear at all.
    for (const adminRoute of [
      '/admin/discovery/records',
      '/admin/discovery/candidates',
      `/admin/discovery/candidates/new?profile_id=${encodeURIComponent(CANDIDATE_PROFILE_ID)}`,
      `/admin/discovery/candidates/${encodeURIComponent(CANDIDATE_PROFILE_ID)}`,
    ]) {
      await page.goto(adminRoute)
      await expect(
        page.getByText(/access denied/i).first(),
        `a team_lead was let into ${adminRoute}`,
      ).toBeVisible()
      await expect(page.getByRole('button', { name: /create candidate/i })).toHaveCount(0)
      await expect(page.getByRole('link', { name: /create candidate/i })).toHaveCount(0)
      await expect(page.getByRole('button', { name: /run probe/i })).toHaveCount(0)
      await expect(page.getByRole('button', { name: /activate/i })).toHaveCount(0)
    }

    await page.waitForTimeout(250)
    expect(
      requests,
      'a team_lead sent a discovery write while bouncing off the admin routes',
    ).toHaveLength(0)
  })

  test('user reaches neither the discovery reads nor any discovery write', async ({
    page,
  }) => {
    await seedSession(page)
    await mockShell(page, ['user'])

    const requests: string[] = []
    page.on('request', (req) => {
      if (req.url().includes('/api/mvp/admin/discovery')) {
        requests.push(`${req.method()} ${req.url()}`)
      }
    })

    // If `user` somehow reached the component tree despite the route guard,
    // these would answer -- they are here so a guard regression fails on
    // the request-log assertion below with a clear diff, not on an
    // unhandled-request timeout that looks like an unrelated flake.
    await page.route('**/api/mvp/admin/discovery/records', (route) =>
      route.fulfill({ json: { records: [record(CANDIDATE_PROFILE_ID)] } }),
    )
    await page.route('**/api/mvp/admin/discovery/queue', (route) =>
      route.fulfill({ json: { entries: [] } }),
    )

    // Both the team_lead read page and an admin route: a `user` holds
    // neither `models:discover` nor `models:promote`, so neither is theirs.
    for (const route of ['/team-lead/discovery', '/admin/discovery/records']) {
      await page.goto(route)
      await expect(
        page.getByText(/access denied/i).first(),
        `a user was let into ${route}`,
      ).toBeVisible()
      await expect(page.getByText(CANDIDATE_PROFILE_ID)).toHaveCount(0)
    }

    // `ProtectedRoute` renders `AccessDenied` (not a redirect) for an
    // authenticated caller whose roles do not satisfy `requiredRoles` --
    // the same surface `security-guards.spec.ts` documents for every other
    // admin/team-lead route.
    await page.waitForTimeout(250)
    expect(
      requests,
      'a user session reached a discovery endpoint at all',
    ).toHaveLength(0)
  })
})
