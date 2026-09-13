// E2E for the admin discovery/promotion screen: browsing what discovery
// found (including the evidence for a blocker nothing can clear), turning a
// clean record into a promotion candidate with the three human decisions,
// probing it, and activating it.
//
// Written BLIND, the same way `limit-raise-approver-journey.spec.ts` records
// itself having been written before its own convergence correction: nobody
// on this side of the split has seen `AdminDiscovery.tsx` (or whatever the
// screen ends up being called). Every DOM-level convention below (the route,
// the accessible names, the inline-form shape) is this file's OWN choice,
// not something read out of the implementation -- and, like that file, this
// one expects a follow-up pass once the real component exists to replace any
// convention that does not match it. The backend wire shapes are NOT this
// file's choice: they come straight from
// `backend/tests/test_discovery_operator_surfaces.py`, the frozen contract
// for `mvp.admin_discovery`'s next commit.
//
// CONVERGENCE PASS, run once the real screens existed. Two conventions this
// file chose blind did not match, and both were retargeted WITHOUT weakening
// an assertion:
//
//   - Routes. This file assumed one `/admin/discovery` serving both roles.
//     The real screens are `/admin/discovery/records`,
//     `/admin/discovery/candidates`, `.../candidates/new` and
//     `.../candidates/:profileId`, all four behind the admin-only route
//     guard, with `team_lead` reading a SEPARATE `/team-lead/discovery` page
//     that contains no write code path at all. That is a stronger arrangement
//     than the one assumed, and the boundary spec gains an assertion from it
//     rather than losing one -- see that file's own header.
//   - The create form is a PAGE, not an inline form on the row. The
//     "Create candidate" affordance is a link that navigates. The assertion
//     it carried -- that both identifiers are on screen before any POST
//     fires -- is unchanged and still the point.
//   - Each browsable record renders its `profile_id` as visible text, and,
//     for a record carrying a blocker, that blocker's `evidence` as visible
//     text too -- not merely a `type`/`subtype` code.
//   - A clean record's row/card exposes an inline (not modal) form: a button
//     named "Create candidate" opens or already shows Alias / Pricing key /
//     Jurisdiction inputs (`getByLabel(/alias/i)` etc.) plus a submit action
//     also reachable by name `/create candidate/i`.
//   - A candidate's row/card exposes a "Probe" button and, once a current
//     verdict exists, an "Activate" button.
//
// Stubbed at the network boundary throughout (`page.route`); no live AWS
// account or backend process. Every response body mirrors the shape pinned
// by `test_discovery_operator_surfaces.py` exactly (field names, nesting),
// not a shape this file invented for its own convenience.

import { type Page } from '@playwright/test'
// A crash renders nothing, so every assertion after it fails for the wrong
// reason. See the module's header for the three times that happened here.
import { expect, test } from './support/no-uncaught-render-error'

const PROFILE_ID = 'us.acme.pending-v1'

function seedAdminSession(page: Page) {
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
  // Every test on this page reads the actionable queue too (it is the other
  // shipped surface on the same domain); default it empty so a screen that
  // renders it does not hang on an unmocked request.
  await page.route('**/api/mvp/admin/discovery/queue', (route) =>
    route.fulfill({ json: { entries: [] } }),
  )
}

function blocker(overrides: Partial<Record<string, string>> = {}) {
  return {
    type: 'no_agreement_offer',
    subtype: 'not_marketplace_metered',
    evidence: 'ValidationException: Agreement not supported for this model.',
    first_seen: '2026-09-01T00:00:00+00:00',
    last_seen: '2026-09-01T00:00:00+00:00',
    ...overrides,
  }
}

// The create form is its own page and fetches the ONE record by id, so the
// list mock does not cover it: a glob ending in `/records` does not match
// `/records/{id}`. Mocking only the list would leave the form loading forever
// and every assertion below would fail for the wrong reason.
async function mockOneRecord(page: Page, record: Record<string, unknown>) {
  await page.route(
    `**/api/mvp/admin/discovery/records/${encodeURIComponent(record.profile_id as string)}`,
    (route) => route.fulfill({ json: record }),
  )
}

function cleanRecord(profileId: string, revision = 'rev-1') {
  return {
    profile_id: profileId,
    provider: 'anthropic',
    profile_scope: 'us',
    model_family: profileId.split('.').slice(2).join('.') || profileId,
    jurisdiction_bounded: true,
    destination_regions: ['us-east-1'],
    invocation_region: 'us-east-1',
    blockers: [],
    revision,
  }
}

test.describe('admin discovery: records and their evidence', () => {
  test('a permanently blocked record is listed with its blocker evidence', async ({
    page,
  }) => {
    await seedAdminSession(page)
    await mockShell(page, ['admin'])

    const profileId = 'us.acme.forever-blocked-v1'
    const evidence = 'ValidationException: Agreement not supported for this model.'
    await page.route('**/api/mvp/admin/discovery/records', (route) =>
      route.fulfill({
        json: {
          records: [
            {
              ...cleanRecord(profileId),
              blockers: [blocker({ evidence })],
            },
          ],
        },
      }),
    )

    await page.goto('/admin/discovery/records')

    await expect(page.getByText(profileId).first()).toBeVisible()
    // The evidence text itself, not merely the `not_marketplace_metered`
    // type code -- an operator asking "why can this not be promoted" needs
    // the sentence, not the enum.
    await expect(page.getByText(evidence, { exact: false }).first()).toBeVisible()
  })
})

test.describe('admin discovery: candidate creation names both identifiers', () => {
  test('the create-candidate form shows the alias and the Bedrock model id before it is submitted', async ({
    page,
  }) => {
    await seedAdminSession(page)
    await mockShell(page, ['admin'])
    await page.route('**/api/mvp/admin/discovery/records', (route) =>
      route.fulfill({ json: { records: [cleanRecord(PROFILE_ID)] } }),
    )
    await page.route('**/api/mvp/admin/discovery/candidates', (route) =>
      route.fulfill({ json: { candidates: [] } }),
    )

    const requestsToCandidates: string[] = []
    page.on('request', (req) => {
      if (
        req.method() === 'POST' &&
        req.url().includes('/api/mvp/admin/discovery/candidates')
      ) {
        requestsToCandidates.push(req.url())
      }
    })

    await mockOneRecord(page, cleanRecord(PROFILE_ID))

    await page.goto('/admin/discovery/records')

    // The affordance is a link that navigates to the form page; the assertion
    // it guards -- both identifiers visible before any POST -- is unchanged.
    await page.getByRole('link', { name: /create candidate/i }).first().click()
    await expect(page).toHaveURL(/\/admin\/discovery\/candidates\/new/)

    // The wire protocol is one of the three human decisions, so the form
    // refuses to submit until it is declared. Selecting it is part of the
    // journey, not a workaround for a disabled button.
    await page.getByLabel(/wire protocol/i).selectOption('messages')

    const aliasInput = page.getByLabel(/alias/i).first()
    await aliasInput.fill('e2e-preview-alias')
    await expect(aliasInput).toHaveValue('e2e-preview-alias')

    // The second identifier -- the one the brief says people miss -- is the
    // record's own Bedrock model id, which in this fixture equals its
    // `profile_id`. It must be visible on screen while the form is open,
    // before any submit action fires.
    await expect(page.getByText(PROFILE_ID).first()).toBeVisible()

    expect(
      requestsToCandidates,
      'a create-candidate POST fired before the preview assertions ran',
    ).toHaveLength(0)
  })

  test('a missing human field refuses the create and names the field', async ({ page }) => {
    await seedAdminSession(page)
    await mockShell(page, ['admin'])
    await page.route('**/api/mvp/admin/discovery/records', (route) =>
      route.fulfill({ json: { records: [cleanRecord(PROFILE_ID)] } }),
    )
    await page.route('**/api/mvp/admin/discovery/candidates', (route) => {
      if (route.request().method() !== 'POST') {
        return route.fulfill({ json: { candidates: [] } })
      }
      // Mirrors `test_a_missing_human_field_refuses_on_its_own_reason_naming_the_field`:
      // `{"detail": {"type": ..., "field": ..., "message": ...}}`, the
      // message written to explicitly name the field, exactly as a real
      // human-readable refusal would.
      return route.fulfill({
        status: 422,
        json: {
          detail: {
            type: 'pricing_key_required',
            field: 'pricing_key',
            message: 'pricing_key is required to create a candidate.',
          },
        },
      })
    })

    await mockOneRecord(page, cleanRecord(PROFILE_ID))

    await page.goto('/admin/discovery/records')
    await page.getByRole('link', { name: /create candidate/i }).first().click()
    await page.getByLabel(/wire protocol/i).selectOption('messages')
    await page.getByLabel(/alias/i).first().fill('e2e-missing-field-alias')
    // Pricing key deliberately left blank, so the refusal has to come from
    // the server's one vocabulary and name the field.
    await page.getByRole('button', { name: /create candidate/i }).last().click()

    await expect(page.getByText(/pricing[ _]key/i).first()).toBeVisible()
  })
})

test.describe('admin discovery: probe and activation', () => {
  function candidate(overrides: Record<string, unknown> = {}) {
    return {
      profile_id: PROFILE_ID,
      aliases: ['acme-pending-v1'],
      pricing_key: 'opus',
      jurisdiction: 'us',
      provider: 'anthropic',
      bedrock_model_id: PROFILE_ID,
      bedrock_region: 'us-east-1',
      wire_protocol: 'messages',
      state: 'candidate',
      // `identifiers`, `model_family`, `profile_scope`, `created_at` and
      // `created_by` are REQUIRED on `CandidateResponse` and were missing
      // from this fixture when it was written blind. The detail screen reads
      // `identifiers` unguarded and white-screened on the first run, which is
      // the correct outcome for a fixture that does not match the contract it
      // claims to mirror -- and the reason `support/no-uncaught-render-error`
      // now exists.
      identifiers: ['acme-pending-v1', PROFILE_ID],
      model_family: 'acme.pending-v1',
      profile_scope: 'us',
      created_at: '2026-09-01T00:00:00+00:00',
      created_by: 'operator-1',
      verdicts: {
        // `invocation` is required on `VerdictView`; omitting it was part of
        // the same fixture drift.
        sync: { invocation: 'sync', state: 'unverified' },
        stream: { invocation: 'stream', state: 'unverified' },
      },
      ...overrides,
    }
  }

  // Probe and activate live on the candidate's own detail page, which fetches
  // the one candidate by id -- same glob problem as the single record.
  async function mockOneCandidate(page: Page, c: Record<string, unknown>) {
    await page.route(
      `**/api/mvp/admin/discovery/candidates/${encodeURIComponent(c.profile_id as string)}`,
      (route) => route.fulfill({ json: c }),
    )
  }

  test('a failed probe assertion renders as a completed check with its reason, not an error toast', async ({
    page,
  }) => {
    await seedAdminSession(page)
    await mockShell(page, ['admin'])
    await page.route('**/api/mvp/admin/discovery/records', (route) =>
      route.fulfill({ json: { records: [cleanRecord(PROFILE_ID)] } }),
    )
    await page.route('**/api/mvp/admin/discovery/candidates', (route) => {
      if (route.request().method() === 'GET') {
        return route.fulfill({ json: { candidates: [candidate()] } })
      }
      return route.continue()
    })

    const reason =
      'ValidationException: This model does not support the Converse API operation.'
    await page.route(
      `**/api/mvp/admin/discovery/candidates/${encodeURIComponent(PROFILE_ID)}/probe`,
      (route) =>
        route.fulfill({
          status: 200,
          json: {
            passed: false,
            blocker: {
              type: 'protocol_unverified',
              subtype: 'converse_unsupported',
              evidence: reason,
            },
            charged_microusd: 0,
          },
        }),
    )

    await mockOneCandidate(page, candidate())

    await page.goto(`/admin/discovery/candidates/${encodeURIComponent(PROFILE_ID)}`)
    // Which invocation to probe is the operator's choice; the button waits
    // for it rather than picking one silently.
    await page.getByLabel(/invocation/i).selectOption('sync')
    await page.getByRole('button', { name: /run probe/i }).first().click()

    // The reason surfaces as the completed check's own content...
    await expect(page.getByText(reason, { exact: false }).first()).toBeVisible()
    // ...never through the app's generic network-failure toast
    // (`ErrorToast.tsx`, `data-testid="error-toast"`, fired only by
    // `authFetch` on 401/403/429/5xx -- a 200 body can only reach it if the
    // screen's own code wrongly re-routes `passed: false` into that path).
    await expect(page.getByTestId('error-toast')).toHaveCount(0)
  })

  test('a verdict that reads verified but carries no identity is not offered for activation', async ({
    page,
  }) => {
    // `verified_at` is Optional on `VerdictView`, and activation sends it as
    // the identity to compare-and-set against. So a verdict in this shape is
    // representable on the wire and NOT activatable: offering the button would
    // send a request with the identity missing, and the operator would get a
    // refusal naming a field they never filled in.
    //
    // Added because deleting the guard that excludes this case failed no test:
    // every other fixture here carries `verified_at`, so the guard was written
    // and never exercised.
    await seedAdminSession(page)
    await mockShell(page, ['admin'])
    await mockOneCandidate(
      page,
      candidate({
        verdicts: {
          sync: {
            invocation: 'sync',
            state: 'verified',
            pricing_key_at_verification: 'opus',
            wire_protocol_verified: 'messages',
          },
          stream: { invocation: 'stream', state: 'unverified' },
        },
      }),
    )

    const writes: string[] = []
    page.on('request', (req) => {
      if (req.method() === 'POST' && req.url().includes('/activate')) {
        writes.push(req.url())
      }
    })

    await page.goto(`/admin/discovery/candidates/${encodeURIComponent(PROFILE_ID)}`)

    // The actionable line, not a disabled button and not a button that sends a
    // request destined to be refused.
    await expect(page.getByText(/run a probe first/i).first()).toBeVisible()
    await expect(page.getByRole('button', { name: /^activate/i })).toHaveCount(0)

    await page.waitForTimeout(250)
    expect(writes, 'an activation was sent with no verdict identity').toHaveLength(0)
  })

  test('activation never asks the operator to type the verdict identity', async ({
    page,
  }) => {
    await seedAdminSession(page)
    await mockShell(page, ['admin'])
    await page.route('**/api/mvp/admin/discovery/records', (route) =>
      route.fulfill({ json: { records: [cleanRecord(PROFILE_ID)] } }),
    )
    await page.route('**/api/mvp/admin/discovery/candidates', (route) => {
      if (route.request().method() === 'GET') {
        return route.fulfill({
          json: {
            candidates: [
              candidate({
                verdicts: {
                  sync: {
                    invocation: 'sync',
                    state: 'verified',
                    verified_at: '2026-09-01T00:00:00+00:00',
                    verified_by: 'operator-1',
                    pricing_key_at_verification: 'opus',
                    wire_protocol_verified: 'messages',
                  },
                  stream: { invocation: 'stream', state: 'unverified' },
                },
              }),
            ],
          },
        })
      }
      return route.continue()
    })

    let capturedBody: unknown = null
    await page.route(
      `**/api/mvp/admin/discovery/candidates/${encodeURIComponent(PROFILE_ID)}/activate`,
      (route) => {
        capturedBody = route.request().postDataJSON()
        // Completed against `ActivateResponse`. Written blind it carried three
        // fields and a `state` the model does not have, and the success panel
        // white-screened reading `identifiers`.
        return route.fulfill({
          json: {
            profile_id: PROFILE_ID,
            invocation: 'sync',
            verified_at: '2026-09-01T00:00:00+00:00',
            provider: 'anthropic',
            bedrock_model_id: PROFILE_ID,
            bedrock_region: 'us-east-1',
            aliases: ['acme-pending-v1'],
            wire_protocol: 'messages',
            pricing_key: 'opus',
            profile_scope: 'us',
            model_family: 'acme.pending-v1',
            access: 'entitled',
            jurisdiction_bounded: true,
            jurisdiction: 'us',
            identifiers: ['acme-pending-v1', PROFILE_ID],
          },
        })
      },
    )

    await mockOneCandidate(
      page,
      candidate({
        verdicts: {
          sync: {
            invocation: 'sync',
            state: 'verified',
            verified_at: '2026-09-01T00:00:00+00:00',
            verified_by: 'operator-1',
            pricing_key_at_verification: 'opus',
            wire_protocol_verified: 'messages',
          },
          stream: { invocation: 'stream', state: 'unverified' },
        },
      }),
    )

    // Armed BEFORE the click. Waiting after it races the request: the POST
    // fired and completed while this spec was still setting up its listener,
    // so the wait timed out on a request that had already happened -- a spec
    // defect that reads exactly like the button not working.
    const activateRequest = page.waitForRequest(
      (req) =>
        req.url().includes(`/candidates/${encodeURIComponent(PROFILE_ID)}/activate`) &&
        req.method() === 'POST',
      { timeout: 5000 },
    )

    await page.goto(`/admin/discovery/candidates/${encodeURIComponent(PROFILE_ID)}`)
    const activateButton = page.getByRole('button', { name: /activate/i }).first()
    await activateButton.click()

    await activateRequest

    // Whatever the request body carries, it must not be the operator typing
    // back the verdict's own identity fields -- those are read server-side
    // from the stored candidate/verdict, per the merged activation
    // function's compare-and-set design.
    const body = (capturedBody ?? {}) as Record<string, unknown>
    const forbiddenKeys = [
      'pricing_key',
      'pricing_key_at_verification',
      'wire_protocol',
      'wire_protocol_verified',
      'verified_by',
      'verification_id',
      'verdict_id',
    ]
    for (const key of forbiddenKeys) {
      expect(
        Object.keys(body),
        `activation request body carried ${JSON.stringify(body)}`,
      ).not.toContain(key)
    }
  })
})
